import importlib.util
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

spec = importlib.util.spec_from_file_location(
    "scene_observe",
    Path(__file__).resolve().parents[1] / "src/blender_mcp/scene_observe.py",
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class FakeObject:
    def __init__(self, name, object_id=None):
        self.name = name
        self.type = "EMPTY"
        self.location = (0, 0, 0)
        self.rotation_euler = (0, 0, 0)
        self.scale = (1, 1, 1)
        self.users_collection = []
        self.material_slots = []
        self.props = {}
        if object_id:
            self.props[mod.ID_KEY] = object_id

    def get(self, key):
        return self.props.get(key)

    def __setitem__(self, key, value):
        self.props[key] = value

    def select_get(self):
        return False


def test_empty_scene_and_validation():
    scene = SimpleNamespace(name="Empty", objects=[])
    assert mod.observe_scene(scene)["total"] == 0
    with pytest.raises(ValueError):
        mod.observe_scene(scene, limit=501)


def test_paging_does_not_hide_id_lookup():
    target_id = str(uuid.uuid4())
    objects = [FakeObject(f"Object-{i:03d}") for i in range(250)]
    objects[-1].props[mod.ID_KEY] = target_id
    scene = SimpleNamespace(name="Paged", objects=objects)
    result = mod.observe_scene(scene, limit=20)
    assert result["has_more"] and result["total"] == 250
    match = mod.observe_scene(scene, limit=1, object_id=target_id)
    assert match["objects"][0]["name"] == "Object-249"
    objects[-1].name = "Renamed"
    assert mod.observe_scene(scene, object_id=target_id)["objects"][0]["name"] == "Renamed"
    with pytest.raises(LookupError):
        mod.observe_scene(scene, object_id=str(uuid.uuid4()))


def test_duplicate_uuid_requires_explicit_repair():
    duplicate = str(uuid.uuid4())
    a, b = FakeObject("A", duplicate), FakeObject("B", duplicate)
    scene = SimpleNamespace(name="Duplicates", objects=[a, b])
    read = mod.observe_scene(scene)
    assert read["objects"][0]["id"] == duplicate
    assert read["objects"][1]["id"] is None
    assert a.get(mod.ID_KEY) == b.get(mod.ID_KEY)
    repaired = mod.observe_scene(scene, assign_ids=True)
    assert len({x["id"] for x in repaired["objects"]}) == 2


def test_readonly_digest_stable():
    scene = SimpleNamespace(name="One", objects=[FakeObject("A")])
    a = mod.observe_scene(scene)
    assert mod.state_digest(a) == mod.state_digest(mod.observe_scene(scene))
    scene.objects[0].name = "B"
    assert mod.state_digest(a) != mod.state_digest(mod.observe_scene(scene))
