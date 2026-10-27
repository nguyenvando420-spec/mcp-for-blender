"""Start the real Blender GUI add-on for BAS-05 MCP client acceptance on Xvfb."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

import bpy

args = sys.argv[sys.argv.index("--") + 1:]
port, done_file, output_dir = int(args[0]), Path(args[1]), Path(args[2])
root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bas05_fork_addon", root / "addon.py")
addon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(addon)
addon.register()
# Avoid the normal delayed auto-start creating a duplicate instance/port.
bpy.context.scene.blendermcp_auto_start_server = False
server = addon.BlenderMCPServer(host="127.0.0.1", port=port)
server.start()
heartbeat = {"count": 0, "last": None, "max_gap_seconds": 0.0}
def _bas06_heartbeat():
    import time
    now = time.monotonic()
    if heartbeat["last"] is not None:
        heartbeat["max_gap_seconds"] = max(
            heartbeat["max_gap_seconds"], now - heartbeat["last"])
    heartbeat["last"] = now
    heartbeat["count"] += 1
    return 0.1
bpy.app.timers.register(_bas06_heartbeat, first_interval=0.1)
if not server.running:
    raise RuntimeError("BAS05 real Blender GUI bridge did not start")
bpy.types.blendermcp_server = server

# BAS-12 release acceptance golden material/camera scene state.
# This is a disposable CI workspace, never a live user project.
import json

golden = bpy.context.scene
golden_camera = golden.camera
golden_cube = golden.objects.get("Cube")
if golden_camera is None or golden_cube is None or golden_cube.type != "MESH":
    raise RuntimeError("Golden startup scene lacks Cube mesh or Camera")
golden_camera.data.lens = 41.25
golden_mat = bpy.data.materials.new("BAS12_Golden_Material")
golden_mat["_bas12_golden"] = True
golden_mat.use_nodes = True
golden_mat.diffuse_color = (0.125, 0.25, 0.5, 1.0)
shader = golden_mat.node_tree.nodes.get("Principled BSDF")
if shader is None:
    raise RuntimeError("Golden material missing Principled BSDF")
shader.inputs["Base Color"].default_value = (0.125, 0.25, 0.5, 1.0)
shader.inputs["Metallic"].default_value = 0.22
shader.inputs["Roughness"].default_value = 0.41
golden_cube.data.materials.clear()
golden_cube.data.materials.append(golden_mat)


def _bas12_golden_probe():
    scene = bpy.context.scene
    materials = [mat for ob in scene.objects if ob.type == "MESH" and ob.data
                 for mat in ob.data.materials if mat and mat.get("_bas12_golden")]
    if len(materials) != 1 or scene.camera is None:
        raise RuntimeError("BAS-12 golden material or camera missing")
    mat = materials[0]
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    return {
        "lens": float(scene.camera.data.lens),
        "diffuse": [float(v) for v in mat.diffuse_color],
        "base_color": [float(v) for v in bsdf.inputs["Base Color"].default_value],
        "metallic": float(bsdf.inputs["Metallic"].default_value),
        "roughness": float(bsdf.inputs["Roughness"].default_value),
        "scene_id": scene.get("_bas05_scene_id"),
        "revision": scene.get("_bas05_scene_revision"),
    }


def _bas12_golden_gui_probe_timer():
    command_path = output_dir / "bas12-golden-edit.request.json"
    if not command_path.exists():
        return 0.05
    result_path = output_dir / "bas12-golden-edit.result.json"
    try:
        command = json.loads(command_path.read_text())
        command_path.unlink()
        scene = bpy.context.scene
        revision_before = scene.get("_bas05_scene_revision")
        if command.get("action") == "mutate":
            matches = [mat for ob in scene.objects if ob.type == "MESH"
                       for mat in ob.data.materials if mat and mat.get("_bas12_golden")]
            if len(matches) != 1:
                raise RuntimeError("Cannot locate unique golden material")
            mat = matches[0]
            scene.camera.data.lens = 92.75
            mat.diffuse_color = (0.9, 0.2, 0.1, 1.0)
            bsdf = mat.node_tree.nodes.get("Principled BSDF")
            bsdf.inputs["Base Color"].default_value = (0.8, 0.3, 0.2, 1.0)
            bsdf.inputs["Metallic"].default_value = 0.87
            bsdf.inputs["Roughness"].default_value = 0.95
            bpy.context.view_layer.update()
        elif command.get("action") != "inspect":
            raise ValueError("Unknown BAS-12 golden GUI test action")
        state = _bas12_golden_probe()
        if state["revision"] != revision_before:
            raise AssertionError("Untracked direct GUI mutation unexpectedly advanced MCP revision")
        result_path.write_text(json.dumps({"ok": True, "action": command["action"],
                                           "state": state}))
    except Exception as exc:
        result_path.write_text(json.dumps({"ok": False, "error": type(exc).__name__,
                                           "message": str(exc)}))
    return 0.05


bpy.app.timers.register(_bas12_golden_gui_probe_timer, first_interval=0.05)


def finish_when_requested():
    if not done_file.exists():
        return 0.2
    try:
        # Small deterministic CPU scene for the BAS-06 subprocess render job.
        scene = bpy.context.scene
        scene.render.engine = "CYCLES"
        scene.cycles.samples = 8
        scene.render.resolution_x = 320
        scene.render.resolution_y = 240
        scene.render.resolution_percentage = 100
        result = bpy.ops.wm.save_as_mainfile(filepath=str(output_dir / "bas05-scene.blend"))
        import json
        (output_dir / "bas06-heartbeat.json").write_text(json.dumps({
            "count": heartbeat["count"],
            "max_gap_seconds": round(heartbeat["max_gap_seconds"], 4)
        }, indent=2) + "\n")
        print("BAS05_GUI_SAVE", result, flush=True)
        server.stop()
        bpy.ops.wm.quit_blender()
    except Exception as exc:
        print("BAS05_GUI_FINISH_ERROR", type(exc).__name__, str(exc), flush=True)
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(finish_when_requested, first_interval=0.2)
print("BAS05_GUI_READY port=" + str(port), flush=True)
