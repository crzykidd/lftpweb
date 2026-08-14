---
name: 2026-08-13-field-help-sweep
status: pending
created: 2026-08-13
model: sonnet
completed:
result:
---

# Task: Per-field help across the settings surface, and one wrong label to fix

`dfff677` built `frontend/src/components/FieldHelp.tsx` and demonstrated it on three fields —
**Sync mode**, **Patterns-only**, and **Known-hosts policy**. This task applies it across the
rest of the settings surface.

## Fix this first: the extraction label is wrong

`QueuesTab.tsx` and `PostProcessingTab.tsx` label extraction as **"7zz — zip/7z/rar/rar5/…"**.
That is false. `855e7a3` established that Alpine's `7zip` package is built **without the RAR
codec** — `7zz i` lists no `Rar` handler — which is why a separately-built `unrar` binary was
added to the image. `README.md` and `NOTICE` are correct; the UI labels are not.

This is the same class of defect as the Dockerfile comment that claimed rar support for nine
phases while extraction was completely broken for it. Correct the labels to match reality:
`7zz` for zip/7z/tar/gz/bz2/xz, `unrar` for rar and rar5.

## The bar for a help entry

**Not every field needs one.** A field whose label already explains it is noise with an info
icon next to it. Add help where one of these is true:

- **A wrong answer costs something** — data deleted, a transfer that silently never runs, a
  security control weakened.
- **The label cannot explain itself** — a term of art, or a number whose units and effect are
  not obvious.
- **The behaviour is genuinely surprising**, including to someone who read the label carefully.

**Keep each entry short.** Two or three sentences. A popover full of prose does not get read,
and the Docs → Concepts page already exists for anything that needs real explanation — link to
it rather than duplicating it.

## Priority order

Work down this list and stop when you judge the remainder is not earning its icon. **Finishing
the list is not the goal; covering what matters is.**

**Highest — a wrong answer destroys data or silently does nothing:**
- `verify_hash_on_disk` — proves the file is *readable end to end*, not *correct*. It is a
  weaker guarantee than a `.sfv`/`.md5` sidecar, and on a `move` queue it participates in
  gating an irreversible remote delete.
- `delete_archives_after_extract` — deletes the archive volumes after a successful extract.
- `failed_retention_enabled` / `_days` — deletes `_FAILED_` diagnostic directories.
- Retention settings (currently API-only — say so rather than pretending there is a UI).
- `re_download_externally_removed` — off by default; explain the `*arr`-import loop it prevents
  and that it only affects `copy` queues.
- `staging_path` — **routinely misunderstood, including inside this codebase.** It is the
  *final destination* an item is relocated to after post-processing, not a staging area
  downloads land in. `local_path` is where lftp writes.

**High — silently changes whether things happen:**
- The four post-processing toggles' inherit/override state, if `dfff677`'s existing inline
  explanation is not already sufficient — check before adding a second one.
- `auto_queue_enabled`, `scan_interval_s` (the 10s option already carries a load warning —
  do not duplicate it).
- The settle gate's toggle, scan count, and time floor.

**Medium — units and effects are not obvious:**
- Transfer settings: bandwidth ceiling, max concurrent, small-item threshold, fast-lane
  concurrency and reserve, minimum share floor, mirror parallel count, `pget` connection count,
  max attempts, retry backoff.
- `extra_lftp_settings` — free text passed to lftp; note that a rejected setting can fail in
  confusing ways (`net:reconnect-interval-base` refusing `5s` cost this project a debugging
  session).
- Auth modes and the trusted CIDR list; backup interval and keep count; log level and
  `LFTPWEB_DEBUG_LIBS`.

## Verify every claim

Same rule as the docs task: **read the code before writing the sentence.** A great deal changed
on 2026-08-12/13 and several behaviours were reshaped within hours. Where you cannot confirm
something, leave it out. The extraction label above is precisely what happens when UI text
outlives the behaviour it describes.

## Before you start

- `frontend/src/components/FieldHelp.tsx` and its three existing usages, for the established
  pattern.
- `frontend/src/pages/docs/ConceptsPage.tsx` — link to it instead of restating it.
- The settings tabs under `frontend/src/pages/settings/`.
- `prompts/open-issues.md`'s "worth reading" sections — an accurate, current summary of the
  subtle behaviour, written while it was being built.

## Working tree check

`git status --porcelain`. If files you need are dirty, list them and ask.

## Conventions to honor

- A frontend test runner exists (**Vitest + happy-dom, `npm test`**). Add tests for anything
  testable you introduce, and run it.
- `docs/decisions.md`, newest at top — your bar for what got an entry and what did not.
- `CHANGELOG.md`; `DESIGN.md` §9.3 if it describes the settings surface (standing approval).
- `npm run lint` / `npm run build` / `npm test` clean; `uv run pytest` unchanged (887) unless
  you touch the backend, which this task should not need to.
- **You cannot see the UI.** You cannot judge whether an icon per field becomes visual noise at
  the density of these forms. Say so, and err toward fewer.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line message, which fields you gave
   help to **and which you deliberately skipped and why**, any claim you could not verify, test
   and lint results, and anything not fixed. Never `git add -A`, never push.
