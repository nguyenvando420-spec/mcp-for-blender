# ADR-003 — Job lifecycle, persistence và artifact publication

Status: Proposed for BAS-03 review, 26/10/2026.

## Context

Render, film và inference vượt vòng đời synchronous socket command. Restart, timeout và retry có thể tạo side effect trùng hoặc artifact dở.

## Decision

Job v1 có trạng thái queued/running/succeeded/failed/cancelled, progress 0–1, timeout, cancel request, attempt, timestamps và lease. Persistence dùng local durable runner ở P2–P4; P5 có thể thay backend bằng Redis/API mà giữ envelope. Tạo job phải atomically reserve `(caller, operation, idempotency_key)`; duplicate trả cùng job. Worker lease hết thì recovery requeue tăng attempt, xác minh checkpoint/output trước retry. Cancellation là yêu cầu bền, terminal cancelled chỉ sau worker xác nhận dừng/dọn output. Nếu đã hoàn tất trước cancel thì giữ succeeded. Timeout phải tạo lỗi typed hoặc cancel theo policy; không im lặng trả success.

Artifact được ghi vào staging, kiểm tra bytes/hash/reopen hoặc reimport phù hợp, rồi publish immutable manifest. Manifest liên kết `job_id`, version và provenance. Không publish output dở. Terminal job không được đổi trạng thái. Snapshot scene copy trước render và không để worker headless dùng socket GUI.

## Consequences

Contract validation và transition checks ở BAS-03; durable store, scheduler, process cleanup và recovery integration tests thuộc BAS-13/17/19/21/22. Chưa chọn Redis hoặc FastAPI ở giai đoạn này.
