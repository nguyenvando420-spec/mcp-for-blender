"""BAS-04 policy primitives for Studio requests and legacy MCP exposure.

This is an application authorization boundary, NOT an OS, Python, or Blender
sandbox. Add-on socket authentication, atomic file opens and network fetch
enforcement must be implemented at their respective runtime boundaries.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import secrets
import struct
import time
from urllib.parse import unquote, urlsplit
from uuid import UUID

from .studio.contracts import EffectScope, STUDIO_OPERATIONS, validate_request

PROFILE_ENV = "BLENDER_MCP_PROFILE"
DEV_ACK_ENV = "BLENDER_MCP_DEV_ACK"
DEV_ACK_VALUE = "I_ACCEPT_UNRESTRICTED_LOCAL_PYTHON"
MAX_COMMAND_BYTES = 1024 * 1024  # command framing in BAS-06 must enforce this too
MAX_URL_LENGTH = 2048
MAX_CONFIRMATION_AGE_SECONDS = 300

READ_OPERATIONS = frozenset({"studio_scene_inspect", "studio_capture_viewport"})
FILE_OPERATIONS = frozenset({
    "studio_scene_save", "studio_scene_open", "studio_scene_snapshot",
    "studio_scene_restore", "studio_render_still", "studio_export_artifacts",
})
CONFIRM_OPERATIONS = frozenset({
    "studio_scene_open", "studio_scene_restore", "studio_delete_object",
})
OBSERVE_LEGACY_TOOLS = frozenset({
    "get_addon_status", "get_scene_info", "disable_telemetry", "look",
    "viewport_latest", "viewport_capture", "viewport_pick",
})


class Profile(str, Enum):
    OBSERVER = "observer"
    STUDIO_LOCAL = "studio-local"
    DEVELOPER_LOCAL = "developer-local"
    WORKER = "worker"
    REMOTE = "remote"


class PolicyDenied(PermissionError):
    """Stable permission_denied error; the caller must not execute a side effect."""

    code = "permission_denied"


def profile_from_env(environ: dict[str, str] | None = None) -> Profile:
    env = os.environ if environ is None else environ
    try:
        profile = Profile(env.get(PROFILE_ENV, Profile.OBSERVER.value))
    except ValueError as exc:
        raise PolicyDenied("Unknown Blender MCP profile; fail closed") from exc
    if profile is Profile.DEVELOPER_LOCAL and env.get(DEV_ACK_ENV) != DEV_ACK_VALUE:
        raise PolicyDenied(f"{DEV_ACK_ENV} must explicitly acknowledge raw Python")
    return profile


def require_loopback(host: str) -> None:
    """Literal loopback only: DNS aliases and public interface bindings are denied."""
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise PolicyDenied("Blender bridge must use a loopback address")


def raw_python_allowed(profile: Profile) -> bool:
    return profile is Profile.DEVELOPER_LOCAL


def exposed_legacy_tools(profile: Profile, registered: set[str]) -> set[str]:
    """Studio tools are not registered by BAS-04; other legacy tools stay hidden."""
    if profile is Profile.DEVELOPER_LOCAL:
        return registered
    if profile in (Profile.OBSERVER, Profile.STUDIO_LOCAL):
        return registered & OBSERVE_LEGACY_TOOLS
    return set()  # worker and remote need authenticated, purpose-built gateways


@dataclass(frozen=True)
class Policy:
    profile: Profile
    read_roots: tuple[Path, ...] = ()
    write_roots: tuple[Path, ...] = ()
    egress_hosts: frozenset[str] = field(default_factory=frozenset)
    authenticated: bool = False
    worker_job_id: str | None = None


def _under_root(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def canonical_allowed_file(uri: str, roots: tuple[Path, ...], *, writing: bool,
                           overwrite: bool = False) -> Path:
    """Validate a local file URI; no file is opened by this preflight check.

    Rejects parent traversal, symlinks (even symlinks into allowed roots),
    UNC authorities and non-file schemes. BAS-12 must use an atomic dirfd /
    no-follow open to close TOCTOU between validation and file access.
    """
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        raise PolicyDenied("Only local file:// URIs are permitted")
    if parsed.query or parsed.fragment or not parsed.path:
        raise PolicyDenied("File URI query/fragment or empty path denied")
    if "%2f" in parsed.path.lower() or "%5c" in parsed.path.lower():
        raise PolicyDenied("Encoded path separators denied")
    pathname = unquote(parsed.path)
    if "\x00" in pathname or "\\" in pathname:
        raise PolicyDenied("Invalid path characters")
    raw = Path(pathname)
    if not raw.is_absolute() or ".." in raw.parts:
        raise PolicyDenied("Absolute canonical path without traversal required")
    candidate = raw.resolve(strict=False)
    for configured in roots:
        try:
            root = Path(configured).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PolicyDenied("Configured allowed root is invalid") from exc
        # Require both the lexical and resolved path to be under the same root;
        # a symlink OUTSIDE that points INTO the root is not authorized.
        if not root.is_dir() or not _under_root(raw, root) or not _under_root(candidate, root):
            continue
        # Symlink in any component below the configured root is not trusted.
        cur = root
        for part in raw.relative_to(root).parts:
            cur = cur / part
            if cur.is_symlink():
                raise PolicyDenied("Symlink path component denied")
        if writing:
            if candidate.exists() and not overwrite:
                raise PolicyDenied("Existing output requires overwrite authorization")
            if not candidate.parent.is_dir():
                raise PolicyDenied("Output parent directory does not exist")
        elif not candidate.is_file():
            raise PolicyDenied("Input file does not exist")
        return candidate
    raise PolicyDenied("File path lies outside configured allowed roots")


def check_egress_url(url: str, allowed_hosts: frozenset[str]) -> str:
    """Preflight only; the fetcher must pin/check DNS, redirects, TLS and size."""
    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
        raise PolicyDenied("URL exceeds maximum length or is not a string")
    if any(c in url for c in ("\r", "\n", "\x00", "\\")):
        raise PolicyDenied("Malformed network URL")
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
            raise PolicyDenied("Only credential-free HTTPS URLs are permitted")
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise PolicyDenied("Invalid network authority or port") from exc
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise PolicyDenied("IP literal network destinations are denied")
    if not host or host not in allowed_hosts or port not in (None, 443):
        raise PolicyDenied("URL destination is not on egress allowlist")
    if not parsed.netloc:
        raise PolicyDenied("Malformed network URL")
    return url


# Compact binary tokens fit the v1 contract's 256-character NonEmpty field.
# Layout: operation(1), scene UUID(16), revision(8), snapshot UUID(16),
# expiry(8), random nonce(16), HMAC-SHA256(32). BASE64URL output = 130 chars.
_TOKEN_STRUCT = struct.Struct("!B16sQ16sQ16s")
_CONFIRM_CODES = {
    "studio_scene_open": 1,
    "studio_scene_restore": 2,
    "studio_delete_object": 3,
    "studio_scene_save": 4,
    "studio_render_still": 5,
    "studio_export_artifacts": 6,
}


def _confirmation_fields(operation: str, scene_id: str, scene_revision: int,
                         snapshot_id: str | None) -> tuple[int, bytes, int, bytes]:
    try:
        code = _CONFIRM_CODES[operation]
        if not (0 <= scene_revision < 2**64):
            raise ValueError("Invalid scene revision")
        no_snapshot = operation in ("studio_scene_save", "studio_render_still",
                                    "studio_export_artifacts")
        if not no_snapshot and snapshot_id is None:
            raise ValueError("Snapshot required")
        if no_snapshot and snapshot_id is not None:
            raise ValueError("Unexpected snapshot")
        return (code, UUID(scene_id).bytes, scene_revision,
                UUID(snapshot_id).bytes if snapshot_id else bytes(16))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise PolicyDenied("Invalid operation, scene or snapshot binding") from exc


def issue_confirmation(secret: bytes, *, operation: str, scene_id: str,
                       scene_revision: int, snapshot_id: str | None = None,
                       now: int | None = None) -> str:
    """Trusted UI/backend only: user confirmation must precede issuance."""
    if len(secret) < 32:
        raise PolicyDenied("Invalid confirmation issuer key")
    fields = _confirmation_fields(operation, scene_id, scene_revision, snapshot_id)
    timestamp = int(time.time()) if now is None else now
    try:
        payload = _TOKEN_STRUCT.pack(
            fields[0], fields[1], fields[2], fields[3],
            timestamp + MAX_CONFIRMATION_AGE_SECONDS, secrets.token_bytes(16)
        )
    except (struct.error, OverflowError, ValueError) as exc:
        raise PolicyDenied("Invalid confirmation timestamp") from exc
    signature = hmac.new(secret, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")


def verify_confirmation(secret: bytes, token: str, *, operation: str, scene_id: str,
                        scene_revision: int, snapshot_id: str | None = None,
                        now: int | None = None) -> None:
    if len(secret) < 32 or not isinstance(token, str) or len(token) > 256:
        raise PolicyDenied("Invalid confirmation key/token")
    expected = _confirmation_fields(operation, scene_id, scene_revision, snapshot_id)
    try:
        decoded = base64.b64decode(
            token + "=" * (-len(token) % 4), altchars=b"-_", validate=True
        )
        if len(decoded) != _TOKEN_STRUCT.size + 32:
            raise ValueError("Invalid token size")
        payload, signature = decoded[:-32], decoded[-32:]
        if not hmac.compare_digest(hmac.new(secret, payload, hashlib.sha256).digest(), signature):
            raise ValueError("MAC mismatch")
        code, scene, revision, snapshot, expiration, nonce = _TOKEN_STRUCT.unpack(payload)
        if (code, scene, revision, snapshot) != expected:
            raise ValueError("Bound operation/scene mismatch")
        timestamp = int(time.time()) if now is None else now
        if not (timestamp < expiration <= timestamp + MAX_CONFIRMATION_AGE_SECONDS):
            raise ValueError("Expired or future confirmation")
        if not any(nonce):
            raise ValueError("Missing nonce")
    except (ValueError, TypeError, binascii.Error, struct.error, OverflowError) as exc:
        raise PolicyDenied("Confirmation invalid, stale or not bound to scene") from exc



# Versioned resource-bound confirmations for destructive file/object actions.
# v1 confirmations remain supported for scene_restore; v2 prevents a valid
# delete or overwrite confirmation being redirected to another target.
_RESOURCE_TOKEN_STRUCT = struct.Struct("!B16sQ16sQ16s32s")


def issue_resource_confirmation(secret: bytes, *, operation: str, scene_id: str,
                                scene_revision: int, resource: str,
                                snapshot_id: str | None = None,
                                now: int | None = None) -> str:
    """Trusted UI/backend ONLY, after displaying the actual resource preview."""
    if len(secret) < 32 or not isinstance(resource, str) or not resource:
        raise PolicyDenied("Invalid resource confirmation")
    fields = _confirmation_fields(operation, scene_id, scene_revision,
                                  snapshot_id if operation not in ("studio_scene_save",
                                      "studio_render_still", "studio_export_artifacts") else None)
    exp = int(time.time()) if now is None else now
    try:
        data = _RESOURCE_TOKEN_STRUCT.pack(
            *fields, exp + MAX_CONFIRMATION_AGE_SECONDS,
            secrets.token_bytes(16),
            hashlib.sha256(resource.encode("utf-8")).digest())
    except (ValueError, struct.error, OverflowError) as exc:
        raise PolicyDenied("Invalid confirmation payload") from exc
    return base64.urlsafe_b64encode(
        data + hmac.new(secret, b"BAS12-RESOURCE-V2" + data, hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")


def verify_resource_confirmation(secret: bytes, token: str, *,
                                 operation: str, scene_id: str,
                                 scene_revision: int, resource: str,
                                 snapshot_id: str | None = None,
                                 now: int | None = None) -> None:
    if len(secret) < 32 or not isinstance(token, str) or not isinstance(resource, str):
        raise PolicyDenied("Missing trusted resource confirmation")
    fields = _confirmation_fields(operation, scene_id, scene_revision,
                                  snapshot_id if operation not in ("studio_scene_save",
                                      "studio_render_still", "studio_export_artifacts") else None)
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4),
                               altchars=b"-_", validate=True)
        if len(raw) != _RESOURCE_TOKEN_STRUCT.size + 32:
            raise ValueError("Resource confirmation version mismatch")
        data, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(hmac.new(
                secret, b"BAS12-RESOURCE-V2" + data, hashlib.sha256).digest(), signature):
            raise ValueError("Invalid confirmation signature")
        code, sid, revision, snapshot, expires, nonce, digest = _RESOURCE_TOKEN_STRUCT.unpack(data)
        current = int(time.time()) if now is None else now
        if (code, sid, revision, snapshot) != fields or not current < expires <= current + 300:
            raise ValueError("Expired or mismatched scene confirmation")
        if not any(nonce) or not hmac.compare_digest(
                digest, hashlib.sha256(resource.encode("utf-8")).digest()):
            raise ValueError("Mismatched confirmation resource")
    except (ValueError, TypeError, binascii.Error, struct.error) as exc:
        raise PolicyDenied("Resource confirmation missing/invalid/stale") from exc


def authorize_studio_request(data: dict, policy: Policy, *,
                             confirmation_secret: bytes | None = None,
                             now: int | None = None):
    """Fail-closed preflight; validates typed inputs, capability and file scope.

    A higher-trust dispatcher MUST validate live scene revision, spent nonce,
    snapshot digest and atomic filesystem effects before dispatch.
    """
    if len(json.dumps(data, default=str).encode("utf-8")) > MAX_COMMAND_BYTES:
        raise PolicyDenied("Studio payload exceeds command limit")
    request = validate_request(data)  # Pydantic raises invalid_input before any side effect
    allowed = {
        Profile.OBSERVER: READ_OPERATIONS,
        Profile.STUDIO_LOCAL: STUDIO_OPERATIONS,
        Profile.DEVELOPER_LOCAL: STUDIO_OPERATIONS,
        Profile.WORKER: frozenset({"studio_render_still", "studio_export_artifacts"}),
        Profile.REMOTE: STUDIO_OPERATIONS,
    }[policy.profile]
    if request.operation not in allowed:
        raise PolicyDenied("Operation not allowlisted for this profile")
    if (request.operation == "studio_scene_inspect"
            and getattr(request, "bind_missing_ids", False)
            and policy.profile not in (Profile.STUDIO_LOCAL, Profile.DEVELOPER_LOCAL)):
        raise PolicyDenied("ID binding requires authorized local scene mutation")
    if policy.profile is Profile.REMOTE and not policy.authenticated:
        raise PolicyDenied("Remote caller requires authenticated API gateway")
    if policy.profile is Profile.WORKER and not policy.worker_job_id:
        raise PolicyDenied("Worker requires a verified job identity")
    if request.effect_scope is EffectScope.FILE and not policy.write_roots:
        raise PolicyDenied("No writable sandbox roots configured")
    for field_name in ("input_uri", "output_uri"):
        uri = getattr(request, field_name, None)
        if uri is not None:
            is_output = field_name == "output_uri"
            canonical_allowed_file(
                uri, policy.write_roots if is_output else policy.read_roots,
                writing=is_output,
                # Artifact jobs may replay an existing idempotency key. The
                # worker checks the durable payload digest before allowing
                # replay; it still rejects overwriting any destination.
                overwrite=(bool(getattr(request, "overwrite", False))
                           or request.operation in ("studio_render_still",
                                                    "studio_export_artifacts")),
            )
    spec = getattr(request, "spec", None)
    texture_uri = getattr(spec, "texture_uri", None)
    if texture_uri:
        if texture_uri.startswith("file://"):
            canonical_allowed_file(texture_uri, policy.read_roots, writing=False)
        else:
            check_egress_url(texture_uri, policy.egress_hosts)
    needs_confirmation = (request.operation in CONFIRM_OPERATIONS or
                          (request.operation in ("studio_scene_save",
                            "studio_render_still", "studio_export_artifacts") and request.overwrite))
    if needs_confirmation:
        if not confirmation_secret:
            raise PolicyDenied("Trusted confirmation verifier is unavailable")
        if request.operation == "studio_scene_open":
            resource = str(request.input_uri) + "|" + str(request.expected_sha256)
        elif request.operation in ("studio_scene_save", "studio_render_still",
                                  "studio_export_artifacts"):
            resource = str(request.output_uri) + "|" + str(request.expected_sha256)
        elif request.operation == "studio_delete_object":
            resource = str(request.object_id)
        else:
            resource = None
        if resource is not None:
            verify_resource_confirmation(
                confirmation_secret, request.confirmation_token,
                operation=request.operation, scene_id=str(request.scene_id),
                scene_revision=request.scene_revision, resource=resource,
                snapshot_id=str(request.snapshot_id) if getattr(request, "snapshot_id", None) else None,
                now=now)
        else:
            verify_confirmation(
                confirmation_secret, request.confirmation_token,
                operation=request.operation, scene_id=str(request.scene_id),
                scene_revision=request.scene_revision,
                snapshot_id=str(request.snapshot_id) if getattr(request, "snapshot_id", None) else None,
                now=now)
    return request
