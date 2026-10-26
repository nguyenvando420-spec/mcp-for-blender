# ADR-003 — Job lifecycle, persistence và artifact publication

Status: Accepted cho contract v1 sau review kỹ thuật của Codex theo tiêu chí BAS-03, 26/10/2026. Implementation/runtime gates thuộc các task tiếp theo; chưa có review độc lập của con người.

## Context

Render, film và inference vượt vòng đời synchronous socket command. Restart, timeout và retry có thể tạo side effect trùng hoặc artifact dở.

## Decision

Job v1 có trạng thái queued/running/succeeded/failed/cancelled, progress 0–1, timeout, cancel request, attempt, timestamps và lease. Persistence dùng local durable runner ở P2–P4; P5 có thể thay backend bằng Redis/API mà giữ envelope. Tạo job phải atomically reserve `(caller, operation, idempotency_key)`; cùng key/cùng payload hash trả cùng job, khác hash trả conflict. Namespace có owner ID, digest do runner tính từ semantic input; request correlation ID không nằm trong digest. Worker lease hết thì recovery requeue tăng attempt, xác minh checkpoint/output và replay safety trước retry, ghi recovery reason/checkpoint; lease-expired recovery phải đợi lease hết và không requeue khi cancel pending. Cancellation là yêu cầu bền, terminal cancelled chỉ sau runner xác minh worker đã dừng/dọn output và ghi `stop_confirmed=true`. Nếu đã hoàn tất trước cancel thì giữ succeeded. Timeout phải tạo lỗi typed hoặc cancel theo policy; không im lặng trả success.

Artifact được ghi vào staging, kiểm tra bytes/hash/reopen hoặc reimport phù hợp, rồi publish immutable manifest. Manifest liên kết `job_id`, version và provenance. Không publish output dở. Terminal job không được đổi trạng thái. Snapshot scene copy trước render và không để worker headless dùng socket GUI.

## Consequences

Contract validation và transition checks ở BAS-03; durable store, scheduler, process cleanup và recovery integration tests thuộc BAS-13/17/19/21/22. Chưa chọn Redis hoặc FastAPI ở giai đoạn này.

Timeout là deadline wall-clock kể từ created_at, bao gồm queue và recovery; giữ deadline/identity/input/source scene bất biến. Progress là tiến độ tổng thể không giảm qua attempts. Local durable metadata store dùng SQLite cho MVP theo kế hoạch, transaction cho reservation/transition và staging/atomic publication cho artifacts; API/Redis chỉ bổ sung sau.
