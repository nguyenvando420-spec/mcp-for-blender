# BAS-03 — Studio contract v1 và inventory

Ngày 26/10/2026. Contract ở `src/blender_mcp/studio/contracts.py`; JSON Schemas sinh từ chính validators trong `docs/schemas/`. Đây là **thiết kế đã kiểm thử tĩnh**, chưa đăng ký `studio_*` vào FastMCP và chưa chứng minh các thao tác mới chạy trong Blender GUI.

## Inventory hiện tại

Server FastMCP `src/blender_mcp/server.py` có 14 tool decorator: `get_addon_status`, `disable_telemetry`, `get_scene_info`, `execute_blender_code`, `record_trajectory_feedback`, `look`, `generate_3d`, `search_assets`, `import_asset`, `search_mentions`, `open_viewport`, `viewport_latest`, `viewport_capture`, `viewport_pick`. Server còn có resources cho object/material/collection/viewport. Add-on protocol 15 có lệnh quan sát, `execute_code`, `export_scene`, tích hợp tài nguyên và generator tùy bật; registry tại `addon.py:1537–1620`. Các lệnh addon **không phải** Studio tools; `export_scene` hiện không được expose như MCP tool độc lập. `get_scene_info` thường dùng script qua `execute_code` và fallback lệnh native. `look` dùng `get_viewport_screenshot` hoặc script. Các operation mutation chưa có typed handler riêng; raw Python không được coi là adapter Studio an toàn.

| Add-on dispatch group | Commands hiện có |
| --- | --- |
| Base, luôn đăng ký | `ping`, `get_scene_info`, `get_world_state_snapshot`, `get_addon_info`, `get_object_info`, `list_scene_items`, `get_viewport_screenshot`, `pick_viewport_object`, `execute_code`, `describe_node_type`, `bpy_api_lookup`, `drain_human_activity`, `get_telemetry_consent`, `set_telemetry_consent`, `get_polyhaven_status`, `get_hyper3d_status`, `get_sketchfab_status`, `get_polypizza_status`, `get_hunyuan3d_status`, `get_tripo_status`, `get_meshy_status`, `export_scene` |
| Poly Haven, khi bật | `get_polyhaven_categories`, `search_polyhaven_assets`, `download_polyhaven_asset`, `get_polyhaven_asset_preview`, `set_texture` |
| Hyper3D, khi bật | `create_rodin_job`, `poll_rodin_job_status`, `import_generated_asset` |
| Sketchfab, khi bật | `search_sketchfab_models`, `get_sketchfab_model_preview`, `download_sketchfab_model` |
| Poly Pizza, khi bật | `search_polypizza_models`, `download_polypizza_model` |
| Hunyuan3D, khi bật | `create_hunyuan_job`, `poll_hunyuan_job_status`, `import_generated_asset_hunyuan` |
| Tripo Premium | `create_tripo_job`, `poll_tripo_job_status`, `import_generated_asset_tripo` |
| Meshy, khi bật | `create_meshy_job`, `poll_meshy_job_status`, `import_generated_asset_meshy`; own-key có thêm `refine_meshy_job` |

`get_addon_info.capabilities` hiện chỉ liệt kê một tập con command base, không bao gồm `export_scene` hay trạng thái của mọi provider; BAS-05 phải tạo Studio capability handshake riêng, không dùng danh sách này như ma trận hỗ trợ đầy đủ.

## Mapping 15 operations MVP

“Hiện có” là primitive để tái sử dụng; cột handler là **đích cần implement**, không khẳng định handler đó đã tồn tại.

| Operation v1 | Primitive/tool hiện có | Adapter → addon handler hoặc worker dự kiến | Task sở hữu |
| --- | --- | --- | --- |
| `studio_scene_inspect` | `get_scene_info`, `list_scene_items`, `get_object_info` | typed observe → ID/revision aware `get_scene_info` | BAS-08 |
| `studio_scene_save` | Blender bpy save qua raw code | path policy + atomic save → `studio_scene_save` | BAS-12 |
| `studio_scene_open` | Blender bpy open qua raw code | hash/path/snapshot/confirmation + reconnect → `studio_scene_open` | BAS-12 |
| `studio_scene_snapshot` | chưa có snapshot typed | verified scene copy → `studio_scene_snapshot` | BAS-12 |
| `studio_scene_restore` | chưa có restore typed | snapshot hash + revision/confirmation + reconnect → `studio_scene_restore` | BAS-12 |
| `studio_create_object` | raw `execute_code` (không là public adapter) | primitive/text discriminated payload → `studio_create_object` | BAS-07 |
| `studio_set_transform` | raw `execute_code` | transform/rename/duplicate typed → `studio_set_transform` | BAS-07/08 |
| `studio_delete_object` | raw `execute_code` | snapshot + confirmation → `studio_delete_object` | BAS-07/12 |
| `studio_manage_collection` | `collection_resource` chỉ đọc | create/move/rename typed → `studio_manage_collection` | BAS-07 |
| `studio_assign_material` | `set_texture` chỉ cho Poly Haven | PBR/texture, bounded modifiers/GN preset → `studio_assign_material` | BAS-10 |
| `studio_set_camera` | raw `execute_code` | typed camera → `studio_set_camera` | BAS-11 |
| `studio_set_light` | raw `execute_code` | light/world discriminated payload → `studio_set_light` | BAS-11 |
| `studio_capture_viewport` | `look`, `viewport_capture`, `get_viewport_screenshot` | typed capture → `get_viewport_screenshot` | BAS-08 |
| `studio_render_still` | golden CLI smoke; `look` chỉ là preview | headless worker từ scene copy; job/artifact adapter | BAS-13 |
| `studio_export_artifacts` | addon `export_scene`, bpy save | path policy + GLB export/reimport và BLEND save/reopen → export worker | BAS-13 |

Danh mục trên khớp **15 tên chuẩn ở mục 5 của kế hoạch Notion**, thay bản draft `841cbf8` đã tách primitive/text và bỏ sót restore/BLEND. Primitive/text, light/world và material/modifier dùng payload phân biệt bằng `kind`; transform/rename/duplicate dùng `action`. V1 đề xuất allowlist bevel (segments ≤16), subdivision (levels ≤3), Geometry Nodes `linear_array` (count ≤100); BAS-10 phải implement và nghiệm thu trước khi quảng bá capability. Không nhận Python hoặc node graph tùy ý.

## Wire contract và gate

- Request dùng `contract.major=1`, `request_id` UUID, `effect_scope` literal đúng operation và payload có type/bounds, cấm trường lạ. Mutation bắt buộc `scene_id`, `scene_revision` không âm và `idempotency_key`; `request_id` chỉ là correlation. Read inspect/capture có thể bỏ scene context để bootstrap; nếu cung cấp thì ID/revision phải đi cùng nhau.
- Capability handshake trả `contract`, addon protocol, tập `operations`/`features`. Major khác bị từ chối; minor chỉ mở rộng additive. Cho phép tên capability tương lai trong handshake nhưng parser chỉ chấp nhận 15 operation v1; `ensure_supported` từ chối operation chưa quảng bá trước dispatch. Không suy capability từ protocol version một mình.
- Object ID là UUID lưu ở datablock, tên chỉ để hiển thị. BAS-08 phải xử lý rename/duplicate/append/import/deletion: duplicate/import cấp UUID mới; rename giữ ID. Scene có UUID riêng; revision tăng sau mutation/restore trong cùng scene, mismatch trả `stale_scene` trước side effect. Open scene khác trả scene ID/revision mới và invalidate context cũ. Response luôn có scene ID/revision, scene diff/warnings hoặc job handle. ID/revision hiện chưa được addon cung cấp.
- Response dùng `succeeded` + result, `accepted` + job ID, hoặc `failed` + typed error. Result có object refs, artifact IDs, snapshot ID và scene diff. Long operation trả job handle, không chờ trong main thread. JSON Schemas mô tả cấu trúc/types/bounds; các điều kiện liên trường (confirmation, state transition, matching IDs) vẫn phải chạy Pydantic validators, không chỉ JSON Schema.
- URI là dữ liệu wire có scheme, không cấp quyền. BAS-04/12 phải kiểm tra allowed root, symlink, overwrite và quyền đọc ghi. `effect_scope` không là authorization token. Delete/open/restore yêu cầu token xác nhận gắn scene/revision, open/delete cần snapshot; overwrite save cần token khi `overwrite=true`. Schema chỉ buộc có token, policy phải xác minh token và snapshot.
- Job: `queued → running → succeeded|failed|cancelled`; running phải có lease. `cancel_requested=true` chỉ ghi yêu cầu bền; `cancelled` cần thêm `stop_confirmed=true` sau khi runner xác minh worker dừng/dọn output (queued: xác nhận gỡ khỏi queue). Runner có thể hoàn tất succeeded nếu completion thắng cancel. Không tự coi socket timeout là dừng side effect.
- Recovery running→queued tăng đúng `attempt+1`, giữ progress tổng thể không giảm, có `Recovery(replay_safe=true)` và checkpoint URI nếu có. Lease-expired recovery không xảy ra trước lease hết; worker-restart recovery cần runner xác minh owner đã chết. Không requeue khi cancel pending; side effect chưa xác định phải failed/manual intervention thay vì retry mù. `timeout_seconds` là deadline toàn job kể từ `created_at`, gồm queue/retry; timeout terminal failed dùng error `timeout` sau process cleanup. Identity, input hash, operation, source scene, deadline và thời điểm tạo bất biến; terminal bất biến.
- Idempotency namespace `(owner_id, operation, idempotency_key)` được runner reserve atomically. Payload digest SHA-256 do runner tính trên semantic input đã normalize (gồm scene context, bỏ request correlation ID); cùng key/cùng hash trả lại job cũ, khác hash trả `conflict`. BAS-03 định nghĩa fields/semantics; unique constraint và transaction thuộc durable runner.
- Manifest có job ID; artifact có ID/kind/URI/bytes/SHA-256/version và provenance bắt buộc source commit, scene ID/revision, Blender/worker version, input hashes; model ID/license đi cùng, asset licenses có trường riêng. Timestamp có timezone; artifact IDs unique và job ID phải khớp manifest. Staging → validation/reopen/reimport → checksum → publish manifest cuối; chưa có artifact store ở BAS-03.

### API surface dự kiến

`validate_request(dict)`, `ensure_supported(request,capabilities)`, `StudioResponse`, `Job`, `validate_transition(previous,current)`, `ArtifactManifest`, `Capabilities` và `json_schemas()` là contract dùng chung. Có thể regenerate bằng `.venv/bin/python scripts/export_studio_schema.py`. Không nhập module này vào `server.py` cho tới BAS-05.

## ADR và phần chưa thực thi

- [ADR-001](adr/001-bridge-protocol.md): bridge/main thread, version/capabilities.
- [ADR-002](adr/002-transport-permissions.md): transport và permission profiles.
- [ADR-003](adr/003-job-persistence.md): persistence, lifecycle, idempotency/artifacts.
- [Local validation record](BAS-03-evidence.md): environment, commands, results and limits.

Chưa có durable queue/worker, auth socket, allowed-root enforcement, stable ID/revision implementation, GUI test, render/GLB output từ Studio contract. BAS-04/05/06/08/12/13 phải triển khai và kiểm thử các phần tương ứng; đừng coi JSON Schema là bảo đảm runtime Blender.
