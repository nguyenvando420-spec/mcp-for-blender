"""BAS-12 gateway snapshot and restore policy tests, with an isolated fake bpy bridge."""
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from blender_mcp.security_policy import Policy, Profile, issue_confirmation, issue_resource_confirmation
from blender_mcp.studio.registry import StudioGateway
from blender_mcp.studio.snapshot_store import SnapshotStore

SECRET = b"z" * 48


def test_addon_snapshot_ignores_user_compression_preference(tmp_path, monkeypatch):
    """A compressed-save preference must not break the snapshot wire format."""
    from types import SimpleNamespace
    import uuid
    from test_server_threading import BlenderMCPServer

    server = object.__new__(BlenderMCPServer)
    sid, snapshot_id = str(uuid4()), str(uuid4())
    scene = SimpleNamespace(objects=[])
    path = tmp_path / "snapshot.blend"
    server._studio_scene_context_v1 = lambda: (scene, sid, 2)
    server._bas12_snapshot_path = lambda *_: path
    server._bas07_scene_fingerprint = lambda _: "f" * 64

    def save_as_mainfile(*, filepath, copy, compress=True):
        assert copy is True
        # Emulate Blender saving compressed data unless explicitly disabled.
        Path(filepath).write_bytes(b"compressed-default" if compress else b"BLENDER-v450-payload")
        return {"FINISHED"}

    fake_bpy = SimpleNamespace(ops=SimpleNamespace(wm=SimpleNamespace(
        save_as_mainfile=save_as_mainfile)))
    monkeypatch.setitem(BlenderMCPServer.studio_scene_snapshot_v1.__globals__, "bpy", fake_bpy)
    monkeypatch.setitem(BlenderMCPServer.studio_scene_snapshot_v1.__globals__, "uuid", uuid)
    result = server.studio_scene_snapshot_v1(sid, 2, snapshot_id, str(path))
    assert result["snapshot_id"] == snapshot_id
    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["bytes"] == path.stat().st_size


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
                "studio_scene_restore_v1", "studio_scene_open_v1"]}
        if command == "studio_scene_snapshot_v1":
            assert params["scene_id"] == self.sid and params["scene_revision"] == self.rev
            path = Path(params["path"])
            path.write_bytes(b"BLENDER-v450" + b"content" * 80)
            return {"scene_id": self.sid, "scene_revision": self.rev,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        if command == "studio_scene_open_v1":
            self.rev += 1
            return {"scene_id": str(uuid4()), "old_scene_id": self.sid,
                    "scene_revision": self.rev}
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



def test_resource_token_binds_target_path_and_checksum(client):
    gateway, bridge, root, sid = client
    content = b"BLENDER-v450" + b"one" * 60
    existing = root / "prior.blend"
    existing.write_bytes(content)
    prior_hash = hashlib.sha256(content).hexdigest()
    confirmed = issue_resource_confirmation(
        SECRET, operation="studio_scene_save", scene_id=sid, scene_revision=2,
        resource=existing.as_uri() + "|" + prior_hash)
    raw = req("studio_scene_save", sid, 2, "replace",
              output_uri=existing.as_uri(), overwrite=True,
              expected_sha256=prior_hash, confirmation_token=confirmed)
    # Wrong target and wrong digest must fail before even querying Blender.
    for altered in ({**raw, "output_uri": (root / "other.blend").as_uri()},
                    {**raw, "expected_sha256": "0" * 64}):
        result = gateway.invoke(altered, "studio_scene_save")
        assert result["status"] == "failed"
        assert not any(x[0] == "studio_scene_snapshot_v1" for x in bridge.calls)
    accepted = gateway.invoke(raw, "studio_scene_save")
    assert accepted["status"] == "succeeded", accepted
    assert hashlib.sha256(existing.read_bytes()).hexdigest() != prior_hash
    store = SnapshotStore(root)
    assert any(store.verify(d.name).sha256 == prior_hash
               for d in (root / ".bas12-snapshots").iterdir()
               if (d / "manifest.json").is_file())
    repeated = gateway.invoke({**raw, "request_id": str(uuid4())},
                              "studio_scene_save")
    assert repeated["status"] == "failed"


def test_scene_open_requires_snapshot_and_invalidates_prior_scene(client):
    gateway, bridge, root, sid = client
    source = root / "other-scene.blend"
    source.write_bytes(b"BLENDER-v450" + b"new-file" * 32)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    prior = gateway.invoke(req("studio_scene_snapshot", sid, 2, "open-prior"),
                           "studio_scene_snapshot")
    prior_id = prior["result"]["snapshot_id"]
    token = issue_resource_confirmation(
        SECRET, operation="studio_scene_open", scene_id=sid,
        scene_revision=2, snapshot_id=prior_id,
        resource=source.as_uri() + "|" + digest)
    good = req("studio_scene_open", sid, 2, "opening", input_uri=source.as_uri(),
               expected_sha256=digest, snapshot_id=prior_id, confirmation_token=token)
    wrong = gateway.invoke({**good, "expected_sha256": "0" * 64}, "studio_scene_open")
    assert wrong["error"]["code"] == "permission_denied"
    assert not any(t[0] == "studio_scene_open_v1" for t in bridge.calls)
    opened = gateway.invoke(good, "studio_scene_open")
    assert opened["status"] == "succeeded", opened
    assert opened["scene_id"] != sid and opened["scene_revision"] == 3
    assert opened["result"]["snapshot_id"] != prior_id
    again = gateway.invoke({**good, "request_id": str(uuid4())}, "studio_scene_open")
    assert again["status"] == "failed"
