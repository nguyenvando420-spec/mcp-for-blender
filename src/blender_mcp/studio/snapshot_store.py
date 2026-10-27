"""BAS-12 snapshot storage core.

A snapshot is immutable, written into a private UUID directory beneath an
explicit writable sandbox root. Blender must save COPY=True into the allocated
path, then finalize its manifest; callers may only restore verified snapshots.
This module does not execute bpy or issue destructive confirmation tokens.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5


class SnapshotError(ValueError):
    pass


def _uuid(value: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SnapshotError("Invalid snapshot or scene UUID") from exc


def _root(path: str | os.PathLike[str]) -> Path:
    raw = Path(path).expanduser()
    if not raw.is_absolute() or ".." in raw.parts:
        raise SnapshotError("An absolute sandbox root without traversal is required")
    if raw.is_symlink() or not raw.is_dir() or raw.resolve(strict=True) != raw:
        raise SnapshotError("Sandbox root is not a canonical directory")
    mode = raw.stat().st_mode
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SnapshotError("Sandbox root must not be group/world writable")
    return raw


def _dir_open(path: Path) -> int:
    """No-follow root access. All children are accessed relative to this fd."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise SnapshotError("Not a directory")
    return fd


def _child_dir(parent_fd: int, name: str, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, dir_fd=parent_fd)
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or (st.st_mode & 0o077):
        os.close(fd)
        raise SnapshotError("Snapshot directory is not private")
    return fd


def _read(fd: int, filename: str, max_bytes: int) -> bytes:
    file_fd = os.open(filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                      dir_fd=fd)
    try:
        st = os.fstat(file_fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            raise SnapshotError("Snapshot file has invalid type/size")
        with os.fdopen(os.dup(file_fd), "rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise SnapshotError("Snapshot file exceeds limit")
        return data
    finally:
        os.close(file_fd)


def _write_exclusive(fd: int, filename: str, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    out = os.open(filename, flags, 0o600, dir_fd=fd)
    try:
        with os.fdopen(os.dup(out), "wb") as stream:
            stream.write(content)
            stream.flush()
        os.fsync(out)
    finally:
        os.close(out)


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    path: Path
    sha256: str
    scene_id: str
    scene_revision: int
    idempotency_key: str


class SnapshotStore:
    """Linux/POSIX dirfd-backed immutable snapshot manifest and checksum.

    A Blender backend must hold a single main-thread write lock across
    begin -> bpy.save_as_mainfile(copy=True) -> finalize, and report failures.
    The UUID folder is intentionally private; symlink and path traversal are
    never accepted through a caller-supplied snapshot identifier.
    """

    def __init__(self, root: str | os.PathLike[str], *, max_bytes: int = 2 << 30):
        self.root = _root(root)
        if type(max_bytes) is not int or max_bytes < 1024:
            raise SnapshotError("Invalid size limit")
        self.max_bytes = max_bytes

    def _folder(self, snapshot_id: str, create: bool = False):
        rid = _uuid(snapshot_id)
        root_fd = _dir_open(self.root)
        try:
            snaps_fd = _child_dir(root_fd, ".bas12-snapshots", create=create)
            try:
                snap_fd = _child_dir(snaps_fd, rid, create=create)
                return snap_fd
            finally:
                os.close(snaps_fd)
        finally:
            os.close(root_fd)

    def begin(self) -> tuple[str, Path]:
        rid = str(uuid4())
        fd = self._folder(rid, create=True)
        os.close(fd)
        # Only Blender should write this pathname, and only in our private
        # directory. No user-controlled basename reaches bpy.
        return rid, self.root / ".bas12-snapshots" / rid / "scene.blend"

    def begin_keyed(self, *, scene_id: str, scene_revision: int,
                    idempotency_key: str, label: str = "") -> tuple[str, Path, bool]:
        """Stable request reservation; do not repeat uncertain partial writes."""
        sid = _uuid(scene_id)
        if type(scene_revision) is not int or scene_revision < 0:
            raise SnapshotError("Invalid scene revision")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256:
            raise SnapshotError("Invalid idempotency key")
        rid = str(uuid5(NAMESPACE_URL, json.dumps(
            [str(self.root), sid, idempotency_key], separators=(",", ":"))))
        request_digest = hashlib.sha256(json.dumps(
            [sid, scene_revision, idempotency_key, label],
            separators=(",", ":")).encode()).hexdigest()
        fd = self._folder(rid, create=True)
        try:
            names = os.listdir(fd)
        finally:
            os.close(fd)
        if "manifest.json" in names:
            existing = self.verify(rid)
            manifest = json.loads((existing.path.parent / "manifest.json").read_text())
            if (existing.scene_id != sid or existing.scene_revision != scene_revision
                    or existing.idempotency_key != idempotency_key
                    or manifest.get("request_digest") != request_digest):
                raise SnapshotError("Snapshot reservation conflict")
            return rid, existing.path, True
        if "scene.blend" in names:
            raise SnapshotError("Snapshot may have been written; state unknown")
        return rid, self.root / ".bas12-snapshots" / rid / "scene.blend", False

    def finalize(self, snapshot_id: str, *, scene_id: str, scene_revision: int,
                 idempotency_key: str, label: str = "",
                 scene_inventory: list | None = None) -> Snapshot:
        rid = _uuid(snapshot_id)
        sid = _uuid(scene_id)
        if type(scene_revision) is not int or scene_revision < 0:
            raise SnapshotError("Invalid scene revision")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256:
            raise SnapshotError("Invalid idempotency key")
        objects = scene_inventory or []
        if not isinstance(objects, list) or len(objects) > 4000:
            raise SnapshotError("Invalid snapshot scene inventory")
        for record in objects:
            if (not isinstance(record, dict) or set(record) != {"object_id", "name"}
                    or not isinstance(record["name"], str)
                    or not 1 <= len(record["name"]) <= 256):
                raise SnapshotError("Invalid snapshot object inventory")
            _uuid(record["object_id"])
        fd = self._folder(rid)
        try:
            digest = hashlib.sha256()
            file_fd = os.open("scene.blend", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                              dir_fd=fd)
            try:
                st = os.fstat(file_fd)
                if not stat.S_ISREG(st.st_mode) or st.st_size < 16 or st.st_size > self.max_bytes:
                    raise SnapshotError("Missing/invalid Blender scene")
                while True:
                    chunk = os.read(file_fd, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                # BLEND header changes across versions; only verify standard
                # stable signature. Blender reopen will perform deeper checks.
                os.lseek(file_fd, 0, os.SEEK_SET)
                if os.read(file_fd, 7) != b"BLENDER":
                    raise SnapshotError("Snapshot does not contain a BLEND file")
                os.fsync(file_fd)
            finally:
                os.close(file_fd)
            payload = {
                "version": 1, "snapshot_id": rid, "scene_id": sid,
                "scene_revision": scene_revision, "idempotency_key": idempotency_key,
                "file": "scene.blend", "sha256": digest.hexdigest(),
                "request_digest": hashlib.sha256(json.dumps(
                    [sid, scene_revision, idempotency_key, label],
                    separators=(",", ":")).encode()).hexdigest(),
                "bytes": st.st_size, "scene_inventory": objects,
            }
            manifest = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
            _write_exclusive(fd, "manifest.json", manifest)
            os.fsync(fd)
            return Snapshot(rid, self.root / ".bas12-snapshots" / rid / "scene.blend",
                            digest.hexdigest(), sid, scene_revision, idempotency_key)
        finally:
            os.close(fd)

    def verify(self, snapshot_id: str) -> Snapshot:
        rid = _uuid(snapshot_id)
        fd = self._folder(rid)
        try:
            content = _read(fd, "manifest.json", 1024 * 1024)
            try:
                manifest = json.loads(content)
                if (manifest["version"] != 1 or manifest["snapshot_id"] != rid
                        or manifest["file"] != "scene.blend"
                        or type(manifest["scene_revision"]) is not int
                        or manifest["scene_revision"] < 0):
                    raise SnapshotError("Corrupt snapshot manifest")
                sid = _uuid(manifest["scene_id"])
                sha = manifest["sha256"]
                if not isinstance(sha, str) or len(sha) != 64:
                    raise SnapshotError("Invalid checksum")
            except (ValueError, KeyError, TypeError) as exc:
                raise SnapshotError("Corrupt snapshot manifest") from exc
            file_fd = os.open("scene.blend", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                              dir_fd=fd)
            try:
                st = os.fstat(file_fd)
                if not stat.S_ISREG(st.st_mode) or st.st_size != manifest["bytes"] or st.st_size > self.max_bytes:
                    raise SnapshotError("Snapshot size mismatch")
                digest = hashlib.sha256()
                while True:
                    chunk = os.read(file_fd, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                if digest.hexdigest() != sha:
                    raise SnapshotError("Snapshot checksum mismatch")
            finally:
                os.close(file_fd)
            return Snapshot(rid, self.root / ".bas12-snapshots" / rid / "scene.blend",
                            sha, sid, manifest["scene_revision"], manifest["idempotency_key"])
        finally:
            os.close(fd)


    def publish_new(self, snapshot_id: str, output: Path) -> tuple[str, bool]:
        """Atomically publish a private COPY without overwriting any existing file.

        The destination must already pass BAS-04 canonical_allowed_file.
        All path components are opened using no-follow dirfds and the copy is
        staged before an O_EXCL-equivalent hardlink commits the destination.
        This never hardlinks the immutable source .blend into user output.
        """
        checked = self.verify(snapshot_id)
        requested = Path(output)
        try:
            relative = requested.relative_to(self.root)
        except ValueError as exc:
            raise SnapshotError("Output outside sandbox root") from exc
        if (not relative.parts or ".." in relative.parts
                or requested.name.startswith(".bas12-")):
            raise SnapshotError("Unsafe output filename")
        target_fd = _dir_open(self.root)
        temp = ".bas12-publish-" + uuid4().hex
        temp_created = False
        try:
            for part in relative.parts[:-1]:
                fd = _child_dir(target_fd, part, create=False)
                os.close(target_fd)
                target_fd = fd
            source_fd = os.open(
                checked.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                temp_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                  | getattr(os, "O_NOFOLLOW", 0), 0o600,
                                  dir_fd=target_fd)
                temp_created = True
                digest = hashlib.sha256()
                try:
                    while True:
                        chunk = os.read(source_fd, 1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(temp_fd, view)
                            view = view[written:]
                    os.fsync(temp_fd)
                finally:
                    os.close(temp_fd)
                if digest.hexdigest() != checked.sha256:
                    raise SnapshotError("Source snapshot changed during copying")
                # Atomic publish or FileExistsError, even under concurrent
                # writers; never replace an existing destination.
                os.link(temp, relative.parts[-1], src_dir_fd=target_fd,
                        dst_dir_fd=target_fd, follow_symlinks=False)
                os.fsync(target_fd)
                return checked.sha256, False
            finally:
                os.close(source_fd)
        finally:
            if temp_created:
                try:
                    os.unlink(temp, dir_fd=target_fd)
                except FileNotFoundError:
                    pass
            os.close(target_fd)


    def _open_parent(self, path: Path) -> tuple[int, str]:
        """Traverse a target path relative to the trusted root, no symlinks."""
        try:
            parts = Path(path).relative_to(self.root).parts
        except ValueError as exc:
            raise SnapshotError("File lies outside sandbox") from exc
        if (not parts or any(p in ("", ".", "..") for p in parts)
                or parts[-1].startswith(".bas12-")):
            raise SnapshotError("Invalid target pathname")
        fd = _dir_open(self.root)
        try:
            for part in parts[:-1]:
                child = _child_dir(fd, part, create=False)
                os.close(fd)
                fd = child
            return fd, parts[-1]
        except BaseException:
            os.close(fd)
            raise

    def verify_external(self, path: Path, expected_sha256: str) -> str:
        """Hash a .blend using a no-follow file descriptor and strict bounds."""
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            raise SnapshotError("An expected SHA-256 is required")
        fd, name = self._open_parent(Path(path))
        try:
            input_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            try:
                st = os.fstat(input_fd)
                if not stat.S_ISREG(st.st_mode) or not 16 <= st.st_size <= self.max_bytes:
                    raise SnapshotError("Input is not a bounded BLEND file")
                if os.read(input_fd, 7) != b"BLENDER":
                    raise SnapshotError("Invalid BLEND header")
                os.lseek(input_fd, 0, os.SEEK_SET)
                digest = hashlib.sha256()
                while chunk := os.read(input_fd, 1024 * 1024):
                    digest.update(chunk)
                if digest.hexdigest() != expected_sha256:
                    raise SnapshotError("Source checksum changed")
                return digest.hexdigest()
            finally:
                os.close(input_fd)
        finally:
            os.close(fd)

    def backup_existing(self, path: Path, expected_sha256: str, *,
                        scene_id: str, scene_revision: int, idempotency_key: str
                        ) -> Snapshot:
        """Durable copy of the currently existing file BEFORE overwriting it."""
        self.verify_external(path, expected_sha256)
        rid, dest, replay = self.begin_keyed(
            scene_id=scene_id, scene_revision=scene_revision,
            idempotency_key="prior-file:" + idempotency_key,
            label="prior-file:" + str(path) + "|" + expected_sha256)
        if replay:
            return self.verify(rid)
        parent_fd, name = self._open_parent(path)
        try:
            infd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            try:
                outfd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                | getattr(os, "O_NOFOLLOW", 0), 0o600)
                digest = hashlib.sha256()
                try:
                    while chunk := os.read(infd, 1024 * 1024):
                        digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            view = view[os.write(outfd, view):]
                    os.fsync(outfd)
                finally:
                    os.close(outfd)
                if digest.hexdigest() != expected_sha256:
                    raise SnapshotError("Target changed during backup")
            finally:
                os.close(infd)
        finally:
            os.close(parent_fd)
        saved = self.finalize(
            rid, scene_id=scene_id, scene_revision=scene_revision,
            idempotency_key="prior-file:" + idempotency_key,
            label="prior-file:" + str(path) + "|" + expected_sha256)
        if saved.sha256 != expected_sha256:
            raise SnapshotError("Backup verification failed")
        return saved

    def consume_confirmation(self, token: str) -> None:
        """One-shot durable reservation before a destructive side effect.

        No automatic retry after a crash between reservation and action.
        """
        if not isinstance(token, str) or len(token) > 256:
            raise SnapshotError("Invalid confirmation")
        marker = hashlib.sha256(token.encode("ascii")).hexdigest()
        root_fd = _dir_open(self.root)
        try:
            folder = _child_dir(root_fd, ".bas12-consumed-confirmations", create=True)
            try:
                _write_exclusive(folder, marker, b"spent\n")
                os.fsync(folder)
            except FileExistsError as exc:
                raise SnapshotError("Confirmation already spent; state may be unknown") from exc
            finally:
                os.close(folder)
        finally:
            os.close(root_fd)

    def publish_overwrite(self, snapshot_id: str, output: Path, *,
                          expected_sha256: str, backup_id: str) -> str:
        """Replace an already backed-up BLEND using atomic same-directory rename.

        The root is private; same-process callers must serialize file effects.
        An external process changing the target between verification and rename
        is not part of a supported shared-writer contract.
        """
        artifact = self.verify(snapshot_id)
        prior = self.verify(backup_id)
        if prior.sha256 != expected_sha256:
            raise SnapshotError("Pre-overwrite backup differs from approved target")
        self.verify_external(output, expected_sha256)
        parent_fd, name = self._open_parent(Path(output))
        temp = ".bas12-replacement-" + uuid4().hex
        created = False
        try:
            old_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            try:
                before = os.fstat(old_fd)
                temp_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                  | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent_fd)
                created = True
                try:
                    digest = hashlib.sha256()
                    with open(artifact.path, "rb") as source:
                        while chunk := source.read(1024 * 1024):
                            digest.update(chunk)
                            view = memoryview(chunk)
                            while view:
                                view = view[os.write(temp_fd, view):]
                    os.fsync(temp_fd)
                    if digest.hexdigest() != artifact.sha256:
                        raise SnapshotError("New scene snapshot changed during copy")
                finally:
                    os.close(temp_fd)
                now = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (not stat.S_ISREG(now.st_mode) or now.st_ino != before.st_ino
                        or now.st_size != before.st_size
                        or now.st_mtime_ns != before.st_mtime_ns):
                    raise SnapshotError("Target changed before replace")
                os.replace(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                created = False
                os.fsync(parent_fd)
                return artifact.sha256
            finally:
                os.close(old_fd)
        finally:
            if created:
                try:
                    os.unlink(temp, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            os.close(parent_fd)
