# BAS-03 — Review kỹ thuật contract v1

Reviewer: Codex (self-review), 26/10/2026. Đối chiếu trực tiếp task BAS-03 và mục 5 của kế hoạch Notion; không ghi nhận review độc lập của con người hoặc GUI UAT.

| Tiêu chí | Bằng chứng và quyết định |
| --- | --- |
| Mapping 15 operations | `BAS-03-contracts.md`; khớp 15 tên chuẩn. Primitive/text gộp ở create object; restore là operation riêng; export có GLB và BLEND; light có world; material có modifiers/GN preset typed. |
| Input/output typed | Discriminated request union; stable scene/object UUID, revision, idempotency, scope literal, version/capabilities, error enums, response variants, scene diff/warnings. Read bootstrap có thể chưa biết scene. |
| Job contract | 5 states, lease, deadline, owner/idempotency/input digest, explicit stop acknowledgement, recovery reason/replay safety/checkpoint, immutable identity/terminal. Runner vẫn phải xác minh sự thật của stop/replay và persist atomically. |
| Artifact contract | Bytes/hash/version/URI, job linkage và unique IDs, source commit/scene revision/runtime/input/model/license provenance, timestamps có timezone. |
| ADR/schema/type/negative tests | 3 ADR accepted cho thiết kế; 5 generated JSON Schema; 17 unittest cases gồm cả 15 valid operations và negative cases; normal pytest/lint/build CI là gate trước merge. |

## Lỗi draft đã sửa

- Draft `841cbf8` không khớp danh mục chuẩn: thiếu restore/BLEND, tách primitive/text và đổi tên một số operations. Đã thay mapping và contracts, thêm test khớp tập 15 operation.
- Cancel request chưa thể hiện stop acknowledgement riêng; đã bổ sung `stop_confirmed` và negative test.
- Recovery trước lease expiry, thay operation/deadline/input hoặc tăng attempt sai thời điểm có thể lọt transition check; đã chặn và kiểm thử.
- Request mutation thiếu idempotency; artifact provenance thiếu source scene revision; đã buộc fields cần thiết.
- Capability validator cũ từ chối operation tương lai, mâu thuẫn minor additive; đã cho phép quảng bá thêm nhưng gate dispatch vẫn từ chối operation không hỗ trợ.

## Giới hạn nghiệm thu

Đây là nghiệm thu contract/design. BAS-04/05/06/08/12/13 và worker tasks sở hữu enforcement, stable UUID/revision trong Blender, persistence, process cleanup, actual rendering và artifact validation. Không dùng contract tests để tuyên bố các runtime feature đó đã chạy. Main chỉ tích hợp sau final PR CI pass; merge SHA và kết quả CI lưu trong task Notion.

- JSON Schema draft chưa buộc có discriminator operation dù runtime parser yêu cầu; đã bỏ default trên 15 operation literals và thêm negative schema/runtime parity test cho thiếu operation, operation lạ và scope sai.
