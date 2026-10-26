"""Create a minimal Blender scene and render a CPU PNG + .blend.

Run with:
  blender --background --factory-startup --python scripts/blender_headless_smoke.py -- --output-dir /tmp/blender-baseline

This file is executed by Blender's Python interpreter, not by system Python.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 1))
    cube = bpy.context.object
    cube.name = "BAS01_BaselineCube"

    bpy.ops.object.camera_add(location=(5, -7, 5))
    camera = bpy.context.object
    target = Vector((0, 0, 1))
    camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()

    bpy.ops.object.light_add(type="AREA", location=(2, -3, 6))
    bpy.context.object.data.energy = 800

    scene = bpy.context.scene
    scene.camera = camera
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 8
    # Ubuntu's Blender packages can be built without OpenImageDenoiser.
    # Disable Cycles per-view-layer denoising for portable CPU headless CI.
    for view_layer in scene.view_layers:
        if hasattr(view_layer, "cycles"):
            view_layer.cycles.use_denoising = False
    scene.render.resolution_x = 320
    scene.render.resolution_y = 180
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(output_dir / "preview.png")

    project_path = output_dir / "baseline.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(project_path))
    bpy.ops.render.render(write_still=True)

    preview = output_dir / "preview.png"
    assert project_path.is_file() and project_path.stat().st_size > 0
    assert preview.is_file() and preview.stat().st_size > 0
    print("BAS01_SMOKE_OK " + json.dumps({
        "blender_version": bpy.app.version_string,
        "blend_file": str(project_path),
        "preview": str(preview),
        "objects": len(scene.objects),
        "render_engine": scene.render.engine,
    }))


if __name__ == "__main__":
    main()
