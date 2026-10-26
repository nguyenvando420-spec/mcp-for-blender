# BAS-05 — Typed FastMCP Studio bridge (implementation evidence)

Date: 2026-10-26. Scope: first safe Studio vertical slice with capability negotiation and client-profile enforcement. Branch: feature_bas05-typed-studio.

## Surface

Three MCP tool names:
- studio_handshake: add-on v15+ capabilities, explicit feature/operation list, client profile, current Python executable and installed package path; does not infer support from protocol alone.
- studio_scene_inspect(request): BAS-03 strict JSON envelope; gets typed read-only scene payload with provisional scene UUID/revision.
- studio_create_object(request): BAS-03 strict envelope; limited to primitive cube/sphere/cylinder/cone/plane; add-on implements operation on Blender's main thread, never via execute_code.

The request object is JSON at the MCP surface to retain request_id when reporting INVALID_INPUT; it is parsed into strict discriminated Pydantic BAS-03 types **before** command dispatch. Successful/failed replies are validated StudioResponse, with request_id, code and scene revision. No arbitrary Python or unknown operation dispatch path is exposed by Studio tools.

Profile visibility and tools/call are both checked: observer allows handshake and inspect; studio-local and acknowledged developer-local additionally allow create; remote and worker publish NO tools until BAS-21 authenticates their endpoints. In all non-developer profiles legacy execute_blender_code remains hidden/blocked by BAS-04. Add-on's raw localhost socket remains unauthenticated until BAS-06; this is NOT a security sandbox.

The add-on stores scene UUID/revision as custom properties; primitive objects get UUID, and idempotency key cache lives in the scene properties. Replays of the same normalized payload return the recorded result rather than creating a second object; mismatched payloads yield conflict. This is a transitional main-thread implementation, **not** BAS-08's full edit-tracking/stable ID system or BAS-12's durable atomic snapshot. Direct user edits are not yet revision-tracked; do not use these identifiers for destructive operations.

## Job and capability semantics

studio_render_still, studio_export_artifacts and other potentially long operations are **not** advertised, registered or dispatched. Their subsequent implementation must return BAS-03 Job handles immediately after durable enqueue (BAS-06/12/13), with bounded tool calls; this PR does not queue imaginary jobs or synchronously block on renders. Unsupported requests fail capability_unavailable.

## Commands for acceptance

- uv run --frozen --python 3.12.14 --with pytest==8.4.2 pytest -q tests/test_studio_gateway.py tests/test_studio_contracts.py tests/test_security_policy.py tests/test_fork_baseline.py
- uv run --frozen --python 3.12.14 --with pytest==8.4.2 pytest -q
- uv run --frozen --python 3.12.14 python -m compileall -q src tests
- uv run --frozen --python 3.12.14 mcp-for-blender --help (must log local checkout/package path in handshake)
- Run CI's Blender 4.5.15 golden headless smoke; then interactive Blender GUI MCP STDIO client acceptance: handshake => scene inspect => create cube => inspect and verify UUID/revision. Local fixture tests cannot replace GUI acceptance.

Environment baseline: gateway Python 3.12.14, Blender 4.5.15 (pinned by BAS-02), contract major 1, addon protocol 15. Actual OS/client path/version should be captured from handshake and CI run; **no runtime execution claimed** in this document before a real CI/client report.

Outstanding: real MCP stdio checkout/GUI acceptance evidence, independent review, confirmed CI green and durable job handles for long operations. Do not mark BAS-05 Done until acceptance and merge.
