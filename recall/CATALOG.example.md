# EXAMPLE catalog -- recall layer 2

EVERY entry below is INVENTED to show the FORMAT. None of them describes a real
machine, and none of the paths exists: run the engine against this file and it
will correctly report the paths as missing. That is the point -- an entry is
worth something only when it is verified against a real filesystem.

Copy this file, delete the examples, then point `HARNESS_RECALL_CATALOG` at
your copy.

What belongs here: a REACTIVABLE artifact, meaning a thing that exists and has
a "how to turn it back on". A fact, a decision or a lesson belongs in memory
instead; the two are linked with the `memory:` field, never duplicated.

Entry schema:

```
## <name>
aliases: the natural words the operator would actually type, comma-separated
path: /absolute/path
type: service|venv|site|skill|dataset|doc|pipeline
status: active|revivable|sunset|dead
resume: one line
reactivate: the command or procedure that brings it back
check: (optional) health command proving REAL life (rc 0 = alive)
project: parent project
memory: <agent>:<note-slug> (soft pointer into memory)
updated: YYYY-MM-DD
origin: human|auto|external (absent = human; auto/external never acts alone)
supersedes: name of the entry this one replaces (optional)
superseded_by: name of the entry that replaces this one (mark, NEVER delete)
sheet: (optional) working sheet whose content is injected on match
importance: optional integer (reserved, unused)
```

Only `check:` commands from a small allowlist are ever executed (`systemctl`
read-only verbs, `test`, `pgrep`, `curl` with no disk write). A `check:` field
is never written by a model: the curation pass restores it.

---

## nightly-backup
aliases: backup, nightly backup, snapshot job, backup timer
path: /opt/example/backup/nightly-backup.timer
type: service
status: active
resume: Nightly snapshot of the application data directory to cold storage, run by a user timer at 03:15.
reactivate: systemctl --user enable --now nightly-backup.timer
check: systemctl --user is-active nightly-backup.timer
project: example-platform
memory: builder:backups-are-verified-by-restore
updated: 2026-01-14
origin: human

## invoice-pipeline
aliases: invoices, invoice pipeline, billing export, monthly invoices
path: /opt/example/pipelines/invoices
type: pipeline
status: revivable
resume: Monthly export turning raw billing rows into per-customer invoices. Stopped after the billing provider migration; the code still runs, the credentials do not.
reactivate: activate /opt/example/pipelines/invoices/.venv then run python3 run.py --month YYYY-MM ; a fresh provider API key is required first
project: example-platform
memory: builder:billing-provider-migration
updated: 2026-02-02
origin: human

## docs-site-v1
aliases: old docs, docs v1, legacy documentation site
path: /srv/example/docs-v1
type: site
status: sunset
resume: First documentation site, static generator, retired when the handbook moved to the new stack. Kept online read-only for the permalinks.
reactivate: re-enable the docs-v1 server block, then reload the web server
project: example-docs
superseded_by: docs-site
updated: 2025-11-30
origin: human

## docs-site
aliases: docs, documentation site, handbook, docs site
path: /srv/example/docs
type: site
status: active
resume: Current documentation site. Built from the handbook repository, deployed by the docs pipeline on every merge to the main branch.
reactivate: run /srv/example/docs/deploy.sh --env production
check: curl -sf -o /dev/null --max-time 6 https://docs.example.com
project: example-docs
supersedes: docs-site-v1
memory: builder:handbook-single-source
updated: 2026-02-10
origin: human
