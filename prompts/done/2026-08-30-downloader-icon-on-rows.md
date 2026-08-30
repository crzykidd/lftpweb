---
name: 2026-08-30-downloader-icon-on-rows
status: completed        # pending | completed | failed
created: 2026-08-30
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-30
result: >
  Migration 033 adds item.download_client_id/download_client_matched_at (ON DELETE SET NULL,
  forward-only, no backfill). Written from ClientSyncScheduler._update_preflight itself (never
  from a client_skip_enabled/withhold-gated path) via a new _write_client_attribution pass, path
  match only, write-once-and-leave-it. Joined into JobOut/HistoryJobOut exactly like
  arr_instance_name/kind. ClientBrandMark (LifecycleIcons.tsx) renders next to ArrRowChip on
  TransfersPage.tsx's shared Row (covers both Active and Complete boxes); simple-icons ships
  neither a sabnzbd nor an rtorrent mark (checked against the dataset), so it is a text-chip
  fallback (SAB/rT) always, never a logo. All gates green: pytest 2029 passed/49 skipped (+10),
  ruff check/format clean, frontend lint/tsc/vitest clean, 843 passed (+4).
---

# Task: show which download client brought an item down, next to the *arr icon

The user's ask: *"add another icon to the list, right next to the ARR icon. We should show the
SAB or rtorrent icon based on what downloader was used. This should be in pending/downloading
and complete lists."*

**The data does not exist yet.** The `item` table has no link to a download client. The *arr icon
works because migration 018 persisted `arr_status`/`arr_download_id` on `item`, letting
`arr_instance_kind` be `LEFT JOIN`ed into `JobOut`/`HistoryJobOut`. The client↔item match is
currently computed inside `core/settle.py.find_client_completion` and thrown away. So this task is
four things in order: persist it, write it, join it, draw it.

## Before you start

Read, in this order:

1. `backend/lftpweb/migrations/018_arr_integration.sql` — the shape this mirrors.
2. `backend/lftpweb/migrations/027_download_clients.sql` — the `download_client` table, and its
   own note on `ON DELETE SET NULL` for a parent going away.
3. `core/clientsync.py._update_preflight` — where a client transfer is already matched to a queue
   and item every pass. **This is the natural write point.**
4. `backend/lftpweb/api/jobs.py` — how `arr_instance_name`/`arr_instance_kind` are joined into
   `JobOut` and `HistoryJobOut`.
5. `frontend/src/components/LifecycleIcons.tsx` — `ArrBrandMark`, `ArrTextChip`, `ArrRowChip`,
   and the **module comment's rule about never inventing glyph path data**.
6. `CLAUDE.md` — commit rules; gates in the **foreground**, from the repo root.

## 1. Migration 033

Add to `item`:

- `download_client_id INTEGER REFERENCES download_client(id) ON DELETE SET NULL`
- `download_client_matched_at TEXT`

Follow migration 018's own commenting style. Deleting a client instance must null the column, not
orphan or cascade-delete items — the item is still real, we just no longer know who fetched it.

**Forward-only, and say so in the migration comment:** items downloaded before this ships have no
recorded client and never will. That was the user's explicit, informed choice over live-resolving
from the poller cache (which would make History rows silently lose their icon as SAB/rTorrent
forget old jobs). Do not add a backfill that guesses.

## 2. Write it from the poller

`_update_preflight` already matches every reported transfer to a queue and an item every pass.
Record the attribution there.

- **Independent of any skip/withhold setting.** Do NOT write it from the
  `settle.SettleSettings.client_skip_enabled` recheck path — that is off for some users, and the
  icon must not depend on an unrelated toggle. This is the mistake most likely to be made here.
- Write once and leave it: if `download_client_id` is already set to this same instance, don't
  rewrite `download_client_matched_at` on every pass. Only update if it was NULL or the instance
  actually changed (a release genuinely re-fetched by a different client).
- The match quality rules are already established — `_client_content_path_matches` for path, the
  category map as fallback. Reuse them; do not invent a third matching notion.

## 3. Join it into the API

`JobOut` and `HistoryJobOut` each gain `client_instance_name: str | None` and
`client_instance_kind: str | None`, `LEFT JOIN`ed exactly the way the *arr fields already are.
`null` whenever the item has no recorded client. Mirror the *arr fields' own docstring style,
including what `null` means and why the raw id stays server-side.

Frontend `api/types.ts` gains the matching optional fields.

## 4. Draw it

A new `ClientBrandMark` in `LifecycleIcons.tsx`, rendered immediately next to `ArrRowChip` on:

- `frontend/src/pages/TransfersPage.tsx` (~line 303) — the pending/downloading rows.
- The complete/history rows — find them via `frontend/src/hooks/useCompleteJobs.ts` and its
  consumers; the same `ArrRowChip` is already rendered there.

Rules:

- **`kind` → logo is a display switch, and it is allowed.** `ArrBrandMark` already does exactly
  this for `sonarr`/`radarr`. The repo's "no `if client_type === 'sabnzbd'`" rule (spec §4.4/§5.1)
  governs *behavior* — capabilities, gating, field support — never which picture to draw. Do not
  contort this into a capability lookup; do add a comment saying why the switch is legitimate
  here, so the next reader doesn't 'fix' it.
- **Check whether simple-icons actually ships `sabnzbd` and `rtorrent` marks before using one.**
  If a real mark exists, copy the path data verbatim and add a NOTICE entry in the exact format
  the Sonarr/Radarr entries already use. **If it does not exist, fall back to an `ArrTextChip`-
  style text chip (`SAB`, `rT`) — do NOT invent or approximate SVG path data from memory.** That
  rule is stated in `LifecycleIcons.tsx`'s own module comment and in `EventsLinkButton.tsx`.
  Report which way it went.
- Renders **nothing at all** when `client_instance_kind` is null — same "no data, no mark" rule
  `ArrRowChip` follows for a null `arr_status`. An item with no recorded client must not get a
  placeholder or a question mark.
- Hover text names the instance (its configured name, e.g. `"SABnzbd"`), like the *arr mark does.
- Sizing and alignment must match `ARR_LOGO_SIZE_PX` and sit inline without shifting the row.

## Layout warning

This adds a mark to a row line that already has lifecycle icons, an *arr chip, and a name that
truncates. Two bugs in this repo were pure layout problems **invisible to every test**, because
jsdom performs no layout. Do not let the new mark push the row's numeric columns or cause the name
cell to reflow. Do not write a test that appears to cover layout — say so in a comment instead.
If something looks wrong on screen, **ask for a screenshot rather than guessing**; guessing from
reported text has been wrong twice here and an image settled it immediately.

## Tests

- Backend: the migration applies cleanly; the poller writes the attribution and does not rewrite
  `download_client_matched_at` on an unchanged repeat pass; deleting a client instance nulls the
  column and leaves the item; `JobOut`/`HistoryJobOut` carry the joined name/kind, and `null` when
  unrecorded; the attribution is written with `client_skip_enabled` **off** (the independence rule
  above — name this test so its purpose is obvious).
- Frontend: the mark renders for a known kind, renders the text fallback for an unknown/future
  kind, and renders **nothing** for `null`.

## Conventions to honor

- Match the surrounding docstring style — these modules explain *why* at length, including which
  earlier decision a line reverses.
- Doc updates ship in the same change set: `DESIGN.md` §17, `docs/download-client-framework-spec.md`,
  and `docs/decisions.md` (newest at top) for the persist-vs-live-resolve choice and its rejected
  alternative, and for the display-switch-is-not-behavior-branching reasoning.
- Gates, each its own **foreground** command from the repo root, reading each exit code:
  `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `npm --prefix frontend run lint`, `npx tsc -b --noEmit` (from `frontend/`; there is no
  `typecheck` script), `npm --prefix frontend test`.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record non-obvious decisions in `docs/decisions.md`.
4. **Do not commit.** Prepare the working tree, then report back to the orchestrating session:
   the file list, a one-line commit message, and the final test counts. The orchestrating session
   surfaces the `y/n` to the user. Never `git add -A`, never push.
