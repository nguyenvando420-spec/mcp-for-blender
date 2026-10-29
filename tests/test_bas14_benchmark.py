"""BAS-14 scorer guardrails. Actual Blender prompt scores come ONLY from real runs."""
import importlib.util
import json
from pathlib import Path
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bas14_benchmark", ROOT / "scripts/bas14_benchmark.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
SUITE_FILE = ROOT / "benchmarks/bas14_suite.json"


def suite():
    return json.loads(SUITE_FILE.read_text(encoding="utf-8"))


def test_locked_case_inventory():
    value = suite()
    m.validate_suite(value)
    assert [c["id"] for c in value["cases"]] == [f"BAS14-{i:02d}" for i in range(1, 21)]
    assert sum(bool(c["expected"]["reopen"]) for c in value["cases"]) == 3
    assert value["cases"][19]["safety_rule"]["rollback_proof"] is True


def test_reject_suite_alterations():
    value = suite()
    value["cases"][0]["expected"]["artifacts"] = []
    with pytest.raises(ValueError):
        m.validate_suite(value)


def test_reject_missing_attempt_not_fake_pass():
    with tempfile.TemporaryDirectory() as directory:
        meta = dict(run_id="probe", client_name="Claude", client_version="test",
                    model_name="model", model_version="2026-10", blender_version="4.5.15",
                    source_commit="a" * 40, environment={"runner": "test"},
                    suite_sha256=m.suite_digest(SUITE_FILE), cases=[])
        result = m.grade(suite(), m.suite_digest(SUITE_FILE), meta, Path(directory))
        assert result["first_pass"] == 0
        assert result["safety_passed"] == 0
        assert not result["accepted"]


def test_retries_do_not_count_as_passes():
    with tempfile.TemporaryDirectory() as directory:
        meta = dict(run_id="probe", client_name="ChatGPT", client_version="test",
                    model_name="model", model_version="2026-10", blender_version="4.5.15",
                    source_commit="a" * 40, environment="Linux",
                    suite_sha256=m.suite_digest(SUITE_FILE),
                    cases=[{"id": "BAS14-01", "attempts": [
                        {"attempt": 1, "status": "failed"},
                        {"attempt": 2, "status": "succeeded", "visual_scores": {axis: 5 for axis in m.AXES}}
                    ]}])
        result = m.grade(suite(), m.suite_digest(SUITE_FILE), meta, Path(directory))
        assert result["first_pass"] == 0
        assert result["retry_count"] == 1
        assert not result["accepted"]


def test_reject_path_traversal_and_symlink(tmp_path):
    (tmp_path / "safe.log").write_text("hello", encoding="utf-8")
    assert m.safe_read(tmp_path, "safe.log") == b"hello"
    with pytest.raises(ValueError):
        m.safe_read(tmp_path, "../outside.log")
    (tmp_path / "link.log").symlink_to(tmp_path / "safe.log")
    with pytest.raises(ValueError):
        m.safe_read(tmp_path, "link.log")


def test_tampered_suite_digest_fails():
    value = dict(run_id="x", client_name="x", client_version="x", model_name="x",
                 model_version="x", blender_version="4.5.15", source_commit="a" * 40,
                 environment="Linux", suite_sha256="0" * 64, cases=[])
    with pytest.raises(ValueError, match="suite SHA mismatch"):
        m.grade(suite(), m.suite_digest(SUITE_FILE), value, ROOT)


def test_reject_incomplete_scene_or_missing_camera():
    with pytest.raises(ValueError):
        m.scene_from_mcp(ROOT, "../fixture.json")
    expected = {"objects": [{"name": "Cube", "type": "MESH", "collection": "A"}],
                "camera": "Cam"}
    observed = {"objects": [{"name": "Cube", "type": "MESH", "collections": ["A"]}]}
    assert "camera" in m.check_scene(expected, observed)[0]



def test_reject_nested_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "session.log").write_text("session", encoding="utf-8")
    (tmp_path / "shortcut").symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        m.safe_read(tmp_path, "shortcut/session.log")


def test_raw_mcp_collection_records_are_scored(tmp_path):
    # Shape emitted by real BAS-08 studio_scene_inspect, including unbound IDs.
    observed = {"objects": [{"name": "Cube", "type": "MESH", "collections": [
        {"name": "Collection", "collection_id": None}]}], "has_more": False}
    (tmp_path / "scene.json").write_text(json.dumps({"status": "succeeded",
        "result": {"summary": json.dumps(observed)}}), encoding="utf-8")
    expected = {"objects": [{"name": "Cube", "type": "MESH",
                              "collection": "Collection"}], "camera": None}
    assert m.check_scene(expected, m.scene_from_mcp(tmp_path, "scene.json")) == []
    observed["objects"][0]["collections"][0]["name"] = "Wrong"
    assert m.check_scene(expected, observed) == ["wrong collection Cube"]


@pytest.mark.parametrize("collections", [[], None, "Collection", [{}], [None]])
def test_missing_collection_records_cannot_pass(collections):
    expected = {"objects": [{"name": "Cube", "type": "MESH",
                              "collection": "Collection"}], "camera": None}
    observed = {"objects": [{"name": "Cube", "type": "MESH",
                             "collections": collections}]}
    assert m.check_scene(expected, observed) == ["wrong collection Cube"]


def test_reject_oversized_evidence_without_reading(tmp_path):
    path = tmp_path / "untrusted.blend"
    with path.open("wb") as handle:
        handle.truncate(512 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="512 MiB"):
        m.safe_read(tmp_path, "untrusted.blend")


def test_malformed_png_must_fail():
    assert not m.validate_png(b"not a PNG")
    # CRC/stream corruption cannot be accepted from a magic-byte-only image.
    assert not m.validate_png(m.PNG_MAGIC + b"junk")


def test_attempt_rows_must_be_objects(tmp_path):
    meta = dict(run_id="unit", client_name="Claude", client_version="test",
                model_name="model", model_version="test", blender_version="4.5.15",
                source_commit="a" * 40, environment={"runner": "test"},
                suite_sha256=m.suite_digest(SUITE_FILE),
                cases=[{"id": "BAS14-01", "attempts": ["not a dict"]}])
    result = m.grade(suite(), m.suite_digest(SUITE_FILE), meta, tmp_path)
    assert not result["accepted"]
    assert result["first_pass"] == 0


def test_overall_threshold_cannot_override_missing_safety(monkeypatch, tmp_path):
    # This is a scorer-only unit fixture, not a real Blender benchmark.
    monkeypatch.setattr(m, "grade_attempt", lambda root, case, attempt: [])
    run = dict(run_id="unit", client_name="Claude", client_version="test",
               model_name="model", model_version="test", blender_version="4.5.15",
               source_commit="a" * 40, environment={"runner": "test"},
               suite_sha256=m.suite_digest(SUITE_FILE),
               cases=[{"id": f"BAS14-{i:02d}", "attempts": [{"attempt": 1}]}
                      for i in range(1, 20)])
    result = m.grade(suite(), m.suite_digest(SUITE_FILE), run, tmp_path)
    assert result["first_pass"] == 19
    assert not result["safety_gate"]
    assert not result["accepted"]
