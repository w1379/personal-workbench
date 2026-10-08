# Architecture

Personal Workbench keeps the existing Python + SQLite + ordinary-file design. The agent is the user-facing interface; the CLI is the agent's tool boundary. There is no web server, model API integration, scheduler, or second editable representation of current facts.

## Module map

| Module | Responsibility |
| --- | --- |
| `system/pis.py` / `pis.cmd` | Short-lived commands, JSON receipts, error/exit status |
| `personal_system/core.py` | Identity, notes, typed fields, state, links, revisions, transactions |
| `personal_system/documents.py` | Immutable originals, versions, extraction, processing status |
| `personal_system/search.py` | FTS5 projections, filters, short-term matching, aliases, evidence ranking |
| `personal_system/delivery.py` | Immutable delivery batches, publication locks/journal, recovery |
| `personal_system/recovery.py` | Explicit integrity checks, full backups, verified restoration |
| `personal_system/inbox.py` / `system/inbox.py` | Optional structured packages and SSH receipts |
| `system/schema/` | Empty schema definitions; no personal dataset or source configuration |
| `system/legacy_tools/` | Retained school/advisor query algorithms; writes disabled |

## Authority and projections

`data/library.sqlite3` stores authoritative notes, fields, object identity, source records, business rows, relationships, revisions, and document metadata. `data/originals/` stores immutable document bytes. Neither is a disposable index.

`search_entries`, `search_fts`, and `fragments_fts` are derived retrieval structures. A rebuild reprojects authoritative records while preserving identity and history. Original text fragments can also be referenced as evidence; repairs must preserve or explicitly map those anchors.

The schema filenames `legacy*.sql` and internal `v2` names are retained to avoid an unnecessary schema rewrite. They define empty supporting tables for sources, notices, admissions, research, careers, and infrastructure. No personal rows are seeded, and the one-off migration/deployment scripts from the originating personal installation are not part of this repository. Optional domain tables do not have to be populated for notes, fields, or documents to work.

## Transactions and history

SQLite WAL allows readers while short write transactions are serialized. Writes use foreign keys, busy handling, and revision checks. An update records its before/after evidence and updates indexes within the transaction. A stale revision is a conflict, not permission to overwrite newer state.

Large filesystem operations, network requests, parsing, and hashing stay outside write transactions. Multi-file backups and deliveries use separate filesystem coordination and validation. They are not described as globally atomic database/filesystem transactions.

## Portability and local scope

The default data root is derived from the checkout, not a fixed path. `--root` can select a disposable or restored data root while the executing program supplies code and schemas. The Windows runtime is installed locally from verified official archives; dependencies are rebuilt, not shipped in Git.

The application itself makes no model calls. The agent may read local data into its own model context according to its configuration. Installation and explicitly requested SSH inbox commands use network access; ordinary records and search do not.

Private user content belongs in the ignored data/workspace/output directories. Generic development history belongs in the repository. Merely publishing the code neither publishes a user's workbench nor turns it into a remotely accessible service.
