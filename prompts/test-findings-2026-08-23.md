# Test findings — 2026-08-23 (#18 stages 1–4, first real-client session)

The user's own browser/live-client testing of the download-client framework, on a real seedbox
(SABnzbd 5.1.1 + rTorrent 0.9.8 on usbx.me). **Collected, not acted on** — nothing here is fixed
until the user says so, and fixes should be grouped by control rather than applied one at a time.

Same convention as `prompts/test-findings-2026-08-21.md`.

---

## 1. rTorrent base-path detection reports `~/downloads/rtorrent`, verification says it does not exist

**Observed:** rTorrent connects — *"Reachable (v0.9.8)"*, capabilities render. Base-path detection
reports `~/downloads/rtorrent` (kind `working`) and the UI prompts:

> *"Reports ~/downloads/rtorrent (working), which doesn't exist over SSH. Which path is it here?"*

**What worked:** the connection, `system.client_version`, the capability readout, detection itself,
and the `not_found` prompt firing rather than silently accepting or silently discarding the path.
The detect → verify → confirm flow behaved as designed.

**Suspected cause — ours, not a real namespace mismatch.** `core/browse.py` handles `~` two
different ways:

- `resolve_remote_dir` — *"`~` and relative paths resolve against the SSH user's home via SFTP
  `realpath`"* (its own docstring).
- `remote_directory_error` — a plain `await sftp.stat(path)`, **no `realpath`, no tilde
  expansion**. This is the function the base-path verification calls.

SFTP servers do not expand `~`, so stat'ing the literal string `~/downloads/rtorrent` fails with
"no such file" even though `/home/crzykidd/downloads/rtorrent` exists. The verifier then correctly
reports `not_found` — for a path it never actually looked at.

**Why it matters beyond cosmetics:** a base path is the containment boundary that authorises
deletion (spec §10.2). A verification that can report `not_found` for an existing path is the same
class of defect in the other direction — it means the check is not measuring what it claims to.

### The user's proposed resolution (2026-08-23) — offer the expansion, don't ask blind

> *"You are always connecting from a user context. If we can get home dir, and pwd, then we should
> be able to give an option in the box with a note that says: It appears your ~ path pwd is xxx."*

**This is the right shape, and it is strictly better than either obvious alternative.** Silently
expanding `~` hides that a translation happened — and a translation is exactly the thing §8.2's
`client_path` column exists to keep visible. Asking blind (today's behaviour) makes the user go
look up something lftpweb can already resolve.

So the `not_found` box for a `~`/relative path should **pre-fill the resolved candidate and explain
it**, e.g.:

> rTorrent reports `~/downloads/rtorrent`. Your SSH home is `/home/crzykidd`, so this is probably
> `/home/crzykidd/downloads/rtorrent`.

— confirmed by the user, not applied automatically. Same propose-don't-apply rule the category→queue
inference and base-path detection already follow.

**It is always resolvable.** An SSH session always has a user and therefore a home; `sftp.realpath(".")`
(already used by `core/browse.py.resolve_remote_dir`) cannot be unavailable in a working connection.
There is no fallback case to design for beyond "the connection is down", which is already handled.

**Worth deciding as part of any fix (do not assume):**

- **A `~` path should almost certainly never be *stored*.** Once persisted, every consumer inherits
  the expansion problem — the scan's walk roots, and stage 5's containment check that authorises
  `rm -rf`. A containment check comparing `~/downloads/rtorrent` against
  `/home/crzykidd/downloads/rtorrent` matches nothing, and "the delete boundary silently matches
  nothing" is a bad way to fail. Store absolute at confirm time; keep the client's own `~` form in
  `client_path` for display, which is what that column is already for (§8.2).
- Should `remote_directory_error` expand `~`/relative paths itself (making the two `core/browse.py`
  functions consistent), or should expansion happen once at the detection layer? Note the two
  functions differ *deliberately* today — one falls back gracefully, one gives a real answer — so
  making them consistent needs care rather than a blanket change.
- **Should the SSH home become a known property of the host** (resolved on connect, cached on the
  pooled connection) rather than an SFTP round trip per caller? Today nothing persists or shares it
  — `core/browse.py:230` resolves it live and the `host` table has no column for it. Caching it
  would make `~` expansion a one-liner anywhere, but touches `core/remote.py`, which is
  load-bearing.
- **Check `docs/download-client-framework-spec.md` §13.6 for whether `directory.default` returning
  a tilde form was among the guesses** — if not, it is a new one, and it is the kind that would
  never have surfaced without a live instance.

### Resolved 2026-08-23 — offered and expanded at the detection layer, never applied blind

`prompts/done/2026-08-23-tilde-and-visibility.md`, `docs/decisions.md`, spec §8.2 correction/§13.6
row 13. Built exactly to the user's own shape: `core.clients.detection._resolve_tilde_candidate`
expands a `~`/relative `client_path` via `sftp.realpath` (the same primitive `resolve_remote_dir`
already used) and re-verifies the result over SSH before ever offering it as `DetectedBasePath.
resolved_candidate` — `None` for an already-absolute path, or when the expansion doesn't check out
either, so a genuine miss can never manufacture a false suggestion. The `not_found` box pre-fills
its input with the candidate and states it plainly; an explicit Add is still required. Layered at
the detection layer specifically, not by touching `remote_directory_error`/`resolve_remote_dir`
themselves — decided deliberately, per this task's own instruction not to just make the two
functions identical, since they differ on purpose (one falls back gracefully, one gives a real
answer). A second leak found while in this code (not part of the original finding):
`unverified`'s own "Accept anyway" was handing a raw `~` string straight through as the saved
`path` with no absoluteness check — fixed the same way, falling back to the same ask-for-the-
SSH-visible-equivalent box for a non-absolute `client_path` in that state too. Covered by six new
`tests/test_clients_detection.py` cases and three new `ClientsTab.test.tsx` cases (the pre-fill,
the `unverified` guard, and its absolute-path regression check).

---

## 2. ROOT CAUSE — a client with no category→queue mapping contributes nothing, silently

**This one explains findings 3, 4 and 5 below. Fix it first; several "bugs" may evaporate.**

**Measured on the live box, 2026-08-23:**

| id | name | type | enabled | version | probed | base paths | **categories** |
|---|---|---|---|---|---|---|---|
| 1 | ultracc SAB | sabnzbd | yes | 5.1.1 | 13:22 | **0** | **0** |
| 2 | ultracc rtorrent | rtorrent | yes | 0.9.8 | 13:28 | 1 | **0** |

Both clients authenticate and report. **Neither has a single category→queue mapping**, and
attribution runs entirely through that mapping (spec §8.3). Per the Preflight source's own rule —
inherited from `core/preflight.py` and the *arr source — *"a row that cannot be attributed is
**silently omitted**"*.

So every row both clients produce is discarded before it reaches the UI. The only Preflight source
contributing anything is the *arr, which is exactly what the user observed.

**The rule is right for the *arr source and wrong here.** For the *arr, an unattributable queue
record genuinely is noise — a release for a queue lftpweb does not manage. For a *configured,
authenticating, explicitly-enabled* download client, silence is the worst possible outcome: a
fully-working client is indistinguishable from a broken one, and nothing anywhere says why.

**What it should do instead (design, not yet decided):**

- Surface "this client reports N items, none attributable to a queue" somewhere visible — the
  Clients row, a Preflight banner, or both. The mount-gate banner (one line per blocked queue, not
  fifty identical rows) is the existing precedent for this shape.
- Consider whether the setup flow should *require* or at least strongly prompt for a category
  mapping before an instance can be enabled — an enabled client with no mappings does nothing at
  all, which is unlikely to ever be what someone meant.
- The category→queue inference offer already exists and was not used here. Worth asking whether it
  should run automatically on first successful probe rather than waiting to be clicked.

### Reinforcing observation — there are no client events at all

> *"There doesn't seem to be any event entries for rutorrent and sab."*

Confirmed against the live box: the only client event ever written is the single
`client_auth_failed` from 13:19, before the API key was corrected. Two working, authenticating,
enabled clients have produced **zero** events between them.

**This is currently by design, and the design is wrong.** `core/clientsync.py` writes events only
on *failure transitions* — mirroring `core/arrsync.py`, which likewise writes nothing per poll.
But the *arr earns that silence: it emits `arr_matched`/`arr_notified`/`arr_imported`/`arr_cleanup`
at meaningful lifecycle points, so a working *arr integration is richly visible in the log. The
client poller has no equivalent, because every event it *could* emit is currently gated off:

| Event that would prove the client is working | Why it never fires |
|---|---|
| a Preflight row appearing / handing over | no category mapping (#2) — every row dropped |
| settle-gate skip | checkbox off by default (stage 2b) |
| auto-queue withheld | checkbox off by default (stage 3) |

So a correctly-configured, fully-working client is **completely invisible** — no rows, no events,
no status anywhere. The only thing that ever proves the integration is alive is it breaking.

That is the same failure as #2 stated more strongly, and it argues for at least one positive
signal: a last-poll outcome on the Clients row (see also the auth-failure visibility gap from the
same session), or a first-successful-poll event per instance, or both. **Do not solve it with a
per-poll event** — a 10-second cadence would bury the log, which is exactly why `arrsync.py`
doesn't do it either.

### Resolved 2026-08-23 — a banner, a status column, and one first-success event; no per-poll noise

`prompts/done/2026-08-23-tilde-and-visibility.md`, `docs/decisions.md`, spec §9.3. All three asks
above, built as named, none of them a per-poll event:

- **The unattributed-clients banner.** `ClientSyncScheduler.unattributed_clients` counts, per
  pass, how many Preflight-eligible transfers an enabled instance couldn't attribute — never `0`
  for a quiet client. `GET /api/queue/preflight`'s `unattributed_clients` surfaces it in the
  mount-gate banner's own shape, one line per affected client: *"SABnzbd: reports 2 items, none
  attributable to a queue — check its category → queue mapping..."*
- **Last-poll outcome on the Clients row, not just last Test.** Migration 029 adds `last_poll_at`/
  `last_poll_ok`/`last_poll_message`/`last_success_at` to `download_client`, written every actual
  poll attempt — a status column, not a log entry, so the "not per failed pass" event rule doesn't
  govern it. `last_poll_message` reuses `_FAILURE_VERB`'s own wording, so a rejected credential
  reads as that on the row, never as "unreachable." `last_success_at` is independent of the other
  three, so a currently-failing instance that worked yesterday still shows when.
- **One positive signal.** `client_poll_first_success` fires once, the first time an instance's
  poll succeeds in a process's lifetime — never on any later successful pass, asserted directly
  across five consecutive polls (`test_first_success_event_fires_once_never_per_poll`).

Also fixed while in this code: finding #5's "settling and SAB said nothing" reads identically to
the settle-gate skip being off (default). `lib/format.ts.settleWaitLabel` — shared by the Files
tree, the lifecycle icon tooltip, and the Preflight chip tooltip — now appends "(download-client
verdict skip is off)" whenever `client_skip_enabled` is `false`.

Covered by `tests/test_clientsync.py` (`unattributed_clients` for an unmapped client, a quiet
client's omission, an out-of-set instance, poll-status persistence for both outcomes, the
never-polled default, and the one-event-not-per-poll assertion), `tests/test_preflight_api.py`
(the banner's own endpoint wiring, two cases), `tests/test_settings_clients_api.py` (a fresh
instance reports never-polled), and five new `ClientsTab.test.tsx`/`format.test.ts` cases for the
Clients-row status states and the settle-skip note.

### Round 4 (2026-08-23) — the banner now names which categories, not just how many

`prompts/done/2026-08-23-path-attribution-and-category-escape-hatch.md`. Live evidence during
this round: rTorrent reported 2 unattributable items while the user already had `ar-tv` mapped --
the count alone gave no way to tell what else needed mapping. `ClientSyncScheduler.
UnattributedClientInfo` widens the banner's own source with `categories: tuple[str, ...]` (the
distinct category names actually seen among this pass's unattributable items) and
`no_category_count: int`, counted separately -- "reported a category with no mapping" and
"reported no category at all" are different problems with different fixes, and folding them into
one number sends a user chasing a mapping that was never the issue. The banner now reads *"reports
2 items in ar-movies, 1 with no category, none attributable to a queue."* This round's other
headline fix (below) also changes what reaches this banner at all: an item now only lands here
once *both* path attribution and the category mapping have failed it, not the category mapping
alone. Covered by two new `tests/test_clientsync.py` cases (the category breakdown, the
no-category count kept distinct), one widened `tests/test_preflight_api.py` case, and two new
`lib/preflight.test.ts`/`PreflightBox.test.tsx` cases for the composed message.

---

## 3. Preflight shows the *arr's status, not the client's — and only one source icon

> *"They show up in the list. However they seem to be getting status from sonarr not sab — as when
> sonarr updates, that is when we seem to update. We should show a sonarr AND a SAB icon and have
> the latest status from SAB. This is the thing I told you yesterday."*

Almost certainly a **consequence of #2** — with no client rows reaching the merge, spec §9.2's
precedence rule has nothing to prefer, so the *arr row stands unopposed. **Re-test after #2 before
treating this as a separate defect.**

**But two parts of it are genuinely not built and will remain after #2 is fixed:**

- **Two source icons on one row.** `PreflightRow` carries a single `source`/`source_kind` pair
  (`core/preflight.py`), so a row deduped across the *arr and a client can only display one badge.
  Showing "Sonarr *and* SAB" needs the row shape widened — the first change to that deliberately
  minimal dataclass in six tasks, so it deserves care rather than a quick field.
- **The user asked for this yesterday** (spec §9.2 exists because of it). §9.2 specifies the
  *precedence* correctly but says nothing about *provenance display* — that a merged row should
  show both contributors. That is a genuine spec gap, not just a missing implementation.

### Resolved 2026-08-23 — status precedence already worked; provenance display was the real gap

`prompts/done/2026-08-23-preflight-provenance-and-ui.md`, `docs/decisions.md`, spec §9.2's own new
note. **Re-verified before changing anything, as this finding's own text asked**: `tests/
test_preflight_client_merge.py::test_client_status_label_always_wins_when_present` predates this
task and passed unmodified when run in isolation first — §9.2's per-field precedence was never
broken, it simply had nothing to prefer while finding #2 kept every client row from reaching the
merge. The provenance half was genuinely missing and is now built: `core/preflight.py.
PreflightRow` gets its first widening in six tasks, `contributors: tuple[PreflightContributor,
...]` — `()` standalone, both pre-merge views (*arr, then client) on a row `api/jobs.py.
_merge_client_field_into_arr` folds together. `lib/preflight.ts.preflightBadges` renders one badge
per contributor, falling back to the row's own top-level fields when standalone — which also fixed
a smaller related gap found while in this code: a standalone *client* row previously showed no
badge at all (the old `SourceChip` was gated on `source === 'arr'` outright). Covered by six new
`tests/test_preflight_client_merge.py` cases and eight new `lib/preflight.test.ts` cases.

---

## 4. A paused, 60%-complete torrent appears nowhere in Preflight

> *"In rutorrent and sonarr I have an old download that is only 60% complete that doesn't show up
> in preflight anywhere but it is currently paused."*

Possibly #2 again. But worth checking independently, because there are two other candidates:

- `TransferPhase.PAUSED` may not be among the phases the Preflight source projects at all — a
  paused torrent is genuinely "something lftpweb knows about but has no work to do on yet", which
  is Preflight's exact definition, so it *should* appear.
- The *arr side may have stopped reporting it (a stale queue record), leaving nothing to merge
  against.

**A paused-but-incomplete item is the single most useful thing Preflight could show** — it is work
that will never arrive unless someone intervenes, and there is currently no other surface in
lftpweb that would tell you.

**Resolved 2026-08-23** — `prompts/done/2026-08-23-preflight-phase-allowlist.md`,
`docs/decisions.md`. Confirmed as the second candidate named above: `PAUSED` was never on the old
denylist, and had no allowlist to land on either. `core/clientsync.py`'s Preflight filter is now a
named allowlist (`_PREFLIGHT_PHASES`) that includes `PAUSED` explicitly, on exactly the reasoning
this finding gives. Covered by `tests/test_clientsync.py::
test_paused_partial_transfer_appears_in_preflight`. Fixed alongside #12, the same filter wrong in
the opposite direction — see that finding's own resolution note.

---

## 5. Nothing from SAB/rTorrent appears in the transfer queue's expanded detail

> *"Overall it doesn't appear that transfer queue are actually doing anything on the sab or
> rtorrent. Nothing mentioned in the expanded info."*
> *"SAB just completed downloading and we go into settling. We don't seem to be getting any info
> back from SAB on the status."*

Same suspected root cause (#2). Note the settle-gate skip is also **off by default**, so even with
client data flowing, settling would not shorten until that checkbox is ticked — worth stating
plainly in the UI, since "we went into settling and SAB said nothing" is indistinguishable from
"the feature is off".

---

## 6. Preflight rows need an expand-for-detail affordance

> *"It is probably time to add a preflight expand option that shows more detail."*

Preflight rows are deliberately inert and thin (`core/preflight.py`: *"no id, no bytes-done, no
queue position — there is no `item` and no `job` behind a row here"*). An expand showing per-source
detail — which client, which category, the client's own raw status, size/remaining — does not
violate that (it adds no *controls*), but it does mean the row shape carries more than it does
today. Design it against §4.6's "framed as a cache" rule so it does not quietly become a second
source of truth.

### Resolved 2026-08-23 — a chevron toggle unfolding per-contributor detail, no new fetch

`prompts/done/2026-08-23-preflight-provenance-and-ui.md`, `docs/decisions.md`. Each row is now a
`<button>` toggling its own local `expanded` state — no prop, no handler reaching outside the row
component — that unfolds `PreflightRowDetail`, rendering `lib/preflight.ts.
preflightDetailEntries`: one line per contributor (or the row's own single view standalone) with
that source's own `source_label`, raw `status_label`, and its own size/remaining, formatted through
the same `preflightSizeLabel` the row's own figure column already uses. Everything shown was
already on the response this box holds — no second request, keeping §4.6's "framed as a cache"
rule intact — and there is no `onClick` anywhere in the detail panel, matching this finding's own
"add no controls" constraint. "Which category" specifically was left out: `PreflightRow` has no
category field on any source (only `queue_id`/`queue_name`, already shown at the row's top level),
and nothing here fabricates one that isn't there. Covered by three new `lib/preflight.test.ts`
cases, including one asserting the detail entry's own keys carry no id/handler.

---

## 7. Disk review lists files, not torrents

> *"Found 1 debris that looks right. The display of everything else seems right, but since it just
> shows files it is hard to map — it would be better to show Torrents and expand each torrent to
> see details like files etc."*

**The debris detection itself is working** — one candidate, and it looked correct.

The grouping is the problem. `reconcile()` operates per-file by necessity (inode accounting is
per-file, §11.1b), but the *presentation* should roll up to the thing a user thinks in: a torrent
or a release. The data to group by already exists — the claiming client's `content_path` — so this
is a display-layer rollup, not a change to the reconciliation.

Note the two piles group differently and that is not a detail: the **seeding estate** groups
naturally by torrent, while **debris** by definition has no torrent to group under, so it should
group by directory. Do not force one shape onto both.

### Resolved 2026-08-23 — grouped for display only; `reconcile()` still per-file, reclaim total still link-aware

`prompts/done/2026-08-23-preflight-provenance-and-ui.md`, `docs/decisions.md`. `core/disk_review.
py.reconcile()` is unchanged — still per-file, because inode accounting is inherently per-file
(spec §11.1b). The rollup lives entirely in `lib/diskReview.ts`: `groupSeedingEstateByTorrent`
(keyed on `claimed_by_client_id` + the claim's own `claimed_transfer_id`, both newly carried on
`SeedingEstateEntry`/`DiskReviewSeedingEstateOut` — lifted from the `ClientClaim` `reconcile()`
already resolves per file, not a new lookup) and `groupDebrisByDirectory` (parent directory, since
debris has no torrent by definition) — two different functions, on purpose, matching this
finding's own instruction not to force one shape onto both. **The reclaim total stays link-aware
through the rollup**: a group's own total is `freedBytes(group.entries, selected)` — the existing
function, called with the *global* selection set — never a naive per-group `size` sum, because
every candidate still carries its own `link_paths` regardless of which directory group it renders
under. Asserted directly: a hardlinked pair split across two different directory groups (the
seeding-directory copy vs. the completed-folder hardlink, the exact real-world shape this spec
names) still reports zero bytes when only one side is selected
(`diskReview.test.ts::"a group's own reclaim total stays link-aware"`). `DiskReviewPage.tsx` now
shows both piles as expandable group rows — directory + file count + link-aware total for debris,
torrent name + client + plain total for the seeding estate (a plain sum is fine there; that pile is
never selectable and never counted as reclaimable, per its own existing docstring). Covered by one
new backend test (`tests/test_disk_review.py::
test_seeding_estate_entries_carry_their_claims_torrent_identity`) and six new `diskReview.test.ts`
cases.

---

## 8. The edit button disappears after a successful test until the page is reloaded

> *"After testing a site and it passes I lose the edit button till I reload the page."*

Straightforward frontend state bug in `ClientsTab.tsx` — the test-result render path drops the
row's action affordances. Low risk, high annoyance.

### Resolved 2026-08-23 — not a state bug: `overflow-hidden` was clipping the row, not hiding it

`prompts/done/2026-08-23-preflight-provenance-and-ui.md`, `docs/decisions.md`. The Edit/Delete
`<td>` in `ClientsTab.tsx` was never conditionally rendered — full `git log -p` on the file shows
it unconditional on every commit since the table was written. The actual mechanism: a *passing*
Test populates `detected_base_paths` (rendered `open` by default) and the capability readout, both
inside the Test column, which can grow far wider than any sibling column once a real client
reports several base paths/categories — `IntegrationsTab.tsx`'s own Test column never carries this
much, which is why the same wrapper class never caused a visible problem there. The instances
table's wrapper used `overflow-hidden` (the convention every other table on this page, and most in
the app, uses purely to clip border-radius corners, since their content never legitimately
overflows) — which **silently clips**, rather than scrolls, whatever pushes the table wider than
its container, the rightmost column first. Reloading resets `testResults` (in-memory-only React
state), the row narrows back down, and the button reappears — matching "after testing ... and it
passes" and "till I reload the page" exactly. Fixed by changing that one wrapper's class to
`overflow-x-auto`, so wide content scrolls instead of disappearing. This class of bug is invisible
to jsdom, which performs no real layout — the honest reason no earlier component test caught it
despite several sessions exercising the same Test-success path; the regression test added asserts
the wrapper's class directly, the only thing a layout-less harness can pin down. **Separately
checked, as this finding's own follow-up asked, and found not to be a bug**: a Test click while
that same instance's edit form is open does not discard the draft — `recomputeCategoryDraft`
already merges a fresh detection's categories against the current draft (`prev.categories`) rather
than replacing it, so a hand-picked queue binding survives (asserted directly, a new
`ClientsTab.test.tsx` case simulating exactly that sequence).

---

## 9. Settle timing after a client reports completion

> *"If queued in arr and then seen in the rtorrent connector and we see that it completed in
> rtorrent, then setting is probably a 5 second thing — or a wait 5-10 seconds after complete
> before queuing for transfer."*

A refinement of the stage 2b settle-gate skip: rather than skipping the gate outright on a terminal
verdict, hold a short fixed delay (~5–10 s) and then queue. Cheap insurance against a client
reporting "complete" a moment before the last bytes are flushed or a final move/rename lands —
which matters most for rTorrent, where completion and the hardlink into the completed folder are
two separate events (spec §1.1).

Worth deciding whether this replaces the immediate skip or becomes its configurable value.

**Resolved** (`prompts/done/2026-08-23-client-completion-delay.md`): the immediate skip now holds
`core/settle.py.CLIENT_COMPLETION_HOLD_S` (10s, the conservative end of the user's own range)
before it satisfies the gate, measured from the client's own `completed_at` rather than from when
lftpweb noticed the verdict — a completion already older than the hold satisfies it with no added
wait, so this only ever lengthens an already-shortened wait, never a normal one. Falls back to
first-observation only when a connector reports no `completed_at` at all. Shipped as a named
constant, not a setting — see `docs/decisions.md` for the argument.

---

## Working as designed — confirmed by this session, no action needed

- **Saving an enabled client with a bad API key is refused**, with the auth error and the
  "Uncheck Enabled to save anyway and fix it later" hint. This is issue #23's fix, confirmed live.
- **Saving a *disabled* client with a bad password succeeds** (rTorrent) — the deliberate escape
  hatch, working as intended.
- **A bad credential at poll time raises `client_auth_failed`**, distinct from unreachability, once
  per failure streak rather than once per attempt.

---

## 10. "Infer mappings from base paths + queues" proposes nothing — the inference uses the wrong signal

> *"I am not sure why infer mappings from base paths and queue don't resolve anything for me. The
> category is ar-tv and the dir for that is ar-tv."*

**`lib/clientCategoryInference.ts` is behaving exactly as written.** It matches each queue's
`remote_path` against the instance's *configured* base paths and proposes the trailing segment as
the category name. Given the live state (see #2):

- **SAB has zero configured base paths** → the normalized base list is empty → the inner loop never
  executes → zero proposals. This is the whole of the reported symptom.
- **rTorrent's one base path is `~/downloads/rtorrent`** → no queue `remote_path`
  (`/home/crzykidd/downloads/complete/…`) sits under it → no match. Finding #1's unexpanded `~`
  would additionally prevent any comparison from succeeding.

**Immediate unblock (config, not a fix):** add `/home/crzykidd/downloads/complete` as SAB's base
path. It *is* the parent of the category folders, so inference should then propose both mappings.

### The design problem underneath, which that workaround does not address

**Base paths are a proxy for the category mapping, and rTorrent proves the proxy is wrong.**

- For **SAB** the proxy happens to hold: categories are subdirectories of `complete_dir`, so
  `<base>/<category>` path arithmetic recovers them. Spec §8.3's own observation — *"the queue
  remote paths already **are** the client's category folders"* — was drawn from a SAB-shaped
  layout and quietly generalized.
- For **rTorrent it can never hold.** Labels live in `d.custom1` and have no relationship to
  `directory.default` whatsoever. Its base path is the *seeding* directory (`working`), not a
  parent of the completed category folders (`content`). No amount of correct configuration makes
  path arithmetic produce rTorrent's categories — the information simply is not in the paths.

**The direct signal already exists and is not being used.** Both connectors declare
`Field.CATEGORY`; SAB additionally exposes categories in `get_config`, and rTorrent's labels come
back on the same `d.multicall2` the poller already issues. Matching the client's *own reported
categories* against queue names (or their trailing path segments) works for both clients, needs no
base path configured at all, and degrades sensibly: a client reporting no categories proposes
nothing, rather than silently proposing nothing for a reason the user cannot see.

**Worth deciding as part of any fix:**

- Should inference key on the client's reported categories, on path arithmetic, or on both with
  the direct signal preferred? Path arithmetic still has value for a SAB with no items yet — it can
  propose from an empty queue, where a category list cannot.
- **A `list_categories` operation is arguably missing from the §2.1 vocabulary.** Today categories
  are only observable as a *field on transfers*, so a client with an empty queue and empty history
  reports none — exactly the fresh-setup case where inference is most wanted. Adding an operation
  is a vocabulary change and should not be done casually, but this is a real gap.
- Whichever signal is used, **the empty result must explain itself** (#2's theme again): "no base
  paths configured", "the client reported no categories", and "nothing matched" are three different
  answers, and today all three render as a silent no-op.

### Resolved 2026-08-23 — `list_categories` joined the vocabulary; direct signal preferred

`prompts/done/2026-08-23-category-binding-redesign.md`, `docs/decisions.md`. Both open questions
above are answered: inference now keys on the client's own reported categories first
(`Operation.LIST_CATEGORIES`, spec §2.1/§8.3, implemented on both connectors — SAB via
`mode=get_config&section=categories`, rTorrent via the in-use `d.custom1` values, both
doc-derived and UNVERIFIED, §13.4/§13.6), falling back to path arithmetic only when the client
reports none at all, and the UI now states which mechanism produced a row's suggestion rather
than rendering all three "empty" cases as the same silent no-op. See #11's own resolution note
below for the control redesign this enabled.

### Round 4 (2026-08-23) — the underlying premise was still wrong; path beats both signals

`prompts/done/2026-08-23-path-attribution-and-category-escape-hatch.md`, `docs/decisions.md`, spec
§8.3's own round-4 correction. This finding's own conclusion -- "the direct signal (the client's
own reported categories) is better than a path-arithmetic proxy" -- was correct for *inferring a
mapping*, but round 4 found the deeper issue: attribution itself should not go through any
category signal at all, direct or guessed, when a transfer's `content_path` already answers the
question by matching a queue's `remote_path`. `core/clientsync.py._update_preflight` now checks
path first (`core/settle.py._client_content_path_matches`, reused rather than reimplemented),
falling back to the category mapping only for a transfer with no `content_path` yet. **This closes
the SABnzbd side of the gap entirely** (history `storage` lands inside the queue's folder, so a
SAB item needs no category configuration once it has a path) but **not the rTorrent side** --
rTorrent reports its own seeding directory as `content_path` (spec §1.1), a different tree from
the queue's `remote_path` under the common hardlink layout, so its category mapping remains as
necessary as it was before this task. Covered by five new `tests/test_clientsync.py` cases (the
headline no-mapping-needed case, the component-boundary guard, the no-path fallback, the
no-information case, and the path-wins-on-disagreement case with its own log assertion).

---

## 11. Category mappings do not survive a save — and the field is unexplained

> *"I don't actually understand those and you can't save them anyway — they go away when I edit
> again after saving."*

### 11a. The concept is undocumented in the UI

The user could not tell what a category mapping is *for*. That is a UI failure, not a user one:
the section has no explanatory text, and the concept is genuinely non-obvious — it only exists
because a client instance is **site-level** (spec §4.5), so one SAB serves several queues and
something has to say which category belongs to which. Every other consequence in this findings
file (#2, #3, #5, #10) flows from this mapping being absent, which makes it the single most
important field on the page and the least explained.

Any fix should state it in one line where the field is: *"SABnzbd sorts downloads into categories.
Tell lftpweb which of your queues each category belongs to, or its downloads can't be matched to a
queue."*

### 11b. The mappings are lost — and the backend is NOT the cause

**Proven by direct probe against the live box (2026-08-23):** a throwaway *disabled* instance was
created via `POST /api/settings/clients` carrying one base path and one category, read back
complete and correct, then deleted (the user's own two instances were never touched).

So persistence works:

- `_replace_categories` / `_replace_base_paths` write correctly.
- `_get_categories` reads them back.
- `startEdit` (`ClientsTab.tsx`) *does* hydrate `form.categories` from `instance.categories`.
- The category `<input>` is correctly wired (`value={cat.category}` +
  `updateCategoryRow(i, {category: …})`).

**The loss is in the form layer, and the leading hypothesis links it to #8.** Finding #8 reports
that after a successful **Test**, the edit affordance disappears until a page reload — i.e. the
test action disturbs edit-mode state. If Test also discards the in-progress form draft, then any
category typed *before* testing is gone before Save is ever pressed, and the user's two symptoms
have one cause.

**Not yet proven.** The decisive observation, which needs the user: **on save, was an error shown,
or did it appear to succeed?** An appeared-to-succeed save points at an empty `categories: []`
payload (draft lost). A 400 points at the enabled-client connection test rejecting the save
(§3a) — a different bug entirely, and one where the mappings were never sent at all.

Note the save payload deliberately filters rows whose `category` is blank
(`form.categories.filter(c => c.category.trim() !== '')`), so a row that *looks* filled in on
screen but whose state was reset would be silently dropped rather than rejected — consistent with
"it looked like it saved."

**Do not fix #8 and #11b separately until the shared cause is confirmed or ruled out.**

### 11c. RESOLVED — the greyed text was a placeholder, and the user's redesign supersedes the whole control

> *"Ohh ok — so it shows a greyed out 'recommendation' but doesn't tell you it won't save without a
> value. So this interface should be simplified. First, we know the categories. So we should show
> them all on setup and suggest the bindings, and if we aren't using that category the person
> leaves it unbound."*

**The mystery in 11b is solved, and neither hypothesis was right.** The category `<input>` carries
`placeholder="ar-tv"` — greyed text that reads as a filled-in recommendation but is not a value.
The save then filters rows whose `category` is blank
(`form.categories.filter(c => c.category.trim() !== '')`), so the row is silently dropped and the
save "succeeds" having stored nothing. Not the Test-clobbers-draft theory (#8), and not a rejected
save. **#8 remains a separate, still-open bug** — do not close it with this.

### The replacement design (the user's, 2026-08-22/23)

> Show every category the client actually has, propose a binding for each, and let the user leave
> the ones they don't use unbound.

**This is strictly better than the current control, for four reasons:**

1. **It eliminates the defect class, not the defect.** With no free-text field, there is no blank
   row, so there is nothing to silently drop. 11b becomes structurally impossible rather than
   guarded against.
2. **It uses the direct signal** — which is exactly what #10 concluded from the other direction.
   Path arithmetic was a proxy that only ever held for SAB and can never work for rTorrent, whose
   labels live in `d.custom1` with no relation to any directory.
3. **"Unbound" becomes explicit and meaningful.** Today an unmapped category is indistinguishable
   from a category nobody has gotten around to; here it is a deliberate, visible choice — and it
   answers #2's "why is this client contributing nothing" for free, because the user can *see* that
   every category is unbound.
4. **Nothing to type**, so nothing to typo. A mistyped category today fails silently and forever:
   it matches no client output and no error is ever raised.

**This makes the `list_categories` gap real, not theoretical** (#10). Categories are currently only
observable as a *field on transfers*, so a client with an empty queue and empty history reports
none — precisely the fresh-setup case this design is for. The operation needs adding to §2.1's
vocabulary, with the usual care: SAB can answer it from `get_config`, rTorrent from the labels
already returned by the `d.multicall2` the poller issues.

**Worth deciding when this is built:**

- **What if the client reports no categories at all** (fresh SAB, or an rTorrent with no labels)?
  Falling back to the current path-arithmetic proposal is reasonable — but it must say which
  mechanism produced the suggestion, not blur them.
- **Uncategorised items.** rTorrent torrents frequently carry no label. Does an "(no category)"
  pseudo-row bind to a queue, or are such items simply never attributable? The second is more
  honest and matches §8.3's silent-omission rule; the first is more useful. Not obvious — decide
  deliberately.
- **Categories appearing later.** A category added in SAB after setup will not be in the stored
  mapping. The page should show newly-seen-but-unmapped categories rather than requiring the user
  to notice, which is the same visibility theme as #2.

### Resolved 2026-08-23 — free-text control removed; one row per reported category

`prompts/done/2026-08-23-category-binding-redesign.md`, `docs/decisions.md`. Built exactly as
designed above: `ClientsTab.tsx`'s category section is now one row per category the client
reports (or, only when it reports none, a labelled base-path-arithmetic guess), the category name
rendered as plain text, a queue `<select>` defaulting to "— not used —", and a suggested binding
is always a pre-selected dropdown value. **No `<input>` exists in this control any more** — 11b's
proven mechanism (a `placeholder` that read as a filled-in value, silently filtered on save) is
now structurally impossible, since nothing here can ever produce a blank category string to
filter. 11a's missing explanation is now a one-line sentence above the rows. The two "worth
deciding" items are both decided, with reasoning, in `docs/decisions.md`'s 2026-08-23 entry:
uncategorised items get no bindable pseudo-row (silent omission, same rule as any other unmatched
category); a category appearing later is picked up by re-testing while the instance is open in
Edit, not a background poll. Covered by `tests/test_settings_clients_api.py::
test_unbound_category_survives_a_save_and_a_re_edit` (the round-trip regression test this finding
asked for) and `frontend/src/pages/settings/ClientsTab.test.tsx`'s new cases.

### Round 4 (2026-08-23) — the mapping is demoted to a fallback; the escape hatch is back

`prompts/done/2026-08-23-path-attribution-and-category-escape-hatch.md`, `docs/decisions.md`.
Two follow-ons to the redesign above, both from live use:

- **Attribution no longer goes through this control at all when a transfer's path already answers
  the question** -- see #10's own round-4 note and spec §8.3's round-4 correction. The mapping
  this finding redesigned is now genuinely optional for a connector like SABnzbd once a transfer
  has a path; it remains load-bearing for rTorrent, unchanged.
- **The manual "Add category" row is back**, deliberately not as a regression to the free-text
  field 11b/11c eliminated: `rtorrent.list_categories` is `DERIVED` and can only report a label
  *currently in use*, so a category that will exist later can never be detected, and the redesign
  above removed the only way to enter one. The new control adds a category as its own row
  (`source: 'manual'`, mirroring base paths' identical escape hatch), never as free text mixed
  into the detected rows -- **a blank or duplicate name is rejected visibly** (`addCategoryRow`),
  so 11b/11c's defect class cannot reappear at this one remaining place a typed string could
  originate. Detected categories also now persist across a reload (migration 030) -- see #14's own
  round-4 note, which is where the reload-shows-nothing complaint was actually raised.

---

## 12. Preflight shows every seeding torrent — the phase filter is a denylist

> *"Now that I mapped ar-tv, preflight shows all my seeding torrents. That shouldn't be the case."*

**Confirmed, with an exact cause.** `core/clientsync.py`'s Preflight projection filters by:

```python
if transfer.phase in (TransferPhase.COMPLETED, TransferPhase.FAILED):
    continue
```

`TransferPhase.SEEDING` is not excluded. An rTorrent seeding torrent maps to `SEEDING` (not
`COMPLETED`), and rTorrent reports it as **active** (`d.is_active`), so `active_only=True` returns
it as well. It passes both guards and becomes a Preflight row.

**The stated assumption is the bug.** That filter's own comment calls itself *"defensive only --
every connector's `active_only=True` contract already excludes terminal transfers"*. That holds for
**SAB**, where finished work leaves the queue for history. It is **false for rTorrent**, where
finished work stays in the list and seeds indefinitely. The assumption was drawn from the usenet
client and generalized to a torrent client whose whole lifecycle differs — the same shape of error
as §8.3's category-folder assumption (#10) and §9.1's cadence split.

### The fix is an allowlist, not one more exclusion

Adding `SEEDING` to the denylist repeats the mistake one phase later. Preflight's definition
(`core/preflight.py`) is *"something lftpweb already knows about but has no work to do on yet"* —
i.e. **work that is coming**. Over a closed nine-value enum, the filter should enumerate what
qualifies, so a phase nobody considered is excluded by default rather than admitted by default.

Candidate allowlist — decide deliberately, do not copy:

| Phase | In Preflight? | Reasoning |
|---|---|---|
| `QUEUED`, `DOWNLOADING` | **yes** | work plainly coming |
| `VERIFYING`, `EXTRACTING` | **yes** | post-download steps before it lands |
| `PAUSED` | **probably yes** — see #4 | it *is* known-but-not-arriving; #4 reports a paused 60% torrent missing from Preflight entirely, which this same allowlist would fix |
| `SEEDING` | **no** | nothing is coming; this is the estate, and it belongs to Disk review / #21 (spec §11.1d's two-piles distinction) |
| `COMPLETED` | **borderline** | complete-but-not-yet-handed-over *is* incoming; already handed over is not. Governed by retirement-on-handover, not by the phase filter |
| `FAILED` | **no** | nothing coming (and stage 3's withhold is the surface for it) |
| `UNKNOWN` | **no** | §4.2: unknown never blocks, and it should not populate either — a row asserting nothing helps nobody |

**Note this and #4 are probably one fix.** #4 (paused torrent invisible) and #12 (seeding torrents
all visible) are the same filter being wrong in both directions at once.

**Also note the seeding estate is not homeless** — it is exactly what Disk review's second pile
(§11.1d) is for. This is a routing error, not missing functionality.

**Resolved 2026-08-23** — `prompts/done/2026-08-23-preflight-phase-allowlist.md`,
`docs/decisions.md`. `core/clientsync.py`'s Preflight filter is now a named allowlist
(`_PREFLIGHT_PHASES`: `QUEUED`, `DOWNLOADING`, `PAUSED`, `VERIFYING`, `EXTRACTING`) rather than a
denylist. `SEEDING` is now excluded — this finding's own fix. Covered by
`tests/test_clientsync.py::test_seeding_transfer_produces_no_preflight_row` and, end-to-end
against a real rTorrent-shaped fixture reproducing the exact live scenario, `test_rtorrent_
active_only_true_admits_only_incoming_rows`. Also fixed alongside #4, the same filter wrong in
the opposite direction — see that finding's own resolution note.

---

## 13. The unattributable-client banner names a nonexistent page and isn't a link

> *"When we have this error — `ultracc rtorrent: reports 2 items, none attributable to a queue —
> check its category → queue mapping in Settings → Integrations → API Clients` — we should link
> right to the place to set [it]."*

Two defects in one sentence of copy (`frontend/src/components/PreflightBox.tsx:227`):

**13a — the breadcrumb is wrong.** There is no "Settings → Integrations → API Clients". `nav.ts`
has two separate tabs: `/settings/integrations` labelled **Integrations** (Sonarr/Radarr) and
`/settings/clients` labelled **Clients** (download clients). The banner sends the user to a page
that does not exist, and to the *arr tab if they follow it literally. This is almost certainly an
echo of the user's own eventual "API Clients" unified-page idea (spec §8.1), written into shipped
copy before that page exists.

**13b — it should be a link, not an instruction.** The banner already knows *which* instance is
unattributable (`unattributed_clients` carries the client id and name), so it can deep-link
straight to that client's own row — not merely the tab. Telling someone the path to a settings page
they must then navigate by hand, when the app knows exactly which record needs editing, is the
avoidable half of the problem.

**Worth doing properly while there:**

- Deep-link to the **specific client**, ideally opening it in edit mode. `/settings/clients` alone
  still leaves the user to find the right row.
- **Audit for the same wrong breadcrumb elsewhere.** If this copy was written once from the
  imagined page name, it may appear in other strings, help text, or the README.
- Keep the banner's shape — one line per affected client, never one per dropped row (the
  mount-gate precedent).
- Note this is the **third** finding in this session caused by shipped text describing something
  that isn't real (the placeholder that looked like a value, #11c; the stale
  "`active_only=True` excludes terminal transfers" comment, #12; and now this). Worth a moment's
  thought about whether user-facing copy naming a navigation path should be generated from `nav.ts`
  rather than hand-written, so it cannot drift from the real routes.

### Resolved 2026-08-23 — a real deep link, not corrected prose; a second occurrence found and fixed

`prompts/done/2026-08-23-category-control-and-banner-link.md`, `docs/decisions.md`. The banner
(`PreflightBox.tsx`) now renders a react-router `<Link to={clientEditHref(u.client_id)}>` straight
to the specific instance, opened in edit mode -- not corrected prose naming a tab. Getting there
required widening the data the finding's own prose assumed already existed: `client_id` did not
reach the banner before this task (`ClientSyncScheduler.unattributed_clients` returned only `(name,
count)`, and `PreflightUnattributedClientOut` had no id field), so it was threaded through
`core/clientsync.py`, `models.py`, and `api/jobs.py`. `ClientsTab.tsx` reads the resulting
`?edit=<id>` via `useSearchParams`, calls its own `startEdit` once instances have loaded, then
clears the param; a stale or malformed id degrades to a no-op, never a crash. The audit this
finding asked for turned up one more occurrence of the same wrong breadcrumb --
`TransferTab.tsx`'s settle-skip help text -- corrected to "Settings → Clients" in the same change.
Generating nav copy from `nav.ts` was considered and deliberately deferred: after this fix only one
hand-written breadcrumb string remains anywhere in the frontend, and building a generator to guard
a single string is more machinery than the remaining problem justifies (see `docs/decisions.md`'s
own reasoning). Covered by `tests/test_clientsync.py` (the widened tuple), `tests/
test_preflight_api.py` (the widened response field), `frontend/src/lib/clientEditLink.test.ts`
(the href builder/parser), `frontend/src/components/PreflightBox.test.tsx` (the link itself, and
its old text no longer appearing), and two new `ClientsTab.test.tsx` cases (the deep link opening
edit mode, and a stale id being ignored).

---

## 14. The new category control doesn't read as a mapping

> *"I don't understand the category queue mapping now. I only see a drop down list with them in
> it."*

The redesign (#11c) fixed the *data* problem — no free-text, nothing silently dropped — and
introduced a *presentation* one. Each row renders as
`[category chip] [queue dropdown] [Remove]`, but:

**14a — no column headers.** Nothing labels which side is the category and which is the queue. The
prose hint above explains the concept, but the control itself is three unlabelled controls in a
row, so it reads as a list rather than a mapping.

**14b — CONFIRMED BY SCREENSHOT: the category chip is crushed to one character.**
(`private_data/screenshots/Screenshot 2026-08-23 110515.png`, supplied by the user.) An earlier
reading of this finding guessed the row was *wrapping*. It is not. The row renders as a sliver
reading `a`, then a full-width `<select>`, then `Remove`.

**Cause:** `inputClasses` (shared by every text input on the page) contains **`w-full`**, and it is
applied to the `<select>` inside a `flex` row. The select demands the full row width and wins
against the category chip's `flex-1 truncate`, collapsing it to a single character.

**This is why the user said "I only see a drop down list with them in it."** The one legible
control shows *queue* names, so the control reads as a list of queues with no visible category
side at all — the mapping's left-hand operand is invisible.

The over-long option text (`{q.name} ({q.remote_path})`) compounds it but is not the cause. The
queue *name* is the identifier a user thinks in; the path belongs in a `title` tooltip or muted
secondary text, not inline in a `<select>` option.

**Method note worth keeping:** two guesses from the reported text (wrapping, then mis-binding) were
both wrong, and one screenshot settled it immediately. For any layout complaint, ask for the image
first — jsdom has no layout engine, so neither the tests nor the code can show this.

**14c — "Remove" is offered on categories the client currently reports, where it means nothing.**
You cannot remove a category SAB has — leaving it unbound is the way to ignore it, which is exactly
what the redesign intends. Removing it just makes the row reappear on the next Test.

`computeCategoryRows` **deliberately preserves a stored mapping for a category the client no longer
reports** (its own docstring: *"a stale mapping is still real configuration the user should see —
and can remove — rather than lose"*). That is the **only** case where Remove is meaningful. So the
button should appear only on stale rows, and those rows should say why they are different — e.g.
*"not currently reported by this client"*.

**Worth doing together:**

- Header row (`Category` / `Queue`), or an explicit per-row `→` so the direction is visible.
- Queue options show the name; the path moves to a tooltip or muted secondary text.
- `Remove` only on stale rows, with a marker explaining the row's status.
- Re-check the row cannot clip at narrow widths — #8 and #14b are both "content wider than its
  container, silently truncated", and jsdom cannot catch either (no real layout), so this needs
  eyes on a real browser rather than a test.

### Resolved 2026-08-23 — all four sub-findings addressed; the layout fix is UNVERIFIED without a browser

`prompts/done/2026-08-23-category-control-and-banner-link.md`, `docs/decisions.md`. Every "worth
doing together" item is built:

- **14a** — a header row (`Category` / `Queue`) above the list, plus a per-row `→`, both hidden
  from assistive tech (`aria-hidden`) since the labels are decorative alongside the `<select>`'s
  own accessible name.
- **14b** — the crushed chip's root cause (`inputClasses`'s `w-full` winning inside the flex row)
  is fixed by wrapping the `<select>` in its own fixed-width `<span className="w-48 shrink-0">`,
  scoped to this one control -- `inputClasses` itself is untouched, so nothing else on the page
  moves. **This is UNVERIFIED against a real browser.** jsdom performs no layout at all (this
  finding's own "Method note" said as much), so no test in this repo can prove a `flex`/width
  computation renders correctly -- the same honest limitation #8's own resolution note already
  recorded for an identical class of bug. The regression tests added assert content (both category
  names present in full, in DOM order) and structure (a fixed-width wrapper class exists), which is
  the most a layout-less harness can pin down; a human needs to look at the actual rendered row
  before this is called done.
- **14c** — `Remove` now renders only on a stale row (`isStaleCategoryRow`, a new pure function
  computed from `testResults[editingId]?.detected_categories` at render time, not stored on
  `CategoryRowDraft`), paired with "Not currently reported by this client — its queue binding is
  kept until you remove it." A category the client still reports shows neither.
- **Queue options** render the queue name only; the full `name (remote_path)` moved to a `title`
  attribute on both the `<option>` and the `<select>` itself.
- **The detected-categories hint** (screenshot evidence, the same image that settled 14b) is
  reworded rather than the categories being persisted -- see `docs/decisions.md`'s own reasoning
  for why persistence was judged disproportionate to the actual problem (wording, not data loss).

Covered by `frontend/src/lib/clientCategoryInference.test.ts` (`isStaleCategoryRow`, the reworded
hint, the untouched two-argument default) and two new `ClientsTab.test.tsx` cases (headers +
stale-vs-live Remove visibility, and the option text/title split). **What remains unverified**:
the actual crushed-chip layout, at real and narrow browser widths, and the header row's alignment
against the rows below it -- both need a human with a browser, not another test.

### Round 4 (2026-08-23) — reversed: detected categories now persist, on the user's own evidence

`prompts/done/2026-08-23-path-attribution-and-category-escape-hatch.md`, `docs/decisions.md`. The
"reworded rather than persisted" decision immediately above is reversed here, explicitly on new
evidence rather than a change of mind in the abstract: the user showed a saved instance's category
rows reading as data loss on reload, which a reworded hint cannot fix for anyone who has not yet
clicked Test again this session. Migration 030 adds `detected_categories_json`/
`detected_categories_at` to `download_client`, written on every successful Test
(`api/settings_clients.py._persist_detected_categories`) and read back into `DownloadClientOut`.
`ClientsTab.tsx` now falls back through three sources in order -- this session's `testResults`,
then the instance's own persisted value, then `null` ("never tested, ever") -- and shows the
persisted value's age when that is the one in use. Also fixed while in this code: the manual "Add
category" escape hatch (see #11's own round-4 note) is tagged `(manual)` and always removable,
distinct from a detected row's staleness-gated Remove. Covered by
`tests/test_settings_clients_api.py` (persisted-categories round trip, the "never tested" vs.
"tested and found none" distinction) and four new `ClientsTab.test.tsx` cases (the manual add, the
visible blank/duplicate rejection, and the persisted-categories-survive-a-reload case).

---

## 15. "Not used" must be an explicit saved state, not the absence of a mapping

> *"You have to plan for having a category you see in SAB or torrents not being used with lftp. So
> it is best to show all the categories and then map them to a path or flag them as 'Not used by
> this instance'."*

**The current design collapses two different states into one.** A category row's dropdown defaults
to `— not used —`, which is also what a never-configured category shows. So lftpweb cannot
distinguish:

| State | Should the banner warn? |
|---|---|
| Not configured yet — the user has not looked at it | **yes** |
| Deliberately not used by lftpweb | **never again** |

Today both render identically and both count toward the "reports N items, none attributable"
banner. **Consequence: once a user decides a category is irrelevant, they get nagged about it
forever**, with no way to silence it short of inventing a queue binding they do not want.

### What this requires

- **Show every category the client reports**, not only those already saved or detected in the
  current browser session (see #11c's persistence problem, and the reload-shows-nothing case).
- Each category resolves to one of **three** states, saved explicitly:
  1. bound to a queue,
  2. **explicitly marked "not used by this instance"**,
  3. undecided (the only state that warns).
- **The banner counts only state 3.** A client whose every category is bound or explicitly excluded
  is fully configured and must be silent — that is the whole point.
- A category that appears *later* (rTorrent labels are `DERIVED` — only ones in use are reportable)
  arrives in state 3, which is correct: it genuinely is undecided, and the banner surfacing it is
  the desired behaviour rather than noise.

### Why this matters beyond tidiness

It is the difference between a warning that means something and one the user learns to ignore. The
"none attributable" banner exists because finding #2 showed a silently-contributing-nothing client
is indistinguishable from a broken one. A banner that cannot be resolved is the same failure with
the opposite sign — it stops carrying information.

**Sequenced with the per-client relevance display** (SAB: *"12 of 12 matched by folder — no mapping
needed"*; rTorrent: *"0 of 2 matched — mapping required"*), since both change what this section
says about an unmapped category and splitting them would mean writing that copy twice.

### Resolved 2026-08-23 — three-state, persisted; the banner counts only undecided

`prompts/done/2026-08-23-category-tristate-and-exclusion.md`, `docs/decisions.md`, spec §8.3
round 5. Built exactly as specified: migration 031 adds `download_client_category.excluded`
(mutually exclusive with `queue_id`, enforced by `DownloadClientCategoryIn`'s own
`model_validator`, not a table-level `CHECK` — SQLite's `ADD COLUMN` can't add one without a full
rebuild). `core.clientsync.ClientSyncScheduler._update_preflight` now consults each instance's
excluded-category set and skips it before it can reach `unattributed_clients`'s own count —
asserted directly (`test_client_fully_bound_or_excluded_produces_no_banner`): a client whose
every category is bound or explicitly excluded produces **no banner entry at all**, not a reduced
one. A category appearing later still arrives undecided and still warns
(`computeCategoryRows`/`withQueueSelection`/`withExcludedToggle` on the frontend keep the
mutual-exclusion rule identical to the backend's). The per-client relevance copy this finding
asked to be sequenced with landed in the same task — see #16's own resolution note, since the two
were built together. Covered by three new `tests/test_clientsync.py` cases, two new
`tests/test_settings_clients_api.py` cases (the round trip and the 422 on setting both), and new
`clientCategoryInference.test.ts` cases for the mutual-exclusion helpers.

---

## 16. TWO lftpweb instances share one seedbox — "not used" is a safety boundary, not a preference

> *"My seedbox goes to 2 different download locations. SAB and rtorrent see them all, but
> sonarr/radarr don't, cause the other sonarr/radarr is at a different location running a different
> instance of lftpweb."*

**This is a deployment shape the entire framework was designed without knowing about, and it
changes the stakes of #15.**

The shape: **one seedbox, one SABnzbd, one rTorrent — but two independent lftpweb instances**, each
with its own *arr pair, each responsible for a different subset of the download locations. Both
lftpwebs see *everything* the clients report, because the clients serve both.

So **this instance permanently sees work that is not its business**, by design, forever. That is
not a misconfiguration to be cleaned up; it is the steady state.

### Consequence 1 — #15 stops being cosmetic

A category belonging to the *other* instance is exactly the "deliberately not used by lftpweb" case.
Without an explicit saved state for it, the "none attributable" banner nags permanently about work
this instance is correctly ignoring, and the warning becomes noise.

### Consequence 2 — the disk review scan can propose the other instance's data as debris

**This is the serious one, and it is a latent data-loss path once stage 5 exists.**

The scan (§11) proposes `B − A − C`: on disk, unclaimed by any client, unused by *this* lftpweb.
The other instance's content is currently protected only by set **A** — SAB/rTorrent still claim it,
and set A is a union across clients (§11.1a).

**That protection is temporary.** As soon as the other instance imports a release and SAB drops it
from history — or its torrent is removed — that content is claimed by nobody *this* instance can
see. Set C only knows about this lftpweb's own items. The content becomes, by the scan's own
arithmetic, indistinguishable from debris.

Stage 5 would then offer to delete another site's data, with a correct-looking reclaim figure and
no signal anything was wrong.

### What "not used by this instance" must therefore mean

Not one thing, but **two**:

1. **Do not warn** about it (the #15 banner behaviour), and
2. **Never scan it, never propose it, and never let it fall inside a delete containment
   boundary** — a hard exclusion from `core/disk_review.py`'s walk and from §10.2's containment
   check.

The second is the load-bearing half. A flag that only silences a banner would leave the delete path
exactly as dangerous while *appearing* to have addressed the problem.

### Open questions this raises — do not answer them by assumption

- **Is category the right exclusion unit, or is it path?** The other instance's content is
  identified by *where it lands*, and a category is only a proxy for that. An excluded **base path**
  (or sub-path) may be the more direct and safer expression, with category exclusion as
  convenience. This deserves deciding explicitly.
- **Should two lftpwebs sharing a seedbox be a first-class documented deployment** (README, spec
  §1.1's reference workflow) rather than an emergent surprise? It is plainly a real topology, and
  every "unclaimed means safe to remove" assumption in §11 was written without it in mind.
- **Does the same hazard exist for `move`-mode source deletion today**, independent of stage 5?
  That path deletes `<queue.remote_path>/<rel_path>` on confirmed import and is scoped to this
  instance's own items, so it looks safe — but it should be *confirmed* safe rather than assumed,
  now that we know two instances share a tree.
- Should the scan **refuse to run at all** on a base path known to be shared, until exclusions are
  configured? Failing closed is the house style for anything that deletes.

### Resolved 2026-08-23 — path exclusion as the enforceable primitive; fail-closed where it can't resolve

`prompts/done/2026-08-23-category-tristate-and-exclusion.md`, `docs/decisions.md`, spec §8.3
round 5/§10.2/§11.2. **Category is the wrong exclusion unit on its own, decided explicitly** (the
first open question above) — implemented as path exclusion (`download_client_excluded_path`,
migration 031, the enforceable primitive, addable directly in Settings → Clients) with category
exclusion as a convenience that resolves into it wherever spec §1.1's `<base>/<category>` layout
holds (`core/disk_review.resolve_category_exclusion_paths`, run against a client's own
`content`-kind base paths). `core/disk_review.py.reconcile()` takes a new `excluded_paths`
parameter and drops every entry under one before any candidate/claim logic runs — never proposed
as debris, never shown as seeding estate, never counted as a broken seed
(`test_excluded_path_is_never_proposed_as_debris`,
`test_excluded_path_is_also_never_shown_as_seeding_estate`,
`test_excluded_path_suppresses_broken_seed_reporting_too`).

**The hard part's own rule, built literally:** a client with no `content`-kind base path at all
(rTorrent, under the reference layout — its only declared base path is the seeding/`working`
directory) cannot have an excluded category resolved to a path. `run_scan` fails closed by
suppressing debris for **every one of that client's declared base paths**, via the existing
`unavailable_roots` mechanism (§11.1a's own "a contributor that didn't report blocks the whole
root," reused rather than reinvented) — and because that mechanism is populated *before* the walk
loop runs, the fail-closed case is genuinely never scanned at the SSH level, not merely discarded
after (`test_resolve_client_exclusions_fails_closed_with_no_content_base_path`,
`test_excluding_a_whole_base_path_protects_everything_under_it`). A manually-excluded *sub*-path
is still walked over the wire and filtered inside `reconcile()` — pruning the remote `find`
command itself was judged out of scope (it touches `core/remote.py`, load-bearing) and is named,
not hidden, in `docs/decisions.md`.

**§10.2's future containment check got its seed now, not deferred to stage 5:**
`core/disk_review.py.is_authorized_delete_target(path, base_paths, excluded_paths)` is pure,
unit-tested, and unused by anything yet — stage 5's own delete sequence must call it rather than
re-derive containment. The second open question above (should this be a first-class documented
topology) is answered in the affirmative by this task's own spec §8.3 round-5 section, which
states the two-instance shape as the reason the whole correction exists; a README/reference-
workflow write-up specifically for it was judged out of this task's scope and is not yet done.
The third question (does `move`-mode source deletion have the same hazard) was not investigated
here — it remains open. The fourth (should the scan refuse to run at all on a shared base path)
was answered narrowly rather than broadly: it refuses **debris on that specific path**, not the
whole scan, since the seeding-estate pile and other, unrelated base paths are still useful to see.

Covered by fourteen new `tests/test_disk_review.py` cases (the `reconcile()`-level exclusion
behaviour, `resolve_category_exclusion_paths`, `is_authorized_delete_target`, and the pure
`_resolve_client_exclusions` derivation step in isolation from any database or SSH connection)
and two new `tests/test_settings_clients_api.py` cases (the excluded-paths CRUD round trip and
cascade-delete). **Not verified against the user's real two-instance deployment** — see this
task's own final report for what that leaves standing before stage 5.

---

## 17. Ambiguous items should be SHOWN as a third "unclaimed" pile, not suppressed

> *"I think we should actually show these as 'unclaimed' so the user can see them and possibly act
> on them, but we call out that this is not the norm and requires a confirmation dialog or
> something. Things can show up in weird categories etc — we might want to clean up."*

**This corrects the reading of "fail closed" used in findings #16 and the round that followed it.**

Fail-closed was implemented as *do not show* — a base path with unresolvable exclusions had its
debris suppressed entirely, and the user saw only a line saying so. But **suppression is the same
failure as finding #2**: content that exists and is never surfaced is indistinguishable from
content that isn't there, and the user cannot act on what they cannot see.

> **Fail-closed should mean "never act without explicit confirmation", not "never display".**

### The shape

Three piles, not two (extending §11.1d):

| Pile | What | Selectable? |
|---|---|---|
| **Debris** | unclaimed by any client, unused by lftpweb, in a resolvable path | yes |
| **Seeding estate** | claimed and seeding — informational | no (#21's territory) |
| **Unclaimed** *(new)* | ownership genuinely undeterminable — unclaimed, in a tree where exclusions cannot be resolved to paths | **only behind an explicit gate** |

The unclaimed pile must **state plainly why it is abnormal**: in a single-instance setup it should
be empty or near-empty, and a populated one usually means either debris from an interrupted
operation or another lftpweb instance's content (finding #16).

### On the gate mechanism — decide deliberately

The user said "a confirmation dialog **or something**". Note a **standing preference against
confirmation dialogs** recorded earlier in this project (the pause/bandwidth controls were
deliberately built as a checkbox plus a debounced auto-commit plus a result banner, *never* a
confirm dialog). This is plausibly the exception that earns one — the failure mode is deleting
another site's data — but it is worth choosing between:

- a distinct action, visually separated, that cannot be reached by the normal select-and-remove
  flow (accident-proof without being repetitive), or
- an actual confirmation step naming what is unresolvable and why.

**Ask or decide explicitly; do not default to a modal because it is easiest.**

### Why this matters beyond convenience

The user's own reason — *"things can show up in weird categories, we might want to clean up"* — is
the real one. A seedbox accumulates content from aborted grabs, renamed categories, and manual
operations. That material is **exactly** what the disk review exists to find, and it is precisely
the material most likely to be unattributable. Suppressing the ambiguous pile suppresses the
feature's most valuable output.
