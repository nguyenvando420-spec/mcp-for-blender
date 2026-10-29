"""BAS-03 v1 wire contracts, shared by future GUI and worker adapters.

Validation does not dispatch to Blender, authorize paths, persist jobs, or prove
that a worker stopped. The owning runner supplies verified lifecycle facts.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
Uri = Annotated[str, StringConstraints(min_length=1, max_length=4096, pattern=r"^[a-z][a-z0-9+.-]*://\S+$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitCommit = Annotated[str, StringConstraints(pattern=r"^([0-9a-f]{40}|[0-9a-f]{64})$")]
Channel = Annotated[float, Field(ge=0, le=1)]
Vec3 = tuple[float, float, float]
Color3 = tuple[Channel, Channel, Channel]
Color4 = tuple[Channel, Channel, Channel, Channel]
STUDIO_OPERATIONS = frozenset({
    "studio_scene_inspect", "studio_scene_save", "studio_scene_open",
    "studio_scene_snapshot", "studio_scene_restore", "studio_create_object",
    "studio_set_transform", "studio_delete_object", "studio_manage_collection",
    "studio_assign_material", "studio_set_camera", "studio_set_light",
    "studio_capture_viewport", "studio_render_still", "studio_export_artifacts",
})


class EffectScope(str, Enum):
    READ = "read"
    SCENE = "scene"
    FILE = "file"
    EXTERNAL = "external"


class ErrorCode(str, Enum):
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_VERSION = "unsupported_version"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    PERMISSION_DENIED = "permission_denied"
    STALE_SCENE = "stale_scene"
    OBJECT_NOT_FOUND = "object_not_found"
    CONFLICT = "conflict"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    WORKER_UNAVAILABLE = "worker_unavailable"
    INTERNAL = "internal"


class ContractVersion(StrictModel):
    major: Literal[1] = 1
    minor: int = Field(default=0, ge=0)


class Capabilities(StrictModel):
    contract: ContractVersion = Field(default_factory=ContractVersion)
    addon_protocol: int = Field(ge=0)
    # Unknown advertised operations are allowed for additive minor versions;
    # they never become valid v1 requests merely by being advertised.
    operations: set[NonEmpty]
    features: set[NonEmpty] = Field(default_factory=set)


class ObjectRef(StrictModel):
    """UUID persisted on a datablock; name is only a display label."""

    id: UUID
    name: NonEmpty
    kind: Literal["object", "collection", "material", "camera", "light"]


class RequestBase(StrictModel):
    contract: ContractVersion = Field(default_factory=ContractVersion)
    request_id: UUID
    scene_id: UUID | None = None
    scene_revision: int | None = Field(default=None, ge=0)
    idempotency_key: NonEmpty | None = None
    effect_scope: EffectScope

    @model_validator(mode="after")
    def check_context(self):
        if (self.scene_id is None) != (self.scene_revision is None):
            raise ValueError("scene_id and scene_revision must be supplied together")
        if self.effect_scope != EffectScope.READ:
            if self.scene_id is None or self.idempotency_key is None:
                raise ValueError("mutation requires scene context and idempotency_key")
        return self


class SceneInspect(RequestBase):
    operation: Literal["studio_scene_inspect"]
    effect_scope: Literal[EffectScope.READ, EffectScope.SCENE]
    query: str | None = None
    limit: int = Field(default=20, ge=1, le=500)
    offset: int = Field(default=0, ge=0, le=1000000)
    object_id: UUID | None = None
    bind_missing_ids: bool = False

    @model_validator(mode="after")
    def require_explicit_mutation_scope(self):
        if self.bind_missing_ids:
            if self.effect_scope != EffectScope.SCENE:
                raise ValueError("ID binding must use scene mutation scope")
            if self.query or self.offset or self.object_id is not None:
                raise ValueError("ID binding requires whole scene")
        elif self.effect_scope != EffectScope.READ:
            raise ValueError("Normal inspection is read-only")
        return self


class CaptureViewport(RequestBase):
    operation: Literal["studio_capture_viewport"]
    effect_scope: Literal[EffectScope.READ]
    max_size: int = Field(default=768, ge=200, le=2000)
    mode: Literal["viewport", "camera"] = "viewport"


class PrimitiveSpec(StrictModel):
    kind: Literal["primitive"]
    primitive: Literal["cube", "sphere", "cylinder", "cone", "plane"]


class TextSpec(StrictModel):
    kind: Literal["text"]
    text: Annotated[str, StringConstraints(min_length=1, max_length=4096)]


class CreateObject(RequestBase):
    operation: Literal["studio_create_object"]
    effect_scope: Literal[EffectScope.SCENE]
    name: NonEmpty
    spec: Annotated[Union[PrimitiveSpec, TextSpec], Field(discriminator="kind")]
    location: Vec3 = (0.0, 0.0, 0.0)


class SetTransform(RequestBase):
    operation: Literal["studio_set_transform"]
    effect_scope: Literal[EffectScope.SCENE]
    object_id: UUID
    action: Literal["transform", "rename", "duplicate"]
    name: NonEmpty | None = None
    location: Vec3 | None = None
    rotation_euler: Vec3 | None = None
    scale: Vec3 | None = None

    @model_validator(mode="after")
    def check_action(self):
        transforms = (self.location, self.rotation_euler, self.scale)
        if self.action == "transform" and (all(v is None for v in transforms) or self.name is not None):
            raise ValueError("transform requires transform fields only")
        if self.action == "rename" and (self.name is None or any(v is not None for v in transforms)):
            raise ValueError("rename requires name only")
        return self


class ManageCollection(RequestBase):
    operation: Literal["studio_manage_collection"]
    effect_scope: Literal[EffectScope.SCENE]
    action: Literal["create", "move_object", "rename"]
    name: NonEmpty | None = None
    collection_id: UUID | None = None
    object_id: UUID | None = None

    @model_validator(mode="after")
    def check_action(self):
        required = {
            "create": (self.name is not None and self.collection_id is None and self.object_id is None),
            "move_object": (self.collection_id is not None and self.object_id is not None and self.name is None),
            "rename": (self.collection_id is not None and self.name is not None and self.object_id is None),
        }
        if not required[self.action]:
            raise ValueError(f"invalid fields for collection action {self.action}")
        return self


class DeleteObject(RequestBase):
    operation: Literal["studio_delete_object"]
    effect_scope: Literal[EffectScope.SCENE]
    object_id: UUID
    snapshot_id: UUID
    confirmation_token: NonEmpty


class PbrSpec(StrictModel):
    kind: Literal["pbr"]
    base_color: Color4
    metallic: Channel
    roughness: Channel
    texture_uri: Uri | None = None
    texture_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def texture_pair(self):
        if (self.texture_uri is None) != (self.texture_sha256 is None):
            raise ValueError("texture URI and SHA-256 must be supplied together")
        return self


class BevelSpec(StrictModel):
    kind: Literal["bevel"]
    width: float = Field(gt=0, le=10)
    segments: int = Field(ge=1, le=16)


class SubdivisionSpec(StrictModel):
    kind: Literal["subdivision"]
    levels: int = Field(ge=1, le=3)


class GeometryPresetSpec(StrictModel):
    kind: Literal["geometry_nodes"]
    preset: Literal["linear_array"]
    count: int = Field(ge=1, le=100)
    offset: Vec3


class AssignMaterial(RequestBase):
    operation: Literal["studio_assign_material"]
    effect_scope: Literal[EffectScope.SCENE]
    object_id: UUID
    spec: Annotated[
        Union[PbrSpec, BevelSpec, SubdivisionSpec, GeometryPresetSpec],
        Field(discriminator="kind"),
    ]


class SetCamera(RequestBase):
    """Create/update a camera, optionally framing scene geometry by world bounds."""
    operation: Literal["studio_set_camera"]
    effect_scope: Literal[EffectScope.SCENE]
    camera_id: UUID | None = None
    name: NonEmpty | None = None
    location: Vec3 | None = None
    rotation_euler: Vec3 | None = None
    lens_mm: float | None = Field(default=None, ge=1, le=300)
    clip_start: float | None = Field(default=None, ge=0.001, le=100)
    clip_end: float | None = Field(default=None, ge=0.1, le=100000)
    target_id: UUID | None = None
    frame_scene: bool = False
    framing_margin: float = Field(default=1.15, ge=1, le=2)
    active: bool = True
    resolution_x: int | None = Field(default=None, ge=64, le=8192)
    resolution_y: int | None = Field(default=None, ge=64, le=8192)

    @model_validator(mode="after")
    def validate_camera(self):
        framing = self.target_id is not None or self.frame_scene
        if self.target_id is not None and self.frame_scene:
            raise ValueError("target_id and frame_scene are mutually exclusive")
        if (self.resolution_x is None) != (self.resolution_y is None):
            raise ValueError("resolution_x and resolution_y must be supplied together")
        if framing and (self.location is not None or self.rotation_euler is not None):
            raise ValueError("framing computes both camera location and rotation")
        if not framing and self.camera_id is None and (
                self.location is None or self.rotation_euler is None):
            raise ValueError("camera creation requires a location and rotation, or framing")
        if self.clip_start is not None and self.clip_end is not None and self.clip_start >= self.clip_end:
            raise ValueError("clip_start must be below clip_end")
        return self


class LightSpec(StrictModel):
    kind: Literal["light"]
    light_id: UUID | None = None
    name: NonEmpty | None = None
    light_type: Literal["POINT", "SUN", "SPOT", "AREA"]
    energy: float = Field(ge=0, le=100000)
    color: Color3
    location: Vec3
    rotation_euler: Vec3 | None = None
    size: float | None = Field(default=None, ge=0.01, le=100)

    @model_validator(mode="after")
    def validate_light(self):
        if self.size is not None and self.light_type != "AREA":
            raise ValueError("light size applies only to AREA")
        return self


class WorldSpec(StrictModel):
    kind: Literal["world"]
    color: Color3
    strength: float = Field(ge=0, le=100)


class SetLight(RequestBase):
    operation: Literal["studio_set_light"]
    effect_scope: Literal[EffectScope.SCENE]
    spec: Annotated[Union[LightSpec, WorldSpec], Field(discriminator="kind")]


class SceneSave(RequestBase):
    operation: Literal["studio_scene_save"]
    effect_scope: Literal[EffectScope.FILE]
    output_uri: Uri
    overwrite: bool = False
    expected_sha256: Sha256 | None = None
    confirmation_token: NonEmpty | None = None

    @model_validator(mode="after")
    def overwrite_confirmation(self):
        if self.overwrite and (self.confirmation_token is None or self.expected_sha256 is None):
            raise ValueError("overwrite requires confirmation_token and target checksum")
        if not self.overwrite and (self.expected_sha256 is not None or self.confirmation_token is not None):
            raise ValueError("non-overwrite save rejects confirmation and target checksum")
        return self


class SceneOpen(RequestBase):
    operation: Literal["studio_scene_open"]
    effect_scope: Literal[EffectScope.FILE]
    input_uri: Uri
    expected_sha256: Sha256
    snapshot_id: UUID
    confirmation_token: NonEmpty


class SceneSnapshot(RequestBase):
    operation: Literal["studio_scene_snapshot"]
    effect_scope: Literal[EffectScope.FILE]
    label: NonEmpty | None = None


class SceneRestore(RequestBase):
    operation: Literal["studio_scene_restore"]
    effect_scope: Literal[EffectScope.FILE]
    snapshot_id: UUID
    confirmation_token: NonEmpty


class RenderStill(RequestBase):
    operation: Literal["studio_render_still"]
    effect_scope: Literal[EffectScope.FILE]
    width: int = Field(ge=1, le=16384)
    height: int = Field(ge=1, le=16384)
    samples: int = Field(ge=1, le=65536)
    output_uri: Uri
    timeout_seconds: int = Field(default=600, ge=1, le=86400)
    overwrite: bool = False
    expected_sha256: Sha256 | None = None
    confirmation_token: NonEmpty | None = None

    @model_validator(mode="after")
    def confirm_overwrite(self):
        if self.overwrite:
            if self.expected_sha256 is None or self.confirmation_token is None:
                raise ValueError("confirmed overwrite requires target checksum and token")
        elif self.expected_sha256 is not None or self.confirmation_token is not None:
            raise ValueError("new artifact must not contain overwrite confirmation")
        return self


class ExportArtifacts(RequestBase):
    operation: Literal["studio_export_artifacts"]
    effect_scope: Literal[EffectScope.FILE]
    output_uri: Uri
    formats: set[Literal["glb", "blend"]] = Field(min_length=1)
    object_ids: list[UUID] = Field(min_length=1)
    overwrite: bool = False
    expected_sha256: Sha256 | None = None
    confirmation_token: NonEmpty | None = None

    @model_validator(mode="after")
    def confirm_overwrite(self):
        if self.overwrite:
            if self.expected_sha256 is None or self.confirmation_token is None:
                raise ValueError("confirmed overwrite requires target checksum and token")
        elif self.expected_sha256 is not None or self.confirmation_token is not None:
            raise ValueError("new artifact must not contain overwrite confirmation")
        return self


StudioRequest = Annotated[
    Union[
        SceneInspect, SceneSave, SceneOpen, SceneSnapshot, SceneRestore,
        CreateObject, SetTransform, DeleteObject, ManageCollection, AssignMaterial,
        SetCamera, SetLight, CaptureViewport, RenderStill, ExportArtifacts,
    ],
    Field(discriminator="operation"),
]
_request_adapter = TypeAdapter(StudioRequest)


def validate_request(data: dict) -> StudioRequest:
    """Validate JSON-compatible request data, including operation-specific types."""
    return _request_adapter.validate_json(json.dumps(data))


def ensure_supported(request: StudioRequest, capabilities: Capabilities) -> None:
    if request.operation not in capabilities.operations:
        raise ValueError(ErrorCode.CAPABILITY_UNAVAILABLE.value)


class SceneDiff(StrictModel):
    created: list[UUID] = Field(default_factory=list)
    updated: list[UUID] = Field(default_factory=list)
    deleted: list[UUID] = Field(default_factory=list)


class CaptureResult(StrictModel):
    """Verified scene-bound offscreen PNG and transport metadata."""
    mode: Literal["viewport", "camera"]
    source: Literal["offscreen"]
    mime_type: Literal["image/png"]
    width: int = Field(ge=1, le=2000)
    height: int = Field(ge=1, le=2000)
    sha256: Sha256
    image_base64: str
    scene_fingerprint: Sha256


class ResultPayload(StrictModel):
    objects: list[ObjectRef] = Field(default_factory=list)
    artifact_ids: list[UUID] = Field(default_factory=list)
    snapshot_id: UUID | None = None
    scene_diff: SceneDiff = Field(default_factory=SceneDiff)
    summary: str | None = None
    capture: CaptureResult | None = None


class StudioError(StrictModel):
    code: ErrorCode
    message: NonEmpty
    retryable: bool = False
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class StudioResponse(StrictModel):
    contract: ContractVersion = Field(default_factory=ContractVersion)
    request_id: UUID
    scene_id: UUID
    scene_revision: int = Field(ge=0)
    status: Literal["succeeded", "accepted", "failed"]
    result: ResultPayload | None = None
    job_id: UUID | None = None
    error: StudioError | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_variant(self):
        if self.status == "succeeded" and (self.result is None or self.error or self.job_id):
            raise ValueError("succeeded requires result only")
        if self.status == "accepted" and (self.job_id is None or self.error or self.result):
            raise ValueError("accepted requires job_id only")
        if self.status == "failed" and (self.error is None or self.result or self.job_id):
            raise ValueError("failed requires error only")
        return self


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Recovery(StrictModel):
    reason: Literal["lease_expired", "worker_restart"]
    replay_safe: Literal[True]
    checkpoint_uri: Uri | None = None


class Job(StrictModel):
    contract: ContractVersion = Field(default_factory=ContractVersion)
    job_id: UUID
    request_id: UUID
    owner_id: NonEmpty
    idempotency_key: NonEmpty
    payload_sha256: Sha256
    operation: NonEmpty
    scene_id: UUID
    scene_revision: int = Field(ge=0)
    state: JobState
    progress: float = Field(ge=0, le=1)
    cancel_requested: bool = False
    stop_confirmed: bool = False
    attempt: int = Field(default=1, ge=1)
    timeout_seconds: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    lease_expires_at: datetime | None = None
    recovery: Recovery | None = None
    error: StudioError | None = None
    artifact_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_state(self):
        times = [self.created_at, self.updated_at]
        if self.lease_expires_at is not None:
            times.append(self.lease_expires_at)
        if any(value.utcoffset() is None for value in times):
            raise ValueError("job timestamps must include timezone")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")
        if self.state == JobState.QUEUED and self.lease_expires_at is not None:
            raise ValueError("queued job cannot retain a lease")
        if self.state == JobState.QUEUED and self.attempt == 1 and self.progress != 0:
            raise ValueError("initial queued progress must be zero")
        if self.state == JobState.RUNNING and (
            self.lease_expires_at is None or self.lease_expires_at <= self.updated_at
        ):
            raise ValueError("running requires a live lease")
        if self.state == JobState.SUCCEEDED and (self.progress != 1 or self.error):
            raise ValueError("succeeded requires progress=1 and no error")
        if self.state == JobState.FAILED and self.error is None:
            raise ValueError("failed requires error")
        if self.state == JobState.CANCELLED and not (self.cancel_requested and self.stop_confirmed):
            raise ValueError("cancelled requires cancel request and confirmed stop")
        if self.stop_confirmed and self.state != JobState.CANCELLED:
            raise ValueError("stop_confirmed is only valid for cancelled jobs")
        if self.state in (JobState.QUEUED, JobState.RUNNING) and self.error is not None:
            raise ValueError("nonterminal job cannot carry a terminal error")
        if self.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED) and self.lease_expires_at:
            raise ValueError("terminal job cannot retain a lease")
        return self


ALLOWED_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.QUEUED: {JobState.RUNNING, JobState.FAILED, JobState.CANCELLED},
    JobState.RUNNING: {JobState.QUEUED, JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED},
    JobState.SUCCEEDED: set(),
    JobState.FAILED: set(),
    JobState.CANCELLED: set(),
}
_IMMUTABLE_JOB_FIELDS = (
    "contract", "job_id", "request_id", "owner_id", "idempotency_key", "payload_sha256",
    "operation", "scene_id", "scene_revision", "timeout_seconds", "created_at",
)


def validate_transition(previous: Job, current: Job) -> None:
    """Validate persisted updates; the runner verifies stop/replay facts first."""
    Job.model_validate(previous.model_dump())
    Job.model_validate(current.model_dump())
    if previous.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
        if current != previous:
            raise ValueError("terminal job is immutable")
        return
    if any(getattr(previous, field) != getattr(current, field) for field in _IMMUTABLE_JOB_FIELDS):
        raise ValueError("job identity or immutable input changed")
    if current.updated_at < previous.updated_at or current.progress < previous.progress:
        raise ValueError("job time or progress moved backwards")
    if previous.cancel_requested and not current.cancel_requested:
        raise ValueError("cancel request cannot be withdrawn")
    if current.state != previous.state and current.state not in ALLOWED_TRANSITIONS[previous.state]:
        raise ValueError(f"invalid transition {previous.state.value} -> {current.state.value}")
    recovering = previous.state == JobState.RUNNING and current.state == JobState.QUEUED
    if recovering:
        if current.attempt != previous.attempt + 1 or current.recovery is None:
            raise ValueError("recovery requeue requires attempt+1 and replay evidence")
        if current.cancel_requested:
            raise ValueError("cannot requeue while cancellation is pending")
        if current.recovery.reason == "lease_expired" and current.updated_at < previous.lease_expires_at:
            raise ValueError("cannot recover before lease expiry")
    elif current.attempt != previous.attempt or current.recovery != previous.recovery:
        raise ValueError("attempt and recovery only change during recovery requeue")


class Provenance(StrictModel):
    source_commit: GitCommit
    scene_id: UUID
    scene_revision: int = Field(ge=0)
    blender_version: NonEmpty
    worker_version: NonEmpty
    input_sha256: list[Sha256] = Field(default_factory=list)
    model_id: NonEmpty | None = None
    model_license: NonEmpty | None = None
    asset_licenses: list[NonEmpty] = Field(default_factory=list)

    @model_validator(mode="after")
    def model_license_pair(self):
        if (self.model_id is None) != (self.model_license is None):
            raise ValueError("model ID and license must be supplied together")
        return self


class Artifact(StrictModel):
    artifact_id: UUID
    job_id: UUID
    kind: Literal["blend", "glb", "png", "mp4", "manifest", "other"]
    uri: Uri
    bytes: int = Field(gt=0)
    sha256: Sha256
    version: int = Field(ge=1)
    provenance: Provenance


class ArtifactManifest(StrictModel):
    contract: ContractVersion = Field(default_factory=ContractVersion)
    job_id: UUID
    artifacts: list[Artifact] = Field(min_length=1)
    created_at: datetime

    @model_validator(mode="after")
    def same_job(self):
        if self.created_at.utcoffset() is None:
            raise ValueError("manifest timestamp must include timezone")
        if any(item.job_id != self.job_id for item in self.artifacts):
            raise ValueError("artifact job_id does not match manifest")
        if len({item.artifact_id for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("duplicate artifact_id")
        return self


def json_schemas() -> dict[str, dict]:
    """Generate the published JSON Schemas from the runtime validators."""
    return {
        "request": _request_adapter.json_schema(),
        "response": StudioResponse.model_json_schema(),
        "job": Job.model_json_schema(),
        "artifact_manifest": ArtifactManifest.model_json_schema(),
        "capabilities": Capabilities.model_json_schema(),
    }
