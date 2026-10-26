"""BAS-06 process-backed render worker. Never imports bpy or runs on its GUI thread.

This is an internal, opt-in substrate for previously saved immutable .blend
snapshots. MCP render tool exposure requires BAS-12 sandbox/snapshot and BAS-13
render contract acceptance; no external shell/argv is accepted from clients.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4


def _now():
    return datetime.now(timezone.utc).isoformat()


class RenderWorker:
    """Durable SQLite status + subprocess headless Blender render.

    A worker crash leaves jobs 'interrupted_unknown', not 'cancelled'.
    Never automatically retries a render with uncertain side effects.
    """

    def __init__(self, workspace: Path, blender_executable: Path):
        self.workspace = Path(workspace).resolve()
        self.blender_executable = Path(blender_executable).resolve(strict=True)
        if not self.blender_executable.is_file():
            raise ValueError("Blender worker binary missing")
        self.workspace.mkdir(parents=True, exist_ok=True)
        for directory in ("snapshots", "staging", "artifacts", "logs"):
            (self.workspace / directory).mkdir(exist_ok=True)
        self.db_path = self.workspace / "jobs.sqlite3"
        self._lock = threading.RLock()
        self._processes = {}
        self._threads = {}
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                request_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                snapshot TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                state TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, timeout_seconds INTEGER NOT NULL,
                output_path TEXT, output_sha256 TEXT, error TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                stop_confirmed INTEGER NOT NULL DEFAULT 0,
                UNIQUE(owner_id, idempotency_key)
            )""")
            # Running/queued jobs from previous crashed process cannot be
            # safely replayed until ownership/side-effects are reconciled.
            db.execute("""UPDATE jobs SET state='interrupted_unknown',
                error='worker_restart_requires_manual_reconcile',
                updated_at=? WHERE state IN ('running','queued')""", (_now(),))

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=5.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _snapshot(self, path: Path):
        path = Path(path)
        if not path.is_absolute() or path.suffix != ".blend":
            raise ValueError("Absolute .blend snapshot path required")
        root = self.workspace / "snapshots"
        if path.is_symlink() or path.resolve(strict=True).parent != root:
            raise ValueError("Snapshot must be direct regular file in workspace/snapshots")
        if not path.is_file():
            raise ValueError("Snapshot missing")
        return path

    def _get(self, db, job_id):
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row is not None else None

    def get(self, job_id: str, owner_id: str):
        with self._lock, self._connect() as db:
            row = self._get(db, job_id)
        if row is None or row["owner_id"] != owner_id:
            raise KeyError("Job not found")
        return row

    def submit(self, *, snapshot: Path, owner_id: str, request_id: str,
               idempotency_key: str, timeout_seconds: int = 120):
        if not owner_id or not request_id or not idempotency_key:
            raise ValueError("Owner, request id and idempotency key required")
        if not 1 <= timeout_seconds <= 3600:
            raise ValueError("Render timeout outside allowed range")
        snapshot = self._snapshot(snapshot)
        payload_sha = hashlib.sha256(
            json.dumps({"snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                        "timeout_seconds": timeout_seconds}, sort_keys=True).encode()
        ).hexdigest()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE owner_id=? AND idempotency_key=?",
                             (owner_id, idempotency_key)).fetchone()
            if row is not None:
                if row["payload_sha256"] != payload_sha:
                    raise ValueError("idempotency_conflict")
                return dict(row)
            job_id = str(uuid4())
            now = _now()
            db.execute("""INSERT INTO jobs (
                job_id,owner_id,request_id,idempotency_key,snapshot,
                payload_sha256,state,created_at,updated_at,timeout_seconds)
                VALUES (?,?,?,?,?,?,'queued',?,?,?)""",
                (job_id, owner_id, request_id, idempotency_key,
                 str(snapshot), payload_sha, now, now, timeout_seconds))
        thread = threading.Thread(target=self._run, args=(job_id,), daemon=True)
        with self._lock:
            self._threads[job_id] = thread
        thread.start()
        return self.get(job_id, owner_id)

    def _run(self, job_id: str):
        proc = None
        stagedir = self.workspace / "staging" / job_id
        try:
            with self._lock, self._connect() as db:
                row = self._get(db, job_id)
                if row["cancel_requested"]:
                    db.execute("UPDATE jobs SET state='cancelled', stop_confirmed=1, updated_at=? WHERE job_id=?",
                               (_now(), job_id))
                    return
                db.execute("UPDATE jobs SET state='running', updated_at=? WHERE job_id=?",
                           (_now(), job_id))
            stagedir.mkdir(exist_ok=False)
            frame_prefix = stagedir / "frame_"
            logpath = self.workspace / "logs" / (job_id + ".log")
            # Fixed command template, no user-supplied argv, no shell=True.
            cmd = [str(self.blender_executable), "--disable-autoexec",
                   "-b", row["snapshot"], "-o", str(frame_prefix),
                   "-F", "PNG", "-f", "1"]
            with logpath.open("wb") as logfile:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                        stdout=logfile, stderr=subprocess.STDOUT,
                                        close_fds=True, start_new_session=True)
                with self._lock:
                    self._processes[job_id] = proc
                    # A cancel may arrive between state='running' and Popen.
                    # Re-check under the same lock and stop the newly started
                    # process before awaiting its result.
                    with self._connect() as db:
                        cancelled = bool(self._get(db, job_id)["cancel_requested"])
                    if cancelled and proc.poll() is None:
                        proc.terminate()
                try:
                    exitcode = proc.wait(timeout=row["timeout_seconds"])
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                    raise TimeoutError("render_worker_timeout_after_process_exit")
            with self._lock, self._connect() as db:
                latest = self._get(db, job_id)
                if latest["cancel_requested"]:
                    db.execute("UPDATE jobs SET state='cancelled', stop_confirmed=1, updated_at=? WHERE job_id=?",
                               (_now(), job_id))
                    return
            if exitcode != 0:
                raise RuntimeError("render_process_failed")
            frame = stagedir / "frame_0001.png"
            if not frame.is_file() or not frame.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError("invalid_render_png")
            sha = hashlib.sha256(frame.read_bytes()).hexdigest()
            output = self.workspace / "artifacts" / (job_id + ".png")
            os.replace(frame, output)
            with self._lock, self._connect() as db:
                db.execute("""UPDATE jobs SET state='succeeded', output_path=?,
                    output_sha256=?, updated_at=? WHERE job_id=?""",
                    (str(output), sha, _now(), job_id))
        except Exception as exc:
            with self._lock, self._connect() as db:
                row = self._get(db, job_id)
                # Failure must not be mislabeled as confirmed cancellation.
                db.execute("UPDATE jobs SET state='failed', error=?, updated_at=? WHERE job_id=?",
                           (type(exc).__name__ + ": " + str(exc)[:160], _now(), job_id))
        finally:
            with self._lock:
                self._processes.pop(job_id, None)
                self._threads.pop(job_id, None)
            shutil.rmtree(stagedir, ignore_errors=True)

    def cancel(self, job_id: str, owner_id: str):
        self.get(job_id, owner_id)
        with self._lock, self._connect() as db:
            row = self._get(db, job_id)
            if row["state"] in ("succeeded", "failed", "cancelled", "interrupted_unknown"):
                return row
            db.execute("UPDATE jobs SET cancel_requested=1, updated_at=? WHERE job_id=?",
                       (_now(), job_id))
            proc = self._processes.get(job_id)
            if proc is not None and proc.poll() is None:
                proc.terminate()
        return self.get(job_id, owner_id)
