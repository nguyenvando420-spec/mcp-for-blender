"""BAS-06 real process worker → Blender 4.5.15 CPU render → SQLite recovery.

Uses the .blend produced by the real GUI E2E; this invokes a separate
Blender headless subprocess, never bpy on the GUI timer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import time

from blender_mcp.studio.process_worker import RenderWorker

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    spec = json.loads((ROOT / "blender-runtime.json").read_text())
    assert platform.system() == "Linux" and platform.machine() == "x86_64"
    blender = ROOT / ".blender-runtime" / "runtime" / spec["artifacts"]["Linux-x86_64"]["executable"]
    assert blender.is_file()
    assert args.scene.is_file()
    workspace = args.output_dir.resolve()
    worker = RenderWorker(workspace, blender)
    snapshot = workspace / "snapshots" / "bas06-scene.blend"
    shutil.copyfile(args.scene, snapshot)
    job = worker.submit(
        snapshot=snapshot, owner_id="bas06-ci", request_id="bas06-worker-e2e",
        idempotency_key="bas06-render-fixed-snapshot", timeout_seconds=180,
    )
    job_id = job["job_id"]
    started = time.monotonic()
    while time.monotonic() - started < 205:
        item = worker.get(job_id, "bas06-ci")
        if item["state"] in ("succeeded", "failed", "cancelled", "interrupted_unknown"):
            break
        time.sleep(0.2)
    else:
        raise TimeoutError("Render worker did not finish; inspect worker log")
    assert item["state"] == "succeeded", item
    output = Path(item["output_path"])
    assert output.is_file() and output.stat().st_size > 100
    assert hashlib.sha256(output.read_bytes()).hexdigest() == item["output_sha256"]

    # Fresh worker instance reads terminal metadata; replay does not rerender.
    restarted = RenderWorker(workspace, blender)
    restored = restarted.get(job_id, "bas06-ci")
    assert restored["state"] == "succeeded"
    repeated = restarted.submit(
        snapshot=snapshot, owner_id="bas06-ci", request_id="another-request",
        idempotency_key="bas06-render-fixed-snapshot", timeout_seconds=180,
    )
    assert repeated["job_id"] == job_id
    evidence = {
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"],
                                                 text=True).strip(),
        "blender_version": spec["blender_version"],
        "worker": "headless separate Blender OS process",
        "job_id": job_id,
        "state": restored["state"],
        "output_path": str(output),
        "output_sha256": restored["output_sha256"],
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "replay_job_id_unchanged": True,
        "persisted_after_restart": True,
    }
    (workspace / "bas06-worker-e2e.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print("BAS06_WORKER_PROCESS_E2E_OK", json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
