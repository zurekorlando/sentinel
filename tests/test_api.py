"""
Sentinel Backup API — smoke tests
==================================

Uses ``httpx.AsyncClient`` with ``ASGITransport`` to exercise every
endpoint in-process: no running server, no MinIO, no Docker required.

The ``SENTINEL_JOBS_DIR`` environment variable is overridden via
``monkeypatch`` so the API reads from a throw-away temp directory for
each test.  Module-level singleton caches in ``api.deps`` are reset
before each test to prevent state bleed.

Run
---
    pytest tests/test_api.py -v --tb=short
    # or via make:
    make test-api

Requirements (in addition to the main requirements.txt)
---------
    httpx>=0.27.0
    pytest-asyncio>=0.23.0
    anyio[trio]>=4.4.0
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncGenerator

import httpx
import pytest

import api.deps as _deps
from api.main import app

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_ID = "smoke-test-job"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def job_dir(tmp_path: Path) -> Path:
    """
    Create a temp directory tree containing one valid local-storage job
    config, then return the path to the ``jobs/`` directory inside it.

    Layout::

        tmp_path/
          jobs/
            smoke-test-job.json
          source/               ← source_paths (empty dir, no real files)
          store/                ← local storage backend
          catalog.db            ← SQLite catalog (created on first access)
    """
    jobs = tmp_path / "jobs"
    jobs.mkdir()

    source = tmp_path / "source"
    source.mkdir()

    store = tmp_path / "store"
    store.mkdir()

    cfg: dict[str, Any] = {
        "job_id": JOB_ID,
        "source_paths": [str(source)],
        "exclusions": [],
        "catalog_path": str(tmp_path / "catalog.db"),
        "compression_level": 1,
        "max_workers": 1,
        "webhook_url": None,
        "key_salt_hex": None,
        "storage": {
            "provider": "local",
            "base_path": str(store),
        },
        "retention": {"daily": 7, "weekly": 4, "monthly": 12},
    }
    (jobs / f"{JOB_ID}.json").write_text(json.dumps(cfg, indent=2))
    return jobs


@pytest.fixture(autouse=True)
def isolate_deps(monkeypatch: pytest.MonkeyPatch, job_dir: Path) -> None:
    """
    Point every test at the temp jobs directory and clear lazily-cached
    singletons in ``api.deps`` so no catalog / storage / run-state bleeds
    between tests.
    """
    monkeypatch.setenv("SENTINEL_JOBS_DIR", str(job_dir))
    # Replace module-level dicts with fresh copies; monkeypatch restores
    # the originals automatically after the test.
    monkeypatch.setattr(_deps, "_catalogs", {})
    monkeypatch.setattr(_deps, "_storages", {})
    monkeypatch.setattr(_deps, "_active_runs", {})


@pytest.fixture()
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """
    Async HTTP client backed by the FastAPI ASGI app directly.
    No network socket is opened; all transport happens in-process.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# GET /healthz
# ---------------------------------------------------------------------------


class TestHealthz:
    """Liveness probe endpoint."""

    async def test_returns_200(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/healthz")
        assert r.status_code == 200

    async def test_status_ok(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/healthz")
        assert r.json()["status"] == "ok"

    async def test_version_present(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/healthz")
        assert "version" in r.json()

    async def test_content_type_json(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/healthz")
        assert "application/json" in r.headers["content-type"]


# ---------------------------------------------------------------------------
# GET /api/jobs
# ---------------------------------------------------------------------------


class TestJobsList:
    """Listing all configured backup jobs."""

    async def test_returns_200(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/jobs")
        assert r.status_code == 200

    async def test_returns_list(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/jobs")
        assert isinstance(r.json(), list)

    async def test_contains_test_job(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/jobs")
        ids = [j["job_id"] for j in r.json()]
        assert JOB_ID in ids

    async def test_job_has_required_fields(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/jobs")
        job = next(j for j in r.json() if j["job_id"] == JOB_ID)
        for field in ("job_id", "source_paths", "catalog_path", "storage", "retention"):
            assert field in job, f"Missing field: {field}"

    async def test_empty_jobs_dir_returns_empty_list(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        client: httpx.AsyncClient,
    ) -> None:
        empty_dir = tmp_path / "empty_jobs"
        empty_dir.mkdir()
        monkeypatch.setenv("SENTINEL_JOBS_DIR", str(empty_dir))
        r = await client.get("/api/jobs")
        assert r.status_code == 200
        assert r.json() == []


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}
# ---------------------------------------------------------------------------


class TestJobGet:
    """Fetching a single job's configuration."""

    async def test_known_job_returns_200(self, client: httpx.AsyncClient) -> None:
        r = await client.get(f"/api/jobs/{JOB_ID}")
        assert r.status_code == 200

    async def test_known_job_fields(self, client: httpx.AsyncClient) -> None:
        body = (await client.get(f"/api/jobs/{JOB_ID}")).json()
        assert body["job_id"] == JOB_ID
        assert isinstance(body["source_paths"], list)
        assert body["storage"]["provider"] == "local"
        assert body["retention"]["daily"] == 7

    async def test_unknown_job_returns_404(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/jobs/does-not-exist")
        assert r.status_code == 404

    async def test_404_has_detail(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/jobs/does-not-exist")
        assert "detail" in r.json()


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/runs/latest   (before any backup run)
# ---------------------------------------------------------------------------


class TestLatestRun:
    """Latest-run endpoint when no backup has been triggered yet."""

    async def test_no_runs_returns_404(self, client: httpx.AsyncClient) -> None:
        r = await client.get(f"/api/jobs/{JOB_ID}/runs/latest")
        assert r.status_code == 404

    async def test_unknown_job_returns_404(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/jobs/no-such-job/runs/latest")
        assert r.status_code == 404

    async def test_no_runs_has_detail(self, client: httpx.AsyncClient) -> None:
        r = await client.get(f"/api/jobs/{JOB_ID}/runs/latest")
        assert "detail" in r.json()


# ---------------------------------------------------------------------------
# GET /api/snapshots
# ---------------------------------------------------------------------------


class TestSnapshots:
    """Snapshot listing and lookup endpoints."""

    async def test_list_all_returns_200(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/snapshots")
        assert r.status_code == 200

    async def test_list_all_returns_list(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/snapshots")
        assert isinstance(r.json(), list)

    async def test_filter_by_known_job_returns_200(self, client: httpx.AsyncClient) -> None:
        r = await client.get(f"/api/snapshots?job_id={JOB_ID}")
        assert r.status_code == 200

    async def test_filter_by_known_job_empty(self, client: httpx.AsyncClient) -> None:
        """Fresh catalog → no snapshots."""
        r = await client.get(f"/api/snapshots?job_id={JOB_ID}")
        assert r.json() == []

    async def test_filter_unknown_job_returns_404(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/snapshots?job_id=no-such-job")
        assert r.status_code == 404

    async def test_get_nonexistent_snapshot_returns_404(self, client: httpx.AsyncClient) -> None:
        r = await client.get(f"/api/snapshots/deadbeef1234?job_id={JOB_ID}")
        assert r.status_code == 404

    async def test_delete_nonexistent_snapshot_returns_404(self, client: httpx.AsyncClient) -> None:
        r = await client.delete(f"/api/snapshots/deadbeef1234?job_id={JOB_ID}")
        assert r.status_code == 404

    async def test_get_snapshot_missing_job_id_param(self, client: httpx.AsyncClient) -> None:
        """GET /api/snapshots/{id} requires job_id query param (422 if absent)."""
        r = await client.get("/api/snapshots/deadbeef1234")
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/storage/{job_id}/stats
# ---------------------------------------------------------------------------


class TestStorageStats:
    """Storage statistics endpoint."""

    async def test_known_job_returns_200(self, client: httpx.AsyncClient) -> None:
        r = await client.get(f"/api/storage/{JOB_ID}/stats")
        assert r.status_code == 200

    async def test_response_schema(self, client: httpx.AsyncClient) -> None:
        body = (await client.get(f"/api/storage/{JOB_ID}/stats")).json()
        required = (
            "job_id",
            "total_chunks",
            "total_original_bytes",
            "total_compressed_bytes",
            "dedup_ratio",
            "snapshot_count",
            "storage_provider",
        )
        for key in required:
            assert key in body, f"Missing key in stats response: {key}"

    async def test_empty_catalog_returns_zeros(self, client: httpx.AsyncClient) -> None:
        body = (await client.get(f"/api/storage/{JOB_ID}/stats")).json()
        assert body["total_chunks"] == 0
        assert body["total_original_bytes"] == 0
        assert body["total_compressed_bytes"] == 0
        assert body["snapshot_count"] == 0

    async def test_dedup_ratio_type(self, client: httpx.AsyncClient) -> None:
        body = (await client.get(f"/api/storage/{JOB_ID}/stats")).json()
        assert isinstance(body["dedup_ratio"], float)

    async def test_job_id_matches(self, client: httpx.AsyncClient) -> None:
        body = (await client.get(f"/api/storage/{JOB_ID}/stats")).json()
        assert body["job_id"] == JOB_ID

    async def test_unknown_job_returns_404(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/storage/no-such-job/stats")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/storage/{job_id}/health
# ---------------------------------------------------------------------------


class TestStorageHealth:
    """Storage + catalog health-check endpoint."""

    async def test_known_job_returns_200(self, client: httpx.AsyncClient) -> None:
        r = await client.get(f"/api/storage/{JOB_ID}/health")
        assert r.status_code == 200

    async def test_response_schema(self, client: httpx.AsyncClient) -> None:
        body = (await client.get(f"/api/storage/{JOB_ID}/health")).json()
        assert "status" in body
        assert "storage_healthy" in body
        assert "catalog_accessible" in body

    async def test_status_is_valid_value(self, client: httpx.AsyncClient) -> None:
        body = (await client.get(f"/api/storage/{JOB_ID}/health")).json()
        assert body["status"] in ("ok", "degraded")

    async def test_bool_fields(self, client: httpx.AsyncClient) -> None:
        body = (await client.get(f"/api/storage/{JOB_ID}/health")).json()
        assert isinstance(body["storage_healthy"], bool)
        assert isinstance(body["catalog_accessible"], bool)

    async def test_catalog_accessible_after_stats(self, client: httpx.AsyncClient) -> None:
        """Fetching stats first initialises the catalog; health should report accessible."""
        await client.get(f"/api/storage/{JOB_ID}/stats")
        body = (await client.get(f"/api/storage/{JOB_ID}/health")).json()
        assert body["catalog_accessible"] is True

    async def test_unknown_job_returns_404(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/storage/no-such-job/health")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/jobs/{job_id}/run
# ---------------------------------------------------------------------------


class TestRunJob:
    """
    Backup trigger endpoint.

    These are *queuing* tests only — we confirm the job is accepted and
    queued (HTTP 202) but we do NOT wait for the background thread to
    finish.  Full integration tests (with a real backup run) live in the
    seed_demo.py script and can be run via ``make seed``.
    """

    async def test_returns_202(self, client: httpx.AsyncClient) -> None:
        r = await client.post(
            f"/api/jobs/{JOB_ID}/run",
            json={"passphrase": "smoke-test-passphrase"},
        )
        assert r.status_code == 202

    async def test_response_schema(self, client: httpx.AsyncClient) -> None:
        body = (
            await client.post(
                f"/api/jobs/{JOB_ID}/run",
                json={"passphrase": "smoke-test-passphrase"},
            )
        ).json()
        for key in ("run_id", "job_id", "status", "started_at"):
            assert key in body, f"Missing field in run response: {key}"

    async def test_status_queued_or_running(self, client: httpx.AsyncClient) -> None:
        body = (
            await client.post(
                f"/api/jobs/{JOB_ID}/run",
                json={"passphrase": "smoke-test-passphrase"},
            )
        ).json()
        assert body["status"] in ("queued", "running")

    async def test_job_id_in_response(self, client: httpx.AsyncClient) -> None:
        body = (
            await client.post(
                f"/api/jobs/{JOB_ID}/run",
                json={"passphrase": "smoke-test-passphrase"},
            )
        ).json()
        assert body["job_id"] == JOB_ID

    async def test_run_id_is_uuid_like(self, client: httpx.AsyncClient) -> None:
        """run_id must be a non-empty string (UUID format)."""
        body = (
            await client.post(
                f"/api/jobs/{JOB_ID}/run",
                json={"passphrase": "smoke-test-passphrase"},
            )
        ).json()
        assert len(body["run_id"]) == 36  # "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

    async def test_concurrent_run_returns_409(self, client: httpx.AsyncClient) -> None:
        """A second POST while the first is queued/running must return 409 Conflict.

        With httpx ASGITransport, BackgroundTasks complete synchronously inside
        await client.post(), so a sequential second request would always see
        status='success'.  We inject a 'running' RunState directly to test the
        409 guard without relying on wall-clock concurrency.
        """
        _deps.set_run_state(JOB_ID, _deps.RunState(run_id="fake-run", job_id=JOB_ID, status="running"))

        r2 = await client.post(
            f"/api/jobs/{JOB_ID}/run",
            json={"passphrase": "smoke-test-passphrase"},
        )
        assert r2.status_code == 409

    async def test_concurrent_409_has_detail(self, client: httpx.AsyncClient) -> None:
        """409 response must include a 'detail' field."""
        _deps.set_run_state(JOB_ID, _deps.RunState(run_id="fake-run", job_id=JOB_ID, status="queued"))

        r2 = await client.post(
            f"/api/jobs/{JOB_ID}/run",
            json={"passphrase": "smoke-test-passphrase"},
        )
        assert "detail" in r2.json()

    async def test_unknown_job_returns_404(self, client: httpx.AsyncClient) -> None:
        r = await client.post(
            "/api/jobs/no-such-job/run",
            json={"passphrase": "smoke-test-passphrase"},
        )
        assert r.status_code == 404

    async def test_missing_passphrase_returns_422(self, client: httpx.AsyncClient) -> None:
        """Passphrase is required; omitting it must trigger validation error."""
        r = await client.post(f"/api/jobs/{JOB_ID}/run", json={})
        assert r.status_code == 422

    async def test_run_then_latest_returns_200(self, client: httpx.AsyncClient) -> None:
        """After queuing a run, GET latest-run must succeed (200)."""
        await client.post(
            f"/api/jobs/{JOB_ID}/run",
            json={"passphrase": "smoke-test-passphrase"},
        )
        r = await client.get(f"/api/jobs/{JOB_ID}/runs/latest")
        assert r.status_code == 200

    async def test_run_then_latest_has_run_id(self, client: httpx.AsyncClient) -> None:
        post_body = (
            await client.post(
                f"/api/jobs/{JOB_ID}/run",
                json={"passphrase": "smoke-test-passphrase"},
            )
        ).json()
        get_body = (
            await client.get(f"/api/jobs/{JOB_ID}/runs/latest")
        ).json()
        assert get_body["run_id"] == post_body["run_id"]
