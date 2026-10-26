# BAS-03 — Local validation record (26/10/2026)

## Source and environment

- Branch: `feature_bas03-contracts`, based on `e6410e2eaaaf00821fe309684072e01e02b1fa1a` (BAS-02 main).
- macOS 26.7.1 arm64; gateway Python 3.12.14; Pydantic 2.12.3; JSON Schema validator 4.26.0.
- No Blender GUI or render process used by this contract-only change. BAS-02 golden artifacts are separate evidence.

## Commands and results

| Command | Result |
| --- | --- |
| `.venv/bin/python scripts/export_studio_schema.py` | Pass: 5 schema files written |
| `.venv/bin/python tests/test_studio_contracts.py` | Pass: 16 unittest cases (gồm subcases cho toàn bộ 15 operations chuẩn, types/bounds/negative cases, capability gate, cancellation/stop acknowledgement, recovery/deadline identity, response/artifact/provenance và schema parity) |
| `.venv/bin/python -m compileall -q src tests scripts addon.py main.py` | Pass |
| `git diff --check` | Pass |
| `Draft202012Validator.check_schema` for all 5 generated schemas | Pass |
| `.venv/bin/python -m pytest -q tests/test_studio_contracts.py` | Local không chạy vì chưa cài pytest; suite pytest đã pass ở CI của draft 841cbf8, kết quả final head ở PR #3/Notion |

The test module uses unittest.TestCase, runs directly without pytest, and is collected by the normal pytest suite in CI. These tests validate contracts and state transitions; they do not prove Studio dispatch, job persistence, Blender compatibility or path authorization.

## CI và review

- [PR #3](https://github.com/nguyenvando420-spec/mcp-for-blender/pull/3) là nơi kiểm tra final head và integration.
- Draft `841cbf8`: [push CI 40298881031](https://github.com/nguyenvando420-spec/mcp-for-blender/actions/runs/40298881031) pass Python 3.11.14/3.12.14, mỗi bản 518 passed/56 skipped, CLI/build/lock/compile/lint pass. Đây là evidence của draft trước khi sửa canonical mapping, không thay final CI.
- Review kỹ thuật: [BAS-03-review.md](BAS-03-review.md); environment và schema checksums: [BAS-03-environment.json](BAS-03-environment.json).
- 56 upstream skips không được coi là coverage runtime đã đạt. Final head/CI/merge SHA được ghi trên task BAS-03 ở Notion để không tạo vòng commit tự tham chiếu.
