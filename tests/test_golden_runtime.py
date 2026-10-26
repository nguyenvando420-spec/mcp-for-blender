"""Evidence corruption and installer provenance gates for BAS-02."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

SPEC = importlib.util.spec_from_file_location("run_golden", Path(__file__).parents[1] / "scripts/run_golden.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def bundle(tmp_path):
    for name in ("baseline.blend", "preview.png", "render.log", "reopen.log", "blender-environment.json"):
        (tmp_path / name).write_bytes(b"synthetic evidence")
    (tmp_path / "validation.json").write_text('{"passed": true}')
    manifest = {"schema_version": 1,
                "files": {p.name: runtime.digest(p) for p in tmp_path.iterdir()},
                "validation": {"passed": True}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_bundle_detects_corruption(tmp_path):
    expected = bundle(tmp_path)
    assert runtime.verify_bundle(tmp_path) == expected
    (tmp_path / "baseline.blend").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="corrupted"):
        runtime.verify_bundle(tmp_path)


def test_bundle_rejects_path_escape(tmp_path):
    manifest = bundle(tmp_path)
    manifest["files"]["../outside"] = "a" * 64
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Invalid evidence path"):
        runtime.verify_bundle(tmp_path)


def test_bundle_rejects_incomplete_evidence(tmp_path):
    manifest = bundle(tmp_path)
    del manifest["files"]["preview.png"]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Incomplete"):
        runtime.verify_bundle(tmp_path)


def test_download_checksum_mismatch_never_extracts(tmp_path):
    from io import BytesIO
    config = {"artifacts": {"Linux-x86_64": {
        "filename": "test.tar.xz", "sha256": "0" * 64,
        "urls": ["https://example.invalid/test"], "executable": "blender",
    }}}
    with patch.object(runtime.platform, "system", return_value="Linux"), \
         patch.object(runtime.platform, "machine", return_value="x86_64"), \
         patch.object(runtime, "urlopen", return_value=BytesIO(b"wrong archive")), \
         patch.object(runtime.tarfile, "open") as extract:
        with pytest.raises(RuntimeError, match="All pinned download sources failed"):
            runtime.install(config, tmp_path)
        extract.assert_not_called()
    assert not list(tmp_path.glob("*.part"))
