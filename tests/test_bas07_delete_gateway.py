"""BAS-07 delete authorization, snapshot-before-side-effect, replay regressions."""
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from blender_mcp.security_policy import Policy, Profile, issue_resource_confirmation
from blender_mcp.studio.registry import StudioGateway
from blender_mcp.studio.snapshot_store import SnapshotStore

SECRET = b"q" * 48


class Bridge:
    def __init__(self, sid, oid):
        self.sid, self.oid, self.revision = sid, oid, 3
        self.calls = []
        self.deleted = False
        self.gui_state = "unchanged"

    def __call__(self, cmd, params, read_only):
        self.calls.append((cmd, params))
        if cmd == "get_addon_info":
            return {"protocol_version": 16, "capabilities": [
                "studio_scene_snapshot_v1", "studio_delete_object_v1"]}
        if cmd == "studio_scene_snapshot_v1":
            assert params["scene_id"] == self.sid
            assert params["scene_revision"] == self.revision
            path = Path(params["path"])
            path.write_bytes(b"BLENDER-v450" + b"data" * 40)
            return {"scene_id": self.sid, "scene_revision": self.revision,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "objects": [{"object_id": self.oid, "name": "Target"}],
                    "scene_fingerprint": hashlib.sha256(self.gui_state.encode()).hexdigest()}
        if cmd == "studio_delete_object_v1":
            assert not self.deleted
            assert params["object_id"] == self.oid
            assert params["scene_revision"] == self.revision
            self.deleted = True
            self.revision += 1
            return {"object_id": self.oid, "scene_id": self.sid,
                    "scene_revision": self.revision}
        raise AssertionError(cmd)


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    root = tmp_path / "studio"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("BLENDER_MCP_STUDIO_ROOT", str(root))
    monkeypatch.setenv("BLENDER_MCP_CONFIRM_SECRET", SECRET.decode())
    sid, oid = str(uuid4()), str(uuid4())
    bridge = Bridge(sid, oid)
    gateway = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL, read_roots=(root,),
                                           write_roots=(root,)),
                            confirmation_secret=SECRET)
    return gateway, bridge, root, sid, oid


def envelope(operation, sid, revision, key, scope="scene", **extra):
    return {"operation": operation, "request_id": str(uuid4()),
            "scene_id": sid, "scene_revision": revision,
            "idempotency_key": key, "effect_scope": scope, **extra}


def test_confirmed_delete_with_mandatory_snapshot_and_replay_denied(ctx):
    gateway, bridge, root, sid, oid = ctx
    preview = gateway.invoke(envelope("studio_scene_snapshot", sid, 3, "preview",
                                      scope="file"), "studio_scene_snapshot")
    assert preview["status"] == "succeeded", preview
    snap_id = preview["result"]["snapshot_id"]
    token = issue_resource_confirmation(
        SECRET, operation="studio_delete_object", scene_id=sid,
        scene_revision=3, snapshot_id=snap_id, resource=oid)
    req = envelope("studio_delete_object", sid, 3, "delete", object_id=oid,
                   snapshot_id=snap_id, confirmation_token=token)
    denied = gateway.invoke({**req, "confirmation_token": token + "bad"},
                            "studio_delete_object")
    assert denied["error"]["code"] == "permission_denied"
    assert not bridge.deleted
    done = gateway.invoke(req, "studio_delete_object")
    assert done["status"] == "succeeded", done
    assert done["result"]["scene_diff"]["deleted"] == [oid]
    assert done["result"]["snapshot_id"] != snap_id
    assert SnapshotStore(root).verify(done["result"]["snapshot_id"]).scene_revision == 3
    repeated = gateway.invoke({**req, "request_id": str(uuid4())},
                              "studio_delete_object")
    assert repeated["status"] == "failed"
    assert [call[0] for call in bridge.calls].count("studio_delete_object_v1") == 1


def test_delete_no_preview_or_wrong_target_never_mutates(ctx):
    gateway, bridge, root, sid, oid = ctx
    token = issue_resource_confirmation(
        SECRET, operation="studio_delete_object", scene_id=sid,
        scene_revision=3, snapshot_id=str(uuid4()), resource=oid)
    missing_snapshot = gateway.invoke(envelope(
        "studio_delete_object", sid, 3, "no-preview", object_id=oid,
        snapshot_id=str(uuid4()), confirmation_token=token), "studio_delete_object")
    assert missing_snapshot["status"] == "failed"
    assert not bridge.deleted
    preview = gateway.invoke(envelope("studio_scene_snapshot", sid, 3, "preview",
                                      scope="file"), "studio_scene_snapshot")
    snapshot_id = preview["result"]["snapshot_id"]
    wrong_oid = str(uuid4())
    token2 = issue_resource_confirmation(
        SECRET, operation="studio_delete_object", scene_id=sid,
        scene_revision=3, snapshot_id=snapshot_id, resource=wrong_oid)
    wrong = gateway.invoke(envelope("studio_delete_object", sid, 3, "missing",
                                     object_id=wrong_oid, snapshot_id=snapshot_id,
                                     confirmation_token=token2), "studio_delete_object")
    assert wrong["status"] == "failed"
    assert not bridge.deleted



def test_unknown_transport_after_delete_reports_recovery_checkpoint(ctx):
    gateway, bridge, root, sid, oid = ctx
    original = bridge.__call__

    # Keep the real fake's bound entrypoint but simulate a lost socket reply
    # after the target has already been removed in Blender.
    def flaky(cmd, params, read_only):
        if cmd == "studio_delete_object_v1":
            bridge.calls.append((cmd, params))
            bridge.deleted = True
            raise TimeoutError("Disconnected after side effect")
        return original(cmd, params, read_only)

    gateway.bridge = flaky
    preview = gateway.invoke(envelope("studio_scene_snapshot", sid, 3, "fault-preview",
                                      scope="file"), "studio_scene_snapshot")
    ident = preview["result"]["snapshot_id"]
    token = issue_resource_confirmation(
        SECRET, operation="studio_delete_object", scene_id=sid,
        scene_revision=3, snapshot_id=ident, resource=oid)
    request = envelope("studio_delete_object", sid, 3, "fault-delete",
                       object_id=oid, snapshot_id=ident, confirmation_token=token)
    failed = gateway.invoke(request, "studio_delete_object")
    assert failed["status"] == "failed"
    details = failed["error"]["details"]
    assert details["may_have_partial_effects"] is True
    assert SnapshotStore(root).verify(details["pre_delete_snapshot_id"]).scene_revision == 3
    # The spent token cannot be replayed to delete twice.
    again = gateway.invoke({**request, "request_id": str(uuid4())},
                           "studio_delete_object")
    assert again["status"] == "failed"
    assert [x[0] for x in bridge.calls].count("studio_delete_object_v1") == 1



@pytest.mark.parametrize("manual_edit", ["translate", "mesh_vertex", "material", "camera"])
def test_untracked_manual_gui_edit_rejects_delete_without_revision_change(ctx, manual_edit):
    gateway, bridge, root, sid, oid = ctx
    before_revision = bridge.revision
    preview = gateway.invoke(envelope("studio_scene_snapshot", sid, before_revision,
                                      "preview-" + manual_edit, scope="file"),
                             "studio_scene_snapshot")
    assert preview["status"] == "succeeded", preview
    approved_id = preview["result"]["snapshot_id"]
    token = issue_resource_confirmation(
        SECRET, operation="studio_delete_object", scene_id=sid,
        scene_revision=before_revision, snapshot_id=approved_id, resource=oid)
    # Direct Blender edit, outside MCP, does not increment the BAS-05 revision.
    bridge.gui_state = manual_edit
    assert bridge.revision == before_revision
    rejected = gateway.invoke(envelope(
        "studio_delete_object", sid, before_revision, "delete-" + manual_edit,
        object_id=oid, snapshot_id=approved_id, confirmation_token=token),
        "studio_delete_object")
    assert rejected["status"] == "failed", rejected
    assert rejected["error"]["code"] == "conflict"
    assert bridge.deleted is False
    assert not any(call[0] == "studio_delete_object_v1" for call in bridge.calls)
