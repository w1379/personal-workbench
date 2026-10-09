# Fresh setup for the agent

The user talks to the agent. These commands are the agent's implementation details, not a user onboarding checklist.

## Requirements and root selection

The validated installation path is Windows x64. Use an existing Python with pip to install a separate project runtime; Git is needed to clone the repository. Codex must be able to execute local commands. This repository does not install Codex or configure a model provider.

First check that Git, Python, and pip are available. If a dependency is missing, help install it from official sources within the environment's permissions, then verify it is usable before continuing.

Clone into the user's chosen new directory, read its `AGENTS.md`, and run the following commands from that checkout. Do not copy an existing personal database or reuse the author's paths. If the destination is occupied, inspect it instead of overwriting it.

## Install, initialize, and verify

```powershell
py -3 system/setup_runtime.py
system/pis.cmd init
system/pis.cmd status
system/pis.cmd todos
system/pis.cmd check
```

Stop on any command failure and read its output before continuing. The installer downloads checksum-pinned official Python and SQLite archives, verifies FTS5 with the trigram tokenizer, then installs document parsers from official PyPI into `system/runtime/`. It does not replace system Python. Source versions and archive checksums are recorded in `system/runtime/python/provenance.json`.

`init` creates directories and tables, not personal records. On a new installation, every count returned by `status` should be zero; `todos` should be empty; `check` should report `ok: true` without integrity errors. If they are nonzero, inspect the selected root instead of deleting data. Repeating `init` preserves existing records; it is not a reset command.

The runtime path file uses paths relative to its location. Calls through an absolute `<checkout>/system/pis.cmd` work from other directories. An explicit `--root <other-directory>` selects an isolated data root using the current program's schemas and tools; use it deliberately, never as a fallback to a different user's library.

The installer is pinned rather than “latest”. If upstream availability changes, verify new archives and update checksums deliberately. A checksum failure must not be bypassed.

## Confirm readiness to the user

Report the root, empty initial state, and readiness for natural-language requests. Do not add sample personal fields, example tasks, or reminders. The fictional file in `examples/` exists for documentation and isolated tests only.

Within this checkout, `AGENTS.md` is sufficient navigation. If the user wants to use the workbench from other projects, agree on a short navigation entry in their agent's global instructions pointing to this checkout. Do not silently replace existing global instructions or a different workbench's navigation.

## Optional tools, only when a task needs them

| Task | Tool supplied by the agent/environment |
| --- | --- |
| Web collection and authenticated sources | Browser or HTTP tools with access authorized for that task |
| LaTeX PDF creation | A TeX installation, such as TeX Live with XeLaTeX |
| Legacy DOC/XLS extraction | LibreOffice's `soffice` on PATH; originals are retained if unavailable |
| Scanned PDF or image transcription | A separately chosen OCR workflow; no automatic OCR service is bundled |
| Word/spreadsheet editing | The agent's document tools; extraction in the workbench is not a form editor |
| Selected cross-device submissions | SSH plus an explicitly configured recipient; see [INBOX.md](INBOX.md) |

## Development verification

```powershell
system/runtime/python/python.exe -X utf8 system/run_tests.py
```

The suite uses disposable roots and fictional data, including CLI workflows, search, revisions, delivery, backups, and local inbox packages. It does not contact real SSH hosts or inspect a user's library. Integration outputs in temporary roots must not be copied into the user's empty database.
