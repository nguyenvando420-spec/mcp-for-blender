"""BAS-09 CI skip inventory and real MCP GUI acceptance manifest."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def classify(reason):
    r = reason.lower()
    if any(k in r for k in ("blender", "bpy", "gui", "display", "xvfb")):
        return "blender-or-display-dependent"
    if any(k in r for k in ("optional", "not installed", "importorskip")):
        return "optional-dependency"
    if any(k in r for k in ("platform", "windows", "darwin", "macos")):
        return "platform-specific"
    return "manual-triage"


def skips(path):
    root = ET.parse(path).getroot()
    items = []
    for test in root.iter("testcase"):
        skip = test.find("skipped")
        if skip is None:
            continue
        reason = (skip.get("message") or skip.text or "").strip() or "(unspecified)"
        category = classify(reason)
        items.append({"test": test.get("classname", "") + "::" + test.get("name", ""),
                      "reason": reason, "category": category,
                      "coverage_impact": "not executed by this Python matrix; " +
                      ("manual review required" if category == "manual-triage" else category)})
    return {"total_skipped": len(items), "categories": dict(Counter(x["category"] for x in items)),
            "skips": items}


def gui(path, expected_sha=None):
    data = json.loads(path.read_text())
    def get(*keys):
        value = data
        for key in keys:
            value = value[key]
        return value
    artifact_specs = {
        "bas08-viewport.png": get("bas08_capture", "sha256") if "bas08_capture" in data else None,
        "bas08-camera.png": (data.get("bas08_capture") or {}).get("camera_sha256"),
        "bas05-scene.blend": data.get("scene_blend_sha256"),
    }
    artifacts = []
    integrity = True
    for name, expected in artifact_specs.items():
        asset = path.parent / name
        if not asset.is_file() or not isinstance(expected, str) or len(expected) != 64:
            integrity = False
            continue
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        if digest != expected or asset.stat().st_size < 1000:
            integrity = False
        artifacts.append({"name": name, "sha256": digest,
                          "size": asset.stat().st_size, "matches_source": digest == expected})
    predicates = [
        ("handshake", lambda: get("handshake", "blender_version").startswith("4.5.15")),
        ("inspect", lambda: bool(get("scene_id"))),
        ("create_transform_delete", lambda: get("bas07", "delete_passed") and len(get("bas07", "operations")) >= 10),
        ("text_collection", lambda: any(x["op"] == "studio_manage_collection" for x in get("bas07", "operations")) and any(x["key"] == "bas07-title" for x in get("bas07", "operations"))),
        ("viewport_camera_png", lambda: integrity and len(get("bas08_capture", "sha256")) == 64 and len(get("bas08_capture", "camera_sha256")) == 64),
        ("save_and_reopen", lambda: get("bas12_save", "reopen") == "passed" and get("bas12_open", "socket_kept_alive")),
        ("invalid_args", lambda: get("invalid_error") == "invalid_input"),
        ("disconnect_reconnect", lambda: get("bas09_reconnect", "fresh_stdio_processes") == 2 and get("bas09_reconnect", "no_duplicate_side_effect")),
        ("socket_timeout", lambda: get("bas09_socket_cases", "partial_frame_timeout")),
        ("permission_denied", lambda: get("bas09_socket_cases", "permission_denied") and get("bas09_socket_cases", "unauthorized")),
        ("100_commands", lambda: get("bas06_stress", "requests") == 100 and get("bas06_stress", "creates") == 50 and get("bas06_stress", "inspects") == 50),
        ("out_of_band_gui_changes", lambda: len(get("bas07_out_of_band_gui")) == 5 and all(x["revision_bumped"] for x in get("bas07_out_of_band_gui"))),
        ("independent_headless_reopen", lambda: integrity and get("bas08_binding", "headless_reopen_passed") and get("bas12_snapshot", "reopen") == "passed"),
    ]
    cases = []
    for name, predicate in predicates:
        try:
            ok = bool(predicate())
        except (KeyError, ValueError, IndexError, TypeError):
            ok = False
        cases.append({"case": name, "status": "PASS" if ok else "FAIL"})
    if expected_sha is not None and data.get("source_commit") != expected_sha:
        cases.append({"case": "expected_head_sha", "status": "FAIL"})
    return {"source_commit": data.get("source_commit"),
            "environment": {key: data.get(key) for key in ("blender_version", "gateway_python", "client", "display")},
            "case_count": len(cases), "passed": sum(x["status"] == "PASS" for x in cases),
            "failed": sum(x["status"] == "FAIL" for x in cases),
            "cases": cases, "artifacts": artifacts,
            "human_mouse_gui": "NOT_TESTED",
            "timeout_scope": "incomplete TCP frame; not mutation cancellation"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("pytest", "gui"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-sha", help="Expected checked-out PR head SHA for GUI report")
    args = parser.parse_args()
    result = skips(args.input) if args.kind == "pytest" else gui(args.input, expected_sha=args.expected_sha)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print("BAS09_REPORT", args.kind, result.get("total_skipped", result.get("passed")))
    if args.kind == "gui":
        print("BAS09_EVIDENCE", json.dumps(result, ensure_ascii=False))
    else:
        print("BAS09_SKIPPED_CATEGORIES", json.dumps(result["categories"], sort_keys=True))
    if args.kind == "gui" and result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
