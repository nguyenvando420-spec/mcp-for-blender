"""BAS-12 persistence core security tests (no bpy required)."""
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from blender_mcp.studio.snapshot_store import SnapshotError, SnapshotStore


def _store(tmp_path):
    root = tmp_path / "sandbox"
    root.mkdir(mode=0o700)
    return SnapshotStore(root)


def _fake_blend(path: Path):
    path.write_bytes(b"BLENDER-v450" + b"scene-data" * 64)


def test_allocate_finalize_reopen_verify(tmp_path):
    store = _store(tmp_path)
    snapshot_id, path = store.begin()
    assert path.parent.parent.name == ".bas12-snapshots"
    _fake_blend(path)
    sid = str(uuid4())
    result = store.finalize(snapshot_id, scene_id=sid,
                            scene_revision=11, idempotency_key="save-1")
    assert result.scene_id == sid
    other = SnapshotStore(store.root).verify(snapshot_id)
    assert other == result
    assert other.sha256
    with pytest.raises(FileExistsError):
        store.finalize(snapshot_id, scene_id=sid, scene_revision=11,
                       idempotency_key="save-1")


def test_missing_or_malformed_or_modified_file(tmp_path):
    store = _store(tmp_path)
    with pytest.raises((FileNotFoundError, SnapshotError)):
        store.verify(str(uuid4()))
    rid, path = store.begin()
    with pytest.raises(FileNotFoundError):
        store.finalize(rid, scene_id=str(uuid4()), scene_revision=0,
                       idempotency_key="key")
    path.write_bytes(b"not-a-blend" * 10)
    with pytest.raises(SnapshotError):
        store.finalize(rid, scene_id=str(uuid4()), scene_revision=0,
                       idempotency_key="key")
    _fake_blend(path)
    store.finalize(rid, scene_id=str(uuid4()), scene_revision=0,
                   idempotency_key="key")
    path.write_bytes(b"BLENDER" + b"x" * 100)
    with pytest.raises(SnapshotError, match="mismatch"):
        store.verify(rid)


def test_symlink_escape_and_manifest_tampering(tmp_path):
    store = _store(tmp_path)
    rid, path = store.begin()
    target = tmp_path / "outside.blend"
    _fake_blend(target)
    path.symlink_to(target)
    with pytest.raises(OSError):
        store.finalize(rid, scene_id=str(uuid4()), scene_revision=1,
                       idempotency_key="k")
    path.unlink()
    _fake_blend(path)
    store.finalize(rid, scene_id=str(uuid4()), scene_revision=1,
                   idempotency_key="k")
    manifest = path.parent / "manifest.json"
    manifest.write_text(json.dumps({"snapshot_id": rid, "version": 1}))
    with pytest.raises(SnapshotError, match="manifest"):
        store.verify(rid)


def test_bad_root_modes_and_traversal(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "link"
    linked.symlink_to(outside, target_is_directory=True)
    for candidate in (linked, Path("relative/path"), tmp_path / ".." / "outside"):
        with pytest.raises(SnapshotError):
            SnapshotStore(candidate)
    outside.chmod(0o777)
    with pytest.raises(SnapshotError):
        SnapshotStore(outside)


def test_snapshot_identifiers_never_select_arbitrary_paths(tmp_path):
    store = _store(tmp_path)
    for bad in ("../x", str(tmp_path / "outside"), "not-a-uuid", "", None):
        with pytest.raises(SnapshotError):
            store.verify(bad)
    rid, path = store.begin()
    _fake_blend(path)
    store.finalize(rid, scene_id=str(uuid4()), scene_revision=1,
                   idempotency_key="ok")
    snapshot_dir = path.parent
    injected = tmp_path / "injected"
    injected.mkdir(mode=0o700)
    (store.root / ".bas12-snapshots" / rid).rename(injected / "old")
    snapshot_dir.symlink_to(injected, target_is_directory=True)
    with pytest.raises(OSError):
        store.verify(rid)
