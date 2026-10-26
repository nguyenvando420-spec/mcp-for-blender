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
| `studio_inspect_scene` | `get_scene_info`, `list_scene_items`, `get_object_info` | typed observe → ID/revision aware `get_scene_info` | BAS-08 |
| `studio_capture_viewport` | `look`, `viewport_capture`, `get_viewport_screenshot` | typed capture → `get_viewport_screenshot`; artifact nếu lưu | BAS-08 |
| `studio_create_primitive` | raw `execute_code` (không dùng làm public adapter) | typed scene adapter → `studio_create_primitive` | BAS-07 |
| `studio_create_text` | raw `execute_code` | typed scene adapter → `studio_create_text` | BAS-07 |
| `studio_transform_object` | raw `execute_code` | ID lookup + revision check → `studio_transform_object` | BAS-07/08 |
| `studio_manage_collection` | `collection_resource` chỉ đọc | typed scene adapter → `studio_manage_collection` | BAS-07 |
| `studio_delete_object` | raw `execute_code` | snapshot prerequisite → `studio_delete_object` | BAS-07/12 |
| `studio_set_material` | `set_texture` chỉ cho Poly Haven | typed PBR adapter → `studio_set_material` | BAS-10 |
| `studio_set_camera` | raw `execute_code` | typed scene adapter → `studio_set_camera` | BAS-11 |
| `studio_set_light` | raw `execute_code` | typed scene adapter → `studio_set_light` | BAS-11 |
| `studio_save_scene` | Blender bpy save qua raw code | path policy + atomic save → `studio_save_scene` | BAS-12 |
| `studio_open_scene` | Blender bpy open qua raw code | hash/path check + reconnect → `studio_open_scene` | BAS-12 |
| `studio_snapshot_scene` | không có snapshot typed | copy/reopen verified snapshot handler | BAS-12 |
| `studio_render_still` | `look` camera preview, golden CLI smoke | headless worker từ scene copy; job/artifact adapter | BAS-13 |
| `studio_export_glb` | addon `export_scene` (chưa là public MCP tool) | path policy + export/reimport validation → `export_scene` | BAS-13 |

## Wire contract và gate

- Request dùng `contract.major=1`, `request_id` UUID, `scene_revision` không âm, `effect_scope` đúng operation; payload là discriminated union qua `operation`, cấm trường lạ. `request_id` là correlation; mutating/durable operations cần idempotency key của job hoặc adapter để không lặp side effect.
- Capability handshake trả `contract`, addon protocol, tập `operations` và `features`. Client chỉ dispatch sau khi so major version và capability. Major khác bị từ chối `unsupported_version`; minor chỉ mở rộng additive. Không suy ra capability từ version protocol một mình.
- Object ID là UUID lưu ở custom property/datablock, tên chỉ để hiển thị; BAS-08 phải cài và thử quy tắc duplicate, rename, append/import, deletion. Revision tăng sau mutation và open/restore; mismatch trả `stale_scene` trước side effect. Initial revision của scene phải được trả từ inspect/capabilities. ID/revision hiện **chưa** được addon cung cấp.
- Response dùng `succeeded` + result, `accepted` + job ID, hoặc `failed` + `StudioError`; error code là enum. Long operations không chờ trong main thread. Chỉ trả artifact ID/URI sau khi kiểm tra file và SHA-256.
- File URI chỉ là dữ liệu wire: BAS-04/12 phải cấp phép allowed root, kiểm tra symlink/overwrite/quyền đọc ghi. Schema không cấp quyền. `effect_scope` là khai báo để policy xác minh, không phải authorization token.
- Job: `queued → running → succeeded|failed|cancelled`; running có thể quay lại queued với `attempt+1` khi lease hết/recovery. Cancel request chỉ đặt `cancel_requested=true`; `cancelled` chỉ sau khi worker/runner xác nhận dừng và dọn output. Timeout cần ghi `failed` với code `timeout`, hoặc xác nhận cancel theo policy. Terminal bất biến. Idempotency key phải unique trong namespace caller+operation; retry trả cùng job và không thực hiện side effect hai lần.
- Manifest: `job_id`, mỗi artifact có ID, kind, URI, byte count, SHA-256, version, provenance (commit, Blender/worker version, input hashes, model/license khi có). Publish manifest cuối sau validation; chưa có artifact store ở BAS-03.

### API surface dự kiến

`validate_request(dict)`, `StudioResponse`, `Job`, `validate_transition(previous,current)`, `ArtifactManifest`, `Capabilities` và `json_schemas()` là contract dùng chung. Có thể regenerate bằng `.venv/bin/python scripts/export_studio_schema.py`. Không nhập module này vào `server.py` cho tới BAS-05.

## ADR và phần chưa thực thi

- [ADR-001](adr/001-bridge-protocol.md): bridge/main thread, version/capabilities.
- [ADR-002](adr/002-transport-permissions.md): transport và permission profiles.
- [ADR-003](adr/003-job-persistence.md): persistence, lifecycle, idempotency/artifacts.
- [Local validation record](BAS-03-evidence.md): environment, commands, results and limits.

Chưa có durable queue/worker, auth socket, allowed-root enforcement, stable ID/revision implementation, GUI test, render/GLB output từ Studio contract. BAS-04/05/06/08/12/13 phải triển khai và kiểm thử các phần tương ứng; đừng coi JSON Schema là bảo đảm runtime Blender.
