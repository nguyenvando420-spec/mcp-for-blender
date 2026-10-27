"""BAS-03 schema/type/negative gates; no Blender dispatch is claimed."""

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError as SchemaValidationError
from pydantic import ValidationError

from blender_mcp.studio.contracts import (
    STUDIO_OPERATIONS,
    ArtifactManifest,
    Capabilities,
    Job,
    JobState,
    Recovery,
    StudioResponse,
    ensure_supported,
    json_schemas,
    validate_request,
    validate_transition,
)

OBJECT_ID, SCENE_ID, SNAPSHOT_ID = (str(uuid4()) for _ in range(3))
NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def request(operation, effect_scope="scene", **fields):
    return {
        "operation": operation, "request_id": str(uuid4()),
        "scene_id": SCENE_ID, "scene_revision": 7,
        "idempotency_key": "caller:request-1", "effect_scope": effect_scope,
        **fields,
    }


def samples():
    return [
        request("studio_scene_inspect", "read"),
        request("studio_scene_save", "file", output_uri="file:///tmp/project.blend"),
        request("studio_scene_open", "file", input_uri="file:///tmp/project.blend",
                expected_sha256="a" * 64, snapshot_id=SNAPSHOT_ID, confirmation_token="open:7"),
        request("studio_scene_snapshot", "file"),
        request("studio_scene_restore", "file", snapshot_id=SNAPSHOT_ID, confirmation_token="restore:7"),
        request("studio_create_object", name="Cube", spec={"kind": "primitive", "primitive": "cube"}),
        request("studio_set_transform", object_id=OBJECT_ID, action="transform", location=[0, 1, 2]),
        request("studio_delete_object", object_id=OBJECT_ID, snapshot_id=SNAPSHOT_ID,
                confirmation_token="delete:7"),
        request("studio_manage_collection", action="create", name="Collection"),
        request("studio_assign_material", object_id=OBJECT_ID,
                spec={"kind": "pbr", "base_color": [1, 0.5, 0, 1], "metallic": 0, "roughness": 0.5}),
        request("studio_set_camera", location=[0, -5, 3], rotation_euler=[1, 0, 0]),
        request("studio_set_light", spec={"kind": "light", "light_type": "AREA", "energy": 100,
                                         "color": [1, 1, 1], "location": [0, 0, 3]}),
        request("studio_capture_viewport", "read"),
        request("studio_render_still", "file", width=320, height=180, samples=8,
                output_uri="file:///tmp/render.png"),
        request("studio_export_artifacts", "file", output_uri="artifact://job-1/output",
                formats=["glb", "blend"], object_ids=[OBJECT_ID]),
    ]


def job(state="queued", **changes):
    data = {
        "job_id": str(uuid4()), "request_id": str(uuid4()), "owner_id": "caller-1",
        "idempotency_key": "render-1", "payload_sha256": "a" * 64,
        "operation": "studio_render_still", "scene_id": SCENE_ID, "scene_revision": 7,
        "state": state, "progress": 0, "timeout_seconds": 600,
        "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
    }
    if state == "running":
        data["lease_expires_at"] = (NOW + timedelta(minutes=1)).isoformat()
    data.update(changes)
    return Job.model_validate_json(json.dumps(data))


def manifest():
    job_id = str(uuid4())
    return {
        "job_id": job_id, "created_at": NOW.isoformat(),
        "artifacts": [{
            "artifact_id": str(uuid4()), "job_id": job_id, "kind": "png",
            "uri": "file:///tmp/render.png", "bytes": 1234, "sha256": "a" * 64, "version": 1,
            "provenance": {
                "source_commit": "b" * 40, "scene_id": SCENE_ID, "scene_revision": 7,
                "blender_version": "4.5.15", "worker_version": "1.0.0", "input_sha256": ["c" * 64],
            },
        }],
    }


class StudioContractTests(unittest.TestCase):
    def test_all_15_canonical_operations_validate(self):
        examples = samples()
        self.assertEqual({item["operation"] for item in examples}, STUDIO_OPERATIONS)
        for example in examples:
            with self.subTest(operation=example["operation"]):
                parsed = validate_request(example)
                self.assertEqual(parsed.operation, example["operation"])
                self.assertEqual(parsed.scene_revision, 7)
                # Independent JSON Schema structural validation of valid wire input.
                Draft202012Validator(json_schemas()["request"]).validate(example)

    def test_read_bootstrap_does_not_require_known_scene(self):
        parsed = validate_request({"operation": "studio_scene_inspect", "request_id": str(uuid4()),
                                   "effect_scope": "read"})
        self.assertIsNone(parsed.scene_id)
        self.assertIsNone(parsed.scene_revision)

    def test_mutation_requires_context_and_idempotency(self):
        base = samples()[5]
        for field in ("scene_id", "scene_revision", "idempotency_key"):
            invalid = copy.deepcopy(base)
            invalid.pop(field)
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_request(invalid)

    def test_version_type_scope_and_unknown_fields_rejected(self):
        base = samples()[5]
        for patch in ({"contract": {"major": 2}}, {"scene_revision": -1}, {"scene_revision": "7"},
                      {"request_id": "not-a-uuid"}, {"effect_scope": "read"}, {"code": "print(1)"},
                      {"operation": "studio_execute_code"}):
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                validate_request({**base, **patch})

    def test_json_schema_requires_operation_and_matching_scope(self):
        validator = Draft202012Validator(json_schemas()["request"])
        missing_operation = samples()[5]
        missing_operation.pop("operation")
        for invalid in (missing_operation, {**samples()[5], "effect_scope": "read"},
                        {**samples()[5], "operation": "studio_execute_code"}):
            with self.subTest(invalid=invalid), self.assertRaises(SchemaValidationError):
                validator.validate(invalid)
            with self.assertRaises(ValidationError):
                validate_request(invalid)

    def test_composite_operation_variants(self):
        variants = [
            request("studio_create_object", name="Title", spec={"kind": "text", "text": "Hello"}),
            request("studio_set_transform", object_id=OBJECT_ID, action="rename", name="Renamed"),
            request("studio_set_transform", object_id=OBJECT_ID, action="duplicate", name="Copy"),
            request("studio_manage_collection", action="move_object", object_id=OBJECT_ID,
                    collection_id=str(uuid4())),
            request("studio_assign_material", object_id=OBJECT_ID,
                    spec={"kind": "bevel", "width": 0.02, "segments": 2}),
            request("studio_assign_material", object_id=OBJECT_ID, spec={"kind": "subdivision", "levels": 2}),
            request("studio_assign_material", object_id=OBJECT_ID,
                    spec={"kind": "geometry_nodes", "preset": "linear_array", "count": 3, "offset": [2, 0, 0]}),
            request("studio_set_light", spec={"kind": "world", "color": [1, 1, 1], "strength": 0.5}),
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                validate_request(variant)

    def test_action_payloads_cannot_be_ambiguous(self):
        invalids = [
            request("studio_set_transform", object_id=OBJECT_ID, action="transform"),
            request("studio_set_transform", object_id=OBJECT_ID, action="rename", name="Name", scale=[1, 1, 1]),
            request("studio_manage_collection", action="move_object", collection_id=str(uuid4())),
            request("studio_manage_collection", action="create", name="Name", object_id=OBJECT_ID),
            request("studio_create_object", name="Title", spec={"kind": "text", "primitive": "cube"}),
        ]
        for invalid in invalids:
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validate_request(invalid)

    def test_destructive_requests_require_confirmation(self):
        for index in (2, 4, 7):
            invalid = samples()[index]
            invalid.pop("confirmation_token")
            with self.subTest(operation=invalid["operation"]), self.assertRaises(ValidationError):
                validate_request(invalid)
        with self.assertRaises(ValidationError):
            validate_request(request("studio_scene_save", "file", output_uri="file:///tmp/a.blend", overwrite=True))

    def test_numeric_uri_and_preset_bounds(self):
        invalids = [
            {**samples()[13], "width": 0},
            {**samples()[5], "location": [float("inf"), 0, 0]},
            {**samples()[14], "formats": ["obj"]},
            {**samples()[1], "output_uri": "../escape.blend"},
            request("studio_assign_material", object_id=OBJECT_ID,
                    spec={"kind": "pbr", "base_color": [1.1, 0, 0, 1], "metallic": 0, "roughness": 0.5}),
            request("studio_assign_material", object_id=OBJECT_ID,
                    spec={"kind": "geometry_nodes", "preset": "python", "count": 100000, "offset": [1, 0, 0]}),
        ]
        for invalid in invalids:
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validate_request(invalid)

    def test_capabilities_allow_additive_advertising_but_gate_dispatch(self):
        caps = Capabilities.model_validate_json(json.dumps({
            "addon_protocol": 15, "operations": ["studio_scene_inspect", "future_operation"]
        }))
        ensure_supported(validate_request(samples()[0]), caps)
        with self.assertRaisesRegex(ValueError, "capability_unavailable"):
            ensure_supported(validate_request(samples()[5]), caps)

    def test_response_variants(self):
        base = {"request_id": str(uuid4()), "scene_id": SCENE_ID, "scene_revision": 7}
        for fields in ({"status": "succeeded", "result": {"snapshot_id": SNAPSHOT_ID}},
                       {"status": "accepted", "job_id": str(uuid4())},
                       {"status": "failed", "error": {"code": "stale_scene", "message": "Reinspect scene"}}):
            StudioResponse.model_validate_json(json.dumps({**base, **fields}))
        for fields in ({"status": "accepted"}, {"status": "failed", "result": {}},
                       {"status": "succeeded", "result": {}, "job_id": str(uuid4())}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                StudioResponse.model_validate_json(json.dumps({**base, **fields}))

    def test_cancel_is_request_until_stop_confirmed(self):
        before = job("running", progress=0.4)
        requested = before.model_copy(update={"cancel_requested": True})
        validate_transition(before, requested)
        self.assertEqual(requested.state, JobState.RUNNING)
        candidate = requested.model_copy(update={"state": JobState.CANCELLED, "lease_expires_at": None})
        with self.assertRaisesRegex(ValidationError, "confirmed stop"):
            validate_transition(requested, candidate)
        acknowledged = candidate.model_copy(update={"stop_confirmed": True})
        validate_transition(requested, acknowledged)
        # Queued jobs can be removed and acknowledged without starting a worker.
        queued = job()
        validate_transition(queued, queued.model_copy(update={"state": JobState.CANCELLED,
                            "cancel_requested": True, "stop_confirmed": True}))

    def test_terminal_completion_and_timeout(self):
        running = job("running", progress=0.4)
        done = running.model_copy(update={"state": JobState.SUCCEEDED, "progress": 1.0, "lease_expires_at": None})
        validate_transition(running, done)
        validate_transition(done, done)
        with self.assertRaisesRegex(ValueError, "terminal job is immutable"):
            validate_transition(done, done.model_copy(update={"artifact_ids": [uuid4()]}))
        from blender_mcp.studio.contracts import ErrorCode, StudioError
        timed_out = running.model_copy(update={"state": JobState.FAILED, "lease_expires_at": None,
                                       "error": StudioError(code=ErrorCode.TIMEOUT, message="Worker stopped")})
        validate_transition(running, timed_out)

    def test_recovery_requires_expiry_replay_evidence_and_attempt(self):
        running = job("running", progress=0.4)
        recovered = running.model_copy(update={
            "state": JobState.QUEUED, "attempt": 2, "lease_expires_at": None,
            "updated_at": NOW + timedelta(seconds=61),
            "recovery": Recovery(reason="lease_expired", replay_safe=True),
        })
        validate_transition(running, recovered)
        for patch in ({"attempt": 1}, {"recovery": None}, {"updated_at": NOW},
                      {"payload_sha256": "b" * 64}, {"cancel_requested": True}, {"progress": 0.1}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_transition(running, recovered.model_copy(update=patch))
        with self.assertRaisesRegex(ValueError, "attempt and recovery"):
            validate_transition(running, running.model_copy(update={"attempt": 2}))

    def test_invalid_job_snapshots(self):
        for fields in ({"state": "running", "lease_expires_at": None},
                       {"state": "succeeded", "progress": 0.9}, {"state": "failed"},
                       {"updated_at": (NOW - timedelta(days=1)).isoformat()},
                       {"created_at": "2026-10-26T00:00:00"}, {"progress": -0.1}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                job(**fields)

    def test_artifact_integrity_provenance_and_manifest(self):
        data = manifest()
        ArtifactManifest.model_validate_json(json.dumps(data))
        for field, value in (("sha256", "wrong"), ("bytes", 0), ("job_id", str(uuid4())),
                             ("uri", "relative/path"), ("version", 0)):
            invalid = copy.deepcopy(data)
            invalid["artifacts"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                ArtifactManifest.model_validate_json(json.dumps(invalid))
        invalid = copy.deepcopy(data)
        invalid["artifacts"][0]["provenance"]["model_id"] = "some-model"
        with self.assertRaises(ValidationError):
            ArtifactManifest.model_validate_json(json.dumps(invalid))
        data["artifacts"].append(copy.deepcopy(data["artifacts"][0]))
        with self.assertRaisesRegex(ValidationError, "duplicate artifact_id"):
            ArtifactManifest.model_validate_json(json.dumps(data))

    def test_schema_files_match_validators(self):
        root = Path(__file__).resolve().parents[1] / "docs" / "schemas"
        for name, schema in json_schemas().items():
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(schema)
                saved = json.loads((root / f"studio-{name}-v1.schema.json").read_text())
                if saved != schema:
                    import difflib
                    print("\\n".join(list(difflib.unified_diff(
                        json.dumps(saved, indent=2, sort_keys=True).splitlines(),
                        json.dumps(schema, indent=2, sort_keys=True).splitlines(),
                        fromfile="checked-in", tofile="generated"))[:90]))
                self.assertEqual(saved, schema)
        self.assertEqual(set(json_schemas()["request"]["discriminator"]["mapping"]), STUDIO_OPERATIONS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
