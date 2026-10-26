# BAS-04 — Security threat model and authorization policy

Status: **implementation proposed for review**, 2026-10-26. Based on BAS-03 merged main \`06823a2\`. This does not claim Blender add-on, worker or public network isolation.

## Boundaries / trust model

An LLM prompt, retrieved asset metadata, Studio request payload, file URI, external download URL, and raw TCP peer are **untrusted inputs**. MCP client authentication is not Blender socket authentication. \`effect_scope\`, supplied scene IDs, capability claims, and syntactically valid confirmation strings **never confer authority**. A trusted controller must mint confirmations after explicit user action; model-facing code must not receive the signing key.

Profiles:

| Profile | MCP legacy exposure | Studio v1 intent | Deployment constraint |
|---|---|---|---|
| \`observer\` (default) | explicit observation tool names only | inspect/capture | local loopback, read only |
| \`studio-local\` | observation tool names only | typed operations, permitted file roots | local trusted desktop; Studio operations not yet registered |
| \`developer-local\` | all legacy tools including arbitrary Python | Studio operations | local **only**; \`BLENDER_MCP_DEV_ACK=I_ACCEPT_UNRESTRICTED_LOCAL_PYTHON\` |
| \`worker\` | no legacy tools | job-scoped render/export | needs verified owner/job and isolated filesystem/process, BAS-21 |
| \`remote\` | no legacy tools | authenticated Studio gateway only | TLS/auth/rate limit/session scopes at BAS-21/22, never raw bridge |

The new gateway profile defaults to \`observer\` and legacy \`execute_blender_code\` additionally refuses execution unless \`developer-local\` is explicitly acknowledged. \`BLENDER_MCP_SAFE_MODE\` is an optional AST filter on an already-authorized developer request, **not** a sandbox. Legacy direct add-on socket remains a bypass until BAS-06. Resources and registered handler defense-in-depth need BAS-05 follow-up; the server's direct Python entry point is guarded.

## Threat matrix (expected denial / owning runtime task)

| # | Abuse case | Expected outcome | Owner |
|---|---|---|---|
| 1 | prompt injection triggers raw Python in Studio | \`permission_denied\`, no command sent | BAS-04/BAS-05 |
| 2 | profile typo / developer profile without explicit acknowledgement | \`permission_denied\` | BAS-04 |
| 3 | direct socket client sends \`execute_code\` without auth | reject unauthenticated command, no execution (**not implemented yet**) | BAS-06 |
| 4 | bridge binds \`0.0.0.0\` or non-loopback | \`permission_denied\` in gateway; reject add-on bind too | BAS-04/BAS-06 |
| 5 | malformed/oversized command and huge JSON response | reject before parsing or allocation, bounded memory (**wire enforcement pending**) | BAS-06 |
| 6 | path \`../\` or \`file://remotehost\` escapes root | \`permission_denied\` | BAS-04/BAS-12 |
| 7 | symlink path points outside root | \`permission_denied\` preflight; atomic no-follow open required | BAS-04/BAS-12 |
| 8 | overwrite existing BLEND without permission | \`permission_denied\`; require scoped confirmation and verified snapshot | BAS-04/BAS-12 |
| 9 | delete/restore/open with stale scene revision | \`stale_scene\` before effects; token binding fails | BAS-08/BAS-12 |
| 10 | replay signed confirmation / tamper operation or snapshot ID | \`permission_denied\`, consume nonce atomically (**nonce ledger pending**) | BAS-04/BAS-12 |
| 11 | URL accesses localhost/cloud metadata, redirects, hostile DNS | deny unless exact allowlisted HTTPS; pin DNS/check all redirect hops (**fetch enforcement pending**) | BAS-04/BAS-20/BAS-24 |
| 12 | telemetry exfiltrates prompt/screenshots without opt-in | no outbound request; mock HTTP test, disabled by default; verify add-on and telemetry decorators | BAS-04/BAS-24 |
| 13 | model weights/assets lacking redistribution grant | quarantine, review license/provenance before deployment | BAS-18/BAS-20 |
| 14 | compromised GPU worker writes unrelated projects/secrets | worker root/job restriction + OS volume sandbox (**pending**) | BAS-21/BAS-24 |
| 15 | client advertises forged \`effect_scope\` or capability | strict schema + explicit caller policy, cannot self-grant | BAS-04/BAS-05 |

## File policy and confirmations

- \`Policy(read_roots,write_roots)\` is explicit and defaults to none. File reads/writes use only \`file://\` URIs, absolute paths, resolved root containment and no symlinks; missing input, missing parent and overwrite without permission are refused.
- Validation is a **preflight** and must never be trusted alone to avoid race conditions: BAS-12 must use no-follow/dirfd-style checked opens, atomically stage + fsync + replace output, and validate file type/hash before loading.
- Open/restore/delete require confirmed **matching operation + scene UUID + revision + snapshot ID**, token MAC, expiration; save overwrite has matching operation + scene binding. \`issue_confirmation\` belongs only in trusted UI/backend. BAS-12 must add durable nonce consumption and snapshot verification. Raw contract-provided strings such as \`open:7\` are not confirmations.
- No path authority is implied by \`artifact://\`; job IDs, artifact URIs and artifact publishing are owned by BAS-13/22. This preflight currently denies non-file output URIs, so typed export must use file URIs until an authenticated artifact-store adapter exists.

## Transport and network

- Local MCP stdio is a boundary only for its own registered entry points. Gateway \`resolve_connection\` permits literal loopback; **add-on socket auth and framing are still missing**. Set 1 MiB command design cap and 180s timeout is legacy, not a DoS mitigation. BAS-06 must enforce wire byte limits, send/receive bounds, HMAC/session auth (or OS local IPC peer verification), endpoint allowlists, timeout cancellation semantics, reconnect tests, and **never** bind Blender socket publicly.
- HTTPS-only egress allowlist has an exact hostname preflight; reject IP literals, embedded credentials, alternate ports, CRLF and length over 2048. Not sufficient for SSRF: BAS-20/24 must resolve and pin permitted public IPs, revalidate redirects, block private/link-local/metadata ranges, cap response bytes and downloads, disable unconfigured providers.
- For self-host, terminate TLS at authenticated API boundary and run isolated GPU worker with per-job credentials. No remote raw socket or Python tool.

## License and provenance gate

| Component | Source / observed license | Evidence and next gate |
|---|---|---|
| fork and upstream \`mcp-for-blender\` | repository \`LICENSE\`, \`pyproject.toml\`: MIT | retain attribution, validate notices on redistribution |
| Blender | Blender Foundation, GPL (verify precise redistribution obligations before bundling binary) | BAS-21 packaging/legal review |
| PyTorch/CUDA, AI model implementations | not chosen by BAS-04 | BAS-18 record exact version/commit/license |
| model weights | not chosen; weights terms may differ from code license | BAS-18 explicit commercial and redistribution matrix |
| imported meshes/materials/textures | license varies by provider and individual asset | BAS-20 per-asset source URL, creator, license, hash |
| generated artifacts | provenance contract set in BAS-03, runtime not available | BAS-13/22 propagate model/version/licensing data |

## Telemetry baseline audit / privacy

- \`src/blender_mcp/config.py\` baseline: \`enabled=False\`, Supabase URL/key/bucket empty. \`telemetry.py\` still has HTTP client paths for events and screenshots, and a local UUID/worker initialization.
- Do not equate disabled configuration with audited absence of all outbound requests: asset providers, premium integrations and update checks have separate network paths. Production requires explicit opt-in and an egress deny policy, mock HTTP assertions that zero non-allowlisted requests occur, and clean secret handling.
- No secrets in stdout, exception payloads, artifact manifest or telemetry. Scrub raw params logging at BAS-06/BAS-24.

## Implementation inventory and commands

Changed: \`src/blender_mcp/security_policy.py\` (typed authorization preflight), \`src/blender_mcp/server.py\` (profile-based tool listing, raw code guard, loopback address restriction), \`tests/test_security_policy.py\` (negative tests).

Suggested validation on checkout with Python 3.12.14 and the pinned lockfile:

\`\`\`bash
uv sync --frozen
uv run pytest -q tests/test_security_policy.py tests/test_studio_contracts.py tests/test_safe_mode.py
uv run python -m compileall -q src tests
uv run pytest -q
\`\`\`

Run full CI (Python 3.11/3.12 + Blender 4.5.15 golden smoke) on the PR. Local execution was unavailable in the connected environment; do **not** claim a PASS until GitHub Actions and final review confirm it. No BLEND/GLB/PNG/MP4 was generated for this policy-only change. Baseline golden artifact is from BAS-02, not new BAS-04 evidence.

## Explicit outstanding gates

BAS-05: instantiate authenticated Studio dispatcher, validate capabilities before side effects, guard all handlers and MCP resources. BAS-06: add-on socket authentication, framing/size limits, bind rules and reconnect tests. BAS-08/12: real revisions, snapshot hash, atomic path and nonce verification. BAS-20/24: egress enforcement and zero-exfil test. BAS-21/22: production authenticated API, isolation and GPU worker. Do not treat the new Python policy as an OS sandbox.
