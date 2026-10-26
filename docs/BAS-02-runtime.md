# BAS-02 — Runtime pin và golden scene

## Quyết định

Blender **4.5.15 LTS** là ứng viên mục tiêu CPU, thay cho gói apt Blender 4.0.2 của BAS-01. Nguồn phiên bản: [Blender 4.5 LTS](https://www.blender.org/releases/4-5/). Chỉ công nhận các ô tương thích có log pass. Bản Blender GUI 5.2.2 đã thử ở BAS-01 là bằng chứng riêng, không thay thế thử nghiệm 4.5.15.

`blender-runtime.json` khóa filename, URL và SHA256 của Linux x86_64 và macOS arm64. Checksum được đối chiếu byte giữa [mirror chính thức](https://mirror.blender.org/release/Blender4.5/blender-4.5.15.sha256) và mirror Berkeley. Download chính thức có thể trả Cloudflare challenge; installer thử mirror nhưng vẫn buộc đúng checksum đã commit. Không tải checksum mới để tự tin cậy binary mới mỗi lần chạy. Không dùng apt để chọn phiên bản Blender; apt chỉ cấp thư viện hệ thống ở CI/Colab.

Gateway mặc định **Python 3.12.14** (`.python-version`); CI kiểm tra thêm **3.11.14** với `--python` rõ ràng. Đây là pin development, không đổi phạm vi package upstream `requires-python >=3.10`. `uv.lock` giữ nguyên. Python bpy được đóng gói trong archive Blender, đo thực tế vào manifest; không thay bằng Python gateway hoặc kernel Colab. OS/thư viện hệ thống không được pin bit-for-bit: manifest ghi môi trường thực tế, chưa tuyên bố render bit-identical giữa OS.

## Chạy local / CI

```bash
uv sync --frozen
DISABLE_TELEMETRY=true uv run --frozen python scripts/run_golden.py --output-dir artifacts/bas02-run-1
uv run --frozen python scripts/run_golden.py --output-dir artifacts/bas02-run-1 --verify-only
uv run --frozen python scripts/run_golden.py --output-dir artifacts/bas02-run-1 --reopen
```

Installer dùng cache `.blender-runtime/`, xác minh archive trước khi giải nén và giải nén lại mỗi lần để không tin executable cache đã bị thay. Trên macOS mount DMG chỉ đọc, chép app vào cache trong repo rồi tháo mount; không thay Blender đang cài trong Applications. Không cần GUI hay GPU. Cần dung lượng trống cho archive và runtime. Đổi output directory mỗi lần thử để không trộn bằng chứng; giữ source commit sạch cho nghiệm thu cuối.

Runner tái sử dụng `blender_headless_smoke.py` từ BAS-01, bổ sung material, camera và light có tên cố định. Cycles CPU 8 samples render PNG 320×180. Tiến trình Blender thứ hai mở lại BLEND với script execution tắt, kiểm tra mesh, vị trí, camera, material/node/color, light, engine/device và kích thước render. PNG được decode, kiểm tra kích thước và độ biến thiên pixel. Lỗi Python làm Blender trả mã lỗi khác 0 (`--python-exit-code 1`).

Output gồm `baseline.blend`, `preview.png`, `render.log`, `reopen.log`, `blender-environment.json`, `validation.json` và `manifest.json` (OS/architecture, Blender version/build, gateway Python, bpy Python, engine/device, source SHA/dirty, archive SHA256 và checksum từng file). Manifest không tự checksum chính nó; khi lưu receipt recovery, ghi thêm checksum manifest. SHA256 phát hiện file bị thay/corrupt, không phải chữ ký chống giả mạo manifest. Không đưa scene/binary vào git.

CI Python chạy trên Ubuntu 24.04. Job golden PR/manual tải đúng archive pin, render + reopen và upload output 30 ngày, cả khi fail để còn log. Artifact CI có hạn lưu, không thay thế lưu trữ lâu dài Colab.

## Colab và phục hồi sau dừng runtime

Mở `notebooks/BAS-02-colab-golden.ipynb` trong Colab. Notebook yêu cầu SHA 40 ký tự của PR đã review, checkout detached đúng SHA rồi chạy cùng runner. Kernel Colab chỉ điều phối; uv cài gateway Python đã pin.

1. Chạy Setup và Phase A. Mount Drive khi Colab yêu cầu. Bộ artifact được copy vào `MyDrive/BlenderAIStudio/BAS-02/<UTC>-<SHA>` và verify checksum ngay trên Drive. Lưu đường dẫn.
2. Dùng **Runtime → Disconnect and delete runtime**. Ghi nhận thao tác này trong bằng chứng. Chỉ disconnect trình duyệt chưa chứng minh runtime đã dừng.
3. Kết nối runtime mới, chạy lại Setup ở cùng SHA. Chạy Phase B với đường dẫn cũ, không chạy Phase A. Cell yêu cầu thư mục local Phase A không còn, kiểm tra source SHA sạch, hash artifact trên Drive rồi tải lại Blender đã pin để mở BLEND và decode PNG.
4. Lưu receipt `recovery-<uuid>.json` ngoài bộ artifact gốc, kèm checksum manifest và log `BAS02_RECOVERY_OK`. Đọc lại receipt sau khi tắt runtime thứ hai để xác minh truy cập từ storage bền vững.

Notebook chưa có output chạy được commit. Việc có notebook, copy vào Drive ở cùng runtime, hoặc mở lại local đều **chưa** thỏa tiêu chí Colab sau runtime dừng. Không tự đánh dấu Done trước bằng chứng này và tích hợp PR.

## Ma trận tương thích

| Môi trường | Blender | Python gateway / bpy | Trạng thái |
| --- | --- | --- | --- |
| macOS arm64 local CPU | 4.5.15 pin SHA256 | 3.12.14 / đo trong manifest | Chờ chạy |
| Ubuntu 24.04 x86_64 CI CPU | 4.5.15 pin SHA256 | 3.12.14 / đo trong manifest | Chờ CI |
| Colab Linux x86_64 CPU | 4.5.15 pin SHA256 | 3.12.14 / đo trong manifest | Chưa thử; cần Drive + runtime reset |
| Windows, Linux ARM64, macOS Intel, GPU | — | — | Chưa thử, chưa hỗ trợ bởi installer |

Chỉ sửa trạng thái trên cơ sở log tại đúng head. Không suy rộng kết quả CPU smoke thành nghiệm thu MCP GUI hoặc GPU; các phần đó thuộc BAS-09 và các ticket worker.
