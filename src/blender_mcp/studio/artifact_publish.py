"""BAS-13 Linux-only atomic replacement of complete artifact directories.

A trusted UI must issue a resource-bound HMAC confirmation for
output_uri + "|" + sha256(existing manifest.json). A private verified backup
is completed before the atomic directory exchange. Uncertain crash states
are retained for manual reconciliation; we never silently claim rollback.
"""
from __future__ import annotations

import ctypes
import fcntl
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat

from .contracts import ArtifactManifest

RENAME_EXCHANGE = 2
MAX_FILE = 512 * 1024 * 1024


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verified_delivery(path: Path) -> str:
    """A complete self-contained BAS-13 output and its manifest digest."""
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("conflict: previous artifact is not a plain directory")
    if path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("conflict: artifact directory is not private")
    manifest_path = path / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("conflict: existing manifest missing")
    data = manifest_path.read_bytes()
    if len(data) > 1024 * 1024:
        raise ValueError("conflict: existing manifest exceeds size limit")
    manifest = ArtifactManifest.model_validate_json(data)
    names = {"manifest.json"}
    for artifact in manifest.artifacts:
        filename = Path(artifact.uri).name
        if (filename not in ("still.png", "scene.glb", "scene.blend",
                             "render-details.json") or filename in names
                or (path / filename).as_uri() != artifact.uri):
            raise ValueError("conflict: previous artifact URI or kind mismatch")
        file = path / filename
        if (file.is_symlink() or not file.is_file()
                or not 0 < file.stat().st_size <= MAX_FILE
                or file.stat().st_size != artifact.bytes
                or _sha(file) != artifact.sha256):
            raise ValueError("conflict: existing artifact file changed")
        names.add(filename)
    if {p.name for p in path.iterdir()} != names:
        raise ValueError("conflict: unexpected existing artifact files")
    return hashlib.sha256(data).hexdigest()


def _sync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def backup_delivery(destination: Path, backup: Path, expected_sha256: str):
    actual = verified_delivery(destination)
    if actual != expected_sha256:
        raise ValueError("conflict: expected destination checksum mismatch")
    if backup.exists() or backup.is_symlink():
        raise ValueError("conflict: backup already exists")
    backup.mkdir(mode=0o700)
    try:
        for file in destination.iterdir():
            if file.is_symlink() or not file.is_file():
                raise ValueError("conflict: invalid backup input")
            with file.open("rb") as src, (backup / file.name).open("xb") as target:
                shutil.copyfileobj(src, target, 1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
        _sync_dir(backup)
        if (set(p.name for p in backup.iterdir()) !=
                set(p.name for p in destination.iterdir())
                or any(_sha(p) != _sha(destination / p.name)
                       for p in backup.iterdir())):
            raise ValueError("conflict: backup checksum verification failed")
    except Exception:
        shutil.rmtree(backup)
        raise


def atomic_exchange(new_directory: Path, existing_directory: Path):
    """Linux renameat2(RENAME_EXCHANGE); never emulate with two renames."""
    if (new_directory.is_symlink() or existing_directory.is_symlink()
            or not new_directory.is_dir() or not existing_directory.is_dir()):
        raise ValueError("conflict: exchange requires real directories")
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "renameat2"):
        raise OSError("renameat2(RENAME_EXCHANGE) unavailable")
    libc.renameat2.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
        ctypes.c_uint,
    ]
    libc.renameat2.restype = ctypes.c_int
    left = os.open(new_directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    right = os.open(existing_directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        rc = libc.renameat2(left, os.fsencode(new_directory.name),
                            right, os.fsencode(existing_directory.name),
                            RENAME_EXCHANGE)
        if rc != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        os.fsync(left)
        os.fsync(right)
    finally:
        os.close(left)
        os.close(right)


@contextmanager
def publish_lock(workspace: Path):
    """Cross-process single writer lock for this private artifact workspace."""
    lockfile = Path(workspace) / "publish.lock"
    fd = os.open(lockfile, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
