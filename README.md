# ai-code-review-runner

面向本地代码仓库的自动化评审任务服务。任务从提交到结束的全过程在并发与
异常场景下保持一致且可解释；不依赖外部服务、真实凭据或付费资源——存储
使用本地 SQLite，评审对象为本地目录中的 Python 源码。

## 架构

```
review_service/
  models.py     任务状态机、错误类别、范围配置、容量上限、任务记录
  store.py      SQLite 持久化：幂等键约束、状态迁移校验、重启恢复、列迁移
  pipeline.py   评审流水线：范围枚举 -> 逐文件 解析/索引/规则，逐文件结果
  scheduler.py  有界并发调度器：并行上限 + 有界队列 + 取消
  service.py    编排层：提交、生命周期、取消、重启恢复
  app.py        FastAPI HTTP 层
main.py         本地入口（uvicorn）
tests/          pytest 测试套件
```

## 可配置评审范围（scope）

提交任务时可携带 `scope` 对象，声明这次要看仓库的哪些部分：

```json
{
  "idempotency_key": "demo-1",
  "repo_path": "/tmp/demo-repo",
  "scope": {
    "include_dirs": ["src", "lib"],
    "include_names": ["*.service", "main.py"],
    "include_extensions": [".py"],
    "exclude": ["src/generated/*", "*_test.py"],
    "recursive": true
  }
}
```

| 字段 | 含义 | 默认 |
|---|---|---|
| `include_dirs` | 仓库相对路径的子目录列表；空 = 仓库根目录 | `[]` |
| `include_names` | 文件名的 fnmatch 模式列表；空 = 所有文件名 | `[]` |
| `include_extensions` | 后缀列表（如 `.py`）；空数组 = 所有后缀 | `[".py"]` |
| `exclude` | 作用于仓库相对 POSIX 路径的 fnmatch 模式列表 | `[]` |
| `recursive` | 是否向 include 目录的子目录递归 | `true` |

匹配语义：先按 `include_dirs` + `recursive` 遍历（始终跳过 `.` 开头目录与
`__pycache__`），再按 `include_names` 与 `include_extensions` 过滤（两者都给出
时取交集），得到候选集；随后 `exclude` 命中的文件被剔除并记入逐文件结果。
遍历顺序按相对路径排序，结果稳定、可复现。

- 不指定 `scope` 时行为与上一版一致：递归扫描全部 `*.py`。
- `include_dirs` 指向不存在或不是目录的路径：任务失败，
  `error_category=INPUT_INVALID`。
- 范围内一个候选文件都没有：任务失败，`error_category=UNSUPPORTED_INPUT`。
- scope 参与幂等指纹：同一幂等键配不同 scope 会被拒绝（409）。

## 逐文件结果（files 与 summary）

任务成功后，响应里除汇总的 `findings` 外，还有每个进入范围的文件的去向
（`files`，按相对路径排序）与总体统计（`summary`）：

```json
{
  "state": "SUCCEEDED",
  "files": [
    {"path": "src/ok.py", "status": "REVIEWED",
     "findings": [{"rule": "no-todo", "path": "src/ok.py", "line": 2, "message": "..."}]},
    {"path": "src/gen.py", "status": "EXCLUDED", "excluded_by": "src/gen*"},
    {"path": "src/blob.py", "status": "FAILED",
     "error_category": "NOT_TEXT", "error_detail": "... not decodable as UTF-8 ..."}
  ],
  "summary": {
    "total_candidates": 3, "reviewed": 1, "excluded": 1, "failed": 1,
    "truncated": false, "omitted_files": 0, "omitted_paths": [],
    "has_skips_or_failures": true
  }
}
```

- `REVIEWED`：文件被正常评审，自带该文件的 findings。
- `EXCLUDED`：被用户排除规则挡掉，`excluded_by` 给出命中的那条规则。
- `FAILED`：处理失败，`error_category` 给出可区分原因（见下表）。单文件失败
  **不会**让整单失败，其余文件照出结论。
- 凡是进过范围的文件都会在 `files` 里出现，一个都不会无声消失。
- `summary.has_skips_or_failures` 为 `true` 即表示本次有文件被排除、失败或
  被截断，一眼可辨。

单文件失败的可区分原因：

| 类别 | 含义 |
|---|---|
| `INPUT_INVALID` | 文件读不出来（如悬空符号链接、权限不足） |
| `NOT_TEXT` | 字节内容无法按 UTF-8 解码成文本 |
| `UNSUPPORTED_INPUT` | 被 scope 纳入但不是引擎支持的源码类型（当前仅 `*.py`） |
| `PARSE_ERROR` | Python 语法错误 |
| `INDEX_ERROR` / `RULE_ERROR` | 索引 / 规则执行在该文件上失败 |

## 容量上限（大仓库）

服务级配置（环境变量，见文末）限制单次任务的文件数与总字节数：

- `REVIEW_MAX_FILES`（默认 5000）、`REVIEW_MAX_TOTAL_BYTES`（默认 50 MiB）。
- `REVIEW_LIMIT_POLICY` 决定超限后的确定性处理方式：
  - `REJECT`（默认）：任务明确失败，`error_category=SCOPE_LIMIT_EXCEEDED`，
    detail 给出实际值与上限，不做任何半截处理。
  - `TRUNCATE`：按相对路径排序保留能放进预算的前缀（超大单文件会被跳过、
    继续装更小的），`summary.truncated=true`、`omitted_files`/`omitted_paths`
    （最多列 100 条）清楚标注被截掉的部分。同一仓库重复执行结果一致。

批量变大后，既有并行上限（`REVIEW_MAX_CONCURRENCY`）、排队
（`REVIEW_MAX_QUEUE`）、取消与幂等语义不变。

## 与上一版的兼容

- 不传 `scope` 的提交行为与上一版一致（递归扫描 `*.py`、全部默认规则）。
- **行为变化**：上一版中“仓库里某个 `.py` 语法错误会让整单 FAILED/
  PARSE_ERROR”；现在语法错误是单文件 `FAILED/PARSE_ERROR`，任务整体
  SUCCEEDED 并在 `summary` 中体现。任务级失败只保留给：路径不存在、不是
  目录、范围内零候选、未知规则名、超限拒绝。
- `findings` 中的 `path` 现在统一为仓库相对路径（原来为绝对路径）。
- 数据库自动做列迁移（新增 `scope` / `file_results` / `summary` 列），旧
  库文件可直接打开；旧任务记录的这些字段读为默认空值。

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
- 键相同且负载（`repo_path` + `rules` + `scope` 的内容哈希）相同：返回首次创建的
  任务（HTTP 200），不会重复调度、重复产生结论。
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

任务级与文件级共用同一套类别；文件级失败落在 `files[].error_category`，
任务级失败落在任务的 `error_category`。

| 类别 | 含义 |
|---|---|
| `INPUT_INVALID` | 输入畸形：路径不存在、include 目录无效、文件不可读 |
| `UNSUPPORTED_INPUT` | 格式良好但不支持：范围内零候选、未知规则名、非支持的源码类型 |
| `NOT_TEXT` | 文件字节无法按 UTF-8 解码为文本 |
| `PARSE_ERROR` | 解析阶段失败（Python 语法错误） |
| `INDEX_ERROR` | 符号索引阶段失败 |
| `RULE_ERROR` | 规则执行阶段失败 |
| `CAPACITY` | 并发/队列上限导致的明确拒绝 |
| `SCOPE_LIMIT_EXCEEDED` | 仓库超出文件数/总字节上限且策略为 REJECT |
| `INTERRUPTED` | 服务重启打断（保留给需要标记中断的场景） |
| `INTERNAL` | 未预期的内部错误 |

畸形或不受支持的输入不会被静默忽略：一律映射为上述类别之一并落库。

## 重启恢复

- 所有任务状态持久化于 SQLite（WAL 模式）。
- 服务启动时 `reset_inflight()` 将 `QUEUED`/`RUNNING` 任务重置为 `QUEUED`
  并清空其（可能不完整的）findings，随后重新调度执行——不会重复计入
  已有结论；已终态的任务不受影响。
- 恢复后同一幂等键的重放仍返回原任务。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/tasks` | 提交任务（可带 `scope`）。201 新建 / 200 幂等重放 / 409 键冲突 / 503 容量拒绝 |
| GET | `/tasks` | 列出全部任务 |
| GET | `/tasks/{task_id}` | 查询单个任务（404 不存在）；响应含 `files` 逐文件结果与 `summary` |
| POST | `/tasks/{task_id}/cancel` | 取消任务（404 不存在） |

## 本地验证

```bash
uv sync                                        # 安装依赖
uv run pytest tests/ -v -o log_cli=true        # 运行测试并查看任务日志
uv run python main.py                          # 启动服务（127.0.0.1:8000）
```

手动验证示例：

```bash
mkdir -p /tmp/demo-repo/src && echo 'x = 1  # TODO' > /tmp/demo-repo/src/a.py
printf '\xff\xfe binary' > /tmp/demo-repo/src/blob.py
curl -s -X POST localhost:8000/tasks \
  -H 'content-type: application/json' \
  -d '{"idempotency_key": "demo-1", "repo_path": "/tmp/demo-repo",
       "scope": {"include_dirs": ["src"]}}'
curl -s localhost:8000/tasks/<task_id>   # 查看 files 逐文件结果与 summary
```

环境变量：`REVIEW_DB_PATH`（默认 `review_tasks.db`）、
`REVIEW_MAX_CONCURRENCY`（默认 2）、`REVIEW_MAX_QUEUE`（默认 16）、
`REVIEW_MAX_FILES`（默认 5000）、`REVIEW_MAX_TOTAL_BYTES`（默认 52428800）、
`REVIEW_LIMIT_POLICY`（`REJECT` 默认 / `TRUNCATE`）。

测试覆盖：`tests/test_scope.py`（范围过滤、逐文件去向、确定性、幂等）、
`tests/test_limits.py`（超限拒绝/截断）、`tests/test_error_categories.py`
（错误类别）、`tests/test_http.py`（HTTP 端到端含 scope）、以及原有的并发、
取消、恢复、幂等用例。测试日志（`-o log_cli=true`）包含每个任务的
task_id、幂等键、状态迁移，以及每个文件的去向与错误/跳过类别。
