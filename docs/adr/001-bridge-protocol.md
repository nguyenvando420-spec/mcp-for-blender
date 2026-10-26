# ADR-001 — Giữ FastMCP, version bridge và main-thread dispatch

Status: Accepted cho contract v1 sau review kỹ thuật của Codex theo tiêu chí BAS-03, 26/10/2026. Implementation/runtime gates thuộc các task tiếp theo; chưa có review độc lập của con người.

## Context

Server hiện dùng FastMCP qua stdio, socket localhost đến add-on. Add-on nhận command và chạy bpy trong Blender main thread qua queue/timer. Protocol hiện là số 15 nhưng chưa biểu diễn capability của Studio operations hay stable IDs/revision.

## Decision

Giữ server và socket bridge hiện tại. BAS-05 đăng ký typed Studio tools và handshake `contract.major/minor`, `addon_protocol`, capabilities trước dispatch. Major mismatch từ chối; minor có thể thêm trường/tính năng additive, client phải kiểm tra capability từng operation. Add-on chỉ xử lý bpy ngắn trong main thread; render/inference/file IO dài chuyển worker/process và trả job handle. Không gọi bpy từ background thread. Raw `execute_code` chỉ ở developer local profile, không là đường triển khai Studio operation.

## Consequences

Không cần gateway mới ở P1. BAS-06 phải đo 100 command/reconnect/timeout và xử lý command queue; BAS-08 phải implement ID/revision. Protocol 15 vẫn hỗ trợ legacy tools nhưng không chứng minh Studio capability. Chưa đổi transport wire hiện hành trong BAS-03.
