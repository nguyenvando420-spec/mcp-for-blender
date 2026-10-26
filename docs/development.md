# Blender AI Studio — Phát triển trên fork

Tài liệu này dành cho fork [nguyenvando420-spec/mcp-for-blender](https://github.com/nguyenvando420-spec/mcp-for-blender), không phải hướng dẫn cài bản phát hành PyPI upstream.

## 1. Clone và chạy bằng local checkout

Yêu cầu: Git, uv và Python 3.12.14 (theo .python-version, cập nhật BAS-02). Lệnh bên dưới dùng trên Linux/macOS; Windows chưa được nghiệm thu. Runtime Blender, golden smoke và Colab: xem [BAS-02-runtime.md](BAS-02-runtime.md).

~~~bash
git clone https://github.com/nguyenvando420-spec/mcp-for-blender.git
cd mcp-for-blender
git remote add upstream https://github.com/ahujasid/mcp-for-blender.git
uv sync --frozen

# Chạy suite tests hiện hữu + các kiểm thử fork:
DISABLE_TELEMETRY=true uv run --frozen --with pytest==8.4.2 pytest -q

# Gói được build từ mã checkout này:
uv build

# CLI từ môi trường fork, KHÔNG phải uvx PyPI:
DISABLE_TELEMETRY=true uv run --frozen mcp-for-blender --help
~~~

Đường dẫn executable để khai báo trong Claude/Codex: sau lệnh uv sync, dùng đường dẫn **tuyệt đối** tới `.venv/bin/mcp-for-blender` (Linux/macOS) hoặc `.venv\\Scripts\\mcp-for-blender.exe` (Windows). Đặt biến DISABLE_TELEMETRY=true và BLENDER_MCP_SAFE_MODE=1 trong cấu hình client; kết nối addon trên localhost:9876. Chế độ safe mode không thay thế sandbox khi triển khai từ xa.

Khi muốn thử Blender GUI: **đảm bảo không có addon đang làm việc cần bảo toàn**, sau đó dùng `uv run --frozen mcp-for-blender install-addon` để cài bản được gói từ fork; lệnh này có thể thay thế addon cùng tên đã cài và ghi .bak. Khởi động lại Blender hoặc tắt/bật Add-on, mở Viewport (phím N) và kiểm tra tab MCP for Blender. Sau đó kiểm tra `get_addon_status`, `get_scene_info`, `look` trong client. Không chạy hai MCP server trên cùng một port.

## 2. Chạy kiểm thử tĩnh

~~~bash
uv run --frozen python -m compileall -q src tests scripts addon.py main.py
uv run --frozen --with ruff==0.14.0 ruff check --select E4,E7,E9,F63,F7,F82 tests/test_fork_baseline.py scripts/blender_headless_smoke.py
uv run --frozen --with pytest==8.4.2 pytest -q
uv build
~~~

CI chạy các lệnh này trên Python 3.11.14 và 3.12.14 (truyền `--python` theo matrix). Lint giới hạn ở tests baseline/runtime và scripts để không khóa build bởi các lỗi kiểu cũ của upstream.

## 3. Blender headless smoke

Đây là lệnh BAS-01 cấp thấp cho Blender CLI bất kỳ; không dùng làm bằng chứng runtime pin của BAS-02. Bài test CPU nhẹ, không phụ thuộc GPU, dùng Cycles 8 samples. Chạy:

~~~bash
blender --background --factory-startup --python scripts/blender_headless_smoke.py -- --output-dir artifacts/bas01
~~~

Kỳ vọng: ghi `BAS01_SMOKE_OK` và artifact `baseline.blend`, `preview.png`. BAS-02 dùng `uv run --frozen python scripts/run_golden.py --output-dir artifacts/bas02-run-1` để tải Blender pin, render và mở lại BLEND. Job PR/manual **Fork baseline CI** dùng cùng runner. Chạy headless này độc lập với Add-on GUI; Colab không thay thế kiểm thử GUI.

## 4. Đồng bộ upstream và phát triển tính năng

~~~bash
git fetch upstream
git checkout -b feature_<ten-tinh-nang> main
# Không thay đổi trực tiếp nhánh main khi chưa qua CI và PR.
~~~

Trước khi cập nhật upstream: xem thay đổi qua `git log main..upstream/main` và `git diff main..upstream/main`, rồi tạo PR đồng bộ kèm log test. Không xóa copyright / MIT notice. Phải đồng bộ `addon.py` và `src/blender_mcp/bundled/addon.py`; bài test kiểm tra điều đó.

## 5. Kiểm soát quyền và giới hạn

- Giữ cổng 9876 chỉ ở localhost khi sử dụng socket addon; **không expose qua Internet**.
- Không cung cấp trực tiếp tool execute_blender_code cho người dùng không tin cậy.
- Không commit API keys, dữ liệu scene nhạy cảm, file .blend, ảnh render hoặc checkpoint model lớn; artifact nên nằm ngoài git.
- `DISABLE_TELEMETRY=true` vô hiệu hóa telemetry upstream ở môi trường thử nghiệm; kiểm tra lại trước khi triển khai tự host.
- Blender GPU Docker worker, remote MCP authentication, image-to-3D là những giai đoạn sau; Dockerfile hiện có chưa chạy Blender GPU.
