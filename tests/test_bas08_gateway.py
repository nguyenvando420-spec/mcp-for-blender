"""BAS-08: typed gateway capture proof, error cases and strict PNG validation."""
import base64
import hashlib
import struct
import zlib
import pytest
from uuid import uuid4

from blender_mcp.security_policy import Policy, Profile
from blender_mcp.studio.registry import StudioGateway

SCENE = str(uuid4())


def png(width=1, height=1):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data +
                struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    pixels = b"\x00" + bytes([255, 0, 0, 255]) * width
    return (bytes.fromhex("89504e470d0a1a0a") +
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(pixels * height)) + chunk(b"IEND", b""))


class CaptureBridge:
    def __init__(self, *, broken=None):
        self.calls = []
        self.broken = broken

    def __call__(self, operation, params, read_only):
        self.calls.append((operation, params, read_only))
        if operation == "get_addon_info":
            return {"protocol_version": 16, "capabilities": ["studio_capture_viewport_v1"]}
        assert operation == "studio_capture_viewport_v1"
        assert read_only is True
        if self.broken == "no-viewport":
            raise Exception("capability_unavailable: open a 3D viewport")
        if self.broken == "stale":
            return {"scene_id": SCENE, "scene_revision": 2, "capture": {}}
        picture = png()
        return {"scene_id": SCENE, "scene_revision": 1,
                "capture": {
                    "mode": "viewport", "source": "offscreen", "mime_type": "image/png",
                    "width": 1 if self.broken != "dimensions" else 2,
                    "height": 1,
                    "image_base64": base64.b64encode(picture).decode("ascii"),
                    "sha256": hashlib.sha256(picture).hexdigest(),
                    "scene_fingerprint": "a" * 64,
                }}


def request():
    return {"operation": "studio_capture_viewport", "request_id": str(uuid4()),
            "effect_scope": "read", "scene_id": SCENE, "scene_revision": 1,
            "max_size": 320, "mode": "viewport"}


def test_valid_capture_and_observer_only_read_dispatch():
    bridge = CaptureBridge()
    result = StudioGateway(bridge, Policy(Profile.OBSERVER)).invoke(
        request(), "studio_capture_viewport")
    assert result["status"] == "succeeded", result
    cap = result["result"]["capture"]
    assert base64.b64decode(cap["image_base64"]) == png()
    assert bridge.calls[-1] == ("studio_capture_viewport_v1", {
        "max_size": 320, "mode": "viewport", "scene_id": SCENE, "scene_revision": 1}, True)


def test_capture_failure_is_actionable():
    result = StudioGateway(CaptureBridge(broken="no-viewport"), Policy(Profile.OBSERVER)).invoke(
        request(), "studio_capture_viewport")
    assert result["status"] == "failed"
    assert result["error"]["code"] == "capability_unavailable"
    assert "3D Viewport" in result["error"]["message"]


def test_capture_stale_context_fails_closed():
    result = StudioGateway(CaptureBridge(broken="stale"), Policy(Profile.OBSERVER)).invoke(
        request(), "studio_capture_viewport")
    assert result["error"]["code"] == "stale_scene"


def test_capture_rejects_wrong_png_dimensions():
    result = StudioGateway(CaptureBridge(broken="dimensions"), Policy(Profile.OBSERVER)).invoke(
        request(), "studio_capture_viewport")
    assert result["status"] == "failed"
    assert result["error"]["code"] == "invalid_input"


class IdBindingBridge:
    """Spy on the mutation/read-only boundary and revision update."""
    def __init__(self):
        self.calls = []

    def __call__(self, command, params, read_only):
        self.calls.append((command, params, read_only))
        if command == "get_addon_info":
            return {"protocol_version": 16,
                    "capabilities": ["studio_scene_inspect_v1", "studio_bind_ids_v1"]}
        if command == "studio_scene_inspect_v1":
            if params["bind_missing_ids"]:
                assert not read_only
                assert params["scene_id"] == SCENE and params["scene_revision"] == 1
                return {"scene_id": SCENE, "scene_revision": 2,
                        "ids_bound": 3, "objects": [], "scene_fingerprint": "a" * 64}
            assert read_only
            return {"scene_id": SCENE, "scene_revision": 1, "objects": []}
        raise AssertionError(command)


def test_binding_explicit_mutation_with_typed_gateway():
    bridge = IdBindingBridge()
    env = {"operation": "studio_scene_inspect", "request_id": str(uuid4()),
           "effect_scope": "scene", "scene_id": SCENE, "scene_revision": 1,
           "idempotency_key": "bind:initial", "bind_missing_ids": True}
    gateway = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL))
    resp = gateway.invoke(env, "studio_scene_inspect")
    assert resp["status"] == "succeeded", resp
    assert resp["scene_revision"] == 2
    import json
    assert json.loads(resp["result"]["summary"])["ids_bound"] == 3
    assert bridge.calls[-1][2] is False


def test_observer_cannot_request_mutating_inspection():
    bridge = IdBindingBridge()
    env = {"operation": "studio_scene_inspect", "request_id": str(uuid4()),
           "effect_scope": "scene", "scene_id": SCENE, "scene_revision": 1,
           "idempotency_key": "bind:forged", "bind_missing_ids": True}
    denied = StudioGateway(bridge, Policy(Profile.OBSERVER)).invoke(env, "studio_scene_inspect")
    assert denied["error"]["code"] == "permission_denied"
    assert not bridge.calls


def test_bind_requires_context_and_no_filter():
    from blender_mcp.studio.contracts import validate_request
    from pydantic import ValidationError
    base = {"operation": "studio_scene_inspect", "request_id": str(uuid4()),
            "effect_scope": "scene", "scene_id": SCENE, "scene_revision": 1,
            "idempotency_key": "bind", "bind_missing_ids": True}
    for patch in [
        {"effect_scope": "read"},
        {"scene_id": None, "scene_revision": None},
        {"idempotency_key": None},
        {"query": "Cube"},
        {"offset": 1},
        {"object_id": str(uuid4())},
    ]:
        with pytest.raises(ValidationError):
            validate_request({**base, **patch})
    with pytest.raises(ValidationError):
        validate_request({**base, "bind_missing_ids": False})

def test_old_addon_with_inspect_only_cannot_receive_binding():
    class Legacy(IdBindingBridge):
        def __call__(self, command, params, read_only):
            if command == "get_addon_info":
                self.calls.append((command, params, read_only))
                return {"protocol_version": 16, "capabilities": ["studio_scene_inspect_v1"]}
            raise AssertionError("Legacy addon must not receive bind params")
    bridge = Legacy()
    data = {"operation": "studio_scene_inspect", "request_id": str(uuid4()),
            "effect_scope": "scene", "scene_id": SCENE, "scene_revision": 1,
            "idempotency_key": "bas08-bind", "bind_missing_ids": True}
    response = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL)).invoke(
        data, "studio_scene_inspect")
    assert response["status"] == "failed"
    assert response["error"]["code"] == "capability_unavailable"
    assert [call[0] for call in bridge.calls] == ["get_addon_info"]
