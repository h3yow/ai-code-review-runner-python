# AI Code Review Runner (Python, 本地版)

一个面向**本地代码仓库**的自动化评审任务服务：负责任务的提交、有界并发调度、
取消、异常归类与重启恢复。不访问网络、不需要凭据或付费资源。

- Python 3.14，FastAPI 提供 HTTP 接口
- 线程池 + 有界等待队列，容量可配，满员明确拒绝
- 幂等提交：同一键只有一份结果；同键不同内容返回冲突
- 任务状态确定、不可跳变（CAS 版本控制 + 原子 JSON 落盘）
- 取消排队中/执行中的任务，取消后绝不残留部分结论
- 重启后未完成任务收敛到一致终态或重新排队，不重复计入结论

## 快速开始

```bash
uv sync
uv run uvicorn main:app --host 127.0.0.1 --port 8000
# 或
uv run python main.py
```

健康检查：

```bash
curl -s http://127.0.0.1:8000/health
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/reviews` | 提交评审（201 新建 / 200 幂等重复 / 409 冲突 / 503 队列满） |
| `GET` | `/reviews/{task_id}` | 按任务 id 查询 |
| `GET` | `/reviews/by-key/{idempotency_key}` | 按幂等键查询 |
| `GET` | `/reviews` | 列出全部任务 |
| `POST` | `/reviews/{task_id}/cancel` | 取消排队中/执行中的任务 |

提交示例：

```bash
curl -s -X POST http://127.0.0.1:8000/reviews \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_path": "/abs/path/to/your/repo",
    "idempotency_key": "review-2026-09-26-001",
    "rules": ["line-length", "todo-comment", "print-call"]
  }'
```

规则可省略（默认执行全部）。支持解析的文件类型：`.py`、`.txt`、`.md`、
`.json`；仓库中出现其他类型文件会返回 `UNSUPPORTED_FILE`（不会静默跳过）。

任务对象关键字段：`task_id`、`idempotency_key`、`state`、`result`、
`error_category`、`error_message`、`version`。

状态集合：`PENDING → RUNNING → SUCCEEDED | FAILED`，以及
`PENDING/RUNNING → CANCELLING → CANCELLED`。完整规则见
[`docs/design.md`](docs/design.md)。

## 本地验证

运行全部单测（含幂等重复提交、并发抢跑、取消、重启恢复、错误归类与 HTTP 集成）：

```bash
uv run pytest
```

观察生命周期日志（任务标识、幂等键、状态迁移、错误类别会打印到 stderr）：

```bash
uv run pytest -s -k test_duplicate_submission_returns_single_task
```

手工验证并发/取消可使用两个终端：先提交一个较大的本地仓库并立即调用
`/reviews/{id}/cancel`，再 `GET` 任务，终态应为 `CANCELLED` 且 `result=null`。

容量配置在构造 `ReviewService` 时调整：

```python
ReviewService(
    store_path="tasks.json",
    max_concurrency=2,    # 同时运行上限
    max_queue_size=100,   # 等待队列上限（0 = 不排队，满员即 QUEUE_FULL）
)
```

任务数据默认持久化在进程工作目录的 `tasks.json`，删除该文件即可清空全部任务。

## 错误类别

`VALIDATION_ERROR`、`REPO_NOT_FOUND`、`UNSUPPORTED_FILE`、`MALFORMED_INPUT`、
`INDEX_ERROR`、`RULE_EXECUTION_ERROR`、`IDEMPOTENCY_CONFLICT`、`QUEUE_FULL`、
`TASK_NOT_FOUND`、`CANCELLED`、`INTERRUPTED`、`INTERNAL_ERROR`。

各类别的语义、HTTP 映射与重启恢复行为见 [`docs/design.md`](docs/design.md)。
