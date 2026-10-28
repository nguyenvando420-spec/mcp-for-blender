"""BAS-10 typed material/geometry gateway security tests (no Blender required)."""
from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from blender_mcp.security_policy import Policy, Profile
from blender_mcp.studio.registry import StudioGateway, visible_studio_tools

SID = str(uuid4())
OID = str(uuid4())


class MaterialBridge:
    def __init__(self):
        self.calls = []
        self.rev = 0

    def __call__(self, operation, params, read_only):
        self.calls.append((operation, params, read_only))
        if operation == "get_addon_info":
            return {"protocol_version": 16, "capabilities": ["studio_assign_material_v1"]}
        assert operation == "studio_assign_material_v1"
        assert read_only is False
        if params["scene_revision"] != self.rev:
            raise ValueError("stale_scene: revision")
        if params["object_id"] != OID:
            raise ValueError("object_not_found: invalid id")
        self.rev += 1
        return {"scene_id": SID, "scene_revision": self.rev,
                "object_id": OID, "name": "Cube",
                "material_id": str(uuid4())}


def raw(spec, **extra):
    return {"operation": "studio_assign_material",
            "request_id": str(uuid4()), "effect_scope": "scene",
            "scene_id": SID, "scene_revision": 0,
            "idempotency_key": "bas10", "object_id": OID,
            "spec": spec, **extra}


def pbr(**kwargs):
    return {"kind": "pbr", "base_color": [0.1, 0.2, 0.3, 1.0],
            "metallic": 0.2, "roughness": 0.4, **kwargs}


@pytest.mark.parametrize("spec", [
    pbr(), {"kind": "bevel", "width": 0.15, "segments": 3},
    {"kind": "subdivision", "levels": 2},
    {"kind": "geometry_nodes", "preset": "linear_array",
     "count": 5, "offset": [2, 0, 0]},
])
def test_dispatch_typed_operations(spec):
    bridge = MaterialBridge()
    gateway = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL))
    assert "studio_assign_material" in gateway.handshake()["operations"]
    result = gateway.invoke(raw(spec), "studio_assign_material")
    assert result["status"] == "succeeded", result
    assert result["scene_revision"] == 1
    assert result["result"]["scene_diff"]["updated"] == [OID]
    assert bridge.calls[-1][1]["spec"]["kind"] == spec["kind"]


@pytest.mark.parametrize("spec", [
    pbr(metallic=1.1), pbr(base_color=[1, 0, 0]),
    pbr(texture_uri="file:///tmp/a.png"),
    {"kind": "bevel", "width": -1, "segments": 2},
    {"kind": "subdivision", "levels": 10},
    {"kind": "geometry_nodes", "preset": "execute_python", "count": 3, "offset": [1, 0, 0]},
    {"kind": "geometry_nodes", "preset": "linear_array", "count": 101, "offset": [1, 0, 0]},
    {"kind": "geometry_nodes", "preset": "linear_array", "count": 2, "offset": [1001, 0, 0]},
])
def test_invalid_spec_never_mutates(spec):
    bridge = MaterialBridge()
    result = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL)).invoke(raw(spec), "studio_assign_material")
    assert result["status"] == "failed", result
    assert result["error"]["code"] == "invalid_input", result
    assert not any(cmd == "studio_assign_material_v1" for cmd, _, _ in bridge.calls)


def test_observer_hidden_and_forbidden():
    assert "studio_assign_material" not in visible_studio_tools(Profile.OBSERVER)
    bridge = MaterialBridge()
    result = StudioGateway(bridge, Policy(Profile.OBSERVER)).invoke(raw(pbr()), "studio_assign_material")
    assert result["error"]["code"] == "permission_denied"
    assert not bridge.calls


def test_bad_object_or_stale_revision():
    bridge = MaterialBridge()
    gateway = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL))
    first = gateway.invoke(raw(pbr(), object_id=str(uuid4())), "studio_assign_material")
    assert first["error"]["code"] == "object_not_found"
    stale = gateway.invoke(raw(pbr(), scene_revision=2), "studio_assign_material")
    assert stale["error"]["code"] == "stale_scene"


def test_offline_texture_allowed_root_and_checksum(tmp_path):
    path = tmp_path / "asset.png"
    contents = b"synthetic-test-data"
    path.write_bytes(contents)
    spec = pbr(texture_uri=path.as_uri(), texture_sha256=hashlib.sha256(contents).hexdigest())
    bridge = MaterialBridge()
    gateway = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL, read_roots=(tmp_path,)))
    assert gateway.invoke(raw(spec), "studio_assign_material")["status"] == "succeeded"
    assert bridge.calls[-1][1]["spec"]["texture_uri"] == path.as_uri()


@pytest.mark.parametrize("failure", ["missing", "hash", "outside", "web", "symlink"])
def test_texture_fail_closed(tmp_path, failure):
    root = tmp_path / "root"
    root.mkdir()
    asset = root / "a.png"
    asset.write_bytes(b"data")
    target = asset
    if failure == "missing":
        target = root / "missing.png"
    elif failure == "outside":
        target = tmp_path / "other.png"
        target.write_bytes(b"data")
    elif failure == "symlink":
        target = root / "symlink.png"
        target.symlink_to(asset)
    uri = "https://example.com/a.png" if failure == "web" else target.as_uri()
    digest = hashlib.sha256(b"bad" if failure == "hash" else b"data").hexdigest()
    bridge = MaterialBridge()
    gateway = StudioGateway(bridge, Policy(Profile.STUDIO_LOCAL, read_roots=(root,)))
    result = gateway.invoke(raw(pbr(texture_uri=uri, texture_sha256=digest)), "studio_assign_material")
    assert result["status"] == "failed"
    assert result["error"]["code"] == "permission_denied"
    assert not any(cmd == "studio_assign_material_v1" for cmd, _, _ in bridge.calls)
