# ai-code-review-runner

面向本地代码仓库的自动化评审任务服务。任务从提交到结束的全过程在并发与
异常场景下保持一致且可解释；不依赖外部服务、真实凭据或付费资源——存储
使用本地 SQLite。评审范围可由用户按子目录 / 文件名 / 后缀 / 排除规则配置，
每个进入范围的文件都有可复现、可区分的逐文件结果。

## 架构

```
review_service/
  models.py     任务状态机、错误类别、范围/容量配置、逐文件结果、任务记录
  scope.py      范围发现（包含/排除/递归）与容量上限（拒绝/截断）
  store.py      SQLite 持久化：幂等键约束、状态迁移校验、重启恢复
  pipeline.py   评审流水线：范围 -> 读取 -> 解析/索引 -> 逐文件规则
  scheduler.py  有界并发调度器：并行上限 + 有界队列 + 取消
  service.py    编排层：提交、生命周期、取消、重启恢复
  app.py        FastAPI HTTP 层
main.py         本地入口（uvicorn）
tests/          pytest 测试套件
```

## 可配置评审范围

提交时通过 `scope` 指定本次看哪些文件；所有规则都针对**仓库相对路径**
（POSIX 风格 `/`），且包含规则之间取交集、再叠加排除规则：

| 字段 | 含义 |
|---|---|
| `recursive` | 是否进入子目录，默认 `true` |
| `include_dirs` | 只看这些子目录（如 `["src", "tests/unit"]`），可多个 |
| `include_names` | 只纳入匹配这些文件名 glob 的文件（如 `["README.*"]`） |
| `include_suffixes` | 只纳入这些后缀（如 `[".py", ".md"]`；`"py"` 会规范化为 `.py`） |
| `exclude_patterns` | 排除规则，逐条按用户顺序匹配，首个命中的规则会记录在结果里 |

排除规则匹配语义：

- 以 `/` 结尾（如 `vendor/`）：匹配该子树（目录及其下所有文件）；
- 含 `/`（如 `tests/fixtures/*`）：对完整相对路径做 glob（`*` 不跨目录）；
- 其余（如 `*.pyi`）：只对文件名做 glob。

**默认范围与兼容性**：当一条包含规则都没给时，默认只看 `.py` / `.pyi`，
与升级前的行为完全一致；只要显式给了任意一条包含规则，就完全按用户的来，
一次提交可覆盖范围内所有后缀的文件。内置始终不枚举 `.git/.hg/.svn/
__pycache__` 等元数据目录（它们既不算纳入也不算排除）。

支持程度：服务能读成 UTF-8 文本、且后缀在已知文本类型集合
（`.py/.pyi/.txt/.md/.rst/.cfg/.ini/.toml/.yaml/.yml/.json/.xml/.html/
.css/.js/.ts/.sh` 等）内的文件会被评审；`.py/.pyi` 走解析+索引并适用全部
规则，其它文本文件只适用不依赖语法树的文本类规则（如 `no-todo`）。

### 请求示例

```json
{
  "idempotency_key": "run-2026-10-07",
  "repo_path": "/path/to/repo",
  "rules": ["no-todo", "long-function"],
  "scope": {
    "recursive": true,
    "include_dirs": ["src"],
    "include_suffixes": [".py"],
    "exclude_patterns": ["vendor/", "tests/fixtures/*", "*.pyi"]
  },
  "capacity": {"max_files": 5000, "max_total_bytes": 5242880,
               "overflow_policy": "truncate"}
}
```

## 逐文件结果透明

任务终态的 `file_results` 覆盖**每一个进入范围的文件**，按相对路径稳定
排序，顺序与内容可复现。每个文件必为以下去向之一：

| `disposition` | 含义 | 附加字段 |
|---|---|---|
| `REVIEWED` | 确实被评审，带它自己的 `findings` | `findings`、`size_bytes` |
| `EXCLUDED` | 被用户的排除规则挡掉 | `excluded_by`（命中的规则原文） |
| `FAILED` | 处理失败，原因可区分，整单不崩 | `fail_reason`、`fail_detail` |
| `SKIPPED` | 容量截断，按既定规则未处理 | `fail_reason`（可能为 `FILE_TOO_LARGE`）、`fail_detail` |

逐文件失败原因 `fail_reason`：

| 原因 | 含义 |
|---|---|
| `NOT_TEXT` | 文件不是合法 UTF-8 文本（含 NUL 字节或解码失败） |
| `UNSUPPORTED_FILE_TYPE` | 能读成文本，但不是服务支持的源码类型 |
| `READ_ERROR` | 操作系统层面读取失败 |
| `FILE_TOO_LARGE` | 单文件大小超过 `max_total_bytes` |
| `PARSE_ERROR` | 源码解析失败（如 Python 语法错误） |
| `INDEX_ERROR` | 符号索引阶段失败 |
| `RULE_ERROR` | 规则执行阶段失败 |

单个文件读不成文本或类型不支持，**不再让整单失败**：它落为带可区分原因
的 `FAILED` 文件行，其余可处理文件照常出结论；任务的 `summary` 一眼给出
`total_in_scope / reviewed / excluded / failed / skipped / truncated /
rejected_by_limit`，以及 `has_skips_or_failures` 标志。注意：语法错误由
原来的“整单 `FAILED/PARSE_ERROR`”变为“该文件 `FAILED/PARSE_ERROR`、
任务仍 `SUCCEEDED`”，这是本版有意的语义变化。

## 容量与一致性（大仓库）

每次提交可配置 `capacity`：

| 字段 | 默认值 | 含义 |
|---|---|---|
| `max_files` | `10000` | 范围内文件数上限 |
| `max_total_bytes` | `10485760`（10 MiB） | 范围内总字节数上限（也用于单文件判断） |
| `overflow_policy` | `reject` | 超限时：`reject`（整单拒绝）或 `truncate`（截断） |

- `reject`：发现超限即整单拒绝，任务落库 `FAILED/REPO_TOO_LARGE`，
  `error_detail` 说明撞的是哪条上限与实际规模，不处理任何文件、不做一半。
- `truncate`：按**固定顺序**（先文件数后累计字节，文件按相对路径排序）
  取一个稳定前缀正常处理，之后的文件全部标为 `SKIPPED` 并在 `fail_detail`
  写明触发的上限；首个使累计字节超限的文件若单文件就超过
  `max_total_bytes`，会额外标 `FILE_TOO_LARGE`。`summary.truncated=true`、
  `rejected_by_limit` 标明上限。同一输入多次运行结果完全一致。
- 排除规则挡掉的文件不占用容量计数（它们没有被“处理”）。

批量变大不影响原有机制：并行上限 `REVIEW_MAX_CONCURRENCY`、排队
`REVIEW_MAX_QUEUE`、排队/运行取消、重启恢复照旧生效；幂等键的负载指纹
已纳入 scope 与 capacity（含 policy），同键不同范围/容量会 409 冲突，
同键同负载重放只返回首个任务，绝不重复执行、重复出结论。

## 任务状态机

```
PENDING --> QUEUED --> RUNNING --> SUCCEEDED
              |           |
              |           +-----> FAILED
              |           |
              +-----> CANCELED <--+
              |
              +-----> FAILED (CAPACITY：队列已满被明确拒绝)
```

- 状态迁移由 `models.ALLOWED_TRANSITIONS` 白名单约束，非法迁移抛出
  `InvalidTransitionError`，不可能跳变。
- `SUCCEEDED` / `FAILED` / `CANCELED` 为终态，无任何出边。
- 每次迁移都写入 SQLite 并打印日志（task_id、幂等键、迁移方向、错误类别）。

## 幂等规则

- 提交时必须携带 `idempotency_key`，键在存储层有唯一约束。
- 键相同且负载（`repo_path` + `rules` + `scope` + `capacity` 的内容哈希）
  相同：返回首次创建的任务（HTTP 200），不会重复调度、重复产生结论。
- 键相同但负载不同：拒绝并给出可区分原因（HTTP 409，
  `reason=IDEMPOTENCY_CONFLICT`，附 `existing_task_id`）。
- 多线程/多协程抢跑同一键时，存储层串行化写入，保证只有一条记录。

## 并发与取消规则

- 并行执行数受 `REVIEW_MAX_CONCURRENCY`（默认 2）约束，超出者进入队列。
- 队列长度受 `REVIEW_MAX_QUEUE`（默认 16）约束；队列满时提交被明确拒绝：
  HTTP 503 `reason=CAPACITY_EXHAUSTED`，任务落库为 `FAILED/CAPACITY`，
  客户端可稍后以同一幂等键重试语义另行提交新键。
- 排队中与执行中的任务都可取消（`POST /tasks/{id}/cancel`）：
  - 排队任务：直接从队列移除，状态置 `CANCELED`，永不执行。
  - 执行中任务：协程被取消；流水线在线程中的残余结果会被丢弃——
    终态迁移经状态机校验，`CANCELED` 为终态，迟到的成功结果无法写入，
    因此取消后不会产生成功结果或残留部分结论。
- 对已终态的任务取消是幂等 no-op，返回当前状态。

## 错误类别

| 类别 | 含义 |
|---|---|
| `INPUT_INVALID` | 输入畸形：路径不存在、文件不可读 |
| `UNSUPPORTED_INPUT` | 格式良好但不支持：无 `*.py` 文件、未知规则名 |
| `PARSE_ERROR` | 任务级解析失败（保留；现在语法错误落在逐文件 `fail_reason`） |
| `INDEX_ERROR` | 任务级符号索引阶段失败（保留） |
| `RULE_ERROR` | 任务级规则执行阶段失败（保留） |
| `CAPACITY` | 并发/队列上限导致的明确拒绝 |
| `REPO_TOO_LARGE` | 范围内文件数/总大小超容量上限，按 `reject` 策略整单拒绝 |
| `INTERRUPTED` | 服务重启打断（保留给需要标记中断的场景） |
| `INTERNAL` | 未预期的内部错误 |

任务级类别用于仓库整体问题（路径不存在/不是目录/范围内无文件/未知规则/
仓库超容量）；文件本身读不成或不支持不进入任务级类别，而是落到逐文件的
`fail_reason`（`NOT_TEXT` / `UNSUPPORTED_FILE_TYPE` 等），任务通常仍
`SUCCEEDED`。畸形或不受支持的输入不会被静默忽略：一律映射为上述类别或
逐文件原因并落库。

## 重启恢复

- 所有任务状态持久化于 SQLite（WAL 模式）。
- 服务启动时 `reset_inflight()` 将 `QUEUED`/`RUNNING` 任务重置为 `QUEUED`
  并清空其（可能不完整的）findings，随后重新调度执行——不会重复计入
  已有结论；已终态的任务不受影响。
- 恢复后同一幂等键的重放仍返回原任务。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/tasks` | 提交任务（可带 `scope` / `capacity`）。201 新建 / 200 幂等重放 / 409 键冲突 / 503 队列容量拒绝 |
| GET | `/tasks` | 列出全部任务 |
| GET | `/tasks/{task_id}` | 查询单个任务（404 不存在），含 `file_results` 与 `summary` |
| POST | `/tasks/{task_id}/cancel` | 取消任务（404 不存在） |

## 本地验证

```bash
uv sync                                        # 安装依赖
uv run pytest tests/ -v -o log_cli=true        # 运行测试并查看任务日志
uv run python main.py                          # 启动服务（127.0.0.1:8000）
```

测试布局（新行为均有用例）：

- `tests/test_scope_filtering.py`：包含/排除/递归/默认范围/稳定顺序；
- `tests/test_file_dispositions.py`：四种去向、`NOT_TEXT`、
  `UNSUPPORTED_FILE_TYPE`、`PARSE_ERROR`、汇总标志、整单不崩；
- `tests/test_capacity_limits.py`：文件数/字节数超限的拒绝与确定性截断、
  `FILE_TOO_LARGE`、大批量下并发上限仍生效；
- `tests/test_scope_idempotency_persistence.py`：范围/容量参与幂等指纹、
  逐文件结果跨 SQLite 重开持久化；
- `tests/test_scope_http.py`：HTTP 层范围提交、逐文件结果、拒绝/截断；
- 原有 `test_*`（并发、排队、取消、恢复、幂等、错误类别、HTTP）全部保留。

`-o log_cli=true` 会打印每个任务的任务标识、幂等键、状态迁移、错误类别，
以及每个文件的 `disposition`/`reason` 和任务级
`reviewed/excluded/failed/skipped/truncated` 汇总。

手动验证示例：

```bash
mkdir -p /tmp/demo-repo/src /tmp/demo-repo/vendor
printf '# TODO\nx=1\n'        > /tmp/demo-repo/a.py
printf 'V=1\n'               > /tmp/demo-repo/vendor/v.py
printf '\x00bin'             > /tmp/demo-repo/blob.py
curl -s -X POST localhost:8000/tasks \
  -H 'content-type: application/json' \
  -d '{"idempotency_key": "demo-1", "repo_path": "/tmp/demo-repo",
       "scope": {"include_suffixes": [".py"], "exclude_patterns": ["vendor/"]}}'
curl -s localhost:8000/tasks/<task_id>     # 查看 file_results 与 summary
```

环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `REVIEW_DB_PATH` | `review_tasks.db` | SQLite 路径（旧库会自动加列迁移） |
| `REVIEW_MAX_CONCURRENCY` | `2` | 并行执行上限 |
| `REVIEW_MAX_QUEUE` | `16` | 排队队列长度 |
| `REVIEW_MAX_FILES` | `10000` | 每次任务默认文件数上限 |
| `REVIEW_MAX_TOTAL_BYTES` | `10485760` | 每次任务默认总字节数上限 |
| `REVIEW_OVERFLOW_POLICY` | `reject` | 默认超限策略（`reject` / `truncate`） |

请求级 `capacity` 字段优先于上述环境默认值。
