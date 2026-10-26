# BAS-06 — Socket boundary and bounded Blender GUI dispatch (WIP)

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
