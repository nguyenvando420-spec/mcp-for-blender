"""BAS-12 gateway snapshot and restore policy tests, with an isolated fake bpy bridge."""
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from blender_mcp.security_policy import Policy, Profile, issue_confirmation
from blender_mcp.studio.registry import StudioGateway
from blender_mcp.studio.snapshot_store import SnapshotStore

SECRET = b"z" * 48


class Bridge:
    def __init__(self, scene_id):
        self.sid = scene_id
        self.rev = 2
        self.calls = []

    def __call__(self, command, params, read_only):
        self.calls.append((command, params))
        if command == "get_addon_info":
            return {"protocol_version": 16, "capabilities": [
                "studio_scene_inspect_v1", "studio_scene_snapshot_v1",
                "studio_scene_restore_v1"]}
        if command == "studio_scene_snapshot_v1":
            assert params["scene_id"] == self.sid and params["scene_revision"] == self.rev
            path = Path(params["path"])
            path.write_bytes(b"BLENDER-v450" + b"content" * 80)
            return {"scene_id": self.sid, "scene_revision": self.rev,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        if command == "studio_scene_restore_v1":
            assert params["scene_id"] == self.sid and params["scene_revision"] == self.rev
            assert Path(params["path"]).is_file()
            self.rev += 1
            return {"scene_id": self.sid, "scene_revision": self.rev,
                    "restored_snapshot_id": params["snapshot_id"],
                    "pre_restore_snapshot_id": params["backup_snapshot_id"]}
        raise AssertionError(command)


def req(op, sid, rev, key, **other):
    return {"operation": op, "request_id": str(uuid4()),
            "effect_scope": "file", "scene_id": sid, "scene_revision": rev,
            "idempotency_key": key, **other}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("BLENDER_MCP_STUDIO_ROOT", str(root))
    sid = str(uuid4())
    bridge = Bridge(sid)
    gateway = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL,
                            read_roots=(root,), write_roots=(root,)),
                            confirmation_secret=SECRET)
    return gateway, bridge, root, sid


def test_snapshot_roundtrip_and_replay_no_second_write(client):
    gateway, bridge, root, sid = client
    raw = req("studio_scene_snapshot", sid, 2, "same", label="scene")
    one = gateway.invoke(raw, "studio_scene_snapshot")
    assert one["status"] == "succeeded", one
    two = gateway.invoke({**raw, "request_id": str(uuid4())}, "studio_scene_snapshot")
    assert two["status"] == "succeeded", two
    assert two["result"]["snapshot_id"] == one["result"]["snapshot_id"]
    assert [c[0] for c in bridge.calls].count("studio_scene_snapshot_v1") == 1
    assert SnapshotStore(root).verify(one["result"]["snapshot_id"])


def test_restore_rejected_without_confirmation_and_source_is_checked(client):
    gateway, bridge, root, sid = client
    invalid = req("studio_scene_restore", sid, 2, "blocked",
                  snapshot_id=str(uuid4()), confirmation_token="invalid")
    result = gateway.invoke(invalid, "studio_scene_restore")
    assert result["error"]["code"] == "permission_denied"
    assert not any(c[0] == "studio_scene_restore_v1" for c in bridge.calls)
    snapshot = gateway.invoke(req("studio_scene_snapshot", sid, 2, "source"),
                              "studio_scene_snapshot")
    ident = snapshot["result"]["snapshot_id"]
    token = issue_confirmation(SECRET, operation="studio_scene_restore",
                               scene_id=sid, scene_revision=2, snapshot_id=ident)
    restored = gateway.invoke(req("studio_scene_restore", sid, 2, "restore",
                                  snapshot_id=ident, confirmation_token=token),
                              "studio_scene_restore")
    assert restored["status"] == "succeeded", restored
    assert restored["scene_revision"] == 3
    backup = restored["result"]["snapshot_id"]
    assert backup != ident
    assert SnapshotStore(root).verify(backup).scene_revision == 2


def test_corrupted_source_snapshot_fails_before_restore(client):
    gateway, bridge, root, sid = client
    created = gateway.invoke(req("studio_scene_snapshot", sid, 2, "snapshot"),
                             "studio_scene_snapshot")
    ident = created["result"]["snapshot_id"]
    artifact = SnapshotStore(root).verify(ident)
    artifact.path.write_bytes(b"BLENDERfake-corruption")
    token = issue_confirmation(SECRET, operation="studio_scene_restore",
                               scene_id=sid, scene_revision=2, snapshot_id=ident)
    failed = gateway.invoke(req("studio_scene_restore", sid, 2, "restore",
                                snapshot_id=ident, confirmation_token=token),
                            "studio_scene_restore")
    assert failed["status"] == "failed"
    assert not any(c[0] == "studio_scene_restore_v1" for c in bridge.calls)
