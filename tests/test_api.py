"""End-to-end HTTP API tests using an in-process service."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from review_runner.api import create_app  # noqa: E402
from review_runner.models import TaskState  # noqa: E402
from tests.fakes import GatedPipeline  # noqa: E402
from tests.util import wait_for_state  # noqa: E402


def _client(service):
    app = create_app(service)
    return TestClient(app)


@pytest.fixture
def client(make_service):
    service = make_service()
    service.start()
    with _client(service) as c:
        c.service = service
        yield c


def test_submit_duplicate_and_get(client, tmp_repo):
    body = {"repo_path": str(tmp_repo), "idempotency_key": "api-1"}

    r1 = client.post("/reviews", json=body)
    assert r1.status_code == 201
    assert r1.json()["created"] is True
    task_id = r1.json()["task"]["task_id"]

    r2 = client.post("/reviews", json=body)
    assert r2.status_code == 200
    assert r2.json()["created"] is False
    assert r2.json()["task"]["task_id"] == task_id

    wait_for_state(client.service, task_id, TaskState.SUCCEEDED)
    r3 = client.get(f"/reviews/{task_id}")
    assert r3.status_code == 200
    assert r3.json()["state"] == "SUCCEEDED"

    r4 = client.get("/reviews/by-key/api-1")
    assert r4.status_code == 200
    assert r4.json()["task_id"] == task_id


def test_conflict_returns_409_distinguishable(client, tmp_repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.md").write_text("x\n", encoding="utf-8")

    client.post("/reviews", json={"repo_path": str(tmp_repo), "idempotency_key": "same"})
    r = client.post("/reviews", json={"repo_path": str(other), "idempotency_key": "same"})
    assert r.status_code == 409
    assert r.json()["error"]["category"] == "IDEMPOTENCY_CONFLICT"


def test_queue_full_returns_503(make_service, tmp_repo):
    pipeline = GatedPipeline()
    service = make_service(
        repo_pipeline=pipeline, max_concurrency=1, max_queue_size=0
    )
    service.start()
    with _client(service) as c:
        r1 = c.post(
            "/reviews",
            json={"repo_path": str(tmp_repo), "idempotency_key": "http-1"},
        )
        assert r1.status_code in (201, 200)
        assert pipeline.started.wait(2.0)

        r2 = c.post(
            "/reviews",
            json={"repo_path": str(tmp_repo), "idempotency_key": "http-2"},
        )
        assert r2.status_code == 503
        assert r2.json()["error"]["category"] == "QUEUE_FULL"
    pipeline.gate.set()


def test_not_found_and_validation(client):
    assert client.get("/reviews/nope").status_code == 404
    r = client.post("/reviews", json={"repo_path": "x", "idempotency_key": ""})
    assert r.status_code == 422


def test_cancel_endpoint(make_service, tmp_repo):
    pipeline = GatedPipeline()
    service = make_service(repo_pipeline=pipeline, max_concurrency=1)
    service.start()
    with _client(service) as c:
        r = c.post(
            "/reviews",
            json={"repo_path": str(tmp_repo), "idempotency_key": "http-cancel"},
        )
        task_id = r.json()["task"]["task_id"]
        assert pipeline.started.wait(2.0)
        wait_for_state(service, task_id, TaskState.RUNNING)

        rc = c.post(f"/reviews/{task_id}/cancel")
        assert rc.status_code == 200
        assert rc.json()["state"] in ("CANCELLING", "CANCELLED")

        final = wait_for_state(service, task_id, TaskState.CANCELLED)
        assert final.error_category == "CANCELLED"
        assert final.result is None
    pipeline.gate.set()
    time.sleep(0.02)
