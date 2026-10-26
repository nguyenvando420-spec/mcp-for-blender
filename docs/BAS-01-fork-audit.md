# BAS-01 — Audit baseline của fork (26/10/2026)

## Phạm vi đã đối chiếu

- Repository: https://github.com/nguyenvando420-spec/mcp-for-blender
- Upstream: https://github.com/ahujasid/mcp-for-blender
- Commit nền: `febcd90ece59c3416d7c55fd3723cea46bcaa59d` (fork main và upstream main bằng nhau ở thời điểm kiểm tra).
- Tình trạng khi bắt đầu: chưa có thay đổi tùy chỉnh, chưa có thư mục .github/workflows.
- Python mặc định .python-version: 3.11; pyproject.toml: package v2.2.4, Python >=3.10, mcp >=1.26,<2, httpx; uv.lock đã tồn tại.
- Blender Add-on: addon.py (v1.8, protocol 15); src/blender_mcp/bundled/addon.py cùng blob SHA `fb9e3cffa7971418e2dd2127d7441152ad978ea1`.
- Server: src/blender_mcp/server.py dùng FastMCP/stdio và socket addon trên localhost:9876.
- Tái sử dụng: socket bridge, command queue chạy trong Blender main thread, scene inspection, viewport, import/export asset, tests hiện hữu.
- Dockerfile hiện có chỉ chạy MCP gateway, không chứa Blender render worker.

## Rủi ro đã nhận diện (chưa sửa ở BAS-01)

1. execute_blender_code cho phép Python trong Blender. BLENDER_MCP_SAFE_MODE là opt-in và không chặn trực tiếp socket addon, không phải sandbox OS.
2. Add-on socket không mã hóa hoặc xác thực; chỉ giữ localhost trên môi trường tin cậy, không expose cổng 9876 ra mạng.
3. Telemetry có anonymous events ngoài opt-in nội dung. Môi trường development/CI nên cấu hình DISABLE_TELEMETRY=true và kiểm tra policy trước khi phát hành self-host.
4. Bản root addon.py và bundled/addon.py phải đồng bộ vì `install-addon` dùng bundled version.
5. Không coi tích hợp generator bên thứ ba là mô hình tự host. Quyết định model, checkpoint license và benchmark GPU 16GB thuộc BAS-18/19.

## Quyết định BAS-01

- Giữ bố cục thư mục upstream, **không di chuyển** server.py/addon.py và không tạo monorepo lớn trước khi cần.
- Không chỉnh sửa code nghiệp vụ trong BAS-01; chỉ thêm CI, invariant tests, tài liệu và script headless smoke.
- Mỗi tính năng mới phát triển bằng branch và PR; lưu lại commit upstream trong thay đổi.
- `main` là điểm tích hợp sạch; trước khi đồng bộ upstream, kiểm tra diff và test lại, không force-push.
- Bảo đảm `uv sync --frozen` giữ lock; CLI từ checkout dùng `uv run --frozen mcp-for-blender`, **không dùng** `uvx mcp-for-blender` để kiểm thử fork.

## Bằng chứng và giới hạn

- Đã xác nhận tĩnh cấu trúc, config, commit, hash addon qua GitHub connector.
- CI được định nghĩa trong .github/workflows/ci.yml: compile, lint thay đổi mới, tests toàn bộ, CLI help, build sdist/wheel.
- Workflow headless smoke chạy thủ công qua workflow_dispatch để tránh tải Blender nặng trên mọi PR.
- Những mục **chưa xác minh** tại thời điểm viết tài liệu: CI chạy xanh, headless render trên Ubuntu, kết nối GUI với Claude/Codex, hành vi sau cài đặt trên máy thật. Không đánh dấu BAS-01 Done trước khi có bằng chứng.

## Release gate

- [ ] CI Python 3.11/3.12 xanh (pytest, lint, build).
- [ ] Đã kiểm tra sử dụng executable từ fork checkout.
- [ ] Có kết quả của Blender headless smoke (.blend, PNG) và log phiên bản Blender.
- [ ] Không có bất đồng root/bundled addon, protocol khớp.
- [ ] Truy vết được PR, commit, log CI và đường dẫn bản bàn giao.
