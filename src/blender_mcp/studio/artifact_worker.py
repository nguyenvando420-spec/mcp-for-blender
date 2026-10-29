"""BAS-13 durable, isolated Blender artifact jobs.

The untrusted MCP request never supplies executable code or subprocess flags.
A private verified BLEND snapshot is rendered/exported in a separate process.
Only a complete, validated staging directory is published by atomic rename.
Existing destinations are never overwritten (explicit fail-closed policy).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import threading
from datetime import datetime, timezone
from uuid import UUID, uuid4

from .contracts import ArtifactManifest

MAX_BLEND = 512 * 1024 * 1024
MAX_FILE = 512 * 1024 * 1024
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted_unknown"})


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as fp:
        for block in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_render(path, width, height):
    if path.stat().st_size < 100 or path.stat().st_size > MAX_FILE:
        raise ValueError("invalid_render_png_size")
    with path.open("rb") as f:
        head = f.read(24)
    if head[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
        raise ValueError("invalid_render_png_header")
    if struct.unpack(">II", head[16:24]) != (width, height):
        raise ValueError("invalid_render_png_dimensions")
    # Verify the image is actually decodable, not merely a forged PNG header.
    import zlib
    payload = path.read_bytes()
    position = 8
    chunks = []
    while position + 12 <= len(payload):
        size = struct.unpack(">I", payload[position:position + 4])[0]
        kind = payload[position + 4:position + 8]
        if position + 12 + size > len(payload):
            raise ValueError("truncated_png_chunk")
        body = payload[position + 8:position + 8 + size]
        expected = struct.unpack(">I", payload[position + 8 + size:position + 12 + size])[0]
        if zlib.crc32(kind + body) & 0xffffffff != expected:
            raise ValueError("png_crc_mismatch")
        if kind == b"IDAT":
            chunks.append(body)
        position += size + 12
        if kind == b"IEND":
            break
    if not chunks or position != len(payload):
        raise ValueError("png_missing_data")
    decoder = zlib.decompressobj()
    pixels = decoder.decompress(b"".join(chunks), MAX_FILE)
    if not decoder.eof or decoder.unconsumed_tail or not pixels:
        raise ValueError("png_decode_failed")
    if len(pixels) < width * height or len(set(pixels[:min(len(pixels), 1000000)])) < 2:
        raise ValueError("png_invalid_or_blank_pixel_data")


class ArtifactWorker:
    def __init__(self, root: Path, blender: Path, script: Path, source_commit: str):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir() or self.root.is_symlink():
            raise ValueError("invalid_artifact_root")
        self.blender = Path(blender).resolve(strict=True)
        self.script = Path(script).resolve(strict=True)
        if not self.blender.is_file() or not self.script.is_file():
            raise ValueError("worker_executable_unavailable")
        if len(source_commit) not in (40, 64) or any(c not in "0123456789abcdef" for c in source_commit):
            raise ValueError("source_commit_required")
        self.source_commit = source_commit
        self.work = self.root / ".bas13-worker"
        for name in (".", "stage", "logs"):
            directory = self.work if name == "." else self.work / name
            directory.mkdir(mode=0o700, exist_ok=True)
        self.lock = threading.RLock()
        self.processes = {}
        self.db = self.work / "jobs.sqlite3"
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS jobs(
                job_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
                request_id TEXT NOT NULL, scene_id TEXT NOT NULL,
                scene_revision INTEGER NOT NULL, spec TEXT NOT NULL,
                snapshot TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
                output TEXT NOT NULL, timeout_seconds INTEGER NOT NULL,
                state TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                stop_confirmed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                error TEXT, manifest TEXT,
                UNIQUE(owner_id,idempotency_key)
            )""")
            connection.execute("""UPDATE jobs SET state='interrupted_unknown',
                error='worker_restart_requires_manual_reconcile',updated_at=?
                WHERE state IN ('queued','running')""", (_now(),))

    from contextlib import contextmanager

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _get(self, conn, job_id):
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def get(self, job_id, owner_id):
        with self.lock, self._connect() as db:
            row = self._get(db, str(UUID(job_id)))
        if row is None or row["owner_id"] != owner_id:
            raise KeyError("job_not_found")
        return {k: v for k, v in row.items() if k not in ("snapshot", "spec", "payload_hash")}

    def submit(self, *, owner_id, request_id, idempotency_key, scene_id,
               scene_revision, snapshot, output, spec, timeout_seconds):
        if not (owner_id and idempotency_key) or not 1 <= timeout_seconds <= 3600:
            raise ValueError("invalid_job_parameters")
        output = Path(output)
        if (not output.is_absolute() or output == self.root or self.root not in output.parents
                or output.is_symlink() or not output.parent.is_dir()):
            raise ValueError("destination_must_be_new_sandbox_directory")
        snapshot = Path(snapshot).resolve(strict=True)
        if not snapshot.is_file() or not 8 <= snapshot.stat().st_size <= MAX_BLEND:
            raise ValueError("invalid_verified_snapshot")
        with snapshot.open("rb") as fp:
            if fp.read(7) != b"BLENDER":
                raise ValueError("invalid_verified_snapshot")
        expected = _sha(snapshot)
        payload = {"scene": scene_id, "revision": scene_revision,
                   "snapshot_sha": expected, "output": str(output),
                   "spec": spec, "timeout": timeout_seconds}
        digest = hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE owner_id=? AND idempotency_key=?",
                             (owner_id, idempotency_key)).fetchone()
            if row:
                if row["payload_hash"] != digest:
                    raise ValueError("idempotency_conflict")
                return dict(row)
            if output.exists() or output.is_symlink():
                raise ValueError("destination_must_be_new_sandbox_directory")
            job_id = str(uuid4())
            now = _now()
            db.execute("""INSERT INTO jobs(job_id,owner_id,idempotency_key,payload_hash,
                request_id,scene_id,scene_revision,spec,snapshot,snapshot_sha256,
                output,timeout_seconds,state,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                (job_id, owner_id, idempotency_key, digest, request_id, scene_id,
                 scene_revision, json.dumps(spec), str(snapshot), expected,
                 str(output), timeout_seconds, now, now))
        threading.Thread(target=self._run, args=(job_id,), daemon=True).start()
        return self.get(job_id, owner_id)

    def _run(self, job_id):
        process = None
        stage = self.work / "stage" / job_id
        log = self.work / "logs" / (job_id + ".log")
        try:
            with self.lock, self._connect() as db:
                row = self._get(db, job_id)
                if row["cancel_requested"]:
                    db.execute("UPDATE jobs SET state='cancelled',stop_confirmed=1,updated_at=? WHERE job_id=?",
                               (_now(), job_id))
                    return
                db.execute("UPDATE jobs SET state='running',progress=0.1,updated_at=? WHERE job_id=?",
                           (_now(), job_id))
            stage.mkdir(mode=0o700)
            source = Path(row["snapshot"])
            if _sha(source) != row["snapshot_sha256"]:
                raise ValueError("snapshot_changed_before_execution")
            snap_copy = stage / "source.blend"
            shutil.copyfile(source, snap_copy)
            if _sha(snap_copy) != row["snapshot_sha256"]:
                raise ValueError("snapshot_copy_checksum_mismatch")
            outputs = stage / "outputs"
            outputs.mkdir(mode=0o700)
            task = json.loads(row["spec"])
            config = stage / "config.json"
            config.write_text(json.dumps(task, sort_keys=True), encoding="utf-8")
            command = [str(self.blender), "--background", "--factory-startup",
                       "--disable-autoexec", "--python-exit-code", "1",
                       "--python", str(self.script), "--",
                       str(snap_copy), str(outputs), str(config)]
            with log.open("wb") as f:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                           stdout=f, stderr=subprocess.STDOUT,
                                           close_fds=True, start_new_session=True)
                with self.lock:
                    self.processes[job_id] = process
                    with self._connect() as db:
                        cancel_now = self._get(db, job_id)["cancel_requested"]
                    if cancel_now and process.poll() is None:
                        process.terminate()
                try:
                    code = process.wait(timeout=row["timeout_seconds"])
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    raise TimeoutError("artifact_worker_timeout")
            with self.lock, self._connect() as db:
                cancelled = self._get(db, job_id)["cancel_requested"]
                if cancelled:
                    db.execute("UPDATE jobs SET state='cancelled',stop_confirmed=1,updated_at=? WHERE job_id=?",
                               (_now(), job_id))
                    return
            if code:
                raise RuntimeError("blender_subprocess_failed")
            metadata = json.loads((outputs / "run.json").read_text(encoding="utf-8"))
            if metadata["kind"] != task["kind"]:
                raise ValueError("worker_kind_mismatch")
            details_path = outputs / "render-details.json"
            details_path.write_text(json.dumps({
                "camera": metadata.get("camera"),
                "engine": metadata.get("engine"),
                "samples": metadata.get("samples"),
                "resolution": metadata.get("resolution"),
                "blender_version": metadata["blender_version"],
                "kind": metadata["kind"],
                "export_formats": metadata.get("formats"),
                "export_object_ids": metadata.get("objects_requested"),
                "snapshot_sha256": row["snapshot_sha256"],
                "scene_revision": row["scene_revision"],
            }, sort_keys=True), encoding="utf-8")
            blobs = []
            for filename in list(metadata["files"]) + ["render-details.json"]:
                if filename not in ("still.png", "scene.glb", "scene.blend", "render-details.json"):
                    raise ValueError("unexpected_artifact")
                path = outputs / filename
                if (path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE):
                    raise ValueError("invalid_artifact")
                if filename.endswith(".png"):
                    _validate_render(path, task["width"], task["height"])
                elif filename.endswith(".glb"):
                    with path.open("rb") as f:
                        header = f.read(12)
                    if header[:4] != b"glTF" or struct.unpack("<I",header[8:])[0] != path.stat().st_size:
                        raise ValueError("invalid_glb")
                elif filename.endswith(".blend"):
                    with path.open("rb") as file:
                        if file.read(7) != b"BLENDER":
                            raise ValueError("invalid_blend")
                blobs.append((filename, path.stat().st_size, _sha(path)))
            if not blobs:
                raise ValueError("empty_artifact_job")
            artifact_list = []
            for filename, nbytes, digest in blobs:
                artifact_list.append({
                    "artifact_id": str(uuid4()), "job_id": job_id,
                    "kind": filename.split(".")[-1] if filename != "render-details.json" else "other",
                    "uri": (Path(row["output"]) / filename).as_uri(),
                    "bytes": nbytes, "sha256": digest, "version": 1,
                    "provenance": {
                        "source_commit": self.source_commit,
                        "scene_id": row["scene_id"],
                        "scene_revision": row["scene_revision"],
                        "blender_version": metadata["blender_version"],
                        "worker_version": "bas13.1",
                        "input_sha256": [row["snapshot_sha256"]],
                    },
                })
            manifest = ArtifactManifest.model_validate({
                "job_id": job_id, "created_at": _now(), "artifacts": artifact_list})
            (outputs / "manifest.json").write_text(
                manifest.model_dump_json(indent=2), encoding="utf-8")
            (outputs / "run.json").unlink()
            destination = Path(row["output"])
            # No overwrite and no partially published artifact directory.
            with self.lock:
                with self._connect() as db:
                    if self._get(db, job_id)["cancel_requested"]:
                        db.execute("UPDATE jobs SET state='cancelled',stop_confirmed=1,updated_at=? WHERE job_id=?",
                                   (_now(), job_id))
                        return
                if destination.exists() or destination.is_symlink():
                    raise ValueError("destination_race")
                os.mkdir(destination, 0o700)
                try:
                    os.replace(outputs, destination)
                except Exception:
                    destination.rmdir()
                    raise
                with self._connect() as db:
                    db.execute("""UPDATE jobs SET state='succeeded',progress=1,
                        manifest=?,updated_at=? WHERE job_id=?""",
                        (str(destination / "manifest.json"), _now(), job_id))
        except Exception as exc:
            with self.lock, self._connect() as db:
                db.execute("UPDATE jobs SET state='failed',error=?,updated_at=? WHERE job_id=?",
                           (type(exc).__name__ + ":" + str(exc)[:140],_now(),job_id))
        finally:
            with self.lock:
                self.processes.pop(job_id, None)
            shutil.rmtree(stage, ignore_errors=True)

    def cancel(self, job_id, owner_id):
        self.get(job_id, owner_id)
        with self.lock, self._connect() as db:
            row = self._get(db, job_id)
            if row["state"] in TERMINAL:
                return self.get(job_id, owner_id)
            db.execute("UPDATE jobs SET cancel_requested=1,updated_at=? WHERE job_id=?",
                       (_now(),job_id))
            proc = self.processes.get(job_id)
            if proc and proc.poll() is None:
                proc.terminate()
        return self.get(job_id, owner_id)

# One worker per process/workspace; never reinitialize SQLite per MCP request
# because doing so would mislabel a live process as interrupted_unknown.
_WORKERS = {}
_WORKERS_LOCK = threading.Lock()


def get_artifact_worker(root):
    root = Path(root).resolve(strict=True)
    binary = os.getenv("BLENDER_MCP_RENDER_BIN")
    if not binary:
        raise ValueError("capability_unavailable: configure BLENDER_MCP_RENDER_BIN")
    repo = Path(__file__).resolve().parents[3]
    script = repo / "scripts" / "bas13_blender_job.py"
    source = os.getenv("BLENDER_MCP_SOURCE_COMMIT")
    if not source:
        try:
            source = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                stderr=subprocess.DEVNULL, timeout=3, text=True).strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ValueError("capability_unavailable: source commit unavailable") from exc
    key = (str(root), str(Path(binary).resolve()), source)
    with _WORKERS_LOCK:
        if key not in _WORKERS:
            _WORKERS[key] = ArtifactWorker(root, Path(binary), script, source)
        return _WORKERS[key]
