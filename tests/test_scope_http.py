"""HTTP exposure: scope/capacity payloads and transparent per-file results."""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from review_service.app import create_app

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def client(make_service, tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(f"{repo}/src")
    os.makedirs(f"{repo}/vendor")
    with open(f"{repo}/a.py", "w") as fh:
        fh.write("# TODO\nx = 1\n")
    with open(f"{repo}/src/b.py", "w") as fh:
        fh.write("y = 2\n")
    with open(f"{repo}/vendor/v.py", "w") as fh:
        fh.write("z = 3\n")
    with open(f"{repo}/blob.py", "wb") as fh:
        fh.write(b"\x00binary")
    with open(f"{repo}/readme.md", "w") as fh:
        fh.write("# hi\n")
    svc = make_service()
    await svc.start()
    app = create_app(svc)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        c.review_service = svc  # type: ignore[attr-defined]
        c.repo_dir = repo  # type: ignore[attr-defined]
        yield c


async def _wait_done(client, task_id):
    for _ in range(60):
        await asyncio.sleep(0.03)
        body = (await client.get(f"/tasks/{task_id}")).json()
        if body["state"] in ("SUCCEEDED", "FAILED", "CANCELED"):
            return body
    raise AssertionError("task did not finish")


async def test_scoped_submit_reports_every_file(client):
    payload = {
        "idempotency_key": "http-scope",
        "repo_path": client.repo_dir,
        "scope": {
            "recursive": True,
            "include_suffixes": [".py", ".md"],
            "exclude_patterns": ["vendor/"],
        },
    }
    r = await client.post("/tasks", json=payload)
    assert r.status_code == 201
    body = await _wait_done(client, r.json()["task_id"])
    assert body["state"] == "SUCCEEDED"
    by_path = {fr["path"]: fr for fr in body["file_results"]}
    assert set(by_path) == {
        "a.py", "blob.py", "readme.md", "src/b.py", "vendor/v.py",
    }
    assert by_path["vendor/v.py"]["disposition"] == "EXCLUDED"
    assert by_path["vendor/v.py"]["excluded_by"] == "vendor/"
    assert by_path["blob.py"]["disposition"] == "FAILED"
    assert by_path["blob.py"]["fail_reason"] == "NOT_TEXT"
    assert by_path["readme.md"]["disposition"] == "REVIEWED"
    summary = body["summary"]
    assert summary["total_in_scope"] == 5
    assert summary["failed"] == 1
    assert summary["excluded"] == 1
    assert summary["reviewed"] == 3
    assert summary["has_skips_or_failures"] is True
    # echo of effective request config
    assert body["scope"]["exclude_patterns"] == ["vendor/"]


async def test_capacity_reject_over_http_is_distinguishable(client):
    payload = {
        "idempotency_key": "http-cap",
        "repo_path": client.repo_dir,
        "capacity": {"max_files": 1, "overflow_policy": "reject"},
    }
    r = await client.post("/tasks", json=payload)
    assert r.status_code == 201  # accepted, then fails deterministically
    body = await _wait_done(client, r.json()["task_id"])
    assert body["state"] == "FAILED"
    assert body["error_category"] == "REPO_TOO_LARGE"


async def test_capacity_truncate_marks_skipped_over_http(client):
    payload = {
        "idempotency_key": "http-trunc",
        "repo_path": client.repo_dir,
        "scope": {"include_suffixes": [".py", ".md"]},
        "capacity": {"max_files": 2, "overflow_policy": "truncate"},
    }
    r = await client.post("/tasks", json=payload)
    body = await _wait_done(client, r.json()["task_id"])
    assert body["state"] == "SUCCEEDED"
    assert body["summary"]["truncated"] is True
    assert body["summary"]["rejected_by_limit"] == "max_files"
    skipped = [fr for fr in body["file_results"] if fr["disposition"] == "SKIPPED"]
    assert skipped  # leftovers labelled, not silently dropped
