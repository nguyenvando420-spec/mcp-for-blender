"""BAS-11 gateway/contract negative tests; Blender GUI validation is a separate CI gate."""
from uuid import uuid4

import pytest

from blender_mcp.security_policy import Policy, Profile
from blender_mcp.studio.contracts import validate_request
from blender_mcp.studio.registry import StudioGateway, visible_studio_tools


SID = str(uuid4())


class Bridge:
    def __init__(self):
        self.calls = []
        self.current_rev = 1

    def __call__(self, command, params, read_only):
        self.calls.append((command, params, read_only))
        if command == "get_addon_info":
            return {"protocol_version": 16, "capabilities": [
                "studio_set_camera_v1", "studio_set_light_v1"]}
        if command == "studio_set_camera_v1":
            return {"scene_id": SID, "scene_revision": 2,
                    "object_id": params.get("camera_id") or str(uuid4()),
                    "name": params.get("name") or "Camera"}
        if command == "studio_set_light_v1":
            return {"scene_id": SID, "scene_revision": 2,
                    "object_id": params["spec"].get("light_id") or str(uuid4()),
                    "name": "Light"}
        raise AssertionError(command)


def request(operation, **extra):
    return {"operation": operation, "effect_scope": "scene",
            "request_id": str(uuid4()), "scene_id": SID,
            "scene_revision": 1, "idempotency_key": "bas11:" + str(uuid4()),
            **extra}


def camera(**extra):
    return request("studio_set_camera", location=[2, -5, 3],
                   rotation_euler=[1, 0, 0], **extra)


def light(**overrides):
    spec = {"kind": "light", "light_type": "AREA", "energy": 500,
            "color": [1, 1, 1], "location": [0, 0, 3]}
    spec.update(overrides)
    return request("studio_set_light", spec=spec)


def test_camera_light_and_world_gateway():
    bridge = Bridge()
    gate = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL))
    assert {"studio_set_camera", "studio_set_light"} <= visible_studio_tools(Profile.STUDIO_LOCAL)
    assert {"studio_set_camera", "studio_set_light"} <= gate.capabilities().operations
    assert "camera-framing-bounds-v1" in gate.capabilities().features
    cam = gate.invoke(camera(), "studio_set_camera")
    assert cam["status"] == "succeeded"
    assert len(cam["result"]["scene_diff"]["created"]) == 1
    assert cam["result"]["objects"][0]["kind"] == "camera"
    assert any(call[0] == "studio_set_camera_v1" for call in bridge.calls)
    lamp = gate.invoke(light(), "studio_set_light")
    assert lamp["status"] == "succeeded"
    assert lamp["result"]["objects"][0]["kind"] == "light"
    world = gate.invoke(request("studio_set_light",
                        spec={"kind": "world", "color": [0.1, 0.2, 0.3],
                              "strength": 0.7}), "studio_set_light")
    assert world["status"] == "succeeded"
    assert not world["result"]["objects"]


@pytest.mark.parametrize("patch", [
    {"lens_mm": 0}, {"lens_mm": 301}, {"clip_start": 2, "clip_end": 1},
    {"resolution_x": 1024}, {"target_id": str(uuid4()), "frame_scene": True},
    {"location": [float("inf"), 0, 0]}, {"location": [100001, 0, 0]},
])
def test_bad_camera_fails_before_dispatch(patch):
    raw = camera()
    raw.update(patch)
    bridge = Bridge()
    result = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL)).invoke(raw, "studio_set_camera")
    assert result["status"] == "failed"
    assert result["error"]["code"] == "invalid_input"
    assert not any(c[0] == "studio_set_camera_v1" for c in bridge.calls)


def test_frame_scene_and_missing_target_wires():
    for attrs in ({"frame_scene": True}, {"target_id": str(uuid4())}):
        raw = camera()
        raw.pop("location")
        raw.pop("rotation_euler")
        raw.update(attrs)
        assert validate_request(raw).operation == "studio_set_camera"


@pytest.mark.parametrize("specpatch", [
    {"energy": -1}, {"energy": 100001}, {"color": [2, 0, 0]},
    {"location": [float("nan"), 0, 0]}, {"location": [100001, 0, 0]},
    {"light_type": "LASER"}, {"size": 0},
])
def test_bad_light_fails_before_dispatch(specpatch):
    raw = light(**specpatch)
    bridge = Bridge()
    result = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL)).invoke(raw, "studio_set_light")
    assert result["status"] == "failed"
    assert not any(c[0] == "studio_set_light_v1" for c in bridge.calls)


def test_observer_denied():
    bridge = Bridge()
    gate = StudioGateway(bridge, Policy(Profile.OBSERVER))
    assert "studio_set_camera" not in visible_studio_tools(Profile.OBSERVER)
    result = gate.invoke(camera(), "studio_set_camera")
    assert result["error"]["code"] == "permission_denied"
    assert not any(c[0] == "studio_set_camera_v1" for c in bridge.calls)
