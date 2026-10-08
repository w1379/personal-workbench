# Retained query algorithms

`kb.py` and `advisor_map.py` are retained as implementation dependencies for `school-search` and `advisor-search`. Their generic retrieval functions operate on the current workbench's database. `db.py` blocks write-capable connections.

Use `system/pis.cmd` as the agent-facing tool entry point. The older standalone CLI/import/export routines inside these modules are not the public workflow; their historical seed/config files are not shipped. They are retained to avoid rewriting proven retrieval code during extraction of this repository.

No personal source catalogs, advisor datasets, or author-specific configuration are included.
