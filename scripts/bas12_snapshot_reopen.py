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
print("BAS12_REOPEN_PASS", len(scene.objects), expected_scene_id, revision, flush=True)
