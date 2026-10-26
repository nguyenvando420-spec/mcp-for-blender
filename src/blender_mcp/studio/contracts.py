"""BAS-03 wire contracts for future Studio adapters.

These models validate boundaries only. They do not dispatch to Blender, persist
jobs, assign stable IDs, or authorize paths. Later tasks must implement those
behaviours before exposing the corresponding operation.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, ClassVar, Literal, Union
from uuid import UUID
import json

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitCommit = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Vec3 = tuple[float, float, float]
Color4 = tuple[float, float, float, float]
STUDIO_OPERATIONS = frozenset({
    "studio_inspect_scene", "studio_capture_viewport", "studio_create_primitive",
    "studio_create_text", "studio_transform_object", "studio_manage_collection",
    "studio_delete_object", "studio_set_material", "studio_set_camera",
    "studio_set_light", "studio_save_scene", "studio_open_scene",
    "studio_snapshot_scene", "studio_render_still", "studio_export_glb",
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
    operations: set[str]
    features: set[str] = Field(default_factory=set)

    @field_validator("operations")
    @classmethod
    def known_operations(cls, value: set[str]) -> set[str]:
        unknown = value - STUDIO_OPERATIONS
        if unknown:
            raise ValueError(f"unknown Studio operations: {sorted(unknown)}")
        return value


class ObjectRef(StrictModel):
    """Persistent UUID stored on the Blender datablock; name is only a label."""

    id: UUID
    name: NonEmpty
    kind: Literal["object", "collection", "material", "camera", "light"]


class RequestBase(StrictModel):
    contract: ContractVersion = Field(default_factory=ContractVersion)
    request_id: UUID
    scene_revision: int = Field(ge=0)
    effect_scope: EffectScope
    required_scope: ClassVar[EffectScope]

    @model_validator(mode="after")
    def check_effect_scope(self):
        if self.effect_scope != self.required_scope:
            raise ValueError(f"{self.operation} requires effect_scope={self.required_scope.value}")
        return self


class InspectScene(RequestBase):
    operation: Literal["studio_inspect_scene"] = "studio_inspect_scene"
    required_scope = EffectScope.READ
    query: str | None = None
    limit: int = Field(default=20, ge=1, le=500)


class CaptureViewport(RequestBase):
    operation: Literal["studio_capture_viewport"] = "studio_capture_viewport"
    required_scope = EffectScope.READ
    max_size: int = Field(default=768, ge=200, le=2000)


class CreatePrimitive(RequestBase):
    operation: Literal["studio_create_primitive"] = "studio_create_primitive"
    required_scope = EffectScope.SCENE
    primitive: Literal["cube", "sphere", "cylinder", "cone", "plane"]
    name: NonEmpty
    location: Vec3 = (0.0, 0.0, 0.0)


class CreateText(RequestBase):
    operation: Literal["studio_create_text"] = "studio_create_text"
    required_scope = EffectScope.SCENE
    text: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    name: NonEmpty
    location: Vec3 = (0.0, 0.0, 0.0)


class TransformObject(RequestBase):
    operation: Literal["studio_transform_object"] = "studio_transform_object"
    required_scope = EffectScope.SCENE
    object_id: UUID
    location: Vec3 | None = None
    rotation_euler: Vec3 | None = None
    scale: Vec3 | None = None

    @model_validator(mode="after")
    def has_change(self):
        if all(v is None for v in (self.location, self.rotation_euler, self.scale)):
            raise ValueError("at least one transform field is required")
        return self


class ManageCollection(RequestBase):
    operation: Literal["studio_manage_collection"] = "studio_manage_collection"
    required_scope = EffectScope.SCENE
    action: Literal["create", "move_object", "rename"]
    name: NonEmpty | None = None
    collection_id: UUID | None = None
    object_id: UUID | None = None

    @model_validator(mode="after")
    def check_action(self):
        if self.action == "create" and not self.name:
            raise ValueError("create requires name")
        if self.action == "move_object" and not (self.collection_id and self.object_id):
            raise ValueError("move_object requires collection_id and object_id")
        if self.action == "rename" and not (self.collection_id and self.name):
            raise ValueError("rename requires collection_id and name")
        return self


class DeleteObject(RequestBase):
    operation: Literal["studio_delete_object"] = "studio_delete_object"
    required_scope = EffectScope.SCENE
    object_id: UUID
    snapshot_id: UUID


class SetMaterial(RequestBase):
    operation: Literal["studio_set_material"] = "studio_set_material"
    required_scope = EffectScope.SCENE
    object_id: UUID
    base_color: Color4
    metallic: float = Field(ge=0, le=1)
    roughness: float = Field(ge=0, le=1)

    @field_validator("base_color")
    @classmethod
    def valid_color(cls, value: Color4) -> Color4:
        if any(channel < 0 or channel > 1 for channel in value):
            raise ValueError("color channels must be in [0, 1]")
        return value


class SetCamera(RequestBase):
    operation: Literal["studio_set_camera"] = "studio_set_camera"
    required_scope = EffectScope.SCENE
    camera_id: UUID | None = None
    location: Vec3
    rotation_euler: Vec3
    lens_mm: float = Field(default=50, gt=0, le=1000)


class SetLight(RequestBase):
    operation: Literal["studio_set_light"] = "studio_set_light"
    required_scope = EffectScope.SCENE
    light_id: UUID | None = None
    light_type: Literal["POINT", "SUN", "SPOT", "AREA"]
    energy: float = Field(ge=0)
    color: Vec3
    location: Vec3

    @field_validator("color")
    @classmethod
    def valid_color(cls, value: Vec3) -> Vec3:
        if any(channel < 0 or channel > 1 for channel in value):
            raise ValueError("color channels must be in [0, 1]")
        return value


class SaveScene(RequestBase):
    operation: Literal["studio_save_scene"] = "studio_save_scene"
    required_scope = EffectScope.FILE
    output_uri: NonEmpty
    overwrite: bool = False


class OpenScene(RequestBase):
    operation: Literal["studio_open_scene"] = "studio_open_scene"
    required_scope = EffectScope.FILE
    input_uri: NonEmpty
    expected_sha256: Sha256


class SnapshotScene(RequestBase):
    operation: Literal["studio_snapshot_scene"] = "studio_snapshot_scene"
    required_scope = EffectScope.FILE
    label: NonEmpty | None = None


class RenderStill(RequestBase):
    operation: Literal["studio_render_still"] = "studio_render_still"
    required_scope = EffectScope.FILE
    width: int = Field(ge=1, le=16384)
    height: int = Field(ge=1, le=16384)
    samples: int = Field(ge=1, le=65536)
    output_uri: NonEmpty
    timeout_seconds: int = Field(default=600, ge=1, le=86400)


class ExportGlb(RequestBase):
    operation: Literal["studio_export_glb"] = "studio_export_glb"
    required_scope = EffectScope.FILE
    output_uri: NonEmpty
    object_ids: list[UUID] = Field(min_length=1)


StudioRequest = Annotated[
    Union[
        InspectScene, CaptureViewport, CreatePrimitive, CreateText, TransformObject,
        ManageCollection, DeleteObject, SetMaterial, SetCamera, SetLight,
        SaveScene, OpenScene, SnapshotScene, RenderStill, ExportGlb,
    ],
    Field(discriminator="operation"),
]
_request_adapter = TypeAdapter(StudioRequest)


def validate_request(data: dict) -> StudioRequest:
    """Validate a v1 request, including its operation-specific fields."""
    return _request_adapter.validate_json(json.dumps(data))


class ResultPayload(StrictModel):
    objects: list[ObjectRef] = Field(default_factory=list)
    artifact_ids: list[UUID] = Field(default_factory=list)
    summary: str | None = None


class StudioResponse(StrictModel):
    contract: ContractVersion = Field(default_factory=ContractVersion)
    request_id: UUID
    scene_revision: int = Field(ge=0)
    status: Literal["succeeded", "accepted", "failed"]
    result: ResultPayload | None = None
    job_id: UUID | None = None
    error: StudioError | None = None

    @model_validator(mode="after")
    def valid_variant(self):
        if self.status == "succeeded" and (self.result is None or self.error or self.job_id):
            raise ValueError("succeeded requires result only")
        if self.status == "accepted" and (self.job_id is None or self.error or self.result):
            raise ValueError("accepted requires job_id only")
        if self.status == "failed" and (self.error is None or self.result or self.job_id):
            raise ValueError("failed requires error only")
        return self


class StudioError(StrictModel):
    code: ErrorCode
    message: NonEmpty
    retryable: bool = False
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(StrictModel):
    contract: ContractVersion = Field(default_factory=ContractVersion)
    job_id: UUID
    request_id: UUID
    idempotency_key: NonEmpty
    operation: NonEmpty
    state: JobState
    progress: float = Field(ge=0, le=1)
    cancel_requested: bool = False
    attempt: int = Field(default=1, ge=1)
    timeout_seconds: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    lease_expires_at: datetime | None = None
    error: StudioError | None = None
    artifact_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_state(self):
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("job timestamps must include timezone")
        if self.lease_expires_at is not None and self.lease_expires_at.tzinfo is None:
            raise ValueError("lease timestamp must include timezone")
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
        if self.state == JobState.CANCELLED and not self.cancel_requested:
            raise ValueError("cancelled requires acknowledged cancel request")
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


def validate_transition(previous: Job, current: Job) -> None:
    """Reject impossible durable updates; a cancel request alone does not transition."""
    Job.model_validate(previous.model_dump())
    Job.model_validate(current.model_dump())
    if previous.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
        if current != previous:
            raise ValueError("terminal job is immutable")
        return
    if (previous.job_id, previous.request_id, previous.idempotency_key) != (
        current.job_id, current.request_id, current.idempotency_key
    ):
        raise ValueError("job identity changed")
    if current.updated_at < previous.updated_at or current.progress < previous.progress:
        raise ValueError("job time or progress moved backwards")
    if previous.cancel_requested and not current.cancel_requested:
        raise ValueError("cancel request cannot be withdrawn")
    if current.state != previous.state and current.state not in ALLOWED_TRANSITIONS[previous.state]:
        raise ValueError(f"invalid transition {previous.state.value} -> {current.state.value}")
    if current.state == JobState.CANCELLED and not current.cancel_requested:
        raise ValueError("cancelled requires acknowledged cancel request")
    if current.attempt < previous.attempt or current.attempt > previous.attempt + 1:
        raise ValueError("invalid attempt increment")
    if current.state == JobState.QUEUED and previous.state == JobState.RUNNING:
        if current.attempt != previous.attempt + 1:
            raise ValueError("recovery requeue must increment attempt")


class Provenance(StrictModel):
    source_commit: GitCommit | None = None
    blender_version: NonEmpty
    worker_version: NonEmpty
    input_sha256: list[Sha256] = Field(default_factory=list)
    model_id: str | None = None
    model_license: str | None = None


class Artifact(StrictModel):
    artifact_id: UUID
    job_id: UUID
    kind: Literal["blend", "glb", "png", "mp4", "manifest", "other"]
    uri: NonEmpty
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
