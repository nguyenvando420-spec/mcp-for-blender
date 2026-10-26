# BAS-06 — Socket boundary, bounded Blender GUI dispatch and internal worker

Date: 2026-10-26. Branch: `feature_bas06-socket-hardening`. Base: main `1171d5cff`.

## Implemented security direction

- Add-on v16 requires `BLENDER_MCP_SOCKET_TOKEN` (32+ characters), shared only between trusted MCP server process and Blender. Start fails closed without token. Socket binds literal loopback only; `observer`, `studio-local`, acknowledged `developer-local` profiles at socket ingress.
- Command allowlist denies unknown/dangerous legacy commands by default, including direct `execute_code` outside acknowledged developer-local. HTTP/inference/render/export operations known to block GUI return `async_required`; they are deliberately not exposed until process/worker execution with durable job tracking exists.
- Main-thread timer handles at most four commands per tick and targets a ten-millisecond dispatch admission budget. **Not a hard bound** for single bpy handlers. Socket writer runs in client thread, not Blender timer.
- Request <= 1 MiB, queue capacity 128, bounded request ledger, error codes for unauthorized/invalid/queue full, in-process `request_id` status query. A client timeout means **UNKNOWN**, never "cancelled"; pending side effects must not be retried under a new ID.
- Studio capabilities remain limited to inspect/create; no fake render jobs. Root/bundled add-on identical and version synced.

## Limitations / acceptance gates

**NOT YET ACCEPTED**. Must run Python 3.11/3.12 tests, add socket abuse/reconnect/100-command stress tests, inspect CI, and perform Blender 4.5.15 GUI MCP E2E. The new required token and default observer deny previous anonymous legacy clients; this is an intentional security-breaking transport change. SDK call sites must pass token through process environment. In-memory status is not durable across Blender restart; BAS-12/22 must persist jobs. No network inference/render worker is yet implemented; blocking operations are fail-closed rather than asynchronous. Developer-local raw Python remains a highly unsafe explicit escape hatch.

Environment manifest: Blender 4.5.15 pinned, gateway Python 3.11.14/3.12.14; actual test outcome pending CI.

## PR review and acceptance evidence (updated 2026-10-26)

- PR: https://github.com/nguyenvando420-spec/mcp-for-blender/pull/6
- Review findings fixed after CI discovered test stub missing `hashlib`, concurrent request-id reservation, early worker cancellation race and read-timeout ambiguity. Final head/CI must be rechecked after all changes.
- Completed Actions at commit `02e4f7e2e935a42b7b3d8527f48efdaadcbb1d3d`: https://github.com/nguyenvando420-spec/mcp-for-blender/actions/runs/40339496560; Python 3.11.14: 586 passed, 56 skipped; Python 3.12.14: 586 passed, 56 skipped, 4 warnings. Compiled, built, CLI verified. Blender 4.5.15 golden CPU and true GUI MCP 100 calls (50 typed create + 50 inspect) passed. GUI heartbeat count/max gap persisted to `bas06-heartbeat.json`.
- Independent headless Blender 4.5.15 subprocess render from the GUI-generated BLEND with SQLite-backed job state, output PNG hash, idempotent resubmit and fresh RenderWorker process object readback passed (`BAS06_WORKER_PROCESS_E2E_OK`, output SHA-256 `667cff1d6fd0efcef9743f5756f695e8eb7094274d00571d4eb51c79ef2cb467`). Artifact `bas02-bas06-blender-evidence` ID `12657428624`.
- Regression tests: `uv run --frozen --python 3.12.14 --with pytest==8.4.2 pytest -q tests/test_bas06_socket_security.py tests/test_server_threading.py tests/test_socket_unicode.py tests/test_process_worker.py`; same Python 3.11.14. Full `pytest -q`. Root add-on and bundled bytes must match, expected protocol 16.
- Restart/port rebind regression test now performs 10 cycles and captures `BAS06_REBIND_OK` for investigation. Server binds a literal `127.0.0.1` even when the configured host is `localhost`.

### Explicit scope restrictions and handoffs

- Long HTTP/Hunyuan local inference (including the old 600-second synchronous handler), downloads and export/render GUI commands are denied with `async_required` until their own worker-mediated APIs are integrated. No invented asynchronous MCP capability is advertised.
- The worker only accepts a trusted local `.blend` snapshot from a configured workspace, launches a fixed, non-shell headless Blender command, writes to staging and publishes a verified PNG and SHA-256 on success. Metadata uses SQLite; after worker restart an uncertain `queued/running` job becomes `interrupted_unknown` and **is never automatically replayed**. This is fail-closed recovery, not automatic re-execution.
- This worker is an internal substrate, not a user-facing `studio_render_still` tool. BAS-12 must supply atomic snapshot isolation and output sandbox; BAS-13 must supply typed render settings and public job operation; BAS-22 adds durable distributed scheduling/leases and automated recovery. Inference provider backends remain disabled pending dedicated workers. BAS-06 does not claim full production sandbox, remote access, GPU inference or independent human UAT.
- Deliberate migration requirement: both Blender GUI and the MCP gateway must receive the same `BLENDER_MCP_SOCKET_TOKEN` environment variable (32+ characters), generated locally and **never** logged or committed. `BLENDER_MCP_PROFILE` must match the intended scope on the Blender side and gateway. `developer-local` requires `BLENDER_MCP_DEV_ACK=I_ACCEPT_UNRESTRICTED_LOCAL_PYTHON`. Default observer remains read-only. Old unauthenticated transport clients are intentionally rejected (protocol bump 15→16).
- GUI timer budget is a 10 ms scheduling admission target for four commands; it is not a preemptive timeout for arbitrary bpy operation. Developer-local raw Python remains explicitly unsafe and should never be enabled for untrusted clients.

- BAS-06 handshake now only advertises add-on commands actually allowed by the active socket profile. CI prints each of 10 repeated port rebind cycles with `pytest -s`; successful Python matrix and latest GUI worker E2E must still be checked at the final reviewed SHA.
