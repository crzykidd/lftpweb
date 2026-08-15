---
name: 2026-08-14-local-errors-misclassified-as-remote-gone
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-13
result: >
  Added LOCAL_FS_ERROR, matched by lftp's rename(<src>, <dst>): No such file or directory
  message shape (both operands always local), and put it in TRANSIENT_ERROR_CLASSES so it
  retries with backoff instead of permanently failing/suppressing the item. REMOTE_GONE's
  pattern and never-retry status are unchanged; a genuinely missing remote file still
  classifies REMOTE_GONE. No frontend change needed -- Transfers/History already show the raw
  error_class plus the retained output_tail verbatim. DESIGN.md §4.3 wording drafted in
  docs/decisions.md, not applied -- awaiting approval. Full suite green: 903 backend tests (incl.
  new classify_output cases for both directions), ruff check + format clean, frontend lint/123
  tests/build clean, all three compose files valid.
---

# Task: Stop classifying local filesystem errors as `REMOTE_GONE`, and stop permanently suppressing them

`core/lftp.py:98` classifies a job as `REMOTE_GONE` whenever lftp's output contains the bare
string `no such file`, anywhere, with no regard for whether the path involved is remote or local.
`REMOTE_GONE` is in the never-retry set, so a transient **local** failure permanently fails the
job and suppresses the item.

This fired three separate times in one evening of live testing (2026-08-13/14), every time on a
*local* rename:

```
job 46: pget: …: rename(/mnt/fs02-media/working/box-ar-tv/…mkv.lftp,
                        /mnt/fs02-media/working/box-ar-tv/…mkv): No such file or directory
job 48: pget: rename(/mnt/…/xpost/S06E21….mkv.lftp, /mnt/…/xpost/S06E21….mkv): No such file or directory
        pget: rename(/mnt/…/xpost/S06E22….mkv.lftp, /mnt/…/xpost/S06E22….mkv): No such file or directory
```

Both real causes were local and transient — another process writing into the same directory, and
Sonarr importing and then removing the download folder mid-transfer. Neither had anything to do
with the remote. Each was reported to the user as "the remote file is gone" and left the item
dead rather than retried.

## Before you start

- Read `CLAUDE.md`, `DESIGN.md` §4.3 (error classes and which are transient), and
  `core/lftp.py`'s `classify_output` plus the `_TRANSIENT_CLASSES` whitelist below it.
- Note the existing comment on that whitelist: it is deliberately a whitelist, not
  "everything except the permanent four", because `UNKNOWN` must not be retried blindly.
  **Preserve that reasoning** — this task narrows a misfire, it does not loosen the policy.

## Working tree check

Run `git status --porcelain` first. Other queued work touches `core/queue.py` and the frontend.
If a file this plan needs is dirty, list it and ask. This prompt file is exempt.

## What to do

### 1. Distinguish local from remote in the classifier

`no such file` must only mean `REMOTE_GONE` when it refers to the **remote** side. A message about
a local path is a different failure with a different correct response.

Approaches worth weighing (pick one, justify it in `docs/decisions.md`):

- Match the surrounding message shape. lftp's local failures here are `rename(<src>, <dst>): No
  such file or directory` with both operands being local paths — a distinctly recognizable form.
- Compare the paths in the message against the job's known local root
  (`_RunningProcess.local_root`) versus its remote path. The most precise option, and the data is
  already on the process record.

**Do not simply delete the `no such file` pattern** — a genuinely missing remote file is a real
case that `REMOTE_GONE` exists to name, and it must keep working. Add a test for both directions.

### 2. Give local failures their own class, and make it retryable

Add a class for this (e.g. `LOCAL_FS_ERROR`) and put it in the **transient** set, so the existing
retry-with-backoff path applies. All three live cases were transient by nature: the interfering
process stopped, or the importer finished. A retry would have recovered every one of them.

Make sure the item is not left `auto_queue_suppressed` for this class — the whole point is that it
should be picked up again.

### 3. Make the surfaced message honest

The Transfers row and History currently tell the user the remote file is gone. For a local error
the message must say what actually happened, and the retained `output_tail` (kept on failures, and
since 2026-08-14 on successes too) already carries lftp's exact words. Do not invent new phrasing
where lftp's own message is clearer.

## Testing

- `classify_output` against the three real messages quoted above → the new local class, not
  `REMOTE_GONE`.
- A genuine remote-missing message → still `REMOTE_GONE`.
- The new class is in the transient set and a job carrying it retries rather than suppressing.
- An `UNKNOWN` classification still does **not** retry — the existing whitelist reasoning holds.

Run `uv run pytest` with the fake seedbox up (`docker-compose.test.yml`, `gen_key.sh` first),
`ruff check` **and** `ruff format --check`, `npm run lint`, `npm test`, `npm run build`, and
`docker compose config --quiet` on all three compose files.

## Conventions to honor

- Non-obvious decisions in `docs/decisions.md`, newest at top, with rejected alternatives.
- `CHANGELOG.md` entry — a class of failure that used to be permanent now retries.
- If `DESIGN.md` §4.3's error-class list needs a new entry, **draft the wording in
  `docs/decisions.md` and ask** rather than editing `DESIGN.md` directly.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` it to `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md`.
4. Prepare ONE commit; **do not commit**. Report the file list and a proposed one-line `fix:`
   message back to the orchestrating session, which surfaces the `y/n`. Never `git add -A`,
   never push.
