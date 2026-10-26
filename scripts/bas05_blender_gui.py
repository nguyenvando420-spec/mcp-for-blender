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
if not server.running:
    raise RuntimeError("BAS05 real Blender GUI bridge did not start")
bpy.types.blendermcp_server = server


def finish_when_requested():
    if not done_file.exists():
        return 0.2
    try:
        result = bpy.ops.wm.save_as_mainfile(filepath=str(output_dir / "bas05-scene.blend"))
        print("BAS05_GUI_SAVE", result, flush=True)
        server.stop()
        bpy.ops.wm.quit_blender()
    except Exception as exc:
        print("BAS05_GUI_FINISH_ERROR", type(exc).__name__, str(exc), flush=True)
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(finish_when_requested, first_interval=0.2)
print("BAS05_GUI_READY port=" + str(port), flush=True)
