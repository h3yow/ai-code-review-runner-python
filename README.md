# ai-code-review-runner

面向本地代码仓库的自动化评审任务服务。任务从提交到结束的全过程在并发与
异常场景下保持一致且可解释；不依赖外部服务、真实凭据或付费资源——存储
使用本地 SQLite，评审对象为本地目录中的 Python 源码。

## 架构

```
review_service/
  models.py     任务状态机、错误类别、任务记录
  store.py      SQLite 持久化：幂等键约束、状态迁移校验、重启恢复
  pipeline.py   评审流水线：解析(parse) -> 索引(index) -> 规则(rules)
  scheduler.py  有界并发调度器：并行上限 + 有界队列 + 取消
  service.py    编排层：提交、生命周期、取消、重启恢复
  app.py        FastAPI HTTP 层
main.py         本地入口（uvicorn）
tests/          pytest 测试套件
```

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
- 键相同且负载（`repo_path` + `rules` 的内容哈希）相同：返回首次创建的
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

| 类别 | 含义 |
|---|---|
| `INPUT_INVALID` | 输入畸形：路径不存在、文件不可读 |
| `UNSUPPORTED_INPUT` | 格式良好但不支持：无 `*.py` 文件、未知规则名 |
| `PARSE_ERROR` | 解析阶段失败（Python 语法错误） |
| `INDEX_ERROR` | 符号索引阶段失败 |
| `RULE_ERROR` | 规则执行阶段失败 |
| `CAPACITY` | 并发/队列上限导致的明确拒绝 |
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
| POST | `/tasks` | 提交任务。201 新建 / 200 幂等重放 / 409 键冲突 / 503 容量拒绝 |
| GET | `/tasks` | 列出全部任务 |
| GET | `/tasks/{task_id}` | 查询单个任务（404 不存在） |
| POST | `/tasks/{task_id}/cancel` | 取消任务（404 不存在） |

## 本地验证

```bash
uv sync                                        # 安装依赖
uv run pytest tests/ -v -o log_cli=true        # 运行测试并查看任务日志
uv run python main.py                          # 启动服务（127.0.0.1:8000）
```

手动验证示例：

```bash
mkdir -p /tmp/demo-repo && echo 'x = 1  # TODO' > /tmp/demo-repo/a.py
curl -s -X POST localhost:8000/tasks \
  -H 'content-type: application/json' \
  -d '{"idempotency_key": "demo-1", "repo_path": "/tmp/demo-repo"}'
curl -s localhost:8000/tasks/<task_id>
```

环境变量：`REVIEW_DB_PATH`（默认 `review_tasks.db`）、
`REVIEW_MAX_CONCURRENCY`（默认 2）、`REVIEW_MAX_QUEUE`（默认 16）。
