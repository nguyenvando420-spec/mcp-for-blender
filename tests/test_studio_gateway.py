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
    (Profile.STUDIO_LOCAL, {"studio_handshake", "studio_scene_inspect", "studio_create_object"}),
    (Profile.DEVELOPER_LOCAL, {"studio_handshake", "studio_scene_inspect", "studio_create_object"}),
    (Profile.REMOTE, set()), (Profile.WORKER, set()),
])
def test_exposure(profile, expected):
    assert visible_studio_tools(profile) == expected
