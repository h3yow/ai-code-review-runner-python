# 本地代码评审任务流程 — 设计说明

本服务在**本地文件仓库**上执行自动化代码评审，不依赖任何外部服务、真实凭据或
付费资源。任务从提交到结束的全过程在并发与异常场景下具有确定、可解释的行为。

## 1. 任务状态机

```
                         ┌──────────────────────────────┐
                         │                              │
                         ▼                              │
   提交 ─▶ PENDING ──▶ RUNNING ──▶ SUCCEEDED (终态)     │
              │           │                            │
              │           ├────▶ FAILED (终态)          │
              │           │                            │
              └────▶ CANCELLING ──▶ CANCELLED (终态) ◀──┘
                       (RUNNING 也可进入)
```

合法迁移（其余一律拒绝，禁止跳变）：

| 当前状态 | 可达状态 |
| --- | --- |
| `PENDING` | `RUNNING`、`CANCELLING`、`FAILED` |
| `RUNNING` | `SUCCEEDED`、`FAILED`、`CANCELLING` |
| `CANCELLING` | `CANCELLED`、`FAILED` |
| `SUCCEEDED` / `FAILED` / `CANCELLED` | （无，终态） |

- 每次迁移都带 `version` 做 CAS 乐观并发控制：版本过期或非法跳变都会抛出错误。
- 迁移通过临时文件 + `os.replace` 原子落盘（JSON），崩溃只会留下旧版或新版，
  不会出现半条记录。

## 2. 幂等与并发规则

### 幂等提交

- 每个请求必须携带非空的 `idempotency_key`。
- 请求内容（解析后的仓库绝对路径 + 排序后的规则集合）会生成内容指纹
  `request_fingerprint`。
- **键相同、内容相同**：返回已存在的唯一任务（HTTP 200，`created=false`），
  无论它在排队、运行还是已结束，都不会产生第二份结果。
- **键相同、内容不同**：拒绝，错误类别 `IDEMPOTENCY_CONFLICT`（HTTP 409），
  消息中带原任务 id，可与其他失败明确区分。
- 受理过程由服务级互斥锁串行化，并配合存储的唯一索引，多个线程同时用同一键
  抢跑提交也只会产生一个任务（恰好一个 `created=true`）。
- 规则列表在构造时排序，因此规则书写顺序不同不影响指纹（仍视为同一请求）。

### 并发调度与排队

- 固定数量的 worker 线程执行评审，最多 `max_concurrency` 个任务同时运行。
- 超出并发的任务进入有界等待队列，队列容量 `max_queue_size`。
- 在途容量 = `max_concurrency + max_queue_size`；满员后的新提交被**明确拒绝**，
  错误类别 `QUEUE_FULL`（HTTP 503）。被拒绝的任务以 `FAILED/QUEUE_FULL`
  落库，作为该幂等键的唯一确定结果；同一键的后续重复提交只会取回这条记录。
- 注意：Python 的 `queue.Queue(maxsize=0)` 表示"无界"，因此容量由独立计数器
  强制执行，保证 `max_queue_size=0` 时"不排队、满员即拒"的语义正确。

### 取消

取消是协作式的，任务在各阶段检查点轮询取消信号：

- `PENDING`（排队中）取消：`PENDING → CANCELLING → CANCELLED` 立即落库，
  worker 出队时发现已终态直接退出，任务**不会执行**。
- `RUNNING`（执行中）取消：置位取消信号并迁移到 `CANCELLING`；流水线在下一个
  检查点抛出 `CancellationRequested`，随后落库 `CANCELLED`。
- "完成 vs 取消"的判定与状态落库在同一把调度锁内原子完成：取消信号置位与
  成功提交互斥，恰好只有一个结果成立，取消的任务绝不写入成功结果，部分发现
  （findings）一律丢弃。
- 对终态任务重复取消是幂等的，状态保持不变。

## 3. 错误类别

| 类别 | 含义 | HTTP |
| --- | --- | --- |
| `VALIDATION_ERROR` | 字段缺失/为空、未知规则 id 等 | 400 |
| `REPO_NOT_FOUND` | 仓库路径不存在或不是目录 | 400 |
| `UNSUPPORTED_FILE` | 遇到不支持解析的文件类型（不静默跳过） | 422 |
| `MALFORMED_INPUT` | 文件非 UTF-8、Python 语法错误、不可读 | 422 |
| `INDEX_ERROR` | 索引构建阶段失败 | 422 |
| `RULE_EXECUTION_ERROR` | 规则执行阶段抛错 | 422 |
| `IDEMPOTENCY_CONFLICT` | 同键不同内容 | 409 |
| `QUEUE_FULL` | 并发/队列容量已满 | 503 |
| `TASK_NOT_FOUND` | 任务 id / 幂等键不存在 | 404 |
| `CANCELLED` | 因取消而结束（无结果） | 409* |
| `INTERRUPTED` | 重启时任务停留在 RUNNING/CANCELLING | 409* |
| `INTERNAL_ERROR` | 防御性兜底（不应发生） | 500 |

\* 仅出现在任务记录的 `error_category` 字段中。

流水线阶段（解析 → 索引 → 规则执行）各自把异常归到上表类别；任何畸形或不受
支持的输入都会使任务 `FAILED` 并附带类别与人类可读消息，绝不静默忽略。

## 4. 重启恢复

服务启动时对持久化记录执行收敛：

- `SUCCEEDED / FAILED / CANCELLED`：原样保留，**不会重新执行、不会重复计入**
  既有结论；同键重复提交只取回原记录。
- `PENDING`：保持 `PENDING` 并重新入队执行（可恢复状态）。
- `RUNNING / CANCELLING`：上次进程在中途死亡，无法判断流水线产出是否完整，
  一律收敛为 `FAILED / INTERRUPTED` 并丢弃半成品，且不自动重跑；客户端可用
  **新的幂等键**重新提交一次以获得完整结果。

## 5. 可观察性（日志）

单测与运行日志中，每个任务的关键事件都打印同一行格式，包含：

```
task_id=<id> idempotency_key=<key> transition=<FROM->TO> [error_category=<C>] [note=...]
```

覆盖创建、入队、`PENDING->RUNNING`、`RUNNING->SUCCEEDED/FAILED/CANCELLING`、
`CANCELLING->CANCELLED` 及启动恢复等事件，便于在失败时区分原因。

## 6. 模块划分

- `review_runner/models.py`：状态机、请求指纹、任务/结果模型与序列化。
- `review_runner/storage.py`：JSON 文件存储、CAS 迁移、原子落盘、启动恢复。
- `review_runner/pipeline.py`：本地仓库解析/索引/规则执行（纯本地，无外部依赖）。
- `review_runner/scheduler.py`：有界并发、排队、协作取消与终态提交。
- `review_runner/service.py`：幂等受理、冲突拒绝、恢复编排。
- `review_runner/api.py`：FastAPI HTTP 接口与错误类别 → HTTP 状态码映射。
