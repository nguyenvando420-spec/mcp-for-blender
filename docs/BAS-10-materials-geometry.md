# BAS-10 — Typed PBR, modifiers, Geometry Nodes

Spec: https://app.notion.com/p/40515de298eb8180826ff4da2f0e477e

## Scope implemented on the feature branch

- One typed MCP `studio_assign_material` tool with four explicit variants from the existing BAS-03 contract: `pbr`, `bevel`, `subdivision`, `geometry_nodes` with `linear_array` preset only.
- UUID-scoped Blender main-thread dispatch, scene revision and idempotency controls, strict input validation and bounded geometry node count/offset; no arbitrary Python, shader graph, or node identifiers accepted.
- PBR uses new Principled material, base color/metallic/roughness, optional local SHA-256-pinned image texture packed inside the BLEND; material gets stable `_bas08_material_id`. Original source URI/checksum preserved as metadata; license recorded as user-provided/unverified.
- Texture profile is **offline file:// only** (32 MiB max, supported image extensions, allowed-root and no-follow checks); external assets/downloads are explicitly deferred. Private single-writer POSIX sandbox is required. Input provenance/licensing must be reviewed by the owner before redistribution.
- Bevel and Subdivision modifiers and Geometry Nodes linear array are added to an identified, local MESH; unknown IDs and linked datablocks fail closed. Materials never modify a shared mesh datablock.

## Security and limitations

A PBR material is created anew and assigned as an appended material slot. Modifier operations append a modifier; no arbitrary existing modifier changes. Geometry Nodes preset constructs a Mesh Line → Instance on Points → Realize Instances chain. This is not a general node graph programming interface.

File URI and checksum preflight are not a distributed or cross-process atomic transaction. Texture metadata points at the original user file; packed image is needed for portability, and copy/reopen verification remains mandatory. The saved texture license value `user-provided; license unverified` is a provenance warning, **not** evidence of licensed external redistribution.

## Required acceptance gates (not yet certified)

1. Python 3.11/3.12 contract/security unit tests and source compile under CI on the exact PR head SHA.
2. Blender 4.5.15 GUI MCP test: 3 distinct PBR materials, both basic modifiers and linear-array preset; verify their data with Blender, save BLEND, separate Blender process reopen and compare.
3. Confirm PNG preview matches expected materials/modifiers, and collect environment manifest, checksums and logs.
4. Negative cases: stale revision, missing/wrong UUID, rejected texture traversal/symlink/wrong checksum/remote URL, invalid typed/bounds, repeat idempotency.
5. Review the final diff/CI and record owner acceptance decision. No Done/merge before these gates pass.

Local Blender binary and direct Git clone were not available in the interactive execution environment; GitHub CI is the authoritative remote validation. This branch does not claim full GUI golden-scene acceptance or human manual UAT.
