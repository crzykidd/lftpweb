# Decision record

Non-obvious decisions for lftpweb — approach changes, rejected alternatives, workarounds.
Newest at top. Per the `handoff-prompt-workflow` standard, sessions append here rather than
leaving the reasoning only in a commit message.

---

## 2026-08-12 — Phase 6: the History page — every decision made unattended, recorded for
## review

**Overnight run, no live confirmation possible.** Built `api/history.py` (`GET
/api/history/jobs`, `GET /api/history/jobs/{id}/output`, `GET /api/history/events`), the
matching Pydantic shapes in `models.py`, and the History page itself
(`frontend/src/pages/HistoryPage.tsx`, `components/HistoryJobsSection.tsx`,
`components/HistoryEventsSection.tsx`) — the first UI for the `job`/`event` tables phase 3a
and phase 5 have been filling since the very first overnight run. No schema change: every
column this phase reads already existed. Verified end to end against the real fake seedbox: a
real transfer landed in `/api/history/jobs` with its exact byte count, and a forced failure
(wrong password) carried `error_class: "AUTH_FAILED"` and a real, non-empty `output_tail`
("`pget: /data/pickup/loose-notes2.txt: Login failed: Login incorrect`") fetched through the
on-demand endpoint. Full detail in the phase report; every non-obvious call is recorded here.

**1. `output_tail` is never in the list payload — a separate `GET
/api/history/jobs/{id}/output` fetches it on demand.** DESIGN.md §9.2 says a failed row must
show "the error class and the captured lftp output tail," and the phase 6 prompt says the same
thing again, plus "whatever the UI needs to render output_tail on demand rather than in the
list payload" — an explicit steer away from the obvious "just include it" implementation.
Phase 3a stores up to ~4KB per failed job; `api/jobs.py`'s `JobOut` already inlines it, but
that endpoint's row set is bounded by construction (only the active set plus one terminal row
per item — see `core/queue.py.list_jobs`'s own docstring). History has no such bound — a busy
install accumulates thousands of terminal jobs — so shipping ~4KB × thousands of rows on every
page load would make the row cap (point 3 below) pointless. `HistoryJobOut.has_output_tail`
(a cheap boolean, `output_tail IS NOT NULL`) tells the UI whether there's anything to fetch;
the frontend fetches lazily, only when a user expands a failed row. **Rejected: reuse
`JobOut`/`api/jobs.py`'s shape verbatim for History too.** Simpler (one less endpoint, one
less frontend type), but it's the shape the prompt explicitly warned against, and it would
silently reintroduce the exact "thousands of rows means thousands of 4KB blobs" cost this
phase is supposed to avoid.

**2. Grouping by queue is a frontend concern over an already-filtered/paginated flat
response — not a server-side `{queues: [{jobs: [...]}]}` shape like `GET /api/files`
uses.** DESIGN.md §9.2 says History must be "grouped by queue," the same wording it uses for
Files, but Files groups server-side because it has no row cap — every item in a queue's
current tree is returned. History is capped and paginated (point 3): a global `LIMIT` applied
before grouping means a `{queue_id: [...]}` split computed server-side would represent
*partial, cap-truncated* per-queue lists that don't obviously correspond to "this queue's
recent N jobs." Grouping the returned page client-side, by flattening it into
`(header, job, job, …, header, job, …)` and virtualizing that single flat array (see point 4),
keeps the server response a plain, easy-to-reason-about page of "the N most recent matching
rows" while still rendering with queue headers. **Rejected: server-side grouping identical to
`FilesResponse`.** Would have made the cap's semantics harder to explain ("this queue shows 3
jobs not because there were only 3, but because the global cap landed there") and doubled the
response-shape work for no clear benefit, since the frontend needs to flatten it right back
into one virtualized list anyway (point 4).

**3. Row cap: `LIMIT`/`OFFSET`, default 200, hard-clamped at `MAX_LIMIT = 500` regardless of
what the caller requests, plus a `total` count for a "load more" button.** The phase 6 prompt
says "paginate or cap" — chose both: a default page size sane enough to render without the
caller having to think about it, and a server-enforced ceiling so a buggy or malicious client
asking for `limit=1000000` can't force an unbounded query (`tests/test_history_api.py::
test_row_cap_enforced_even_when_a_larger_limit_is_requested` pins this). `total` is a second
`COUNT(*)` query on the same `WHERE` clause — one extra query per request, judged worth it so
"load more" can show "247 remaining" instead of guessing. **Rejected: cursor/keyset
pagination** (`WHERE (finished_at, id) < (?, ?)`) — more efficient at very large offsets and
immune to page drift when new rows land between pages, but `OFFSET` is simpler to reason
about and to wire into a "load more" button, and this project's own row counts (a homelab
install, not a multi-tenant SaaS) don't call for the extra complexity yet. If offset-based
paging ever becomes visibly slow on a real install, this is the first thing to revisit.

**4. Grouped display + virtualization are the same mechanism: group headers are interleaved
into one flat array and the whole thing is virtualized together (`@tanstack/react-virtual`,
already a project dependency since phase 3b), rather than nested per-queue virtualizers.**
DESIGN.md §9.2 requires both "grouped by queue" and (the phase 6 prompt) "virtualize the
list; a real install will have thousands of rows." A naive per-queue virtualizer-per-section
approach (mirroring `FilesPage.tsx`'s server-grouped sections, each with its own `FileTree`)
works for Files because each queue's own subtree is independently sized and doesn't need to
share a viewport-relative scroll position with siblings; History's page is one capped,
globally-ordered (newest-first) list where sections are incidental groupings of adjacent rows,
not independent trees. One virtualizer over `[header, job, job, header, job, …]` is less code
and scrolls as one continuous list, which is the more natural reading of "recent history,
organized by queue" than N independent scroll areas. **Rejected: `FilesPage.tsx`'s
server-grouped-sections + one virtualizer per section pattern.** Would work, but multiplies
virtualizer instances for no benefit here and doesn't match how a "history feed" is normally
browsed (scroll through everything in time order, with queue as a visual grouping cue, not N
separate lists to scroll independently).

**5. Two independently filtered/paginated sections on one page (`HistoryJobsSection`,
`HistoryEventsSection`) rather than one merged job+event timeline.** DESIGN.md §9.2 describes
History as covering "the `job` and `event` tables" with filters that only partly overlap
(state/error class apply to jobs; kind/level apply to events; date range applies to both) —
read as two related but distinct views on one page, not one interleaved feed, because merging
them would mean either inventing a shared filter vocabulary that doesn't fit either table
well, or a UI where a "date range" filter silently changes what *kind* of row you're looking
at. **Rejected: one merged, chronologically-interleaved list of jobs and events.** Would read
more like a single "activity log," but `job` rows and `event` rows have materially different
shapes (bytes/attempt/exit-code vs. level/kind/message) and DESIGN.md's own phrasing treats
them as the two things this page must surface, not a single reconciled type.

**6. Delete-audit legibility (DESIGN.md §7.3: "what was deleted, from which queue, under
which mode, and what gated it… including deletes that were withheld, with the failing
precondition") is satisfied by resolving `queue_id`/`queue_name`/`rel_path` via a join and
rendering `event.message` verbatim, not by parsing structured fields out of it.**
`core/postprocess.py`'s `_maybe_delete_remote` (phase 5, unchanged by this phase) already
writes messages like `"queue 3 ('e2e-move') mode=move: deleted verified remote copy
/data/pickup/…"` and `"…mode=move: delete withheld -- verification result was CORRUPT, not
VERIFIED"` — every fact the prompt asks for (queue, mode, gating condition) is already in that
string. Extracting it into separate typed columns would mean either a new migration adding
`mode`/`gating_reason` columns to `event` (schema churn phase 5 didn't ask for and this phase
wasn't asked to do) or fragile string-parsing on the frontend that breaks the moment a message
format changes. Chose to resolve only the *relational* context this phase's own tables can
provide for free (which item/queue an event belongs to, via `event.item_id` → `item.queue_id`
→ `path_queue.name`) and otherwise trust the message text phase 5 already wrote carefully for
exactly this audience. `HistoryEventsSection.tsx` gives delete-kind events (`remote_delete`,
`remote_delete_withheld`, `remote_delete_failed`) a distinct amber background and a "Deletes
only" quick filter, but does not attempt to reparse the message. **Rejected: add `event.mode`
and `event.gating_reason` columns via a new migration, populated by `core/postprocess.py`.**
More queryable/filterable in principle, but it's a phase-5 schema change being made two phases
late, for a UI need phase 5's existing message strings already meet — see docs/decisions.md's
own repeated pattern of preferring the smallest change that satisfies the stated requirement.

**7. `event.item_id`/`event.queue_id` resolution uses `LEFT JOIN`, not `JOIN`.**
`event.item_id` is `ON DELETE SET NULL` (migration 001) specifically so an audit row outlives
the item it describes — an inner join would silently drop exactly the events most likely to
matter later (an old delete audit for an item whose queue was since removed).
`tests/test_history_api.py::test_events_survive_their_item_being_deleted` pins this: the event
still surfaces, with `queue_id`/`queue_name`/`rel_path` as `None` rather than the row vanishing.

**8. Date-range filters (`since`/`until`) are UTC calendar days via `<input type="date">`,
not local-timezone-aware.** Every stored timestamp in this project is UTC
(`STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')`, per `db.py`'s own module docstring), and nothing
elsewhere in the app does timezone conversion for the user's locale — `FilesPage.tsx` and
`TransfersPage.tsx` already render raw `Date` objects via `toLocaleString()`/
`toLocaleTimeString()` without any server-side timezone awareness either. A `since` date is
sent as `<date>T00:00:00.000000Z` and `until` as `<date>T23:59:59.999999Z`, compared
lexicographically against `COALESCE(finished_at, queued_at)` (jobs) or `ts` (events). For a
user well away from UTC this means "yesterday" in the picker can include a few hours of what
they'd call "today" or vice versa. **Not fixed here**, because fixing it properly needs a
project-wide decision about timezone display (a Settings-level "display timezone," most
likely) that touches every existing timestamp render, not just this phase's two new filters —
out of scope for a phase whose brief is "build the History page," and flagged here rather than
silently worked around with a per-page hack that the rest of the app doesn't share.

**9. `HistoryJobOut.state` is restricted to `succeeded`/`failed`/`cancelled` at the API
layer** (`state` query param rejected with 422 for anything else, e.g. `queued`/`running`) —
**rather than silently ignoring an out-of-domain filter value or accepting it and returning
zero rows.** This endpoint's entire domain is terminal jobs (DESIGN.md §9.2: "every
completed, failed, and cancelled transfer" — the Transfers page, `api/jobs.py`, owns
`queued`/`running`); a caller asking to filter History by `state=running` is asking a
question this endpoint structurally cannot answer, and returning an empty list would read as
"no running jobs" rather than "wrong endpoint." A clear 422 is cheaper to debug than a
plausible-looking empty result. `tests/test_history_smoke.py::
test_history_jobs_rejects_non_terminal_state_filter` and the equivalent in
`test_history_api.py` pin this.

**10. No live/WebSocket updates on the History page — filters trigger a fresh fetch, plus a
manual Refresh button; no polling interval.** Every other list view in this app is either
WS-driven (Files) or polls every 2s (Transfers, `useJobs.ts`). History is a retrospective,
filtered/paginated *query* over data that, by definition, stopped changing the moment a job or
event became terminal — polling a filtered, paginated query on a timer risks silently
resetting a user's scroll position or "load more" progress out from under them the moment a
new terminal job lands, which is a worse experience than a page that updates when asked.
**Rejected: reuse the 2-second poll pattern from `useJobs.ts`.** Would keep the page
"live" in the same sense Transfers is, but Transfers' poll always re-renders the *same*
bounded set (active jobs); History's poll would have to somehow preserve filter state, page
offset, and any expanded failed-row output across every refetch, or accept the scroll-reset
cost every two seconds — not what "browse what happened" wants.

**Verified, not just asserted:** `tests/test_history_api.py` (22 tests: terminal-state
filtering, the `succeeded`-jobs-visible-here-not-on-Transfers split, `output_tail` absent from
the list payload but fetchable via the on-demand endpoint, every filter dimension — queue,
state, error class, date range, event kind, event level — the row cap clamping an
over-large `limit` rather than honoring it, offset pagination ordering newest-first, the
delete-audit message/queue/item resolution, and events surviving their item's deletion).
`tests/test_history_smoke.py` (5 tests: the routes are wired into the real app, return the
documented empty shape, and the two HTTP-level edge cases — 404 on an unknown job's output,
422 on a non-terminal state filter, and the limit clamp visible through the real endpoint, not
just the function call). `tests/test_history_e2e.py` (2 tests against the real fake seedbox,
modeled on `tests/test_queue.py`'s own fixtures): a real 512-byte transfer lands in
`/api/history/jobs` with `bytes_total`/`bytes_done` both `512` and `state: "succeeded"`; a
forced bad-password failure lands with `error_class: "AUTH_FAILED"` and a real, non-empty
`output_tail` ("`pget: /data/pickup/loose-notes2.txt: Login failed: Login incorrect`",
confirmed by direct observation, not just asserted `len() > 0`, per the phase report).
`uv run pytest`: 268 passed, 0 skipped, 0 failed with the fake seedbox up; 258 passed, 10
skipped without it. Both lint gates clean (`ruff check` and `ruff format --check`, `--config
ruff.toml`, repo-wide — `format --check` caught one file `check` alone had missed, exactly the
failure mode the prompt warned about). `npm run build` and `npm run lint` (oxlint) both clean.
`docker compose config --quiet` clean on all three compose files. Fake-seedbox containers
torn down and confirmed removed via `docker ps -a` after the run.

**Not verified — stated plainly, per the prompt's own instruction:** no browser is available
in this environment. The History page's rendering, the virtualized scroll behavior, the
group-header flattening, the expand/collapse of a failed job's output block, and every filter
control's actual on-screen behavior were **never exercised in an actual browser** — only
confirmed to type-check, build, and lint cleanly. `npm run build`/`npm run lint` prove the
TypeScript is sound and importable; they do not prove the page renders correctly, scrolls
correctly, or that the virtualizer's `measureElement` dynamic-height approach behaves as
intended with real DOM layout. This should be click-tested before being relied on.

---

## 2026-08-11/12 — Phase 5: post-processing and `move` mode — this phase deletes data on a
## machine the user doesn't own; every decision made unattended, recorded for review

**Overnight run, no live confirmation possible, and the highest-consequence phase so far.**
Built `core/verify.py` (sidecar + hash-on-disk verification), `core/extract.py` (7zz-only
extraction), `core/postprocess.py` (the pipeline: verify → move-mode delete gate → extract →
staging move, plus the cross-device-safe `move_tree`), `core/audit.py` (the `event`-table
writer), and `RemoteConnectionPool.delete_path` in `core/remote.py` (the asyncssh delete
mechanism, DESIGN.md §7.4). `move` is now in `api/settings.py`'s `IMPLEMENTED_SYNC_MODES`;
`sync` still isn't. Verified end to end against the real fake seedbox — a `move` queue
transferred a freshly-uploaded file, verified it (hash-on-disk fallback), deleted the remote
copy over asyncssh, and a **second, independent** `RemoteConnectionPool.scan()` confirmed it
gone — not just that `item.remote_deleted_at` got set. Full detail in the phase 5 report;
every non-obvious call is recorded here.

**0. THE USER'S LIVE QUEUE ROW IS NOW LIVE. Left alone on purpose — this is not a bug.** Their
one queue has `sync_mode` stored as `'move'` in the database from before phase 4's guard
existed (see the "mount sentinel" entry below and the 2026-08-11 overnight-plan note in
`prompts/startnewsession.md`). Until this phase, that setting was inert — the API rejected
`move` on every write, and nothing ever read `sync_mode` to decide whether to delete anything.
As of this commit, `move` is implemented and the row was never touched, so **the very next
time that queue completes a download and it verifies, this code will delete the remote copy**.
This migration adds no data migration touching `path_queue` rows, and no code path in this
phase resets or overrides `sync_mode` for an existing row — only `auto_verify` gets
server-side-forced to `1` for a `move` queue, which for their row means verification will
actually run (previously `auto_verify` was presumably `0`, since it never mattered). **Rejected
alternative: silently reset their row's `sync_mode` back to `'copy'`, "for safety," until they
confirm.** Rejected because it isn't ours to decide — DESIGN.md §7.1 exists specifically to
explain why deletion is safe in the intended deployment shape (hardlink pickup directory), and
whichever way they set it up, quietly overwriting a stored setting is exactly the kind of
"helpfully" unrequested action the phase 5 prompt explicitly forbids twice. **Flagged here,
in `prompts/startnewsession.md`, and at the top of the phase report** so it cannot be missed.

**1. `local_path` stays exactly what phases 1–4 already built (the transfer target and the
reconciler's scan root); `staging_path`, when set, is the post-processing Move step's
*destination*, not the download target.** DESIGN.md §6 says "Move — staging → final
destination... the 'download to NVMe, settle on the array' workflow," and §3.1 lists a
nullable `staging_path` alongside the always-set `local_path`, but never says which field is
which role, and the two readings are genuinely opposite (staging_path as where lftp writes
first vs. staging_path as where a finished item ends up). Chose the reading that requires
**zero changes** to the already-verified transfer engine, scanner, or reconciler: `local_path`
is unconditionally where lftp writes and what `core/reconcile.py`/`core/engine.py` compare
against remote (unchanged); the Move step, triggered only after an item is `DOWNLOADED` (i.e.
already fully present at `local_path`), relocates `<local_path>/<rel_path>` to
`<staging_path>/<rel_path>`. **Rejected: make transfers target `staging_path` when set,
`local_path` the final tree.** That reading matches the *name* `staging_path` more naturally,
but it means the reconciler would have to scan a different root during a transfer than after
one completes — reaching back into phase 2/3's scan/reconcile/progress-sampling code for a
phase whose brief is post-processing, and risking exactly the kind of subtle regression an
unattended overnight run should avoid. The frontend's queue form now labels the field "Final
destination" rather than "Staging path" to match actual behavior, without renaming the
underlying column (a rename is a heavier migration for zero functional gain).

**2. After a successful Move-to-final, `core/postprocess.py` does not set any new
`item.state`.** DESIGN.md §3.2's `REMOVED_BOTH` is explicitly wrong for this (its own
definition: "absent locally, remote deleted by us" — a `move`-mode item's *local* copy is
exactly what Move relocates, it never becomes absent from lftpweb's tracked tree, just from
`local_path`). No other existing state fits either. Rather than adding a new state (a CHECK
constraint change means rebuilding the `item` table in a migration — real risk for a phase
already deleting data), the smallest reasonable call: leave `item.state` as whatever the
verify/extract steps last set it to (`VERIFIED` or `EXTRACTED`), and let the next scan
discover `local_path` now empty for that `rel_path`. Since the item was `DOWNLOADED` before,
phase 4's own `core/mount_sentinel.py.resolve_absence()` grace-period machinery already
produces exactly the right outcome — `REMOVED_LOCAL` once the absence persists — because a
post-processing-driven relocation and an `*arr` import moving the file out are, correctly,
indistinguishable to the reconciler. **Rejected: a new `RELOCATED`/`ARCHIVED` state.** More
precise, but it needs a schema migration this phase doesn't otherwise require, a new branch in
every place that already handles the state vocabulary, and buys nothing `REMOVED_LOCAL`
doesn't already give for free. The prompt's own text anticipated this ambiguity ("pick
whatever is consistent and record the call") — this is that record.

**3. Verification for a `move`-mode queue's item always runs, overriding both the global
`verify_enabled` switch and the queue's own `auto_verify` value, rather than being subject to
the same "both toggles must be on" rule every other step follows.** DESIGN.md §6 is explicit
that `auto_verify` is "forced on and cannot be turned off in the UI" for `move`/`sync` — but
doesn't say what happens if the *global* postprocessing switch for verification is off. Chose
to force verification to run regardless of either toggle for a `move` queue specifically,
because muting it via an unrelated site-wide default would silently turn `move` into
"downloads, never deletes, never explains why" — the worst possible failure mode for a feature
whose entire safety story rests on verification being the gate. **Rejected: AND-gate
verification like extract/move (global × per-queue), and let a `move` queue with either
switch off simply never delete anything.** Technically safe (no delete is always the safe
outcome, per the prompt's own bias), but it fails silently from an operator's point of view —
a `move` queue that never deletes because a global switch elsewhere is off looks identical to
one working correctly with nothing yet eligible, and nothing in the UI would explain the gap
without reading the event log closely. Forcing verification on is stricter, not laxer: it
means *more* checking happens before a delete, never less.

**4. `extract` and (the staging) `move` steps are AND-gated: a step only runs when *both* the
site-wide `PostprocessSettings` flag and the queue's own `auto_extract`/`auto_move` column are
true.** DESIGN.md §6 says "toggleable globally and per path queue" without specifying how two
independent toggles combine. AND (both must opt in) was chosen over "queue overrides global"
or "either one enables it" because it's the only combination where flipping *either* switch to
off reliably turns the step off everywhere — the safest reading for a pipeline this phase
explicitly wants defaulting to inert. A fresh install (`postprocess_settings` absent from
`setting`, migration 003's `auto_extract`/`auto_move` at their `DEFAULT 0`) therefore runs
zero post-processing even before anyone visits either Settings page, satisfying "every
post-processing step defaults off" at both layers simultaneously, not just one.

**5. Post-processing's trigger is `core/queue.py._reap_one`'s job-success path only — not also
hooked into `core/engine.py._persist`'s scan-driven state computation.** DESIGN.md §6 says
"triggered on transition to `DOWNLOADED`," which in principle includes an item that becomes
`DOWNLOADED` purely because a rescan found matching bytes already on disk with no lftpweb job
ever having run (e.g. files placed by hand). Given every *realistic* completion in this
deployment goes through a job this app itself spawned, and given the overriding instruction to
be conservative when this phase touches deletion, the smaller, better-tested surface won by
only firing from the one code path that is exercised by every existing transfer test rather
than also reaching into phase 2/3's reconcile/persist logic (already covered by its own
extensive test suite) to add a second trigger site. **Consequence, stated plainly: a file that
lands at `local_path` some other way (an operator's own `cp`, a restore) will never be
verified, extracted, or have its remote counterpart deleted by `move` mode until *something*
else re-touches that item** (e.g. a manual re-queue). Recorded as a known, deliberate scope
reduction rather than a bug — flagged in the phase report — since the alternative (reaching
into `_persist`) is unattended-session-risk for a corner case the prompt's own verification
list never asked for.

**6. Post-processing only ever triggers for a *top-level* item (no `/` in `rel_path`), the
same eligibility shape `core/autoqueue.py` already uses.** `core/queue.py` only ever spawns a
job against a top-level item (a whole release via `mirror`, or a loose top-level file via
`pget` — DESIGN.md §4.7); the phase 2 decision to persist one `item` row per node (not just
top-level ones, see that phase's own decisions.md entry) means a directory's *nested* file/
subdirectory rows also transition to `DOWNLOADED` on the next scan after their parent's job
succeeds, but verifying/extracting/deleting once per nested file inside an already-processed
release would be redundant work and — for `move` — would attempt N remote deletes of paths
already covered by the one delete issued for the release as a whole. The guard is defensive
(nothing currently queues a nested item directly) but cheap and correctly scoped either way.

**7. Deletion goes out as a shell `rm -rf --` over the same pooled asyncssh connection's
`conn.run()`, not asyncssh's SFTP protocol layer.** `core/remote.py`'s existing scan paths
(`_run_primary`, `_run_fallback`) already assume a POSIX shell is reachable this way; SFTP has
no single "remove a possibly-non-empty directory tree" primitive, so a protocol-level
implementation would mean hand-rolling recursive listing + delete in this codebase, more
surface for a delete-path bug than reusing a mechanism already proven for scanning.
`--` guards against a path that happens to start with `-`; a non-empty, non-root path check
(`ValueError` on `""`, `"/"`, `"."`, `".."`) is defense in depth on top of the caller always
constructing `<queue.remote_path>/<item.rel_path>` with a verified non-empty `rel_path` —
never the primary safeguard, since the primary safeguard is verification gating whether
`delete_path` is called at all.

**8. "Hash-on-disk" verification (no `.sfv`/`.md5` sidecar, fallback enabled) means "every
file under the item reads fully end to end with no I/O error" — not a hash compared against
anything, since there is nothing to compare against.** DESIGN.md §6 names "hash-on-disk" as
the fallback without defining what it hashes or checks when there's no reference value. Chose
the weakest defensible reading — full-file readability, explicitly labeled in
`VerifyResult.detail` as confirming "readability, not content correctness" — because a
completed transfer already implies `local_size >= remote_size` (reconciler rule 2) and a clean
lftp exit (`cmd:fail-exit true`, §4.3), so the only additional failure mode this fallback can
actually catch beyond what already happened is a file that looks complete by size/mtime but
errors on a full read (sparse-hole lies, disk corruption after the fact). **Rejected: compute
and merely *store* a hash with nothing to compare it to, calling that "verified."** That would
satisfy the letter of "hash-on-disk" while verifying nothing at all — a `move` queue would
delete on the strength of a number nobody ever checks. The chosen reading is honest about its
own weakness (`detail` says so explicitly) and is off by default (`verify_hash_on_disk`
defaults `False`), per DESIGN.md §6's own words: "no usable verification evidence... must say
so loudly," not be quietly promoted to sounding like a checksum match.

**9. `core/extract.py`'s 7zz binary name is an overridable parameter (`binary=`) and env var
(`LFTPWEB_7Z_BIN`), defaulting to `"7zz"` (the runtime image's Alpine `7zip` package binary),
exactly `core/lftp.py.spawn`'s `lftp_bin` pattern.** Needed because this session's own dev
host runs Ubuntu, whose `7zip` package (installed to verify extraction against a real binary
rather than mocking one) names the identical upstream 7-Zip binary `7z`, not `7zz`. Tests pass
`binary="7z"` (or set the env var) so "verify before reporting" could mean actually invoking a
real 7-Zip process, not asserting against a mock — the same reasoning DESIGN.md §14 gives for
the fake-seedbox-over-mocks approach generally.

**10. A password-less 7zz extraction attempt omits `-p` entirely rather than passing an empty
password, and every subprocess call sets `stdin=DEVNULL`.** 7-Zip's CLI has no "definitely
don't prompt" flag; omitting `-p` is the normal case (target archive isn't encrypted), and
`stdin=DEVNULL` is what actually prevents a hang if an archive turns out to be encrypted
anyway — 7z fails fast on EOF instead of blocking forever waiting for a password that will
never come. Compound-tar extraction (`.tar.gz`/`.tgz`, `.tar.bz2`/`.tbz2`, `.tar.xz`/`.txz`)
needs two 7zz invocations, because 7-Zip only strips one layer of a chained format per call;
the intermediate `.tar` lives in a throwaway subdirectory removed either way.

**11. The extraction target directory (`extract_target_dir`) is a site-wide setting, not a
per-queue column.** DESIGN.md §6 says "Target: in place, or a configured directory" without
saying at what scope the configuration lives. Chose site-level, matching bandwidth/concurrency
(§4.5, "a queue governs what and where, never how fast/how" — extended here to "or how
processed") rather than adding another `path_queue` column and migration for a knob nothing in
the prompt asked to be per-queue. Per-item placement underneath it (`extract_target_dir /
item.rel_path`) still keeps different releases from colliding with each other.

**12. `PostprocessPipeline.trigger()` schedules one `asyncio.create_task` per item and gates
concurrent execution with a single `asyncio.Semaphore` sized from `PostprocessSettings.
concurrency` (default 1), rather than a bounded worker-pool queue.** DESIGN.md §6: "executed
in a thread pool, one item at a time by default (configurable)." A semaphore around
per-item tasks gives the same effective concurrency bound with far less machinery than a
persistent pool + queue, and matches this codebase's existing style (`TransferQueue`'s
admission control is itself a bespoke scheduler, not a library pool). `wait_idle()` is exposed
for tests and clean shutdown (`main.py`'s lifespan now awaits it between `queue.stop()` and
`engine.stop()`, so an in-flight verify/extract/move isn't cut off mid-write against a
database connection about to close).

**13. Switching a queue's `sync_mode` to `move` in the UI requires a fresh, explicit checkbox
confirmation ("I confirm ... is a hardlink pickup directory, not live seeding data") enforced
**client-side only**, not as an API request field.** DESIGN.md §7.1's misconfiguration warning
must appear "in the Settings UI next to the mode selector" and switching to `move` "requires
explicit confirmation" — read as a UX requirement (a human using the form must acknowledge it
before the button does anything), not a new wire-format contract. **Rejected: an API-level
`confirm_move: bool` field on `PathQueueIn`/`PUT .../queues/{id}`.** DESIGN.md's own schema
(§3.1) has no such field, and this project already treats a direct API call (curl, a script)
as an equally valid client elsewhere with no equivalent extra-confirmation gate (e.g. deleting
a queue outright needs no confirmation token either) — adding one only for this one field
would be an inconsistent, unrequested API surface change for a phase whose job is the pipeline
and the delete mechanism, not new API ceremony. `auto_verify`'s forced-on behavior *is*
enforced server-side (decision above), because that one is a safety property the server must
hold regardless of which client asked; the confirmation checkbox is a "did a human read the
warning" gate, which only a human-facing form can meaningfully provide anyway.

**14. `Settings → Post-processing` (`PostProcessingTab.tsx`) was filled in with a working
global-settings form this phase, rather than staying the placeholder it was through phases
3a–4** (`frontend/src/pages/settings/TransferTab.tsx` is still a placeholder despite phase 3a
shipping a complete, tested `TransferSettings` API — a precedent for leaving site-level
Settings UI for a later "polish" phase). Chose to build it anyway because the alternative —
`PostprocessSettings` reachable only via `curl`/direct API calls — leaves no way for the user
to see or change "every post-processing step defaults off" without reading source, for a
feature phase whose entire framing is "the user must be able to tell what this is doing before
it deletes something." `TransferTab` staying a placeholder is unaffected and not revisited
here; it's a separate, lower-stakes gap for whichever phase (likely 9, Polish) picks it up.

**15. `auto_verify`/`auto_extract` — DB columns that have existed since migration 001 but had
no API field, no request/response model field, and no UI control through phases 1–4 — are
wired up for the first time this phase, alongside the new `auto_move` (migration 003).** Not
a phase 5 bug fix so much as this phase being the first one that needed those columns to mean
anything; recorded because a future session grepping for "when was `auto_verify` first
readable from the API" would otherwise have to dig through four phases' git history to learn
it was always in the schema and simply unused until now.

**Verified, not just asserted:** `tests/test_postprocess.py` (24 unit tests: `.sfv`/`.md5`/
hash-on-disk verification including a corrupt-checksum case; zip and native `.7z` extraction
via a real local 7-Zip binary, a password-protected archive, a deliberately corrupt archive
that fails without raising, multi-part-rar volume name filtering; `move_tree`'s same-device
fast path, a genuine EXDEV fallback that actually relocates a nested directory tree, and —
the phase's own required test — an EXDEV fallback whose *copy* fails partway, proving no
partial file is ever left at the destination and the source is untouched; the move-mode delete
gate withholding on `SKIPPED` and on a real `CORRUPT` sidecar mismatch, and only ever calling
the (stubbed) remote pool once verification actually returns `VERIFIED`; every default-off
assertion). `tests/test_postprocess_e2e.py` (1 real end-to-end test against the fake seedbox:
uploads a fresh file to a dedicated, uniquely-named remote subdirectory — deliberately *not*
`docker/test-seedbox/seed_tree.sh`'s shared fixtures, which other tests in this suite still
depend on and a `move` queue would otherwise delete out from under them — transfers it through
the real `TransferQueue`/`PostprocessPipeline` wiring exactly as `main.py` constructs it,
confirms `VERIFIED` + a `remote_delete` event, and **rescans the remote with a second,
independent `RemoteConnectionPool` to confirm the file is actually gone**, not merely that
`remote_deleted_at` got set). `uv run pytest`: 244 passed, 0 skipped (fake seedbox up), 0
failed. Both lint gates clean (`ruff check` and `ruff format --check`, `--config ruff.toml`,
repo-wide). `npm run build` and `npm run lint` clean. `docker compose config --quiet` clean on
all three compose files. Fake-seedbox containers torn down and confirmed removed via
`docker ps -a` after the run; the shared fixture tree (`seed_tree.sh`'s files) was never
modified or deleted by this phase's own tests.

---

## 2026-08-11 — Phase 4: auto-queue and patterns — every decision made unattended, recorded
## for the user to review in the morning

**Overnight run, no live confirmation possible.** Built `core/patterns.py` (the one
evaluator, DESIGN.md §4.7/§12), wired its `counts_predicate` into `core/reconcile.py`
(`EXCLUDED` state, DESIGN.md §3.2 rule 8), `core/autoqueue.py` (pattern-matching intake, the
mount gate, suppression), and `core/mount_sentinel.py` (the `.lftpweb-mount-ok` gate plus the
`REMOVED_LOCAL` grace period this phase requires per the 2026-08-11 entry below). Verified
end to end against the real fake seedbox (`tests/test_autoqueue_e2e.py`), not just unit
tests. Full detail in the phase 4 report; every non-obvious call is recorded here.

**1. `REMOVED_LOCAL` detection + the grace period were built now, not left for a later
phase.** DESIGN.md §3.2 rules 3/5/6/7 were explicitly *not* implemented by phases 2-3 (their
own docstrings say so) — a previously-`DOWNLOADED` item whose local copy disappears simply
reread as a fresh `REMOTE_ONLY` on the next scan. That's exactly the failure the phase 4
prompt calls out by name: without it, auto-queue would re-download everything a user (or an
*arr import) deliberately deleted, the moment the mount comes back healthy. **Rejected
alternative:** rely solely on the blanket per-queue mount gate and skip `REMOVED_LOCAL`
entirely. Rejected because the mount gate only protects against a *dropped* mount — it does
nothing for the ordinary, expected case (§7.2's move-on-import workflow) where the mount is
perfectly healthy and a file is genuinely, intentionally gone. `core/mount_sentinel.
resolve_absence()` is a pure decision function (`(prev_state, prev_first_missing_at,
structural_state, mount_ok, now) -> override|None`) so `core/engine.py._persist` is the only
I/O around it — unit-tested without a filesystem or database
(`tests/test_mount_sentinel.py`).

**2. The mount gate blocks *all* auto-queue action for a queue, not just `REMOVED_LOCAL`
transitions.** `AutoQueue.on_scan()` checks `mount_sentinel.check()` first and returns
immediately if it fails — before even looking at eligible items. This is stricter than
strictly necessary for the `REMOVED_LOCAL` failure direction (which `resolve_absence()`
already handles on its own), but it's the only thing that also protects the *other* failure
direction the prompt names: a **brand-new** queue whose local root never mounted would have
every item read `REMOTE_ONLY` from the very first scan (nothing to compare against history),
and only a blanket gate — not a per-item history check — stops auto-queue from happily
queueing transfers into a directory that isn't really there.

**3. `file_exclude` matches a file's own basename, at any depth, not the full relative
path.** DESIGN.md §4.7 doesn't specify whether a pattern like `*.nfo` should be able to
target a specific nested path (e.g. `Subs/*.nfo` vs. any `*.nfo` anywhere). Chose basename-
only matching — the same convention lftp's own `--exclude-glob` uses by default, and what
every DESIGN.md example (`*.nfo`, `*SAMPLE*`) already assumes. **Rejected:** matching the
full `rel_path` when a pattern contains `/`, which would let a queue exclude, say, only
`Subs/*.srt`. More expressive, but nothing in DESIGN.md or the phase 4 prompt asks for it,
and it complicates `exclude_globs()`'s job of staying literally what lftp receives.

**4. A plain-substring `file_exclude` pattern (no `*?[`) is wrapped `*pattern*` before being
handed to lftp's `--exclude-glob`.** lftp has no substring-match mode of its own — passing
`sample` verbatim would only match a file named exactly `sample`. Wrapping it in `core/
patterns.py.CompiledPatterns.exclude_globs()` keeps the "no wildcards needed" convenience
DESIGN.md §4.7 promises for `select`/`skip` consistent for `file_exclude` too, without lftp
ever seeing anything but a real glob.

**5. A file matched by `file_exclude` is marked `EXCLUDED` in the reconciler unconditionally
— even if a local copy already exists** (e.g. downloaded before the pattern was added).
**Rejected:** only mark `EXCLUDED` when there's no local copy, otherwise show the file's
actual completeness state. Chose the unconditional rule because patterns are meant to
reflect current intent — a file the operator no longer wants counted shouldn't keep
contributing to completeness just because it happened to be fetched previously — and because
the simpler rule is one branch instead of two, with no test in DESIGN.md's own list asking
for the other behavior.

**6. The pattern-preview endpoint samples a directory that *matches* the draft patterns,
preferring it over the first directory alphabetically**, falling back to any directory if
nothing matched. DESIGN.md §9.2 only says "within a sampled item" — doesn't say which one.
Found while writing the endpoint's own test: sampling the first alphabetical item (which
`test_pattern_preview_...` initially assumed) can easily be the *skipped* one, showing the
user a file-exclude preview for an item that was never going to be selected in the first
place — the least useful item to sample.

**7. Pattern CRUD applies immediately** (`POST/PUT/DELETE /api/settings/patterns`) — there is
no per-queue "save patterns" staging step in the UI. **Rejected:** batch pattern edits behind
a save button, so a half-composed pattern set is never live. Rejected for phase 4's scope and
because it keeps auto-queue's "retroactive" behavior (§4.7) simple: every persisted pattern
is always the live, effective one, with no separate "has this been saved yet" state to track.
The live preview endpoint (`POST .../pattern-preview`) covers the "see before you commit"
need for a *single new* pattern being composed, which is the actual editing motion the UI
supports (add one, see it take effect, add another).

**8. `main.py`'s lifespan constructs `TransferQueue` before `Engine`, reversing phases 1-3's
order.** `AutoQueue` needs `TransferQueue.enqueue_item` (the same "manual queue always wins"
path a user action takes), and `Engine` is what invokes `AutoQueue.on_scan()` at the end of
every scan pass — so `TransferQueue` has to exist first. Purely a wiring reorder; neither
component's own behavior changed.

**9. The `REMOVED_LOCAL` grace period is a hard-coded ~10 minute constant
(`core/mount_sentinel.DEFAULT_GRACE_S`), not a Settings UI knob this phase.** DESIGN.md §7.3
names ~10 minutes as its own default without requiring it be configurable. Deferred exposing
it to keep this phase's UI surface smaller; `resolve_absence()` already takes `grace_s` as a
parameter, so wiring a Settings field to it later is additive, not a signature change.

**10. Migration 002 (`auto_queue_patterns_only`) is a bare `ALTER TABLE ... ADD COLUMN ...
CHECK (...) DEFAULT 0`**, the same shape migration 001 already used for `auto_queue_enabled`.
Verified against real SQLite via the full test suite (`ALTER TABLE ADD COLUMN` with an inline
`CHECK` needs SQLite ≥ 3.25-ish semantics; not assumed, proven by `tests/test_db.py` and every
queue-CRUD test passing against a freshly migrated database). `DEFAULT 0` is load-bearing:
every existing queue picks up the column already off, so this migration cannot change
behavior for any queue that already exists — the phase's own non-negotiable.

**Bug caught before it shipped, not a design decision:** `core/patterns.py.CompiledPatterns.
compile()` originally iterated its `patterns` argument three times (once per kind). Fine for
a `list`, silently wrong for a one-shot generator — which is exactly what the pattern-preview
endpoint passes. Fixed by materializing the iterable first, before any test exercised the
generator path; recorded because it's the kind of bug this phase's "one evaluator, two
consumers" design is specifically supposed to prevent from happening *twice*, and it very
nearly happened once, quietly, inside the one module meant to prevent it.

**Also touched, not a phase 4 decision but necessary fallout:** `tests/test_db.py::
test_migrate_is_idempotent` hardcoded `schema_version` row count to `1`; adding migration 002
broke it on contact. Fixed to derive the expected count from `MIGRATIONS_DIR` instead of a
literal, so it doesn't break again at the next migration either.

**What this phase deliberately does not do:** no UI beyond `StateChip`'s new
`REMOVED_LOCAL`/`REMOVED_BOTH` colors and the Settings → Queues pattern editor — no dedicated
"why did this get removed" panel (that's History, phase 6); no post-processing (verify/
extract/move — phase 5); `mount_ok` is exposed on `GET /api/files` and a dedicated
`/api/settings/queues/{id}/autoqueue-status` endpoint, deliberately **not** threaded through
the WebSocket delta/snapshot messages, to avoid touching `core/engine.py`'s WS schema and
every frontend WS type for a field that matters to Settings, not to the live Files view.

---

## 2026-08-11 — The mount sentinel is needed before phase 4 (auto-queue), not before `sync`

**Found while advising on a real NFS deployment, not by a build.** `DESIGN.md` §7.3 specifies
the `.lftpweb-mount-ok` sentinel and the grace period as rails on **delete propagation**, and
§7 defers both along with `sync` mode — which is unscheduled. That placement is wrong by one
phase.

The sentinel's actual job is broader than deleting: it answers *"is this empty directory really
empty, or is the mount gone?"* Auto-queue (phase 4) is the first feature that takes **action**
on local absence, and a network mount that drops makes every tracked item look locally absent
in the same scan.

Both failure directions are bad, and which one you get depends only on how far the lifecycle
has progressed:

- Items read `REMOTE_ONLY` (today's behaviour — `scan_local()` returns `{}` when the root isn't
  a directory) ⇒ auto-queue **re-downloads the entire library** off one blip.
- Items read `REMOVED_LOCAL` (§3.2 rule 3, once phase 3/4 persist lifecycle history) ⇒
  auto-queue **permanently skips** them, since that state means "deliberately removed."

**Today this is harmless** — nothing consumes those states, so a dropout is cosmetic and
self-heals when the mount returns. It stops being harmless the moment auto-queue exists.

**Decision:** §13 phase 4 now requires the sentinel and grace period as part of that phase,
independent of whether `sync` is ever built. Recorded here because the rails are written up in
§7.3 under a deferred feature, which is exactly how a required safeguard gets skipped by someone
reading the phase list top-down.

---

## 2026-08-11 — Note: this session ran concurrently with another session bootstrapping the
## GitHub repo — several unrelated files were dirty on disk throughout phase 3b

> **Correction, added by the orchestrating session:** the concurrency was **deliberate, not
> accidental.** The user asked for the repo-bootstrap work to run in parallel with phase 3b, and
> the orchestrator ran both agents at once with an explicit file split — bootstrap owned
> `.github/`, root docs, and `.claude/commands/`; 3b owned `frontend/`, `backend/`, `tests/`.
> The genuine mistake was one-sided disclosure: **only the bootstrap agent was told another
> agent was running.** Phase 3b discovered it from `git status` and reasonably concluded it was
> uncoordinated. The lesson is not "avoid concurrency" but "tell *both* sides" — and the
> skipped working-tree check below is why 3b found out late rather than up front. The merged
> `prompts/startnewsession.md` was reviewed and is correct; both sessions' edits survive.

**Not a phase 3b decision — a working-tree hazard worth recording so the merge is deliberate,
not accidental.** Partway through building phase 3b, `git status --porcelain` turned up
uncommitted changes to `CLAUDE.md`, `docker-compose.yml`, `standards.md`, and
`prompts/startnewsession.md`, plus untracked `README.md`, `LICENSE`, `NOTICE`,
`CHANGELOG.md`, `ruff.toml`, `docs/repo-setup.md`, `.github/workflows/`, `.claude/commands/`,
and `prompts/done/2026-08-11-adopt-checkin-and-release-standards.md` — none of which this
session touched or was asked to touch. Timestamps put every one of them inside this session's
own working window, and their content (a GitHub repo URL, an AGPL-3.0 `LICENSE`, CI workflows,
`code-checkin-and-pr`/`release-prep-and-cut` adoption) is a different, legitimate task: repo
bootstrap. Another session was very likely running against the same working tree at the same
time.

**Why this matters here specifically:** `prompts/startnewsession.md` is a file both sessions
needed to edit — the repo-bootstrap session had already added a "Repo, branches, and what has
NOT been pushed" section and rewritten the Git-rules bullets by the time phase 3b's own update
landed. This session's edit is additive on top of that content (new "Where we are" paragraph,
phase table row, traps) rather than a rewrite, specifically to avoid clobbering it — but it was
never coordinated with the other session, so a second concurrent save from either side after
this one could still conflict. **Flagged for the user to review the merged diff on
`prompts/startnewsession.md` deliberately**, rather than assuming either session's version is
complete on its own. This session made no changes to any of the other-session files listed
above.

**Also: the phase 3b prompt's own "Working tree check" step (`git status --porcelain`, list
uncommitted changes, ask first) was skipped at the start of this session** — it should have
caught this before any code was written, not partway through. Recorded as a process gap, not
just a one-off: a fresh session should run that check *before* reading DESIGN.md, not after.

---

## 2026-08-11 — Phase 3b: the WebSocket delta fix — `queue_snapshot` replaced by row-level
## `queue_delta` / `item_delta`, proven proportional by test, not by inspection

**The problem, stated precisely (docs/decisions.md's own phase 2 entry flagged this as
scoped-down and warned phase 3 shouldn't inherit it by default — it did, until now).** Phase
2's `core/engine.py.scan_queue` published one `queue_snapshot` — the complete node list —
every time a queue finished scanning. Fine at a 30 s cadence over a few KB tree. Phase 3a
added a ~1 Hz `ProgressSampler`; resending an entire queue's tree (which can run to thousands
of file/directory rows on a real seedbox) to every connected browser every second would have
made the WebSocket the bottleneck the whole no-PTY, no-`jobs -v`-parsing design was built to
avoid elsewhere.

**Fix, in two parts:**

1. **`core/engine.py.diff_nodes`** — a pure function, `(old_nodes, new_nodes) ->
   (changed, removed)`, using `ReconciledNode`'s existing frozen-dataclass equality. `scan_queue`
   now diffs against `self.models[q.id]` before overwriting it, and publishes `queue_delta`
   (`changed` + `removed` rel_paths) instead of the full tree. A full snapshot is sent exactly
   once per connection, from `Engine.snapshot()` — unchanged in shape, just no longer also sent
   on every scan.
2. **`core/queue.py._publish_item_state`** — a one-row `item_delta`, published whenever a job
   lifecycle transition changes an item's state outside a scan (queued, spawned, stopped,
   failed, succeeded, requeued). Without this, the Files page only learned about a state change
   on the *next* full engine scan (up to `scan_interval_s`, default 30 s) — far too slow for the
   phase 3b prompt's own acceptance test ("stop it, and see it go STOPPED — all without a page
   refresh"). `_sample_and_publish_progress` also now updates `item.local_size` and batches an
   `item_delta` per queue for the currently-*running* items each tick — bounded by the active
   set, never the tree, the same guarantee the pre-existing job-level `progress` message already
   had.

**Proven with a test, per the prompt's explicit instruction not to rely on inspection.**
`tests/test_ws_deltas.py::test_scan_delta_payload_does_not_scale_with_tree_size` runs the
identical 2-file mutation against a 20-item queue and a 5,000-item queue and asserts the
*delta* payload grows by under 200 bytes while the *full snapshot* (what the old code sent
every scan, and what a naive future change could regress to) grows over 100×. Measured live
against the real fake seedbox during an actual transfer (throttled to 400 KB/s so there was
time to observe several ticks): `progress` messages averaged **152 bytes** (121–156 byte
range, n=11 ticks), `item_delta` messages averaged **188.5 bytes** (182–190 byte range, n=14),
versus a **2,754-byte** full snapshot for the same 18-node fake-seedbox tree on connect — and
that gap widens, not narrows, as the tree grows, per the unit test above.

**Ambiguity resolved along the way, surfaced rather than silently decided:** DESIGN.md §2/§9
never states whether the Files page's per-row live update (state chip, size) belongs on this
same delta stream or is expected to wait for the next scan. Read literally, "one WebSocket
delivering... deltas" doesn't specify granularity below "queue." Resolved toward the smallest
useful unit (one item row) since anything coarser reintroduces exactly the scaling problem this
fix exists to close.

---

## 2026-08-11 — Phase 3b: `core/queue.py.list_jobs()` broadened beyond `queued`/`running` —
## DESIGN.md §9.2 requires the Transfers page to show terminal states it couldn't reach

**Ambiguity found building the Transfers page, resolved with the smallest reasonable call.**
Phase 3a's `list_jobs()` (`GET /api/jobs`) only ever selected `job.state IN ('queued',
'running')` — deliberately, since that's what the *scheduler* needs to reason about. But
DESIGN.md §9.2 states, in the same breath as describing this page, "**Failed rows show the
error class and the captured lftp output tail**" — impossible under the old query, since the
instant a job fails it's excluded from every future `list_jobs()` call, forever. The phase 3b
prompt's own acceptance test has the same shape: "stop it, and see it go `STOPPED`... without a
page refresh" — but a stopped job's state is `cancelled`, also excluded.

**Fix:** `list_jobs()` now also includes a `failed`/`cancelled` job when it is that item's
*most recent* job (`job.id = MAX(job.id) WHERE item_id = ...`). Self-healing by construction: a
manual retry (`POST /api/items/{id}/retry`) inserts a fresh `queued` row for the same item,
already covered by the first clause, which makes the old failed/cancelled row for that item no
longer the most recent and it drops out of the query on its own — no separate "supersede" logic
needed. `succeeded` jobs are deliberately **not** included; a freshly-completed item's row
already reflects `DOWNLOADED` on the Files page via the WS delta fix above, and DESIGN.md
positions History (the `job`/`event` audit trail) as the place a completed transfer's own job
record lives, not the "job queue" page. Covered by `tests/test_transfers_list_jobs.py`.

**Also added: `POST /api/items/{item_id}/stop`.** The Files page (unlike Transfers) only ever
knows an *item*, never the job id currently servicing it — `GET /api/files` deliberately
doesn't expose one (an item can outlive several job attempts). `TransferQueue.stop_item`
resolves to the item's current active job, if any, and applies the same stop semantics as
`stop_job`; returns `False` (not an error) when there's nothing to stop, matching `start_now`'s
existing "no-op rather than pretend" shape. This is the one place the phase 3b prompt's claim
that "the API you need already exists" didn't quite hold — every other action (Queue, Stop from
Transfers, Retry, Move to top, Start now) mapped directly onto phase 3a's job-scoped API.

---

## 2026-08-11 — Phase 3b: the scan-abort bug (named out-of-scope by phase 3, DESIGN.md §5) —
## fixed. One unreadable subtree now produces a warning, not a vanished tree

**The bug, as phase 3 left it recorded:** `core/remote.py`'s primary scan path (`find
<path> -mindepth 1 -printf ...`) treated *any* nonzero exit that wasn't the "-printf
unsupported" signature as a hard failure, discarding the entire queue's tree. GNU `find` exits
1 the moment it can't stat/read one subdirectory anywhere in the tree — even though it already
printed every record it *could* reach to stdout, and kept scanning the rest. One
permission-denied folder on the seedbox meant the whole Files page rendered empty (or reverted
to its last-known state) with zero indication why.

**Fix:** `interpret_primary_scan_result` (a pure function, unit-tested the same way
`parse_find_records` is — no live SSH connection needed) classifies the exit: exit 0 is a clean
success; the "-printf unsupported" signature still signals the busybox/BSD fallback path
unchanged; a nonzero exit **with usable stdout** is a *partial* success — every record `find`
did produce is kept, and a short human-readable warning (`_summarize_find_stderr`) is derived
from stderr's `find: 'PATH': REASON` lines; a nonzero exit with **no** stdout at all (a
genuinely bad path, or the root itself unreadable) still raises exactly as before — there's
nothing to salvage. `RemoteConnectionPool.scan()` now returns `(entries, warning)`;
`Engine.scan_queue` threads the warning through as a new `scan_warnings` map, surfaced on both
`GET /api/files` (`QueueFiles.warning`) and the WebSocket (`queue_delta.warning`,
`snapshot.queues[].warning`) — distinct from `scan_errors`/`error`, which still means the whole
scan failed and the tree shown is stale.

**Verified live against the real fake seedbox**, not just in unit tests:
`docker/test-seedbox/seed_tree.sh` now seeds a `chmod 000 no-permission/secret/hidden.bin`
subtree specifically for this. `tests/test_remote.py`'s live regression
(`test_live_scan_skips_unreadable_subdirectory_instead_of_aborting`, skipped automatically
without the fake seedbox up) confirms the rest of the tree scans normally, `no-permission`
itself is visible (its own record is stat-able via its readable parent) but nothing beneath it
is, and the warning names it. Reproduced again by hand through the running API during this
phase's E2E verification: `GET /api/files` returned all 18 readable nodes plus `"warning": "1
path skipped (could not be read): find: '/data/pickup/no-permission': Permission denied"`,
`"error": null`.

---

## 2026-08-11 — Phase 3b: virtualization — `@tanstack/react-virtual`, deferred from phase 2
## exactly as its own decision entry said it would be

**Decision.** Phase 2 explicitly deferred adding a virtualization dependency "as a side effect
of phase 2" rather than a deliberate choice (see this file's phase 2 entry, "the Files tree is
not yet virtualized"). Phase 3b is where both the Files tree *and* the new item drawer need it
(DESIGN.md §9.2 requires "smooth at 10k+ rows" for Files and calls the drawer "virtualized;
a release can carry hundreds of files"), so the dependency decision is made once, here, for
both.

**Chosen: `@tanstack/react-virtual`.** Small (no runtime deps beyond React), headless (renders
nothing itself — plain `<div>`s styled with Tailwind, matching the rest of the app rather than
importing a component library's own row chrome), and actively maintained. `FileTree.tsx`
flattens the collapsible tree into a visible-rows array respecting collapse state and
virtualizes *that*, so collapsing a directory is just a shorter array, not a structural change
to how virtualization works. `ItemDrawer.tsx` virtualizes its flat per-file list the same way.

**Rejected: `react-window`.** Comparable size and maturity; `@tanstack/react-virtual` was
preferred for API consistency with a `@tanstack/*` family already implicitly the closest match
to what DESIGN.md's own §9 calls for elsewhere (see the next entry) — no strong technical
reason either way.

---

## 2026-08-11 — Phase 3b: DESIGN.md §9's "TanStack Query for REST" was never adopted in
## phases 1–3a, and phase 3b continues that deviation rather than introducing it mid-project

**Deviation found, not created, by this session — surfaced because it was never recorded.**
DESIGN.md §9 states plainly: "TanStack Query for REST; one WebSocket delivering a full model
snapshot on connect and deltas thereafter." The WebSocket half was built faithfully starting in
phase 2. The REST half was not: `frontend/src/api/client.ts` is a hand-rolled `fetch` wrapper,
and data fetching uses a hand-rolled `usePoll` hook (`frontend/src/hooks/usePoll.ts`, dated to
phase 1's `StatsHeader`) — no `@tanstack/react-query` dependency exists anywhere in
`package.json` as of phase 3a's end, and nothing in `docs/decisions.md` recorded the
substitution.

**This phase's call: keep following the convention actually in the codebase.** `useJobs.ts`
(new, phase 3b) is the same shape as `usePoll` — poll on an interval, expose the latest value —
plus a `refresh()` escape hatch `usePoll` doesn't have, needed so an action (queue/stop/retry/
move-to-top/start-now) can force an immediate refetch instead of waiting up to the poll
interval for its own result to appear. Introducing TanStack Query now, mid-project, three
phases after the convention diverged, would touch every existing data-fetching call site for a
library swap unrelated to phase 3b's actual scope (the prompt's own conventions section:
"don't restructure" the existing frontend structure). **Flagged here for a deliberate decision
either way** — either DESIGN.md §9 should be corrected to describe what's actually built, or a
future phase should do the TanStack Query migration as its own scoped piece of work, not as a
side effect of whichever phase happens to touch data-fetching next.

---

## 2026-08-11 — Phase 3: the live-retune experiment (§4.5) is **verified working**

**Tested against a real running transfer, not left as a maybe.** Held `lftp`'s stdin open on a
read-write fd, fed it an initial script ending in `pget ... &` (backgrounding the transfer so
the command loop stays live), then wrote `set net:limit-total-rate <n>` to that same fd while
the job was running.

**Result: it works.** Clean before/after measurement against the fake seedbox, using
`.lftp-pget-status`'s own accounting (not a guess): capped at 200,000 B/s, a 3s window moved
611,085 bytes (203,695 B/s — matches the cap to within 2%). Immediately after writing `set
net:limit-total-rate 5000000` into the held-open stdin, the same job's throughput jumped
sharply and it finished far faster than the original cap could have allowed. A second run
retuning 300,000 → 3,000,000 mid-flight showed effective size accelerate from ~517 KB to ~8.15
MB over the following 2s (≈3.8 MB/s) — well above the old cap, consistent with the new one.

**Not adopted — admission control still stands alone**, exactly as the phase 3 prompt required.
`core/queue.py` spawns every job with `stdin=DEVNULL`; the held-open-pipe technique was only
exercised in a standalone script, never wired into production. This closes the "unverified"
qualifier on DESIGN.md §4.5's experiment and on §15.2 — a future phase could build on it to
reclaim the "half the pipe sits idle after a partner finishes" cost (§4.5's "residual
inefficiency"), but nothing forces that decision now.

---

## 2026-08-11 — Phase 3: `GET /api/files` must read `item.state` from the database, not
## `core/engine.py`'s in-memory scan model

**Found live, through the running HTTP API — not by static review.** Stopping a job via `POST
/api/jobs/{id}/stop` correctly wrote `item.state = 'STOPPED'` to the database (confirmed by
direct SQL in `tests/test_queue.py`), but `GET /api/files` kept reporting `PARTIAL` for the same
item immediately afterward. `api/files.py` was serving `core/engine.py`'s `engine.models` —
`core/reconcile.py`'s pure structural output (REMOTE_ONLY/LOCAL_ONLY/PARTIAL/DOWNLOADED,
recomputed from scratch on every scan), which has no notion of QUEUED/DOWNLOADING/STOPPED/FAILED
at all. That was the correct thing to serve in phase 2 (nothing else existed), but phase 3 adds
a second writer of `item.state` — `core/queue.py` — and the read path never learned to look at
its output.

**Fix:** `api/files.py.get_files()` now queries the `item` table directly for every field,
including `state`, rather than reproducing the merge from `engine.models` in Python. The
database is genuinely simpler here: `core/engine.py._persist` already knows how to merge
scan-derived and job-derived state (see the next entry), so re-deriving that merge a second time
at the API layer would only be a second place for the two to drift apart.

**Also fixed in the same pass:** `GET /api/files` never exposed the persisted `item.id` at all —
phase 2's read-only Files view never needed it, but `POST /api/jobs` (queue an item, §4.7) takes
exactly that id, and there was no way for a client to obtain one. `FileNode` gained an `id`
field.

---

## 2026-08-11 — Phase 3: a periodic rescan must not overwrite a job-lifecycle state back to a
## purely structural one — DESIGN.md doesn't say who wins

**Ambiguity found building the transfer engine, resolved with the smallest reasonable call.**
`core/engine.py`'s scan loop persists `item.state` fresh on every pass (every `scan_interval_s`,
default 30s, plus on-demand). `core/queue.py` also writes `item.state` — QUEUED on enqueue,
DOWNLOADING on spawn, STOPPED/FAILED on stop or exhausted retries. Nothing in DESIGN.md's §3.2
or §4 says which writer wins when both are live for the same item at once. Left unresolved, a
`STOPPED` item with a still-partial file reads as `PARTIAL` again the moment the next scan runs
— indistinguishable from "never stopped", which quietly defeats §4.6's auto-queue suppression
rule (a state that reverts to non-STOPPED can't stay suppressed for the right reason).

**Fix:** `core/engine.py._persist` now treats an item as "protected" — and leaves its `state`
column alone, refreshing only size/mtime — whenever it currently has a `job` row in
`queued`/`running`, or `auto_queue_suppressed` is set (STOPPED/FAILED). Everything else still
gets the freshly computed structural state. `core/queue.py`'s own success path
(`_reap_one`) clears `auto_queue_suppressed` and sets `DOWNLOADED` itself, so the next scan is
free to confirm it rather than fight over it — the protection only ever applies while `queue.py`
is actively using the row.

---

## 2026-08-11 — Phase 3: three real lftp behaviors found running it for real, none documented
## anywhere in DESIGN.md or `lftp --help`

All three were found by running actual commands against the fake seedbox while building
`core/lftp.py` — see `tests/test_lftp.py` for the pinned regression coverage.

**1. `mirror -c 'REMOTE/item' 'LOCAL/'` creates `LOCAL/item/...` itself — it appends the
remote path's own basename onto the target.** The "obviously" symmetric choice with `pget`
(`LOCAL/item/`, matching the item's own local directory) produces a doubly-nested
`LOCAL/item/item/...` tree instead. `core/lftp.py.build_transfer_command` documents this
explicitly; `core/queue.py` passes the item's *parent* directory as `local_path` for a `mirror`
job, the item's own local directory for `pget`.

**2. A bare `open sftp://user@host` makes lftp's own sftp backend try to prompt for a password
itself — `GetPass() failed -- assume anonymous login` / `Login failed: Password required` —
even when the connect-program's ssh has already authenticated successfully via a key.**
`-u user,` with an *empty* password field suppresses lftp's own prompt and defers entirely to
whatever the connect-program's ssh already established. `core/lftp.py.build_rc_text` always
uses the `-u user,password` form now, with an empty password for `key`/`agent` auth.

**3. `pget:save-status` defaults to `10s`.** Far too coarse for a ~1 Hz progress sampler — a
transfer inspected at the 1s/2s/3s marks under the default had no `.lftp-pget-status` sidecar
at all yet. Every job's rc file now sets `pget:save-status 1s`. This is a genuinely
load-bearing tunable that DESIGN.md §4.4 never mentions, because §4.4 was written assuming the
sidecar simply exists whenever there's progress to read.

**Also found, cosmetic but worth recording:** a script passed to `lftp -c`/`source`d whose
*first line is blank* corrupts quote-stripping on the very next `set key "value with spaces"`
line — the literal quote characters end up in the stored value, and the shell that later execs
that value treats the whole quoted string (spaces and all) as one unfindable program name.
Reproducible on demand; not reproducible once the first line is real content.
`core/lftp.py.build_rc_text` never emits a leading blank line for this reason.

---

## 2026-08-11 — Phase 3: host-key verification for the lftp-spawned ssh child — DESIGN.md §4.2
## never says whether it should match the scanning connection's policy

**Ambiguity found in DESIGN.md, resolved with the smallest reasonable call.** §5/§8 specify
`known_hosts_policy` (accept-and-pin / strict / insecure) for the asyncssh connection
`core/remote.py` uses to scan and test the connection. §4.1/§4.2 describe the *separate* ssh
process `lftp` spawns via `sftp:connect-program` for an actual transfer, but never say whether
it should honor the same policy, default to something else, or fall back to OpenSSH's own
`~/.ssh/known_hosts`.

**Decision:** reuse the exact pin `core/remote.py`'s `KnownHostsStore` already holds for the
host — the same one the scanning connection trusted — written into a throwaway
`known_hosts`-format file alongside the job's rc file (`/run` tmpfs, mode 0600, unlinked with
it), with `-o StrictHostKeyChecking=yes`. `insecure` is passed straight through as
`StrictHostKeyChecking=no` / `UserKnownHostsFile=/dev/null`, matching `core/remote.py`'s own
"insecure means never verify, unconditionally" reading. **`strict`/`accept-and-pin` with no pin
on file yet refuse to spawn the job at all** (`NoHostKeyPinError`) rather than trusting an
unpinned key on the transfer path that the scan path hasn't already vouched for — a transfer job
silently trusting-on-first-use independently of the scanning connection would make the whole
policy decorative. In practice this can only happen if a job is queued before any scan has ever
succeeded, which the engine's own scan loop makes rare but not impossible.

---

## 2026-08-11 — Phase 3: `pget -o <path>` does not create its target's parent directory

**Found running a nested item through the real transfer queue, not anticipated.** `mirror`
creates whatever directory structure it needs under its own target; `pget` does not — queuing
an item whose local target directory didn't exist yet failed with lftp's own `No such file or
directory`, for the *local* side, from inside the container running as the right uid with
correct permissions. `core/queue.py._spawn_decision` now `mkdir -p`s the exact directory a
`pget` job's file will land in (and a `mirror` job's own target-parent) before spawning. For a
genuinely top-level item (DESIGN.md §4.7) this is a no-op — the parent is just the queue's
`local_path`, which the operator already provisioned — but nothing in the schema restricts
`item` rows (or manual queueing) to top-level entries (see the phase 2 decision on that), so it
has to hold generally.

---

## 2026-08-11 — Phase 3: out-of-scope bug found incidentally — one permission-denied
## subdirectory anywhere in a queue aborts that queue's *entire* scan

**Found live while verifying phase 3 through the API, not something phase 3 was asked to fix.**
`core/remote.py`'s primary scan path (`find <path> -mindepth 1 -printf ...`) treats any nonzero
exit as a hard failure unless it matches the "unsupported `-printf`" fallback trigger. GNU
`find` exits `1` the moment it can't `stat`/read one subdirectory's permissions — even though it
still printed every record it *could* read to stdout first. The whole queue's scan is discarded
and reported as failed, rather than the one inaccessible subtree being skipped. Not fixed here
(it's `core/remote.py`, phase 2's module, and out of the phase 3 prompt's scope) — recorded so a
future session doesn't have to rediscover it. Triggered by a test fixture (`chmod 000` on a
seedbox directory) removed before phase 3's verification continued.

---

## 2026-08-11 — Phase 3: two admission-control edge cases DESIGN.md's §4.5 worked examples
## don't cover, decided in code

**"Start now at max bandwidth" bypasses both the main-lane slot count and headroom, not just
headroom.** §4.5 says it "admits immediately with allocation = the full B, deliberately
oversubscribing past the ceiling" and separately that normal admission freezes "while `Σ
allocations > B − reserve`" — the bandwidth side is explicit, but whether it also ignores
`max_concurrent_transfers` (N) is never stated. Decided: yes, unconditionally — it's framed
throughout §4.5 as "the escape hatch", and a version that still queued behind a full N would be
indistinguishable from Move to Top. `core/scheduler.py.admit()` admits every `forced_full_rate`
queued item first, before computing `slots`/`ready` for anything else.

**`UNKNOWN` error class never retries.** §4.3 names the transient classes (`HOST_UNREACHABLE`,
`TLS_ERROR`, timeouts, resets) and the permanent ones (`AUTH_FAILED`, `PERMISSION_DENIED`,
`REMOTE_GONE`, `DISK_FULL`) but never places `UNKNOWN` in either bucket. Decided: retry is a
whitelist (`core/lftp.TRANSIENT_ERROR_CLASSES`), not "retry everything not explicitly
permanent" — a failure our classifier didn't recognize is exactly the case where blindly
hammering the seedbox on a timer is the wrong default; a human should see it once via `FAILED`
rather than have it retry silently up to `max_attempts` first.

---

## 2026-08-11 — Phase 2: `asyncssh.connect()` fails outright under DESIGN.md §11.2's own
## numeric-uid convention — `getpass.getuser()` raises `OSError` on Python 3.13

**Found running the actual built container against the fake seedbox, not anticipated by
DESIGN.md.** `core/remote.py`'s connections all failed with `"No username set in the
environment"` the moment lftpweb ran inside its own container (uid 1000 via compose's native
`user:`, no `/etc/passwd` entry — exactly §11.2's documented identity model, and exactly what
the PUID/PGID entrypoint also produces). Traced to `asyncssh.connect()`: it unconditionally
calls `getpass.getuser()` early in connection setup, for SSH-config `%u` templating, completely
independent of the `username=` kwarg we always pass. `getpass.getuser()` falls through to
`pwd.getpwuid(os.getuid())`, which raises `KeyError` for an unregistered uid — and on Python
3.13, `getpass.getuser()` itself catches that `KeyError` and re-raises `OSError('No username
set in the environment')`. asyncssh's own `except KeyError:` around the call does not catch an
`OSError`, so the exception propagates and every connection attempt fails, for every auth
method, before authentication is ever reached.

**Fix:** `core/remote.py` sets `LOGNAME` at import time — but only if none of
`LOGNAME`/`USER`/`LNAME`/`USERNAME` is already set, so a real environment value is never
overridden. `getpass.getuser()` checks the environment before touching `pwd`, so this sidesteps
the crash entirely without touching container identity, `/etc/passwd`, or asyncssh itself.
Covered by `tests/test_remote_username_env.py`, and reproduced for real: verified failing
against the fully-built runtime image before the fix, and succeeding after, both against the
fake seedbox over the container network (see the phase 2 report for the exact commands).

**Why this belongs in code, not compose.** The trigger is the numeric-uid-with-no-passwd-entry
convention §11.2 already committed to for *both* supported identity mechanisms (PUID/PGID and
compose's native `user:`), so every deployment shape this project supports hits it. Fixing it
by adding `environment: USER=...` to the compose files would work for the two committed compose
files but silently reintroduce the bug for anyone deploying with their own compose/Kubernetes
manifest that follows the same PUID/PGID convention — the fix belongs where the assumption that
breaks it (§11.2) is made, which is the application, not any one deployment's config.

---

## 2026-08-11 — Phase 2: `known_hosts=None` in asyncssh silently disables host-key
## verification *and* skips the `validate_host_public_key` callback entirely

**Found while building the accept-and-pin flow, not anticipated.** The natural-looking way to
say "we're doing our own host-key checking" is `asyncssh.connect(..., known_hosts=None,
client_factory=OurClient)`, expecting `OurClient.validate_host_public_key` to be consulted for
every key. It never is: asyncssh's `_connection_made()` sets `self._trusted_host_keys = None`
whenever `known_hosts is None`, and `validate_host_public_key` is only called when
`self._trusted_host_keys is not None`. The practical effect: with `known_hosts=None`, asyncssh
trusts *any* server host key unconditionally and never asks our callback anything — the
accept-and-pin policy (DESIGN.md §5, §8) silently never ran, and *every* `known_hosts_policy`,
including `strict`, would have behaved as `insecure`.

**Fix:** pass `known_hosts=asyncssh.SSHKnownHosts()` — a real, empty, in-memory known-hosts
object, not `None` and not an empty string/list/bytes (any of which cause asyncssh to fall back
to probing `~/.ssh/known_hosts` on whatever filesystem the process happens to see, which is
worse). An empty `SSHKnownHosts` is non-falsy and holds zero trusted keys, so asyncssh always
defers to `validate_host_public_key`, which is where `core/remote.py`'s
`known_hosts_policy` (`accept-and-pin` / `strict` / `insecure`) is actually enforced, via a
small JSON pin store (`KnownHostsStore`) rather than OpenSSH's own known_hosts file format.
Verified live against the fake seedbox: first connection pins and logs the key; a corrupted
pin is rejected as `HOST_KEY_MISMATCH` on the next fresh connection; `strict` against a never-
pinned host reports `HOST_KEY_UNKNOWN`. See `tests/test_known_hosts_store.py` and the phase 2
report's edge-case script for the exact assertions.

**Also decided: `insecure` bypasses the pin store entirely, checked first.** An earlier draft
checked the stored pin before checking policy, so an `insecure` host that happened to have a
pin recorded under a different policy earlier would be rejected as a "mismatch" — exactly
backwards for a policy that means "never verify." `insecure` is now the first check in
`validate_host_public_key`, unconditional, and never reads or writes the pin store.

---

## 2026-08-11 — Phase 2: credential encryption at rest ships now, not in build phase 8

**Decision, mandated by the phase 2 prompt rather than discovered during the build:** §8's
encryption scheme (`core/crypto.py`) — a per-install secret in `<config_dir>/secret.key`, mode
0600, generated on first run; a Fernet key derived from it via HKDF-SHA256; `host.password_enc`
encrypted at rest — ships in phase 2, the phase where a seedbox password first exists, rather
than waiting for phase 8 as `DESIGN.md` §13's build order literally lists it. Phase 8 still
owns the *rest* of §8: auth modes, sessions, API keys, rate limiting.

**The secret is deliberately not backed up** (§8/§10.2 — `core/backup.py` is phase 7 and will
need to exclude `secret.key` from `VACUUM INTO` targets when it lands), so a restore to a fresh
install cannot recover a stored password. `DecryptionError` is how `core/engine.py` and
`api/settings.py` detect that case: `load_host_config` catches it and proceeds with
`password=None` rather than crashing, and `GET /api/settings/host` reports
`credentials_need_reentry: true` so the UI can surface it — the full "hold all transfers for
this host" behavior §8 describes waits for phase 3's job engine to have transfers to hold.

---

## 2026-08-11 — Phase 2: `item` rows persist per-node (file *and* directory), not just
## §4.7's top-level "item" concept

**Ambiguity found in `DESIGN.md`, resolved with the smallest reasonable call.** §4.7 defines
"item" narrowly — a top-level entry of a queue's `remote_path`, either a directory or a loose
file, the granularity auto-queue patterns match against. But the `item` table (§3.1) has
`UNIQUE(queue_id, rel_path)` with no depth restriction, and §9.2's item drawer promises
"per-file status... over the whole tree" for everything inside a release. Read literally, §4.7's
item definition and the `item` table's evident scope disagree.

**Resolution:** `core/engine.py` persists one `item` row per node the reconciler produces —
every file and every directory in the merged tree, not only top-level entries — because that's
what the Files page (a full tree, not a flat item list) and the future item drawer both need,
and nothing in §3.1's schema forbids it. §4.7's narrower "item" remains the correct unit for
auto-queue pattern matching (phase 4); the two uses of the word describe different granularities
of the same table, and phase 4 should pattern-match against top-level rows specifically rather
than assuming every persisted row is an auto-queue item.

---

## 2026-08-11 — Phase 2: a directory with zero local presence reads as `REMOTE_ONLY`, not
## `PARTIAL` — `DESIGN.md` §3.2 rule 1 doesn't say

**Ambiguity found in `DESIGN.md`, resolved with the smallest reasonable call — surfaced for
review, not silently decided.** Rule 1 states a directory is `DOWNLOADED` only when every
relevant descendant file is complete, "otherwise `PARTIAL`" — a strict binary, with no
carve-out for a directory that has *zero* local presence at all (nothing queued or downloaded
yet). Read literally, a totally-untouched remote-only release directory would show `PARTIAL`,
which reads to a user as "download interrupted," not "nothing has happened yet."

**Decision:** `core/reconcile.py` computes three directory states from rule 1's own
completeness accounting (already computed for `DOWNLOADED` vs not): `DOWNLOADED` when every
relevant file is complete (or vacuously, when there are none), `REMOTE_ONLY` when *no* relevant
file has any local copy, and `PARTIAL` only for the genuine in-between. This is additive to
rule 1, not a departure from it — the `DOWNLOADED`/not-`DOWNLOADED` boundary rule 1 specifies is
unchanged; only the "otherwise" is split into two states instead of collapsed into one. Pinned
by `tests/test_reconcile.py::test_directory_remote_only_with_zero_local_presence` and the
directory-state table alongside it.

---

## 2026-08-11 — Phase 2: one combined scan interval, not `DESIGN.md` §5's separate 30s
## remote / 10s local cadences

**Deviation recorded rather than silently taken.** §5 specifies remote scans every 30s and a
faster local-only walk every 10s, with the gap covered by phase 3's 1 Hz active-file
`ProgressSampler`. `core/engine.py` runs one interval (default 30s, `LFTPWEB_SCAN_INTERVAL_S`)
that scans both sides together, plus `request_rescan()` for an immediate on-demand pass (used
by `POST /api/files/rescan` and after a host/queue config change).

**Why this is acceptable now.** The faster local-only cadence exists to catch local filesystem
changes (an import finishing, a manual delete) between the more expensive remote round-trips —
a scale/responsiveness optimization. With no active transfers yet (phase 3), nothing is
producing local changes on that kind of timescale, and every phase 2 verification (including
the delete/restore flip test) uses `request_rescan()` rather than waiting on a timer. Splitting
the cadence is deferred to whenever it's actually needed, not dropped — `Engine.scan_queue`
already separates the remote scan, the local scan, and the reconcile call, so adding a second,
faster local-only loop later doesn't require restructuring it.

---

## 2026-08-11 — Phase 2: WebSocket "deltas" are per-queue full snapshots, not row-level diffs

**Scoped-down interpretation of `DESIGN.md` §2/§9, recorded rather than silently taken.** "A
full model snapshot on connect, deltas thereafter" is read literally as row-level diffing
elsewhere in the doc's vocabulary (e.g. the `item` table's change tracking). Phase 2's
`api/ws.py` instead sends one `queue_snapshot` message — the complete fresh state of one
queue — every time `core/engine.py` finishes scanning that queue, and the frontend
(`useFilesSocket.ts`) merges it into a `queue_id`-keyed map. A queue that hasn't rescanned since
connecting keeps showing its last-known state rather than vanishing.

**Why this is acceptable now.** A reconciled tree is idempotent state, not an event log — the
whole tree is cheap to hold and to replace outright (`RemoteConnectionPool`'s find output for
the seed tree is a few KB), and there is no job/lifecycle history yet whose *transitions*
specifically need to be pushed. Row-level diffing becomes worth the complexity once phase 3's
per-file progress ticks at ~1 Hz on the active set (§4.4) — pushing a whole queue's tree on
every progress tick would not scale the way pushing one row's bytes-done does. Flagged here so
phase 3 doesn't inherit this shape by default.

---

## 2026-08-11 — Phase 2: the fake seedbox's SSH keypair and password are committed, on purpose

**Decision.** `docker/test-seedbox/test_key`(`.pub`) and the hardcoded `testpass123` in
`sshd_config`/the two Dockerfiles are committed to the repo, despite the general rule (§12.1,
`.gitignore`) that credentials never get committed. This is not an exception to that rule —
these are not credentials to anything real: the containers they authenticate are built from
this repo, reachable only on `127.0.0.1`, hold a synthetic tree of known sizes, and are torn
down after every verification run. Requiring them to be generated fresh on every `docker
compose -f docker-compose.test.yml up` would only add friction for zero safety benefit, since
there is nothing behind them to protect.

**Why a real GNU + a real busybox container, not one container with two `find` shims.**
DESIGN.md §15.7 records `find -printf` as GNU-specific and calls for verifying the fallback
against the real thing. Faking busybox's behavior inside a GNU environment (a wrapper script,
an alias) would test the fallback *trigger* logic but not the actual busybox error text
`core/remote.py`'s detection regex has to match — and that text (`"find: unrecognized:
-printf"`) was itself discovered by running the real binary, not by reading busybox's source.
`docker/test-seedbox/Dockerfile.busybox` deliberately does not install `findutils`, which is
the one thing that would silently stop testing what it exists to test.

---

## 2026-08-11 — Phase 2: the Files tree is not yet virtualized

**Deviation recorded rather than silently taken.** `DESIGN.md` §9.2 calls for a virtualized
tree "smooth at 10k+ rows." `frontend/src/components/FileTree.tsx` renders the full DOM tree
with plain React recursion and no virtualization library — none is installed yet, and adding
one is a dependency decision worth making deliberately rather than as a side effect of phase 2.
§13's build order lists "virtualization tuning" explicitly under phase 9 (Polish), so this is
read as on-schedule rather than a phase 2 gap: the fake seedbox's tree (17 nodes) and any
realistic dev-scale queue are nowhere near where non-virtualized rendering degrades, and
nothing about the read-only, collapsible, per-row-state-chip shape this phase built needs to
change to add virtualization later — only the row-rendering internals of `FileTree.tsx` would.

---

## 2026-08-11 — Phase 3a review: a small bandwidth ceiling silently deadlocked the whole queue

**Found reviewing phase 3a, by setting a 400 KB/s cap and watching a job sit in `queued`
forever.** `DESIGN.md` §4.5 specified the fast-lane reserve as *"10% of B, min 1 MB/s"*. That
floor is unconditional, so:

| ceiling B | reserve | headroom = B − reserve | admits |
|---|---|---|---|
| 400 KB/s | 1 MB/s | **−600 KB/s** | 0 |
| 1 MB/s | 1 MB/s | **0** | 0 |
| 5 MB/s | 1 MB/s | 4 MB/s | 1 |

Any ceiling at or below 1 MB/s produced `headroom <= 0`, so the main lane admitted **nothing,
ever** — jobs accepted, queued, and never run, with no error, no failed state, and no log line.
A user throttling lftpweb to be polite to their uplink would get a permanently dead queue and
no way to tell why.

The design error is worth naming precisely: the fast lane exists to stop small items being
blocked by large ones, and the unclamped floor let it block *everything* instead — the exact
failure it was introduced to prevent, inverted.

**Fix, in both code and `DESIGN.md` §4.5:** the reserve is capped at `B/2`, so it can never
consume the ceiling it is carved from. Explicit user-set reserves are clamped too, not just the
derived default. Regression test `test_low_ceiling_still_admits_work` parametrises ceilings from
100 KB/s upward and asserts work is still admitted.

**Also fixed: the silence.** When the scheduler admits nothing while work is waiting, it now
logs the arithmetic that produced that decision (ceiling, reserve, allocated, headroom, slots).
Admitting nothing is usually correct, but it was previously indistinguishable from a wedged
queue — which is how this hid.

---

## 2026-08-11 — Phase 3a review: a spawn failure left the job queued and the tick hot-looping

**Found in the same session, accidentally**, by running the backend outside its container
without setting `LFTPWEB_RUN_DIR`: `/run/lftpweb` isn't writable by a normal uid, so
`lftp.spawn()` raised `PermissionError` inside `_spawn_decision`. (The misconfiguration was
mine — `run_dir` is configurable and documented. The *failure mode* is the bug.)

`_loop`'s blanket `except Exception` caught it, logged `transfer queue tick failed`, and
continued — so the job stayed `queued`, the tick retried once a second forever, and the API
reported a perfectly healthy job that simply never started. Every real deployment failure of
this shape (read-only `/run`, missing `lftp` binary, wrong uid) would look identical.

**Fix:** `_admit()` catches per-decision, marks that job `failed` with `error_class =
SPAWN_FAILED` and the exception detail on the row, and suppresses the item like any other
permanent error (§4.3, §4.6) — so it surfaces in the UI, doesn't spin, and doesn't take the
other decisions in the same tick down with it. Covered by
`test_spawn_failure_fails_the_job_instead_of_hot_looping`.

---

## 2026-08-11 — Phase 1 review: each migration must be atomic, or a failure wedges the install

**Found in review of the phase 1 build, not by the build itself.** The first migration runner
called `executescript(file)` and then, separately, inserted the `schema_version` row and
committed. `sqlite3.executescript()` commits any open transaction before it runs and then lets
the script's statements commit as they go — so it is not atomic.

Demonstrated rather than assumed. Given a migration `002` whose second statement fails:

- statement 1 stays **committed**, statement 2 fails,
- the `schema_version` row is never written,
- so the next start re-runs `002` from the top, hits `table beta already exists`, and the
  install is **permanently stuck** — no forward path without hand-written SQL repair.

That is the worst class of bug for this component: it corrupts the thing that is supposed to
make schema change safe, it only fires on the unhappy path, and §10.2's pre-migration backup
is build phase 7, so today there is no safety net behind it.

**Fix:** `migrate()` wraps each migration's text *and* its `schema_version` insert in a single
`BEGIN`/`COMMIT` inside the script it hands to `executescript()`, and rolls back on failure. It
has to be done by wrapping the script text — an outer `BEGIN` around the `executescript()` call
would be discarded by the implicit commit. Two rules now documented in `db.py`: migration files
must contain no transaction control of their own, and no pragmas that cannot run inside a
transaction (connection pragmas belong in `connect()`).

Covered by `tests/test_db.py::test_failed_migration_is_rolled_back_entirely`, which asserts the
partial migration leaves nothing behind *and* that a corrected migration then applies cleanly —
the property that actually matters.

---

## 2026-08-11 — Phase 1: app ports moved to 8087 (API/SPA) and 5187 (Vite dev), not 8080/5173

**Decision:** `LFTPWEB_PORT` defaults to `8087` (config, Dockerfile `ENV`/`EXPOSE`/`HEALTHCHECK`/
`CMD`, both compose files), and the Vite dev server defaults to `5187`. Plain literals in
`docker-compose.yml` and `docker-compose.dev.yml` — no `.env` interpolation.

**Why.** The build host already runs other stacks on 8080, 5173, 8090, and several other
common defaults. Chosen deliberately rather than discovered by collision on someone's
seedbox later. Anyone deploying this can still just edit the compose file port lines.

---

## 2026-08-11 — Phase 1: hand-rolled migrations, not Alembic

**Decision:** numbered SQL files in `backend/lftpweb/migrations/NNN_description.sql`,
applied in order by a small runner in `db.py`, tracked in a `schema_version` table.

**Rejected: Alembic.** The schema in DESIGN.md §3.1 is raw SQL with no ORM — there are no
SQLAlchemy models for Alembic to diff against, so it would only be driven manually via
`op.execute()`, which is friction without the autogeneration benefit that's Alembic's main
draw. §10.2's backup-before-migration hook is a few lines in `migrate()` either way, so
there's no capability Alembic buys that this repo needs.

---

## 2026-08-11 — Phase 1: `cap_drop: ALL` needs `CHOWN`/`SETUID`/`SETGID` added back

**Found during the container build, not anticipated by DESIGN.md.** §11.1 specifies
`cap_drop: ALL` and, separately in §11.2, a root-starting entrypoint that `chown`s `/config`
and drops privileges via `su-exec` (a `setuid`/`setgid` call). Tested literally: with
`cap_drop: ALL` and nothing added back, the container crash-loops before the app starts —
`chown(2)` and `setuid(2)`/`setgid(2)` are themselves capability-gated on modern kernels,
even for uid 0. Root without capabilities can't do either.

**Fix:** `docker-compose.yml` keeps `cap_drop: ALL` and adds back exactly `CHOWN`, `SETUID`,
`SETGID` — the standard "drop everything, re-grant the minimum" pattern. This only affects
the entrypoint's brief root phase; once `su-exec` drops to the unprivileged PUID/PGID, the
running app process has none of these capabilities. `DESIGN.md` §11.1's "the app needs no
capabilities at all" is true of the *running app* and should probably be read that way, but
the compose file as literally described doesn't boot — worth a look next design pass.

---

## 2026-08-11 — Phase 1: entrypoint never creates a passwd/group entry for PUID/PGID

**Found during the container build.** `docker-compose.yml`'s `read_only: true` (§11.1) makes
the whole root filesystem read-only except `/config`, `/downloads`, `/staging`, and a `/run`
tmpfs. An `addgroup`/`adduser` step — needed only to give PUID/PGID a friendly name for
logging — writes to `/etc/passwd` and `/etc/group`, both on the read-only root, and fails
outright under that profile.

**Fix:** the entrypoint (`docker/entrypoint.sh`) never calls `addgroup`/`adduser`. `su-exec`
and `chown` both accept raw numeric `uid:gid` without an NSS entry, so nothing actually
needed one; log lines just print the numeric ids instead of a resolved username. Also fixed
in the same pass: an early version of `check_writable()`'s non-fatal path returned a nonzero
exit status from its own `if` test, which `set -e` treated as a script failure and aborted
startup even though the check was designed to only warn — every non-fatal branch now ends
with an explicit `return 0`.

---

## 2026-08-11 — Phase 1: `/api/health` carries `repo_url`, beyond §12's literal 4-field shape

**Ambiguity found during the build.** DESIGN.md §12 defines `/api/health` as
`{status, version, db, uptime_s}`, but separately requires the nav's version link to use
`LFTPWEB_REPO_URL` (§9.1, §12) — a container env var, i.e. a *runtime* value, set after the
SPA has already been built into static files in the Docker image. A Vite build-time constant
can't carry a value that isn't known until the container starts, so the frontend has to fetch
it from the backend, and health is already the request the UI makes to render the version.

**Decision:** added a fifth field, `repo_url`, to `HealthResponse` rather than introducing a
new endpoint. Smallest change that satisfies both requirements; flagged here since it
deviates from the literal shape the design doc states.

---

## 2026-08-11 — Phase 1: `docker-compose.yml`'s `image:` is a placeholder

**Decision:** `image: ghcr.io/crzynet/lftpweb:0.0.1`, not a digest. DESIGN.md §11.2 describes
production as "pulled by digest from the registry," but this repo has no GitHub remote and no
CI (`code-checkin-and-pr` deferred — see below), so no image has ever been published for a
digest to pin. The placeholder documents the eventual shape; replace with a real
`ghcr.io/<owner>/lftpweb@sha256:...` once that standard's registry side is adopted.

---

## 2026-08-11 — Phase 1: venv kept at the identical absolute path across every Docker stage

**Found during the container build.** `uv sync` bakes an *absolute* path to the venv's own
python into every console-script shebang (e.g. `#!/build/.venv/bin/python`) and into
`pyvenv.cfg`. An earlier draft of the Dockerfile built the venv at `/build/.venv` in the
python-builder stage and `COPY --from=`'d it to `/opt/venv` in the runtime stage — every
script under `/opt/venv/bin` (including `uvicorn`) then had a shebang pointing at
`/build/.venv/bin/python`, which doesn't exist in the runtime stage, so every attempt to run
it failed with a bare `No such file or directory` and no other clue. Fixed by using `WORKDIR
/app` — and therefore `/app/.venv` — identically in `python-base`, `python-builder`, `dev`,
and `runtime`, so the `COPY --from=` carries a venv forward that's still valid at its own
recorded path.

---

## 2026-08-11 — Stop is terminal; auto-queue must never resurrect it

**Decision:** stopping a job is a user action with no automatic retry. The item lands in
`STOPPED` with its partial data kept, and carries `auto_queue_suppressed` so auto-queue skips
it. Same flag on `FAILED` after exhausted retries. Only a deliberate manual re-queue clears it.
See `DESIGN.md` §4.6.

**Why it needs saying at all.** The retry policy in §4.3 (transient classes retry with backoff
to `max_attempts`, permanent classes never retry) is meaningless without this. Auto-queue runs
on a scan cadence and matches on patterns; a stopped job still matches its pattern, so the next
pass would re-queue it ~30 s later, forever. That is an unbounded retry loop wearing a
different hat, and a UI that ignores an explicit user instruction. The suppression flag is what
makes "stop" mean stop.

**Also decided:** stop sends SIGTERM, not SIGKILL, so lftp flushes its `.lftp-pget-status`
sidecar and the partial stays resumable; SIGKILL only after a ~10 s grace period.

---

## 2026-08-11 — Three pattern kinds, one evaluator, used by both lftp and the reconciler

**Decision:** auto-queue patterns split into `select` / `skip` (matched against the item name,
enforced by us) and `file_exclude` (matched against paths inside an item, enforced by lftp via
`--exclude-glob`). Matching is case-insensitive, glob when the pattern contains `*?[` and plain
substring otherwise, with skip beating select. `DESIGN.md` §4.7.

**Rejected: SeedSync's substring-OR-glob on every pattern.** Friendlier, but ambiguous as soon
as a pattern contains a metacharacter — `*.nfo` would match both ways with different results.
Dispatching on whether metacharacters are present keeps the convenience (`1080p` works without
`*1080p*`) and drops the ambiguity.

**The bug this uncovered — the important part.** File excludes are passed to lftp, so those
files never arrive. But completeness (§3.2 rule 1) compares every remote child against local,
so an excluded `.nfo` reads as missing and the directory is **permanently `PARTIAL`** — never
`DOWNLOADED`, never verified, never extracted, never deleted under `move`, and re-queued on
every pass. A single exclude pattern would have quietly broken the pipeline for every item it
touched.

**Fix:** one compiled pattern set, used in two places — building the lftp command line *and*
deciding what the reconciler expects an item to contain. Excluded children are marked
`EXCLUDED`, a real state rather than an absence, and don't count toward completeness. The
consequence, accepted: changing `file_exclude` patterns retroactively changes completeness in
both directions, so the pattern preview has to show it rather than let it be discovered.

**Follow-on: an item is a top-level entry, directory *or* loose file.** A root-level
`Movie.mkv` is an item in its own right, matched by a `*.mkv` select and transferred with
`pget`; a directory is matched on its own name, so `*.mkv` does not match `Movie.2024/`
containing an mkv. Item patterns see item names, never contents.

That raised two edge cases, both resolved toward "an intended absence is not a missing one":

- **`file_exclude` also applies to loose top-level files.** Otherwise `*.nfo` would suppress
  nfos inside releases while happily downloading a stray `notes.nfo` at the root. When the item
  is a file, both `skip` and `file_exclude` are tested against its name — making the user enter
  the same pattern twice would be a trap, not a feature.
- **A directory whose children are all excluded is vacuously `DOWNLOADED`, and its local
  directory may not exist at all**, because lftp does not create a directory it has nothing to
  put in. Completeness must not require it. Same bug class as the exclusion bug above, one
  level up.

---

## 2026-08-11 — Alpine base, and `7zz` as the single extraction tool

**Decision:** `python:3.13-alpine` runtime (`node:22-alpine` builder), with the `7zip` package
(7-Zip proper, `7zz`) as the only archive tool. See `DESIGN.md` §11 and §11.1.

This deliberately departs from the sibling projects (`filament-bridge`, `labelforge`,
`partfolder3d`), which all run `python:*-slim` on Debian. Consistency lost to "smallest secure
image that does this job" on request.

**Why Alpine.** ~3× smaller with a much smaller installed package set, which is most of the CVE
surface. The historical objections are largely spent: musl gained DNS TCP fallback in 1.2.4
(Alpine 3.19+), and every dependency we need — `cryptography`, `pydantic-core`, `argon2-cffi` —
publishes `musllinux` wheels, so no Rust toolchain lands in the runtime image.

**Rejected: Debian slim.** Larger, and its archive story is worse — `unrar` is non-free and
`unrar-free` historically cannot read RAR5, which is what scene releases actually ship.

**Rejected: distroless / Chainguard.** Lower CVE counts, but we need `lftp`, `ssh`, and `7zz`
plus a shell for the PUID/PGID entrypoint. Fighting those images to install arbitrary packages
buys little over Alpine.

**`7zz` instead of `unrar` + `p7zip`.** 7-Zip 21.07+ extracts RAR and RAR5 natively, so one
binary covers rar / rar5 / zip / 7z / tar / gz / bz2 / xz — no non-free repo to enable, no
second tool to keep current. Its RAR decoder derives from the unRAR source, whose licence
forbids building a RAR-compatible *compressor*; we only extract, so this is a footnote rather
than a constraint.

**The base image is the smaller half of "secure."** The rest is runtime posture and lives in
compose: non-root, `cap_drop: ALL`, `no-new-privileges`, read-only rootfs, digest-pinned base,
and credentials confined to a `/run` tmpfs at mode 0600 (§11.1).

---

## 2026-08-11 — Admission-control scheduler; allocations are never re-shaped

**Decision:** bandwidth is handed out at admission and fixed for a job's lifetime. Site-level
`max_bandwidth` and `max_concurrent_transfers`, a fast lane for small items, and a sortable
rank for priority. Full algorithm and worked examples in `DESIGN.md` §4.5.

**The insight.** `lftp -c` exits with its transfer and offers no control channel, so a running
job cannot be retuned. Earlier drafts treated that as a defect to work around — first by
dividing by max concurrency, then by dividing by active jobs at spawn. Both were workarounds
for a constraint that a different scheduler simply never encounters. Allocating at admission
and never re-shaping turns the limitation into the design.

**Rejected: re-shaping running jobs.** Requires the control channel we don't have. The
stdin-held-open experiment (§4.5) might supply one, but it is unverified and nothing may depend
on it.

**Rejected: dividing by `max_concurrent`.** Wastes the most throughput in the commonest case —
one large download at a time.

**Rejected: an unmetered fast lane.** Queue 300 small files and it saturates the uplink at its
concurrency cap, starving the rate-limited main lane and blowing past the ceiling precisely
when the ceiling matters. The reserve is carved off `B` instead, so the total stays bounded.

**Accepted cost.** A job admitted at B/2 keeps B/2 after its partner finishes, leaving half the
pipe idle with nothing to claim it (§15.4).

**Fast lane rationale.** Not about small files being special — about head-of-line blocking. A
3 MB `.nfo` arriving while a 40 GB release holds the whole ceiling would otherwise wait an hour
to move a file it could have finished alongside in under a second.

**Site-level, not per-queue.** Parallelism and bandwidth multiply into a single host-wide
connection ceiling; letting each queue raise them independently makes that ceiling
unenforceable. A queue governs *what* and *where*, never *how fast*.

---

## 2026-08-11 — `sync` mode deferred indefinitely; hardlink pickup dir is what makes deletion safe

**Decision:** lftpweb ships `copy` and `move`. `sync` — propagating local deletes back to the
remote — is designed in full now (`DESIGN.md` §7) but **not scheduled**. It is a possible later
feature, built only if it proves wanted. No build phase depends on it.

An earlier draft of this entry called it "phase 2", which read as a commitment. It isn't one.
The design is kept because the seam (`event` audit, §7.4 deletion path, the state model) is v1
work for `move` regardless, and because the safety reasoning below is what a future session
would need in order to decide whether to build it at all — reconstructing that from scratch is
exactly how an irreversible feature ships with the wrong rails.

**Why remote deletion is safe here at all.** The torrent client hardlinks completed files into a
separate pickup directory, and lftpweb points at the pickup dir, never at the torrent data
directory. Unlinking there drops one link; the seeding torrent keeps its own, so the data, the
seed, and the ratio survive. This is a property of *the directory you point at*, not of
lftpweb — hence the misconfiguration warning in §7.1 and inline at the mode selector.

**Rejected: torrent-client API gating.** The usual correct answer (ask qBittorrent/rTorrent
whether the seed goal is met before deleting). Unnecessary here — the hardlink already encodes
the answer — and it would pull a whole integration in for nothing.

**Rejected: minimum-file-age gating.** A poor proxy: it proves neither that seeding finished nor
that the download completed. Here it would gate an operation that is already safe, adding
friction and buying nothing.

**Rejected: a count-based circuit breaker.** This is the subtle one. Sonarr/Radarr import by
*moving* files out, so a local file disappearing is the normal end state of every successful
import — deletes are **routine, not anomalous**. A "more than N deletes is suspicious" breaker
false-positives on every bulk import. Anomaly detection is therefore unavailable as a
safeguard, which concentrates the entire safety load on the mount sentinel gate (§15.1). That
concentration is the reason `sync` defers: it gets built after the surrounding machinery is
proven, not alongside it.

**What defers with it:** the sentinel gate, grace period / `item.first_missing_at`, dry-run, and
the rate-based backstop. **What does not:** `move` deletes too, so verification-before-delete,
deletes through our own asyncssh path (§7.4), and the `event` audit trail are all v1.

---

## 2026-08-11 — `code-checkin-and-pr` deferred until the first GitHub push

**Decision:** do not adopt `code-checkin-and-pr` yet; follow two of its conventions voluntarily.

Every rule in that standard binds to a remote — protected `main`, `dev → main` PRs, seven
required CI checks, image publishing with registry retention. lftpweb has no remote and no CI,
so a `standards.md` row claiming adoption would assert conformance that cannot exist. The
standards index explicitly warns against exactly this ("a clean-looking row that lies").

Instead: commit-prefix conventions (`feat:` / `fix:` / `chore:` / `docs:`), no
`Co-authored-by:` trailers, and the `dev` / `main` branch shape are followed from commit one,
so the history is already conformant when the standard is adopted for real. That adoption
should land in the same change that adds the remote and CI, re-pinning the row to the
then-current version.

---

## 2026-08-11 — Bootstrap adoption done in-session, not via a handoff prompt

**Decision:** the `handoff-prompt-workflow` adoption commit is the one task exempt from the
workflow it installs.

The standard's v2.0.0 threshold pushes any edit beyond ~1–2 files into a `prompts/` file
executed by a spawned subagent. This scaffolding touched six files, so by the letter it wanted
a prompt — but that prompt would have had to live in the `prompts/` directory it was itself
creating, inside a git repo that did not yet exist, and the mandated
`git status --porcelain` working-tree check had no tree to inspect.

Rejected alternative: `git init` first, then write the prompt and spawn an agent for the rest.
Workable, but it splits an atomic, fully-prescribed checklist across two contexts for no gain —
the standard's own adoption section *is* the spec, so a fresh context adds nothing.

Scope of the exemption is exactly one commit. Every task after it goes through the workflow.

---

## 2026-08-11 — lftp is a transfer engine, not a status API

**Decision:** derive transfer progress from the filesystem — local bytes on disk versus known
remote size — and use lftp purely to move bytes. One short-lived lftp process per transfer,
driven over plain pipes. See `DESIGN.md` §1.3 and §4.

**Rejected alternative — SeedSync's approach:** one long-lived interactive lftp per path-pair
over a pexpect PTY, with all transfer state reconstructed by polling `jobs -v` every 0.5 s and
regex-parsing lftp's human-readable verbose output.

**Why rejected.** That parser is ~15 interlocking regexes plus an order-dependent line
dispatcher, and it must survive readline's ANSI/bracketed-paste escapes, PTY line wrapping when
`COLUMNS` isn't honored, and lftp's inconsistent progress grammar (in `` `f' at 2976 (12%) ``
the number is *not* the local size and the percentage is *not* the local percentage). SeedSync's
maintainer records it in fork issue #294 as "the most fragile part of the codebase… the root
cause remains", closed as "do nothing for now". Sharing one process per pair also means one
parse failure or pexpect timeout degrades *every* transfer on that pair, and stopping a job
carries an acknowledged kill-wrong-job race because ids can shift between the status read and
the kill.

**What this buys.** Liveness becomes an exit code, stopping becomes a SIGTERM to one PID,
failures are contained to one transfer, and per-file progress covers the whole tree rather than
whichever files lftp happens to mention. ETA is computed and smoothed uniformly by us, fixing
the directory-ETA problem lftp causes by never emitting an ETA on a mirror header.

**Cost accepted.** Two lftp on-disk conventions must be understood instead:
`<file>.lftp-pget-status` sidecars (sparse-file accounting) and the `xfer:use-temp-file` /
`*.lftp` suffix. Both are short, stable, machine-oriented formats, unlike the verbose output,
which is formatted for humans and has never been a stable interface. If either changed,
progress degrades to raw size (still monotonic) and completion is unaffected, because
completion is the exit code.

**Status:** recorded from `DESIGN.md`, which is still under review. If §1.3 is overturned in
review, supersede this entry rather than editing it.
