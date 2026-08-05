# EXAMPLE staging -- recall automatic drafts

This file shows the STRUCTURE of the staging file, and it is deliberately
EMPTY of entries: a staging file is written by a machine, never by hand.

DRAFT entries are appended by `recall-staging.py` (PostToolUse on Write) when
an artifact is born. They are NEVER read by the match: only the catalog is
served back into a prompt, because an unnamed, undeduplicated, unverified line
would be exactly the kind of entry that lies. The curation pass (`curate.py`)
promotes them, deduplicates them, marks supersessions, then empties this file.

`origin: auto` everywhere here: an automatic entry is RECALLED, it never
triggers an action on its own -- in particular its `check:` is never executed.

Default location: `$HARNESS_STATE_DIR/recall/STAGING.md`, overridable with
`HARNESS_RECALL_STAGING`. The hook creates it with this header on first use.

A promoted draft looks like this once curated: the `draft-` prefix is gone, the
name is a real kebab-case name, the resume says something, and the supersession
fields point at whatever it replaced.

---
