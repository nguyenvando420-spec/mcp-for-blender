"""BAS-03 contract gates; these do not claim Blender dispatch works."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pydantic import ValidationError

from blender_mcp.studio.contracts import (
    ArtifactManifest,
    Capabilities,
    Job,
    STUDIO_OPERATIONS,
    StudioResponse,
    json_schemas,
    validate_request,
    validate_transition,
)


def raises(error_type, match=None):
    case = unittest.TestCase()
    return case.assertRaisesRegex(error_type, match) if match else case.assertRaises(error_type)


def request(operation="studio_create_text", effect_scope="scene", **fields):
    return {
        "operation": operation,
        "contract": {"major": 1, "minor": 0},
        "request_id": str(uuid4()),
        "scene_revision": 7,
        "effect_scope": effect_scope,
        **fields,
    }


def test_request_discriminator_and_scope():
    valid = validate_request(request(name="Title", text="Hello"))
    assert valid.operation == "studio_create_text"
    assert valid.scene_revision == 7
    with raises(ValidationError):
        validate_request(request(name="Title", text="Hello", effect_scope="read"))
    with raises(ValidationError):
        validate_request(request(operation="studio_execute_code", code="print(1)"))
    with raises(ValidationError):
        validate_request(request(name="Title", text="Hello", arbitrary=True))


def test_request_version_revision_and_operation_constraints():
    with raises(ValidationError):
        validate_request(request(name="Title", text="Hello", contract={"major": 2, "minor": 0}))
    with raises(ValidationError):
        validate_request(request(name="Title", text="Hello", scene_revision=-1))
    with raises(ValidationError):
        validate_request(request(operation="studio_transform_object", object_id=str(uuid4())))
    with raises(ValidationError):
        validate_request(request(operation="studio_delete_object", object_id=str(uuid4())))
    with raises(ValidationError):
        validate_request(request(operation="studio_render_still", effect_scope="file",
                                 width=0, height=180, samples=8, output_uri="file:///tmp/a.png"))
    with raises(ValidationError):
        validate_request(request(operation="studio_set_material", object_id=str(uuid4()),
                                 base_color=[1.1, 0, 0, 1], metallic=0, roughness=0.5))
    with raises(ValidationError):
        validate_request(request(operation="studio_create_primitive", primitive="cube",
                                 name="Cube", location=[float("inf"), 0, 0]))
    assert validate_request(request(operation="studio_export_glb", effect_scope="file",
                                    output_uri="file:///tmp/a.glb", object_ids=[str(uuid4())]))


def job(state="queued", **changes):
    now = datetime(2026, 10, 26, tzinfo=timezone.utc)
    data = {
        "job_id": str(uuid4()),
        "request_id": str(uuid4()),
        "idempotency_key": "caller-1:render-1",
        "operation": "studio_render_still",
        "state": state,
        "progress": 0.0,
        "timeout_seconds": 600,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    data.update(changes)
    if state == "running" and "lease_expires_at" not in changes:
        data["lease_expires_at"] = (now + timedelta(minutes=1)).isoformat()
    return Job.model_validate_json(json.dumps(data))


def test_cancel_is_request_until_worker_acknowledges():
    before = job(state="running", progress=0.4)
    requested = before.model_copy(update={"cancel_requested": True})
    validate_transition(before, requested)
    assert requested.state.value == "running"
    acknowledged = requested.model_copy(update={"state": type(before.state).CANCELLED,
                                                "lease_expires_at": None})
    validate_transition(requested, acknowledged)
    with raises(ValueError, match="acknowledged"):
        validate_transition(before, before.model_copy(update={
            "state": type(before.state).CANCELLED, "lease_expires_at": None
        }))


def test_recovery_and_terminal_invariants():
    running = job(state="running", progress=0.4)
    recovered = running.model_copy(update={"state": type(running.state).QUEUED,
                                           "attempt": 2, "lease_expires_at": None})
    validate_transition(running, recovered)
    fresh_running = job(state="running")
    with raises(ValueError, match="increment attempt"):
        validate_transition(fresh_running, fresh_running.model_copy(update={
            "state": type(fresh_running.state).QUEUED, "lease_expires_at": None
        }))
    done = running.model_copy(update={"state": type(running.state).SUCCEEDED,
                                      "progress": 1.0, "lease_expires_at": None})
    validate_transition(running, done)
    with raises(ValueError, match="terminal job is immutable"):
        validate_transition(done, done.model_copy(update={"artifact_ids": [uuid4()]}))
    with raises(ValidationError):
        job(state="succeeded", progress=0.9)
    with raises(ValidationError):
        job(state="failed")
    with raises(ValidationError):
        job(updated_at=(datetime(2026, 10, 25, tzinfo=timezone.utc)).isoformat())
    with raises(ValidationError):
        job(state="running", lease_expires_at=None)


def test_response_and_manifest_variants():
    req = str(uuid4())
    with raises(ValidationError):
        StudioResponse.model_validate_json(json.dumps({
            "request_id": req, "scene_revision": 2, "status": "accepted"
        }))
    accepted = StudioResponse.model_validate_json(json.dumps({
        "request_id": req, "scene_revision": 2, "status": "accepted", "job_id": str(uuid4())
    }))
    assert accepted.job_id
    artifact = {
        "artifact_id": str(uuid4()), "job_id": str(uuid4()), "kind": "png",
        "uri": "file:///tmp/render.png", "bytes": 1234, "sha256": "a" * 64,
        "version": 1, "provenance": {
            "source_commit": "b" * 40, "blender_version": "4.5.15",
            "worker_version": "1.0.0", "input_sha256": ["c" * 64]
        }
    }
    manifest = {"job_id": artifact["job_id"], "artifacts": [artifact],
                "created_at": datetime.now(timezone.utc).isoformat()}
    assert ArtifactManifest.model_validate_json(json.dumps(manifest)).artifacts[0].bytes == 1234
    artifact["sha256"] = "wrong"
    with raises(ValidationError):
        ArtifactManifest.model_validate_json(json.dumps(manifest))
    artifact["sha256"] = "a" * 64
    manifest["job_id"] = str(uuid4())
    with raises(ValidationError, match="does not match"):
        ArtifactManifest.model_validate_json(json.dumps(manifest))


def test_schema_files_match_validators():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "docs" / "schemas"
    for name, schema in json_schemas().items():
        assert json.loads((root / f"studio-{name}-v1.schema.json").read_text()) == schema
    assert len(json_schemas()["request"]["oneOf"]) == 15
    assert len(STUDIO_OPERATIONS) == 15
    with raises(ValidationError):
        Capabilities.model_validate_json(json.dumps({
            "addon_protocol": 15, "operations": ["studio_execute_code"]
        }))


if __name__ == "__main__":
    for test_name in sorted(name for name in globals() if name.startswith("test_")):
        globals()[test_name]()
        print(f"PASS {test_name}")
