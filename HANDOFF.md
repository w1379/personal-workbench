# Software handoff

This file tracks generic software development only. Personal task history and local maintenance details belong in `HANDOFF.local.md` or the user's saved records; neither should be added here.

## Initial review edition — 2026-10-08

- Repository name: `personal-workbench`. Chinese and English READMEs are separate and linked. MIT license. Codex is the documented user entry point; no other agent compatibility is claimed.
- Prepared as an independent code-only checkout with a fresh Git history. No personal database, source catalog, document originals, workspaces, exports, runtime, or old Git history is included.
- Existing storage, schema, retrieval, versioning, delivery, and backup design retained. Author-specific migration/deployment tools omitted. Historical internal schema names remain to avoid an unnecessary rewrite.
- Small portability changes: relative embedded-Python search path; explicit SSH host selection; optional query tools loaded from the executing program rather than the selected data root; a helpful error before runtime installation.
- First publication target is a **private review repository**. Do not change visibility without the owner's later instruction. Current installation scope is Windows x64; no services or automatic synchronization are configured.
- Validation: official runtime installation completed with Python 3.14.7, SQLite 3.53.4/FTS5, and the document parsers. All **68 automated tests passed**, including three fresh-install workflow tests. The local initial database has zero records, documents, fields, and todos; integrity checks have no errors or warnings.
- End-to-end coverage includes calls from an unrelated working directory, a data root containing spaces and Chinese characters, note/search, typed fields and stale-revision rejection, document extraction, workspace links, ZIP delivery, backup, and queries through the restored copy's own program. SSH host validation is checked without contacting a server.
- Publication review: 41 tracked text files; no personal dataset, author-specific paths/hosts, or common credential patterns found in the selected content; local Markdown links resolve; data, runtime, output, and local-handoff exclusions verified. This is a bounded repository review, not a claim that pattern matching proves absence of every possible secret.

## Remaining scope

- General website collection, OCR, LaTeX, and Office editing are provided by the agent/environment and are not bundled services.
- Legacy query modules retain unused standalone routines, blocked from writes by their connection helper; the supported tool entry point is `system/pis.py` / `pis.cmd`. This is a documented cleanup opportunity, not a blocker for the initial code-only edition.
- Some generated delivery notes and diagnostic messages remain Chinese. The English README does not imply complete localization.
