"""BAS-12 reopen verification in a disposable separate Blender process."""
import sys
from pathlib import Path
import bpy

args = sys.argv[sys.argv.index("--") + 1:]
target = Path(args[0])
expected_scene_id = args[1]
revision = int(args[2])
assert target.is_file()
bpy.ops.wm.open_mainfile(filepath=str(target), load_ui=False)
scene = bpy.context.scene
assert scene.get("_bas05_scene_id") == expected_scene_id, (scene.get("_bas05_scene_id"), expected_scene_id)
assert int(scene.get("_bas05_scene_revision", -1)) == revision
assert len(scene.objects) >= 51, len(scene.objects)
assert scene.camera is not None and scene.camera.name in scene.objects
assert bpy.data.worlds is not None
assert not any(o.name == "BAS12_Throwaway" for o in scene.objects)
assert any(obj.name == "BAS05_Cube" and obj.get("_bas05_object_id")
           for obj in scene.objects)
# Validate exact persisted golden camera/material values (Blender float tolerance).
assert abs(float(scene.camera.data.lens) - 41.25) < 1e-4, scene.camera.data.lens
materials = [mat for ob in scene.objects if ob.type == "MESH" and ob.data
             for mat in ob.data.materials if mat and mat.get("_bas12_golden")]
assert len(materials) == 1, "golden material must survive in selected scene"
mat = materials[0]
shader = mat.node_tree.nodes.get("Principled BSDF")
assert shader is not None
def nearly(values, expected):
    return len(values) == len(expected) and all(
        abs(float(x) - float(y)) < 1e-4 for x, y in zip(values, expected))
expected_rgba = (0.125, 0.25, 0.5, 1.0)
assert nearly(mat.diffuse_color, expected_rgba), list(mat.diffuse_color)
assert nearly(shader.inputs["Base Color"].default_value, expected_rgba)
assert abs(float(shader.inputs["Metallic"].default_value) - 0.22) < 1e-4
assert abs(float(shader.inputs["Roughness"].default_value) - 0.41) < 1e-4
print("BAS12_MATERIAL_CAMERA_EXACT_PASS", scene.camera.data.lens,
      list(mat.diffuse_color), flush=True)
print("BAS12_REOPEN_PASS", len(scene.objects), expected_scene_id, revision, flush=True)
