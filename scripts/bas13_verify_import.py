"""Independent Blender GLB import and BLEND reopen acceptance checks for BAS-13."""
import sys
from pathlib import Path
import bpy

args = sys.argv[sys.argv.index("--")+1:]
output = Path(args[0])
target_uuid = args[1]

def check_mesh(obj):
    assert obj.type == "MESH" and len(obj.data.vertices) >= 8, obj.name
    assert obj.data.materials, "Expected retained PBR material"
    material = obj.data.materials[0]
    assert material is not None and material.use_nodes
    pbr = next((node for node in material.node_tree.nodes
                if node.type == "BSDF_PRINCIPLED"), None)
    assert pbr is not None
    color = pbr.inputs["Base Color"].default_value
    assert color[0] > .5 and color[1] > .1 and color[2] > .03, tuple(color)

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=str(output / "scene.glb"))
meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
assert meshes, "No geometry imported from GLB"
check_mesh(meshes[0])
bpy.ops.wm.open_mainfile(filepath=str(output / "scene.blend"))
scene = bpy.context.scene
matched = [o for o in scene.objects if o.get("_bas05_object_id") == target_uuid]
assert len(matched) == 1
check_mesh(matched[0])
assert scene.camera is not None and scene.camera.type == "CAMERA"
assert scene.world is not None and scene.world.use_nodes
print("BAS13_GLB_REIMPORT_BLEND_REOPEN_PASS", len(meshes), flush=True)
