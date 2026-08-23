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

---

## 8. The edit button disappears after a successful test until the page is reloaded

> *"After testing a site and it passes I lose the edit button till I reload the page."*

Straightforward frontend state bug in `ClientsTab.tsx` — the test-result render path drops the
row's action affordances. Low risk, high annoyance.

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

---

## Working as designed — confirmed by this session, no action needed

- **Saving an enabled client with a bad API key is refused**, with the auth error and the
  "Uncheck Enabled to save anyway and fix it later" hint. This is issue #23's fix, confirmed live.
- **Saving a *disabled* client with a bad password succeeds** (rTorrent) — the deliberate escape
  hatch, working as intended.
- **A bad credential at poll time raises `client_auth_failed`**, distinct from unreachability, once
  per failure streak rather than once per attempt.
