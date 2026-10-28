"""BAS-08 independent headless verifier for IDs written by GUI MCP."""
import sys
from pathlib import Path
import bpy

args = sys.argv[sys.argv.index("--") + 1:]
target = Path(args[0])
expected_id = args[1]
assert target.is_file()
bpy.ops.wm.open_mainfile(filepath=str(target), load_ui=False)
scene = bpy.context.scene
objects = [obj for obj in scene.objects if obj.name == "BAS08_ManualObject"]
assert len(objects) == 1, "Manually-created object missing from saved scene"
obj = objects[0]
assert obj.get("_bas05_object_id") == expected_id, "Object stable UUID changed on reopen"
assert obj.type == "MESH" and obj.data is not None
materials = [slot.material for slot in obj.material_slots if slot.material]
# BAS-10 may append a second PBR material; the BAS-08 original must
# remain present and every material must retain a distinct stable UUID.
assert materials and any(m.name == "BAS08_UntrackedMaterial" for m in materials)
assert all(m.get("_bas08_material_id") for m in materials), "Material UUID missing"
assert len({m["_bas08_material_id"] for m in materials}) == len(materials), "Duplicate material UUID"
collections = [scene.collection, *scene.collection.children_recursive]
assert all(col.get("_bas07_collection_id") for col in collections), "Collection UUID missing"
assert len({ob.get("_bas05_object_id") for ob in scene.objects}) == len(scene.objects), "Duplicate/missing object UUID"
print("BAS08_IDS_REOPEN_PASS", expected_id, materials[0]["_bas08_material_id"],
      len(collections), flush=True)
