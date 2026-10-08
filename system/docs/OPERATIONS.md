# Agent operating guide

Commands below assume this checkout as the working directory. From elsewhere use the absolute path to `system/pis.cmd`. Long text and JSON go in UTF-8 files; JSON arguments beginning with `@` read a file. All IDs, revisions, and paths in examples must come from actual receipts, not invented values. Examples are not instructions to seed the live library.

## Choose the smallest operation

| User intent | Tool |
| --- | --- |
| Remember a fact or decision | `note` with source and evidence date |
| Retain a file and make supported text searchable | `save` |
| Locate an existing record/project | `search`, `resolve`, then `get` |
| Store a reusable personal field | `profile-get`, then `profile-set` |
| Register an existing external project | `register --path` |
| Start a new task | `workspace` |
| Deliver results | `bundle` after output validation |
| Read/update a personal todo | `todos`, `register`, `state` |

## Records, files, and sources

```powershell
system/pis.cmd note --file 'workspaces/<task>/decision.md' --title 'Model decision' --source 'User confirmation in the current conversation' --occurred-at '2026-01-15'
system/pis.cmd save 'workspaces/<task>/notice.html' --title 'Example notice' --authority official --evidence-kind official_document --domain school --url 'https://example.edu/notice' --topic 'research'
system/pis.cmd get '<returned-item-id>'
system/pis.cmd relate '<document-id>' '<project-id>' --type related_to --source 'This document supports the project'
```

Do not mark a user statement or agent-generated explanation as official. `--published-at` is the source's publication time, not the download time. For ordinary user-supplied files the `save` defaults are appropriate. A note without an explicit title derives one from its body; explicit titles remain stable on updates.

`save` preserves the original with a SHA-256 hash before attempting extraction. New versions of an existing document require `--document-id` and its fresh `--revision`. Unsupported formats are retained with a processing state; parsing failure is not proof that the original was lost. `parse <version-id>` retries extraction. Already referenced evidence anchors must not be discarded during repairs.

Text, HTML, PDF text layers, DOCX, and XLSX are supported. Legacy DOC/XLS use LibreOffice when present. Scans need a separate OCR step. HTML containing embedded documents may remain partial until those assets are obtained. A parser result is not a semantic completeness guarantee: verify that it is the requested body, not a login screen or error message.

## Search, then read evidence

```powershell
system/pis.cmd search 'research scholarship' --domain school --official-only --history current
system/pis.cmd search '项目 决定' --history all
system/pis.cmd resolve 'project alias'
system/pis.cmd profile-get 'contact.email'
system/pis.cmd school-search 'scholarship' --limit 10
```

`search` supports `--domain`, `--kind`, `--authority`, `--official-only`, `--topic`, `--topic-mode any/all`, `--history current/historical/all`, `--active-only`, `--evidence-kind`, and pagination. Default history is `all`, with current content preferred. Follow `has_more` / `next_offset` when complete results matter. Filters apply before the result limit.

Words separated by spaces are required terms. The agent reformulates synonyms and natural-language questions; there is no semantic embedding service. Short Chinese terms use supplemental substring matching. Results include evidence locations and processing states. Read relevant fragments/originals instead of treating snippets as a complete answer. Use `get` for object metadata, current state, aliases, locations, and links.

`school-search` retains source-aware school evidence ranking. `advisor-search` queries the optional research tables; an empty installation has no advisor dataset. These algorithms do not fetch online content. Use `sql` only for targeted read-only queries that ordinary commands cannot express; it opens a restricted read-only connection.

## Personal fields and revisions

```powershell
system/pis.cmd profile-get 'contact.email'
# Only create this field when the user has actually supplied it.
system/pis.cmd profile-set 'contact.email' --json '@workspaces/<task>/email.json' --status user_confirmed --source 'User confirmation'
# Existing fields require the revision just read.
system/pis.cmd profile-set 'contact.email' --json '@workspaces/<task>/email.json' --revision 1 --status user_confirmed --source 'User correction'
system/pis.cmd history '<returned-profile-item-id>'
```

The JSON file contains the value itself, for example the fictional string `"demo@example.org"`. Preserve string types for identifiers with leading zeroes. `profile-get` returns `found: false` when absent. Read the returned object revision before updating; exit code 3 means a conflict and requires a new read and merge. For notes use `note-update --file ... --revision ...`; for generic object state use `state --json @... --revision ...`.

The record's evidence date and the time it was entered are different. Do not choose current facts by insertion order. Fields already owned by a dedicated table must not also be editable in `current_state`. Custom write helpers must use `core.connect`, `core.transaction`, stable object identity, changes, and index updates in one short transaction; downloads, parsing, and expensive file work stay outside it.

## Projects, workspaces, and documents

```powershell
system/pis.cmd register 'Existing research project' --path '<existing-absolute-local-directory>' --alias 'research project' --description 'Purpose and main entry point'
system/pis.cmd workspace 'Resume draft' --description 'Prepare a resume from confirmed experience'
```

Use the workspace path returned in `locations`. Drafts, scripts, compilation logs, and intermediate files go there. Install/use external tools only as needed for the actual task. Validate a PDF or other deliverable before reporting success. Workspaces are registered, but their changing file contents are not automatically indexed. Save important outputs with `save`, link them to the workspace or project, and use `state` to record the verified result and completion state.

Registering an external path does not copy or move its files. `relocate <item-id> <new-path> --revision N` records an existing new location; it does not move the project for the user.

## Personal todos

Use `todos` for current items and `todos --all` for history, only when the user asks. A todo is an item with `kind=task` and `domain=personal_todo`.

```powershell
system/pis.cmd register '<user-requested-task>' --kind task --domain personal_todo --description '<user context>'
system/pis.cmd state '<returned-task-id>' --json '@workspaces/<task>/todo.json' --revision 1 --source 'User explicitly requested this todo'
```

Minimal state is `{"status":"open","due_on":null,"details":""}`. Use `YYYY-MM-DD` only when the date is known. To complete/cancel, read the current state and revision, preserve its other fields, and write `completed` / `cancelled`. Do not delete history or infer completion from a past deadline. Do not add todos automatically from collected notices.

## Deliveries

```powershell
system/pis.cmd bundle --title 'Resume' 'workspaces/<task>/resume.pdf' 'workspaces/<task>/resume.tex'
system/pis.cmd bundle-list
system/pis.cmd bundle-restore '<returned-bundle-id>'
system/pis.cmd bundle-recover
```

Each batch is an independent snapshot under `exports/history/<bundle-id>/`; `exports/current/` is the latest successful delivery, flat and ready to select. A themed ZIP and manifest are included. Two concurrent tasks should each cite their own historical batch. Old output snapshots must not be edited in place. `bundle` snapshots files but does not by itself index their text—use `save` where retrieval matters.

Publication uses locking, a journal, and recovery. Windows may briefly expose a directory-switch gap; do not claim an infallible atomic directory replacement. If interrupted, use the recovery command and verify the selected batch.

## Maintenance and full recovery

```powershell
system/pis.cmd status
system/pis.cmd check
system/pis.cmd reindex
system/pis.cmd backup
system/pis.cmd restore '<backup-directory>' '<new-empty-destination>'
```

`check` inspects structure, hashes, and index consistency; it does not establish content coverage or meaning. `reindex` rebuilds derived projections from authoritative data; normal writes already update indexes incrementally. Avoid full checks/rebuilds during ordinary recording.

Backups include an online SQLite snapshot, originals, working files, software, and delivery history. They exclude `.git`, caches, prior backups, and the rebuildable runtime. External projects are registered only; their contents are not automatically copied. Cooperative locks and before/after inventories detect changing inputs; a failure must not be reported as a full snapshot.

Restoration verifies the manifest and refuses to overwrite a nonempty destination. If the original installation is unavailable, use an existing Python to run `python -B <backup>/payload/system/pis.py restore <backup> <new-empty-destination>`, then install the runtime in the restored directory. Do not run mutable tasks inside the backup itself. Verify the restored content before switching the user's navigation to it.

No background schedule or retention deletion is installed. Decide any external backup destination with the user. Git tracks software; it is not the backup mechanism for personal content.
