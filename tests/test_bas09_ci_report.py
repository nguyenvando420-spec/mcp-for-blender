"""BAS-09 manifest fail-closed tests independent of Blender."""
import json
import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / 'scripts' / 'bas09_ci_report.py'
spec = importlib.util.spec_from_file_location('bas09_ci_report', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
classify, skips, gui = module.classify, module.skips, module.gui


def test_skip_categories():
    assert classify("requires bpy") == "blender-or-display-dependent"
    assert classify("optional component not installed") == "optional-dependency"
    assert classify("Windows only") == "platform-specific"
    assert classify("unrecognized") == "manual-triage"


def test_skips_keep_all_reasons(tmp_path):
    xml = tmp_path / "results.xml"
    xml.write_text('<testsuites><testsuite><testcase classname="a" name="one"><skipped message="requires bpy"/></testcase><testcase classname="b" name="two"><skipped message="unknown reason"/></testcase></testsuite></testsuites>')
    result = skips(xml)
    assert result["total_skipped"] == 2
    assert len(result["skips"]) == 2
    assert all(x["coverage_impact"] for x in result["skips"])


def test_gui_missing_evidence_fails_closed(tmp_path):
    data = tmp_path / "bas05-e2e.json"
    data.write_text(json.dumps({"passed": True, "blender_version": "4.5.15"}))
    manifest = gui(data)
    assert manifest["case_count"] >= 10
    assert manifest["failed"] >= 10
    assert manifest["human_mouse_gui"] == "NOT_TESTED"
