"""HTTP layer: distinguishable rejection reasons and endpoint behaviour."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from review_service.app import create_app

from tests.conftest import BlockingPipeline

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def client(make_service, repo_dir):
    svc = make_service()
    await svc.start()
    app = create_app(svc)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        c.review_service = svc  # type: ignore[attr-defined]
        c.repo_dir = repo_dir  # type: ignore[attr-defined]
        yield c


async def test_submit_replay_and_conflict(client):
    payload = {"idempotency_key": "k1", "repo_path": client.repo_dir}
    r1 = await client.post("/tasks", json=payload)
    assert r1.status_code == 201
    r2 = await client.post("/tasks", json=payload)
    assert r2.status_code == 200
    assert r2.json()["task_id"] == r1.json()["task_id"]

    conflict = await client.post("/tasks", json={**payload, "rules": ["no-todo"]})
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["reason"] == "IDEMPOTENCY_CONFLICT"
    assert detail["existing_task_id"] == r1.json()["task_id"]


async def test_capacity_rejection_is_distinguishable(make_service, repo_dir, monkeypatch):
    blocker = BlockingPipeline()
    monkeypatch.setattr("review_service.service.run_pipeline", blocker)
    svc = make_service(max_concurrency=1, max_queue=0)
    await svc.start()
    app = create_app(svc)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        r1 = await client.post("/tasks", json={"idempotency_key": "k1",
                                               "repo_path": repo_dir})
        assert r1.status_code == 201
        await asyncio.to_thread(blocker.entered.wait, 10)
        r2 = await client.post("/tasks", json={"idempotency_key": "k2",
                                               "repo_path": repo_dir})
        assert r2.status_code == 503
        assert r2.json()["detail"]["reason"] == "CAPACITY_EXHAUSTED"
        blocker.release.set()


async def test_get_and_cancel_endpoints(client):
    r = await client.post("/tasks", json={"idempotency_key": "k1",
                                          "repo_path": client.repo_dir})
    task_id = r.json()["task_id"]
    for _ in range(50):
        await asyncio.sleep(0.05)
        g = await client.get(f"/tasks/{task_id}")
        if g.json()["state"] in ("SUCCEEDED", "FAILED"):
            break
    assert g.json()["state"] == "SUCCEEDED"
    assert g.json()["findings"]

    listing = await client.get("/tasks")
    assert any(t["task_id"] == task_id for t in listing.json())

    cancel = await client.post(f"/tasks/{task_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["state"] == "SUCCEEDED"  # terminal: no-op

    assert (await client.get("/tasks/nope")).status_code == 404
    assert (await client.post("/tasks/nope/cancel")).status_code == 404


async def test_submit_with_scope_and_per_file_results(client, tmp_path):
    """End to end: scope in, per-file outcomes + summary out."""
    repo = tmp_path / "repo2"
    repo.mkdir()
    (repo / "ok.py").write_text("A = 1  # TODO\n")
    (repo / "blob.py").write_bytes(b"\xff\xfe binary")
    (repo / "skip.py").write_text("B = 2\n")
    payload = {
        "idempotency_key": "k-scope",
        "repo_path": str(repo),
        "scope": {"exclude": ["skip.py"]},
    }
    r = await client.post("/tasks", json=payload)
    assert r.status_code == 201
    task_id = r.json()["task_id"]
    assert r.json()["scope"]["exclude"] == ["skip.py"]

    for _ in range(50):
        await asyncio.sleep(0.05)
        g = await client.get(f"/tasks/{task_id}")
        if g.json()["state"] in ("SUCCEEDED", "FAILED"):
            break
    body = g.json()
    assert body["state"] == "SUCCEEDED"
    by_path = {f["path"]: f for f in body["files"]}
    assert by_path["ok.py"]["status"] == "REVIEWED"
    assert by_path["blob.py"]["status"] == "FAILED"
    assert by_path["blob.py"]["error_category"] == "NOT_TEXT"
    assert by_path["skip.py"]["status"] == "EXCLUDED"
    assert by_path["skip.py"]["excluded_by"] == "skip.py"
    assert body["summary"]["has_skips_or_failures"] is True

    # idempotent replay with the same scope returns the same task
    replay = await client.post("/tasks", json=payload)
    assert replay.status_code == 200
    assert replay.json()["task_id"] == task_id
    # same key with a different scope is a conflict
    conflict = await client.post("/tasks", json={
        **payload, "scope": {"exclude": ["other.py"]}})
    assert conflict.status_code == 409
