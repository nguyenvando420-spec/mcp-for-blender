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

# BAS-07 UAT: Blender GUI main-thread edits originating OUTSIDE the MCP
# request dispatcher. This is an automated simulated direct GUI user edit;
# it does not claim to be a human clicking through the editor.
def _bas07_direct_gui_edit():
    import json
    command_path = output_dir / "bas07-direct-edit.request.json"
    if not command_path.is_file():
        return 0.05
    response_path = output_dir / "bas07-direct-edit.result.json"
    try:
        request = json.loads(command_path.read_text())
        command_path.unlink()
        scene = bpy.context.scene
        before_revision = scene.get("_bas05_scene_revision")
        matches = [obj for obj in scene.objects
                   if obj.get("_bas05_object_id") == request.get("object_id")]
        if len(matches) != 1:
            raise RuntimeError("Expected one Studio UUID in GUI for edit")
        obj = matches[0]
        action = request["action"]
        if action == "translate":
            obj.location.x += 0.25
        elif action == "mesh_vertex":
            if obj.type != "MESH" or not len(obj.data.vertices):
                raise RuntimeError("GUI vertex edit requires a mesh")
            obj.data.vertices[0].co.x += 0.15
            obj.data.update()
        elif action == "material":
            mat = bpy.data.materials.new("BAS07_Manual_GUI_Material")
            mat.diffuse_color = (0.2, 0.5, 0.8, 1.0)
            obj.data.materials.append(mat)
        elif action == "camera":
            if scene.camera is None:
                raise RuntimeError("Scene lacks camera")
            scene.camera.data.lens += 5.0
        elif action == "rename":
            obj.name = "BAS07_Changed_Out_Of_Band"
        else:
            raise RuntimeError("Invalid GUI edit action")
        bpy.context.view_layer.update()
        after_revision = scene.get("_bas05_scene_revision")
        response_path.write_text(json.dumps({
            "action": action, "object_id": request["object_id"],
            "before_revision": before_revision,
            "after_revision": after_revision, "success": True}))
    except Exception as exc:
        response_path.write_text(json.dumps({"success": False,
                                              "error": type(exc).__name__,
                                              "message": str(exc)}))
    return 0.05


bpy.app.timers.register(_bas07_direct_gui_edit, first_interval=0.05)


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
