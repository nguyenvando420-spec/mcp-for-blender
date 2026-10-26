"""BAS-05 registry authorization and typed wire tests; Blender not required."""
from __future__ import annotations

from uuid import uuid4
import pytest

from blender_mcp.studio.registry import StudioGateway, visible_studio_tools
from blender_mcp.security_policy import Policy, Profile

SCENE = str(uuid4())


class FakeBridge:
    def __init__(self, capabilities=None):
        self.calls = []
        self.capabilities = capabilities if capabilities is not None else [
            "studio_scene_inspect_v1", "studio_create_object_v1"]
        self.revision = 0

    def __call__(self, command, params, read_only):
        self.calls.append((command, params, read_only))
        if command == "get_addon_info":
            return {"protocol_version": 15, "addon_version": [1, 8],
                    "blender_version": "4.5.15", "capabilities": self.capabilities}
        if command == "studio_scene_inspect_v1":
            return {"scene_id": SCENE, "scene_revision": self.revision,
                    "name": "Scene", "object_count": 1, "objects": [{"name": "Cube"}]}
        if command == "studio_create_object_v1":
            if params["scene_revision"] != self.revision or params["scene_id"] != SCENE:
                raise ValueError("stale_scene")
            self.revision += 1
            return {"scene_id": SCENE, "scene_revision": self.revision,
                    "object_id": str(uuid4()), "name": params["name"]}
        raise AssertionError("Unexpected bridge command: " + command)


def request(operation, effect_scope="read", **extra):
    data = {"operation": operation, "request_id": str(uuid4()),
            "effect_scope": effect_scope}
    data.update(extra)
    return data


def test_handshake_is_explicit_and_reports_checkout_paths():
    bridge = FakeBridge()
    info = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL)).handshake()
    assert info["operations"] == ["studio_create_object", "studio_scene_inspect"]
    assert info["addon_protocol"] == 15
    assert info["package_file"].endswith("studio/registry.py")
    assert info["python_executable"]


def test_inspect_with_typed_response_and_correlation():
    bridge = FakeBridge()
    data = request("studio_scene_inspect", query="Cube")
    response = StudioGateway(bridge, Policy(Profile.OBSERVER)).invoke(data, "studio_scene_inspect")
    assert response["status"] == "succeeded"
    assert response["request_id"] == data["request_id"]
    assert response["scene_id"] == SCENE
    assert bridge.calls[-1][0] == "studio_scene_inspect_v1"
    assert bridge.calls[-1][2] is True


def test_invalid_never_calls_side_effect_or_info():
    bridge = FakeBridge()
    gateway = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL))
    for raw in [
        request("studio_create_object", "scene", scene_id=SCENE, scene_revision=0,
                idempotency_key="key", name="x",
                spec={"kind": "primitive", "primitive": "cube"}, location=[1, 2]),
        request("studio_create_object", "scene", scene_id=SCENE, scene_revision=0,
                idempotency_key="key", name="x",
                spec={"kind": "primitive", "primitive": "__import__"}),
        request("studio_create_object", "scene", scene_id=SCENE, scene_revision=0,
                idempotency_key="key", name="x",
                spec={"kind": "primitive", "primitive": "cube"}, unknown=1),
    ]:
        result = gateway.invoke(raw, "studio_create_object")
        assert result["status"] == "failed"
        assert result["error"]["code"] == "invalid_input"
        assert result["request_id"] == raw["request_id"]
    assert not bridge.calls


def test_observer_does_not_create_even_when_tool_invoked():
    bridge = FakeBridge()
    raw = request("studio_create_object", "scene", scene_id=SCENE, scene_revision=0,
                  idempotency_key="test", name="MyCube",
                  spec={"kind": "primitive", "primitive": "cube"})
    response = StudioGateway(bridge, Policy(Profile.OBSERVER)).invoke(raw, "studio_create_object")
    assert response["error"]["code"] == "permission_denied"
    assert not bridge.calls


def test_primitive_create_and_stale_scene():
    bridge = FakeBridge()
    gateway = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL))
    raw = request("studio_create_object", "scene", scene_id=SCENE, scene_revision=0,
                  idempotency_key="one", name="Cube2",
                  spec={"kind": "primitive", "primitive": "cube"})
    response = gateway.invoke(raw, "studio_create_object")
    assert response["status"] == "succeeded"
    assert response["result"]["objects"][0]["name"] == "Cube2"
    assert response["scene_revision"] == 1
    stale = gateway.invoke({**raw, "request_id": str(uuid4())}, "studio_create_object")
    assert stale["error"]["code"] == "stale_scene"


def test_no_capability_no_dispatch():
    bridge = FakeBridge(capabilities=["get_scene_info"])
    result = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL)).invoke(
        request("studio_scene_inspect"), "studio_scene_inspect")
    assert result["error"]["code"] == "capability_unavailable"
    assert [c[0] for c in bridge.calls] == ["get_addon_info"]


def test_major_and_unknown_operation_rejected_without_bridge():
    bridge = FakeBridge()
    gateway = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL))
    wrong = request("studio_scene_inspect", contract={"major": 2})
    assert gateway.invoke(wrong, "studio_scene_inspect")["error"]["code"] == "unsupported_version"
    assert gateway.invoke(request("studio_execute_code"), "studio_scene_inspect")["error"]["code"] == "invalid_input"
    assert not bridge.calls


def test_text_is_not_implicitly_advertised_as_supported_variant():
    bridge = FakeBridge()
    raw = request("studio_create_object", "scene", scene_id=SCENE, scene_revision=0,
                  idempotency_key="t", name="Text", spec={"kind": "text", "text": "hello"})
    result = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL)).invoke(raw, "studio_create_object")
    assert result["error"]["code"] == "capability_unavailable"
    assert not any(c[0] == "studio_create_object_v1" for c in bridge.calls)


@pytest.mark.parametrize("profile,expected", [
    (Profile.OBSERVER, {"studio_handshake", "studio_scene_inspect"}),
    (Profile.STUDIO_LOCAL, {"studio_handshake", "studio_scene_inspect", "studio_create_object", "studio_set_transform", "studio_manage_collection"}),
    (Profile.DEVELOPER_LOCAL, {"studio_handshake", "studio_scene_inspect", "studio_create_object", "studio_set_transform", "studio_manage_collection"}),
    (Profile.REMOTE, set()), (Profile.WORKER, set()),
])
def test_exposure(profile, expected):
    assert visible_studio_tools(profile) == expected



def test_unavailable_addon_returns_typed_handshake():
    def unavailable(*_args):
        raise Exception("connection refused: secret implementation details")

    info = StudioGateway(unavailable, Policy(Profile.STUDIO_LOCAL)).handshake()
    assert info["operations"] == []
    assert info["error"]["code"] == "worker_unavailable"
    assert "secret implementation details" not in info["error"]["message"]


def test_observer_handshake_advertises_only_observer_operations():
    info = StudioGateway(FakeBridge(), Policy(Profile.OBSERVER)).handshake()
    assert info["operations"] == ["studio_scene_inspect"]


def test_wire_error_wrappers_preserve_stale_and_conflict_codes():
    for message, code in [("stale_scene: wrong revision", "stale_scene"),
                          ("conflict: key changed", "conflict")]:
        class WireBridge(FakeBridge):
            def __call__(self, command, params, read_only):
                if command == "studio_create_object_v1":
                    raise Exception("Communication error with Blender: " + message)
                return super().__call__(command, params, read_only)

        raw = request("studio_create_object", "scene", scene_id=SCENE,
                      scene_revision=0, idempotency_key="k", name="Cube",
                      spec={"kind": "primitive", "primitive": "cube"})
        response = StudioGateway(WireBridge(), Policy(Profile.STUDIO_LOCAL)).invoke(
            raw, "studio_create_object")
        assert response["error"]["code"] == code


def test_hidden_legacy_raw_code_cannot_be_called_in_studio_profile(monkeypatch):
    import asyncio
    from blender_mcp import server
    monkeypatch.setenv("BLENDER_MCP_PROFILE", "studio-local")
    with pytest.raises(PermissionError, match="permission_denied"):
        asyncio.run(server._call_tool_for_profile("execute_blender_code", {"code": "print(1)"}))


class Bas07Bridge(FakeBridge):
    def __init__(self):
        super().__init__(["studio_scene_inspect_v1", "studio_create_object_v1",
                          "studio_create_text_v1", "studio_set_transform_v1",
                          "studio_manage_collection_v1"])
        self.objects = {}
        self.collections = {}

    def __call__(self, command, params, read_only):
        if command in ("studio_create_text_v1", "studio_set_transform_v1",
                       "studio_manage_collection_v1"):
            self.calls.append((command, params, read_only))
            if params["scene_revision"] != self.revision:
                raise ValueError("stale_scene: revision")
            if command == "studio_create_text_v1":
                ident = str(uuid4())
                self.objects[ident] = params["name"]
                name = params["name"]
            elif command == "studio_set_transform_v1":
                obj_id = params["object_id"]
                if obj_id not in self.objects:
                    raise ValueError("object_not_found: absent")
                if params["action"] == "duplicate":
                    ident = str(uuid4())
                    name = params["name"]
                    self.objects[ident] = name
                else:
                    ident = obj_id
                    name = params["name"] if params["action"] == "rename" else self.objects[ident]
                    self.objects[ident] = name
            else:
                action = params["action"]
                if action == "create":
                    ident, name = str(uuid4()), params["name"]
                    self.collections[ident] = name
                elif action == "rename":
                    ident, name = params["collection_id"], params["name"]
                    if ident not in self.collections:
                        raise ValueError("object_not_found: absent")
                    self.collections[ident] = name
                else:
                    ident = params["object_id"]
                    if ident not in self.objects or params["collection_id"] not in self.collections:
                        raise ValueError("object_not_found: absent")
                    name = self.objects[ident]
            self.revision += 1
            return {"scene_id": SCENE, "scene_revision": self.revision,
                    "object_id": ident, "name": name}
        return super().__call__(command, params, read_only)


def _bas07_request(bridge, operation, key, **kwargs):
    return request(operation, "scene", scene_id=SCENE,
                   scene_revision=bridge.revision, idempotency_key=key, **kwargs)


def test_bas07_typed_create_text_rename_duplicate_and_collection():
    bridge = Bas07Bridge()
    gateway = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL))
    assert {"studio_set_transform", "studio_manage_collection", "studio_create_object"} <= set(
        gateway.handshake()["operations"])
    created = gateway.invoke(_bas07_request(
        bridge, "studio_create_object", "text", name="A",
        spec={"kind": "text", "text": "Vietnamese Unicode: Xin chào"}), "studio_create_object")
    assert created["status"] == "succeeded", created
    oid = created["result"]["objects"][0]["id"]
    renamed = gateway.invoke(_bas07_request(
        bridge, "studio_set_transform", "rename", object_id=oid,
        action="rename", name="B"), "studio_set_transform")
    assert renamed["status"] == "succeeded", renamed
    assert renamed["result"]["objects"][0]["id"] == oid
    copied = gateway.invoke(_bas07_request(
        bridge, "studio_set_transform", "copy", object_id=oid,
        action="duplicate", name="B Copy"), "studio_set_transform")
    assert copied["status"] == "succeeded", copied
    assert copied["result"]["objects"][0]["id"] != oid
    collection = gateway.invoke(_bas07_request(
        bridge, "studio_manage_collection", "collection", action="create",
        name="Props"), "studio_manage_collection")
    assert collection["status"] == "succeeded", collection
    cid = collection["result"]["objects"][0]["id"]
    moved = gateway.invoke(_bas07_request(
        bridge, "studio_manage_collection", "move", action="move_object",
        collection_id=cid, object_id=oid), "studio_manage_collection")
    assert moved["status"] == "succeeded", moved
    assert moved["result"]["objects"][0]["id"] == oid
    assert all(command != "execute_code" for command, _, _ in bridge.calls)


@pytest.mark.parametrize("location", [[float("nan"), 0, 0], [float("inf"), 0, 0],
                                       [100001.0, 0, 0]])
def test_bas07_rejects_bad_coordinates_without_mutation(location):
    bridge = Bas07Bridge()
    req = _bas07_request(bridge, "studio_create_object", "bad", name="Bad",
                         spec={"kind": "primitive", "primitive": "cube"}, location=location)
    response = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL)).invoke(req, "studio_create_object")
    assert response["error"]["code"] == "invalid_input"
    assert all(cmd not in ("studio_create_object_v1", "studio_create_text_v1")
               for cmd, _, _ in bridge.calls)


def test_bas07_missing_object_and_delete_fail_closed():
    bridge = Bas07Bridge()
    gateway = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL))
    missing = gateway.invoke(_bas07_request(
        bridge, "studio_set_transform", "missing", object_id=str(uuid4()),
        action="rename", name="X"), "studio_set_transform")
    assert missing["error"]["code"] == "object_not_found"
    delete = gateway.invoke(_bas07_request(
        bridge, "studio_delete_object", "delete", object_id=str(uuid4()),
        snapshot_id=str(uuid4()), confirmation_token="invalid"),
        "studio_delete_object")
    assert delete["error"]["code"] == "permission_denied"
    assert not any(cmd == "studio_delete_object_v1" for cmd, _, _ in bridge.calls)
