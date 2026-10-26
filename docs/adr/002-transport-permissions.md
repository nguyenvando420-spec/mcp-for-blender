# ADR-002 — Transport và permission profiles

Status: Proposed for BAS-03 review, 26/10/2026.

## Context

Add-on socket hiện không có auth; server chạy stdio cho client local. Raw Python và đường dẫn file tạo quyền rộng. Safe mode hiện hữu không là OS sandbox.

## Decision

Giữ socket trên loopback cho local trusted client trong P1; không bind public interface. BAS-04 định nghĩa profiles: observer (read/capture), studio-local (typed scene và file trong allowed roots), developer-local (raw code với cảnh báo), worker (job-scoped scene copy/output), remote (TLS/auth/quota tại API boundary, về sau). Mọi request có `effect_scope`; policy xác minh profile, operation, path, scene revision và capability trước side effect. Deny by default cho unknown operation/path; không coi scope do client khai báo là quyền. File output có allowlist root, canonicalization, symlink/overwrite tests. Transport auth/allowlist của socket thuộc BAS-06; external service auth thuộc BAS-21/22.

## Consequences

BAS-03 chỉ định nghĩa contract, chưa enforce profile. Không expose raw Python trong Studio/remote. Migration từ client legacy phải giữ khả năng local nhưng không tự nâng quyền. Network/telemetry audit thuộc BAS-04/24.
