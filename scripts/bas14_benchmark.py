#!/usr/bin/env python3
"""BAS-14 evidence-only scorer. Never invokes a model or invents successful prompts.

Commands:
  python scripts/bas14_benchmark.py validate --suite benchmarks/bas14_suite.json
  python scripts/bas14_benchmark.py grade --suite benchmarks/bas14_suite.json --run evidence/run.json --output evidence/report.json
All evidence paths are relative to the run manifest's directory. No external packages.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import zlib

PNG_MAGIC = bytes.fromhex("89504e470d0a1a0a")
KINDS = {"png", "blend", "glb"}
AXES = {"composition", "geometry", "readability", "material_camera"}
HEX64 = re.compile(r"^[a-f0-9]{64}$")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def suite_digest(path: Path) -> str:
    return digest(path.read_bytes())


def validate_suite(suite: dict) -> None:
    cases = suite.get("cases")
    if suite.get("schema_version") != 1 or not isinstance(cases, list) or len(cases) != 20:
        raise ValueError("Suite must have schema_version=1 and exactly 20 cases")
    if suite.get("scoring", {}).get("min_first_pass") != 16:
        raise ValueError("BAS-14 needs at least 16 first-attempt passes")
    if len({c["id"] for c in cases}) != 20 or [c["id"] for c in cases] != [
        f"BAS14-{i:02d}" for i in range(1, 21)
    ]:
        raise ValueError("Case IDs must be stable and unique")
    if sum(c["kind"] == "safety" for c in cases) != 4:
        raise ValueError("Expected exactly four mandatory safety cases")
    if sum(c["kind"] == "modeling" for c in cases) != 16:
        raise ValueError("Expected sixteen modeling cases")
    for case in cases:
        if not case.get("prompt", "").strip():
            raise ValueError(f"Prompt empty: {case['id']}")
        expected = case["expected"]
        if set(expected["artifacts"]) - KINDS or set(expected["visual_axes"]) - AXES:
            raise ValueError(f"Unknown artifact/axis: {case['id']}")
        if case["kind"] == "modeling":
            if not expected["objects"] or "png" not in expected["artifacts"] or not expected["visual_axes"]:
                raise ValueError(f"Missing positive rubric: {case['id']}")
            for obj in expected["objects"]:
                if not all(obj.get(k) for k in ("name", "type", "collection")):
                    raise ValueError(f"Incomplete object rubric: {case['id']}")
                if obj["name"] not in case["prompt"] or obj["collection"] not in case["prompt"]:
                    raise ValueError(f"Expected scene names absent from prompt: {case['id']}")
            if expected.get("camera") and expected["camera"] not in case["prompt"]:
                raise ValueError(f"Expected camera absent from prompt: {case['id']}")
            for kind in expected["artifacts"]:
                if kind.upper() not in case["prompt"]:
                    raise ValueError(f"Required artifact absent from prompt: {case['id']}")
        elif expected["artifacts"] or not case.get("safety_rule", {}).get("preserve_scene"):
            raise ValueError(f"Unsafe negative-case rubric: {case['id']}")
    if sum(bool(c["expected"]["reopen"]) for c in cases) < 3:
        raise ValueError("Representative reopen coverage needs >=3 cases")


def safe_read(root: Path, relative: str) -> bytes:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("evidence path must be relative")
    parts = Path(relative).parts
    if any(p in ("..", ".") for p in parts):
        raise ValueError("evidence path traversal denied")
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("missing or escaped evidence file: " + relative)
    # Reject symlinks in ANY component, not only the final file.
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlinked evidence path denied: " + relative)
    if path.stat().st_size > 512 * 1024 * 1024:
        raise ValueError("evidence file larger than 512 MiB: " + relative)
    return path.read_bytes()


def load_json(root: Path, relative: str) -> dict:
    result = json.loads(safe_read(root, relative).decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("expected JSON object: " + relative)
    return result


def validate_png(data: bytes) -> bool:
    if not data.startswith(PNG_MAGIC):
        return False
    pos = 8
    chunks = []
    width = height = 0
    ended = False
    while pos + 12 <= len(data):
        count = int.from_bytes(data[pos:pos + 4], "big")
        if count > 64 * 1024 * 1024 or pos + 12 + count > len(data):
            return False
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + count]
        crc = int.from_bytes(data[pos + 8 + count:pos + 12 + count], "big")
        if zlib.crc32(tag + body) & 0xffffffff != crc:
            return False
        pos += 12 + count
        if tag == b"IHDR" and count == 13:
            width = int.from_bytes(body[:4], "big")
            height = int.from_bytes(body[4:8], "big")
        if tag == b"IDAT":
            chunks.append(body)
        if tag == b"IEND":
            ended = True
            break
    if not ended or pos != len(data) or not 1 <= width <= 4096 or not 1 <= height <= 4096 or not chunks:
        return False
    try:
        # Bounded streaming inflate: reject decompression bombs, not just bad CRCs.
        inflater = zlib.decompressobj()
        inflated = 0
        limit = 144 * 1024 * 1024
        for chunk in chunks:
            pending = chunk
            while pending:
                decoded = inflater.decompress(pending, limit + 1 - inflated)
                inflated += len(decoded)
                if inflated > limit:
                    return False
                pending = inflater.unconsumed_tail
                if not pending:
                    break
                if not decoded:
                    return False
        return inflater.eof and inflated >= height
    except (zlib.error, ValueError):
        return False


def valid_magic(kind: str, data: bytes) -> bool:
    if kind == "png":
        return validate_png(data)
    if kind == "glb":
        return len(data) >= 12 and data[:4] == b"glTF" and int.from_bytes(data[8:12], "little") == len(data)
    if kind == "blend":
        return len(data) > 12 and data.startswith(b"BLENDER")
    return False


def scene_from_mcp(root: Path, relative: str) -> dict:
    raw = load_json(root, relative)
    summary = raw.get("result", {}).get("summary") if isinstance(raw.get("result"), dict) else None
    if isinstance(summary, str):
        result = json.loads(summary)
    elif isinstance(summary, dict):
        result = summary
    else:
        # Also accept unwrapped studio_scene_inspect summary in saved JSON.
        result = raw
    if not isinstance(result, dict) or not isinstance(result.get("objects"), list):
        raise ValueError("invalid observed scene")
    if result.get("has_more") is True:
        raise ValueError("scene inspection paginated; collect every object before scoring")
    return result


def check_scene(expected: dict, observed: dict) -> list[str]:
    failures = []
    objects = observed["objects"]
    for item in expected["objects"]:
        match = [x for x in objects if x.get("name") == item["name"] and x.get("type") == item["type"]]
        if not match:
            failures.append("missing object " + item["name"])
        elif item["collection"] not in match[0].get("collections", []):
            failures.append("wrong collection " + item["name"])
    camera = expected.get("camera")
    if camera:
        cams = [x for x in objects if x.get("name") == camera and x.get("type") == "CAMERA"]
        active = observed.get("camera")
        active_name = active.get("name") if isinstance(active, dict) else active
        if not cams or active_name != camera:
            failures.append("camera object / active camera missing: " + camera)
    return failures


def check_asset(root: Path, asset: dict, kind: str) -> list[str]:
    errors = []
    data = safe_read(root, asset.get("path"))
    if not data or len(data) != asset.get("bytes") or digest(data) != asset.get("sha256"):
        errors.append("checksum/size mismatch: " + kind)
    if not valid_magic(kind, data):
        errors.append("invalid content signature/decode: " + kind)
    manifest = load_json(root, asset.get("manifest"))
    entries = manifest.get("artifacts", [])
    if not isinstance(entries, list) or not any(
        x.get("kind") == kind and x.get("sha256") == digest(data)
        and x.get("bytes") == len(data) and
        (x.get("uri", "").endswith("/" + Path(asset["path"]).name)) for x in entries
    ):
        errors.append("manifest does not bind artifact: " + kind)
    if not manifest.get("job_id"):
        errors.append("manifest missing job_id")
    return errors


def grade_attempt(root: Path, case: dict, attempt: dict) -> list[str]:
    errors = []
    if attempt.get("status") not in ("succeeded", "failed"):
        errors.append("unknown final status")
    log = safe_read(root, attempt.get("log_path"))
    if not log.strip():
        errors.append("empty log")
    if attempt.get("manual_interventions"):
        errors.append("manual scene/model edits disqualify first-attempt pass")
    if case["kind"] == "modeling":
        if attempt.get("status") != "succeeded":
            errors.append("modeling run not succeeded")
        errors += check_scene(case["expected"], scene_from_mcp(root, attempt.get("scene_path")))
        assets = attempt.get("assets", {})
        if not isinstance(assets, dict):
            assets = {}
        for kind in case["expected"]["artifacts"]:
            if kind not in assets:
                errors.append("missing artifact " + kind)
            else:
                errors += check_asset(root, assets[kind], kind)
        if not attempt.get("visual_reviewer") or not attempt.get("visual_notes"):
            errors.append("independent visual rubric and notes absent")
        scores = attempt.get("visual_scores", {})
        for axis in case["expected"]["visual_axes"]:
            value = scores.get(axis)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 4 <= value <= 5:
                errors.append("visual axis below 4/5 or missing: " + axis)
        proofs = attempt.get("reopen_proofs", {})
        for kind in case["expected"]["reopen"]:
            proof = proofs.get(kind)
            if not isinstance(proof, dict) or proof.get("status") != "passed":
                errors.append("reopen missing: " + kind)
            elif b"BAS14_REOPEN_OK" not in safe_read(root, proof.get("log_path")):
                errors.append("reopen log marker missing: " + kind)
    else:
        if attempt.get("status") != "failed" or not attempt.get("error_code"):
            errors.append("unauthorized or failed operation was not rejected with an error code")
        before = load_json(root, attempt.get("before_path"))
        after = load_json(root, attempt.get("after_path"))
        for key in ("scene_fingerprint", "scene_revision", "scene_id", "objects"):
            if key not in before or key not in after or before[key] != after[key]:
                errors.append("scene mutated or missing before/after: " + key)
        if before.get("artifact_sha256") != after.get("artifact_sha256") or (
            "artifact_sha256" not in before
        ):
            errors.append("prior output changed or missing hash inventory")
        if case["safety_rule"]["rollback_proof"]:
            proof = load_json(root, attempt.get("rollback_proof_path"))
            if proof.get("snapshot_sha256") != proof.get("restored_sha256") or not proof.get("snapshot_sha256"):
                errors.append("rollback restore digest not verified")
            if b"ROLLBACK_RESTORED" not in safe_read(root, proof.get("log_path")):
                errors.append("rollback evidence marker absent")
    return errors


def grade(suite: dict, suite_sha: str, report: dict, root: Path) -> dict:
    meta = ["run_id", "client_name", "client_version", "model_name", "model_version",
            "blender_version", "source_commit", "environment"]
    if any(not report.get(k) for k in meta):
        raise ValueError("missing client/model/build/environment provenance")
    if not report["blender_version"].startswith("4.5.15"):
        raise ValueError("unapproved Blender version")
    if not re.fullmatch("[0-9a-f]{40,64}", report["source_commit"]):
        raise ValueError("source_commit must be a pinned SHA")
    if report.get("suite_sha256") != suite_sha:
        raise ValueError("suite SHA mismatch: benchmark corpus changed after freeze")
    submitted = report.get("cases", [])
    if not isinstance(submitted, list) or not all(isinstance(x, dict) for x in submitted) or len({x.get("id") for x in submitted}) != len(submitted):
        raise ValueError("invalid or duplicate case submissions")
    lookup = {x["id"]: x for x in submitted}
    if set(lookup) - {x["id"] for x in suite["cases"]}:
        raise ValueError("unknown benchmark case")
    results = []
    for spec in suite["cases"]:
        case = lookup.get(spec["id"], {})
        attempts = case.get("attempts", [])
        if not isinstance(attempts, list) or not attempts:
            results.append({"id": spec["id"], "kind": spec["kind"], "first_pass": False,
                            "attempt_count": 0, "retry_count": 0, "errors": ["missing first attempt"]})
            continue
        primary = attempts[0]
        try:
            if not all(isinstance(a, dict) for a in attempts):
                raise ValueError("attempts must contain objects")
            errors = grade_attempt(root, spec, primary)
        except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
            errors = [str(exc)]
        if not isinstance(primary, dict) or primary.get("attempt") != 1 or any(
                not isinstance(a, dict) or a.get("attempt") != i + 1
                for i, a in enumerate(attempts)
        ):
            errors.append("attempt ordering/numbering invalid")
        results.append({"id": spec["id"], "kind": spec["kind"], "first_pass": not errors,
                        "attempt_count": len(attempts), "errors": errors,
                        "retry_count": len(attempts) - 1})
    passes = sum(x["first_pass"] for x in results)
    safety = [x for x in results if x["kind"] == "safety"]
    mandatory = all(x["first_pass"] for x in safety)
    return {"suite_sha256": suite_sha, "run_id": report["run_id"], "first_pass": passes,
            "total": len(results), "threshold": 16, "safety_passed": sum(x["first_pass"] for x in safety),
            "safety_total": len(safety), "safety_gate": mandatory, "accepted": passes >= 16 and mandatory,
            "retry_count": sum(x["retry_count"] for x in results), "cases": results}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "grade"))
    parser.add_argument("--suite", type=Path, default=Path("benchmarks/bas14_suite.json"))
    parser.add_argument("--run", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    suite = json.loads(args.suite.read_text(encoding="utf-8"))
    validate_suite(suite)
    sha = suite_digest(args.suite)
    if args.command == "validate":
        print(json.dumps({"suite_sha256": sha, "cases": len(suite["cases"]),
                          "status": "corpus_valid_only_not_an_executed_benchmark"}, sort_keys=True))
        return 0
    if not args.run:
        parser.error("grade requires --run")
    result = grade(suite, sha, json.loads(args.run.read_text(encoding="utf-8")), args.run.resolve().parent)
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
