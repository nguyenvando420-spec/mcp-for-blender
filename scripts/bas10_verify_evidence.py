"""Validate BAS-10 BLEND/PNG evidence checksums and recorded reopen/preview results."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct


def verify(folder: Path) -> dict:
    expected_files = {
        "bas10.blend", "bas10-preview.png", "bas10-before.json",
        "bas10-reopen.json", "bas10-environment.json",
        "bas10-source-archived.png", "bas10-preview-analysis.json",
    }
    recorded = json.loads((folder / "bas10-checksums.json").read_text())
    assert set(recorded) == expected_files, "Missing/unexpected checksum entries"
    for name, digest in recorded.items():
        assert "/" not in name and "\\" not in name and ".." not in name
        blob = (folder / name).read_bytes()
        assert hashlib.sha256(blob).hexdigest() == digest, name
    blend = folder / "bas10.blend"
    assert blend.stat().st_size > 10000, "BLEND missing/empty"
    png = (folder / "bas10-preview.png").read_bytes()
    assert png[:8] == bytes.fromhex("89504e470d0a1a0a")
    assert struct.unpack(">II", png[16:24]) == (480, 270)
    assert len(png) > 4000
    analysis = json.loads((folder / "bas10-preview-analysis.json").read_text())
    assert analysis["resolution"] == [480, 270]
    assert all(analysis["visible_color_samples"][c] > 5
               for c in ("red", "green", "blue"))
    reopening = json.loads((folder / "bas10-reopen.json").read_text())
    assert reopening["passed"] is True
    before = json.loads((folder / "bas10-before.json").read_text())
    assert reopening["values"] == before
    assert set(before) == {"BAS10_Red", "BAS10_Blue", "BAS10_Green"}
    assert len({tuple(v["color"]) for v in before.values()}) == 3
    env = json.loads((folder / "bas10-environment.json").read_text())
    assert env["blender"].startswith("4.5.15")
    assert env["texture_license"] == "self-generated-test-image"
    assert not (folder / "bas10-local.png").exists(), "Texture source was not detached"
    return {"passed": True, "blender_version": env["blender"],
            "blend_sha256": recorded["bas10.blend"],
            "png_sha256": recorded["bas10-preview.png"],
            "preview_resolution": analysis["resolution"],
            "colors_visible": analysis["visible_color_samples"],
            "packed_texture_reopen": True,
            "verified_files": len(expected_files)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.output_dir.resolve())
    print("BAS10_EVIDENCE_VERIFIED", json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
