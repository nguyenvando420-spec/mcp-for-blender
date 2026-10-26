"""BAS-06 render process/SQLite recovery contract (no bpy, no GPU)."""
from __future__ import annotations

import sys
import time
from pathlib import Path
import pytest

from blender_mcp.studio.process_worker import RenderWorker


@pytest.fixture
def fake_blender(tmp_path):
    script = tmp_path / "fake-blender"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import pathlib, sys, time\n"
        "args = sys.argv\n"
        "if pathlib.Path(args[args.index('-b')+1]).read_bytes() == b'SLOW':\n"
        "    time.sleep(20)\n"
        "prefix = pathlib.Path(args[args.index('-o')+1])\n"
        "pathlib.Path(str(prefix)+'0001.png').write_bytes("
        "b'\\x89PNG\\r\\n\\x1a\\nFAKEPNG')\n"
    )
    script.chmod(0o700)
    return script


def _wait(worker, item, deadline=4):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        result = worker.get(item["job_id"], item["owner_id"])
        if result["state"] in ("succeeded", "failed", "cancelled", "interrupted_unknown"):
            return result
        time.sleep(0.02)
    pytest.fail("worker did not finish")


def test_render_process_success_persist_and_replay(tmp_path, fake_blender):
    base = tmp_path / "workspace"
    worker = RenderWorker(base, fake_blender)
    snap = base / "snapshots" / "scene.blend"
    snap.write_bytes(b"FAKE")
    job = worker.submit(snapshot=snap, owner_id="studio", request_id="rq-1",
                        idempotency_key="key-1")
    assert job["job_id"]
    result = _wait(worker, job)
    assert result["state"] == "succeeded", result
    assert Path(result["output_path"]).is_file()
    assert len(result["output_sha256"]) == 64
    same = worker.submit(snapshot=snap, owner_id="studio", request_id="rq-2",
                         idempotency_key="key-1")
    assert same["job_id"] == job["job_id"]
    reopened = RenderWorker(base, fake_blender)
    assert reopened.get(job["job_id"], "studio")["state"] == "succeeded"
    with pytest.raises(KeyError):
        reopened.get(job["job_id"], "other-user")
    snap.write_bytes(b"CHANGED")
    with pytest.raises(ValueError, match="idempotency_conflict"):
        reopened.submit(snapshot=snap, owner_id="studio", request_id="rq-3",
                        idempotency_key="key-1")


def test_crash_recovery_is_unknown_not_auto_replayed(tmp_path, fake_blender):
    root = tmp_path / "jobs"
    worker = RenderWorker(root, fake_blender)
    with worker._connect() as db:
        db.execute("""INSERT INTO jobs (job_id,owner_id,request_id,idempotency_key,
            snapshot,payload_sha256,state,created_at,updated_at,timeout_seconds)
            VALUES ('r','o','x','k','s','hash','running','now','now',120)""")
    restart = RenderWorker(root, fake_blender)
    job = restart.get("r", "o")
    assert job["state"] == "interrupted_unknown"
    assert job["error"] == "worker_restart_requires_manual_reconcile"
    assert restart.cancel("r", "o")["state"] == "interrupted_unknown"


def test_cancel_waits_for_confirmed_process_exit(tmp_path, fake_blender):
    root = tmp_path / "jobs"
    worker = RenderWorker(root, fake_blender)
    snap = root / "snapshots" / "scene.blend"
    snap.write_bytes(b"SLOW")
    job = worker.submit(snapshot=snap, owner_id="o", request_id="r",
                        idempotency_key="key", timeout_seconds=6)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and worker.get(job["job_id"], "o")["state"] != "running":
        time.sleep(0.01)
    pending = worker.cancel(job["job_id"], "o")
    assert pending["cancel_requested"] == 1
    result = _wait(worker, job)
    assert result["state"] in ("cancelled", "failed"), result
    if result["state"] == "cancelled":
        assert result["stop_confirmed"] == 1


def test_worker_rejects_untrusted_snapshot_paths(tmp_path, fake_blender):
    w = RenderWorker(tmp_path / "sandbox", fake_blender)
    external = tmp_path / "outside.blend"
    external.write_bytes(b"NO")
    with pytest.raises(ValueError):
        w.submit(snapshot=external, owner_id="o", request_id="r", idempotency_key="k")
    symlink = w.workspace / "snapshots" / "link.blend"
    symlink.symlink_to(external)
    with pytest.raises(ValueError):
        w.submit(snapshot=symlink, owner_id="o", request_id="r", idempotency_key="k")
