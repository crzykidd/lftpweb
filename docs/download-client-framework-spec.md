# Download-client connector framework — spec

**Status: proposal. Nothing here is built.** Same convention as
[`docs/transfers-redesign-spec.md`](transfers-redesign-spec.md) and
[`docs/torrent-manager-spec.md`](torrent-manager-spec.md): this is a *proposal document*, not a
description of reality. `DESIGN.md` continues to describe what exists.

This is [#18](https://github.com/crzykidd/lftpweb/issues/18). It builds the pluggable client layer
that `docs/transfers-redesign-spec.md` §4 specced at sketch level, refined by
[`docs/download-client-api-survey.md`](download-client-api-survey.md)'s findings and by design
decisions taken with the user 2026-08-22 (recorded inline below, and in `docs/decisions.md`).

**It is also the foundation [#21](https://github.com/crzykidd/lftpweb/issues/21) (the torrent
manager) depends on.** Where a decision here exists because #21 will need it, that is said so
explicitly — but nothing in #21's own feature set is specced here.

Sections are numbered for reference.

---

## 1. What this is, and the two rules it inherits

A **connector** is a module that talks to one kind of download client — SABnzbd, rTorrent,
qBittorrent, Transmission, Deluge, NZBGet, and whatever follows. The expectation is **7–10 of
them**. Most share almost all their functionality; a few differ sharply (§5).

Two governing rules carry over unchanged from `docs/transfers-redesign-spec.md` §4:

> **§4.1 — Advisory only.** A client may *skip work* (satisfy the settle gate), *withhold work*
> (block a transfer that would move known-bad bytes), or *explain* (annotate rows, write audit
> events). It may **never write `item.state`.**

> **§4.2 — Absent from the client is not a verdict.** Only an *explicit* failure blocks anything.
> Silence means no information, and no information means fall back to today's behavior. An
> unreachable client keeps its last known status; it never downgrades a verdict.

§4.2 is not hypothetical. The entire amber `dropped` state (v0.2.4) exists because SABnzbd
spuriously returned a blank queue to Sonarr's poll and 8 mid-download items flipped terminal `gone`
in a single pass.

**§4.1 is enforced structurally, not by discipline: a connector is handed no database handle.** It
is pure client I/O. It *cannot* write `item.state`, because it has nothing to write to. Every
decision made from a connector's output is made by a caller that owns the database, outside the
connector boundary. A future change that wants to pass a connection into a connector is this rule
being violated, and should be read as such.

### 1.1 The reference workflow this is designed against

**Described by the user, 2026-08-22, as their actual seedbox setup.** It is the *preferred* layout
this framework is designed to serve, and — per the user's instruction — **it is to be written up in
`README.md` as the recommended setup** (§14, stage 1). Other workflows will turn up; this is the one
with a stated answer.

- **SAB and rTorrent both drop into a shared completed directory tree** — on the live test system,
  `/home/crzykidd/downloads/complete/<category>`, matching lftpweb's queue remote paths one-for-one.
- **For TV, both clients share the same folder.** Not one folder each.
- **SAB downloads, then *extracts* into the completed structure.** The extracted files are new
  files. The completed folder holds the only copy; SAB's own working copy is gone.
- **rTorrent downloads, then on completion *hardlinks* its files into the completed folder.** The
  torrent keeps seeding from its own data directory, and the completed-folder entry is a second
  link to the same inodes.

**Three consequences run through this entire document, and each is a place the obvious
implementation is wrong:**

1. **Hardlinks are the normal case for torrent content, not an anomaly** (§10.5). Deleting the
   completed-folder copy of an rTorrent release frees *no space at all* — the torrent still holds a
   link. Space is reclaimed only when the last link goes.
2. **A shared folder means "claimed" is a union across clients, not a per-client question**
   (§11.1). A scan that knows only about SAB sees every rTorrent release in the shared TV folder as
   unclaimed, and vice versa.
3. **rTorrent does not report the completed-folder path at all.** Its `content_path` is the seeding
   directory it downloaded into. The hardlinked copy is invisible to the client's own API, so
   path-matching alone cannot attribute it — which is why §11.1 matches on **inode**, not path.

---

## 2. Two vocabularies, not one

`docs/download-client-api-survey.md` §1's capability matrix conflates two different things in one
column list: **verbs the client can perform** (stop, delete, report free space) and **facts a
record can carry** (ratio, seed time, trackers). They are consumed differently, and separating them
is the decision the rest of this document hangs off:

- A missing **operation** disables a **control**. *"Your client can't recheck."*
- A missing **field** disables a **rule**. *"Your client doesn't report seed time, so a 14-day rule
  is unavailable."*

#21's ranking function is almost entirely a consumer of the *field* vocabulary. #18's settle-gate
skip is almost entirely a consumer of the *operation* vocabulary. One flat capability list serves
neither well, and forces SABnzbd to declare `none` against a wall of torrent verbs that were never
about it.

### 2.1 The operation vocabulary

A **closed enum**, not free strings. Ten connectors authored against free strings produce ten
spellings of the same idea.

| Operation | What it does | Notes |
|---|---|---|
| `test_connection` | Reachability, version, server identity | Mandatory for every connector |
| `list_transfers` | Everything the client knows about, normalized (§3) | Mandatory |
| `list_history` | Finished/failed work, **carrying the real on-disk path** | Native on usenet clients; *derived* on torrent clients (a torrent never leaves the list) |
| `get_transfer` | Exact lookup by client id — the *arr's `downloadId` (§10) | Often *derived* (filter the list) |
| `list_trackers` | One item's announce hosts | **Its own operation, not a field** — an N-call fetch on qBittorrent and rTorrent, so a caller must be able to decide not to pay for it |
| `list_files` | One item's file list | |
| `list_base_paths` | The client's own configured download/complete directories | **A prefill, not the source of truth** — the user configures the real roots (§8.2). rTorrent's `directory.default` will not mention the completed folder it hardlinks into, so the client's own answer is necessarily incomplete |
| `free_space` | Free bytes for a path, and total where reported | Transmission is the only one reporting total |
| `pause` / `resume` | | qBittorrent renamed `pause`→`stop` at 5.0 (§4.2) |
| `remove` | **Unregister the item, leave the data on disk** | See §11 — this is the only removal verb a connector has |
| `set_label` | Category/label assignment | |
| `recheck` | Re-verify data against the torrent | Torrent clients only |

**`remove_with_data` is deliberately absent.** See §11 — this is the single largest simplification
in this design, and it is not an oversight.

**`add_transfer` is deliberately excluded, permanently.** lftpweb does not grab; the *arr does.
Anything that writes the client's own configuration is likewise out of scope. Naming the exclusion
here is cheaper than re-litigating it at connector #6.

### 2.2 The field vocabulary

What one normalized `Transfer` record can carry.

**Mandatory for every connector, never declared, no exceptions:**
`client_id` · `name` · `phase` (§3) · `raw_status`.

**Declared per connector:**

| Field | Notes |
|---|---|
| `content_path` | The real path on disk. **Effectively mandatory for any connector that participates in deletion** (§11) — no path, no delete. This is now the *only* capability gate on deletion, and it is a field rather than a verb |
| `size_bytes` · `bytes_done` · `eta_s` | |
| `error_message` | The explicit-failure signal §4.2 turns on |
| `category` | Feeds the category → queue mapping (§8.3) |
| `added_at` · `completed_at` | |
| `ratio` | **rTorrent reports per-mille — divide by 1000.** A rule comparing raw `d.ratio` against `1.0` treats every torrent as wildly over-seeded |
| `uploaded_bytes` | |
| `seed_time_s` | *Derived* on rTorrent, and the canonical example of why derived needs a caveat (§4.3) |
| `tracker_hosts` | Populated only when `list_trackers` has been called — **hostname only, never the full announce URL** (§10.2) |

**A connector must never declare a field it cannot populate.** The conformance suite (§6.2) asserts
this against a fixture, because a field declared and then returned `None` is worse than one
declared absent: a consumer offers a rule that silently never matches.

---

## 3. The normalized phase vocabulary

`docs/transfers-redesign-spec.md` §4.7 asks for "a normalized verdict vocabulary across very
different APIs." SABnzbd's queue speaks Downloading / Paused / Repairing / Extracting and its
history speaks Completed / Failed; a torrent speaks percent-complete / hashing / stalled / error.

```
queued | downloading | paused | verifying | extracting | seeding | completed | failed | unknown
```

Two properties, both load-bearing:

1. **`raw_status` is preserved alongside** — the same "narrow projection + `raw`" shape
   `core/arrclient.py`'s `QueueRecord`/`HistoryEvent` already use. Display shows the client's own
   word; logic reads `phase`.
2. **`unknown` is the safe default, and `unknown` never blocks anything.** A status string this
   codebase has never seen maps to `unknown`, and `unknown` behaves exactly as "the client said
   nothing" does — §4.2 encoded in the type rather than in a comment. A connector's phase mapping
   must be **total**: it may never raise on an unrecognized status.

---

## 4. The capability declaration is three layers

### 4.1 Why one static dict is not enough

`docs/download-client-api-survey.md` forces this directly:

- **qBittorrent renamed `pause` to `stop` in 5.0** (API v2.11), which makes version detection a
  requirement rather than a nicety.
- Several capabilities are properties of the **deployment**, not the client version.

So:

| Layer | What it is | Where it lives |
|---|---|---|
| **Static** | What this connector *type* could ever do | A class attribute. Renders "if you configured a qBittorrent you'd get…" before any connection exists |
| **Probed** | Refined at `test_connection` time by version/deployment detection | Persisted on the instance row (`capabilities_json`, `capabilities_probed_at`), so the settings UI never has to hit the client to render |
| **Runtime-degraded** | A capability that failed in use, dropped to unavailable with an audit event | In-memory per instance, cleared by the next successful probe |

Layer 3 is `docs/torrent-manager-spec.md` §4a's "declared is not the same as working."

### 4.2 The rule that makes layer 3 safe

> **A transport failure must never degrade a capability.**

An unreachable client, a timeout, a 500 — none of these say anything about what the client
*supports*. Degrading on them would turn one bad network minute into a permanently disabled
feature. This is §4.2's "absent is not a verdict" applied to configuration rather than to status.

Which forces a **three-way error taxonomy**, not one exception type:

| Error | Meaning | Effect |
|---|---|---|
| `ClientUnreachable` | Could not talk to it | Back off (§9), keep last known state, **change no capability** |
| `ClientError` | This call failed | Surface it; change no capability |
| `CapabilityUnavailable` | The client explicitly cannot do this, or a post-condition proved it didn't | **Degrade + audit event** |

Only the third degrades. `core/arrclient.py`'s `ArrClientError` deliberately collapses DNS failure,
500 and timeout into one class because none of those distinctions change what the poller does; here
the distinction *does* change what happens, so it is drawn.

### 4.3 Tri-state, plus a caveat string

`native` / `derived` / `none` — `docs/download-client-api-survey.md` §4.1's conclusion.

**A `derived` capability carries a note, because the semantics differ.** The canonical case:

```
Capability(support=DERIVED,
           note="wall-clock since completion — a stopped torrent still accrues")
```

rTorrent has no seed-time field. It can be derived from `d.timestamp.finished`, but that measures
*wall-clock since completion*, which is **not the same thing**. A site rule meaning "actually
seeding for 14 days" cannot be honored faithfully on rTorrent, and the UI must say so where the
user writes the rule rather than quietly redefining it.

Consumers therefore ask two different questions:

```python
caps.supports(Field.SEED_TIME)                       # native only
caps.supports(Field.SEED_TIME, accept_derived=True)  # derived is good enough
```

**The connector does the deriving, not the framework.** rTorrent's connector knows about
`d.timestamp.finished`; the framework does not and should not. The declaration is what makes the
derivation honest.

### 4.4 A missing capability disables a feature; it never fakes one

`docs/torrent-manager-spec.md` §4a, restated because it is easy to erode: if a client cannot report
ratio, rules needing ratio are unavailable for that client. lftpweb does not estimate ratio from
uploaded bytes and present it as ratio. **And the UI is driven by the declaration, never by the
client's name** — no `if client == "rtorrent"` anywhere.

---

## 5. Baseline profiles — how 7–10 connectors stay cheap

**Decided with the user, 2026-08-22:** SABnzbd and torrent clients are genuinely different, another
NZB client (NZBGet) is likely, and "the features are close to the same except ratios/etc."

That observation is real, and the mechanism for it is **capability profiles a connector starts from
and overrides**:

**The baselines are stated as an exact per-key table, not by category.** An earlier draft described
them in prose ("queue, history, categories, paths, free space, pause, remove") and never named
`list_files`, `set_label` or `resume` at all, which left the stage 0 implementation making a
reasonable guess. Both baselines declare **every** key, so a connector that overrides nothing still
ends up with a complete declaration — which is what §6.2's "every key is declared" check relies on.

| Key | `USENET_BASELINE` | `TORRENT_BASELINE` |
|---|---|---|
| `test_connection` | native | native |
| `list_transfers` | native | native |
| `list_history` | native | **derived** — a torrent never leaves the list |
| `get_transfer` | derived — filter the list | derived |
| `list_trackers` | **none** — usenet has no trackers | **native** |
| `list_files` | native | native |
| `list_base_paths` | native | native |
| `free_space` | native | native |
| `pause` / `resume` | native | native |
| `remove` | native | native |
| `set_label` | native — categories | native |
| `recheck` | **none** | **native** |
| `content_path` | native | native |
| `size_bytes` · `bytes_done` · `eta_s` | native | native |
| `error_message` · `category` | native | native |
| `added_at` · `completed_at` | native | native |
| `ratio` · `uploaded_bytes` · `seed_time_s` | **none** | **native** |
| `tracker_hosts` | **none** | **native** — populated only once `list_trackers` has run |

`TORRENT_BASELINE` is built *from* `USENET_BASELINE` by overriding the seven bolded rows, which is
the reuse mechanism this section exists for.

**A baseline is a starting point, not a claim about any real client.** Every entry above is subject
to correction by a connector's own static declaration and then by its probe (§4.1) — e.g. SABnzbd
may well support a native `get_transfer` by `nzo_id`, to be confirmed against the real API in
stage 1 rather than assumed here.

SABnzbd declares `USENET_BASELINE` plus its handful of differences. NZBGet later is one file, the
same baseline, two or three overrides. A connector author writes ~3 lines instead of ~25, and the
shared shape is stated once rather than copied nine times.

### 5.1 `family` is display metadata only — never branched on

A connector declares `family = "usenet" | "torrent"`. It is used to **group the settings picker**
and pick a default config form. It **must never appear in a capability decision.**

This is `docs/torrent-manager-spec.md` §4a's "never keyed on the client's name" rule generalized one
rung up. The moment something reads `if family == "torrent": has_labels`, Deluge-without-the-label-
plugin is broken — and that is precisely the class of bug the declaration exists to prevent. The
baseline profiles give the convenience of the family grouping *at authoring time*; the family
string itself carries no runtime authority.

---

## 6. Module layout, registry, and the conformance suite

```
core/clients/__init__.py    registry (decorator) + exports
core/clients/base.py        DownloadClient ABC, Operation/Field enums, CapabilitySet, profiles
core/clients/models.py      Transfer, TransferPhase, TrackerInfo, SpaceInfo, RemoveOutcome
core/clients/errors.py      the three-way taxonomy (§4.2)
core/clients/sabnzbd.py     first adapter
core/clientsync.py          the poller (§9)
api/settings_clients.py     instance CRUD, test-connection, capability readout
```

**`core/clients/` is the first subpackage under `core/`**, which is otherwise flat. Deliberate: at
7–10 adapter modules plus four framework modules, a flat `core/` would be unreadable. Flagged here
rather than done silently.

**Gap found in stage 1b (2026-08-22): the ABC does not declare a transport lifecycle.** `§6`'s
layout named the methods a connector performs but said nothing about how its transport is opened
and closed, so `SabnzbdClient` declares `aclose`/`__aenter__`/`__aexit__` on its own and
`api/settings_clients.py` has to reach for them with `getattr(client, "aclose", None)`. That
duck-typing is harmless once and a smell at seven connectors — every caller re-deciding whether
this particular client needs closing is exactly the per-client branching §4.4 exists to forbid,
arriving through the back door. **`DownloadClient` should declare the async-context-manager
protocol** so every connector is closed identically and a leaked `httpx` client is impossible
rather than merely unlikely. `core/arrclient.py` already models the shape to copy (`async with`,
one client per use). Fix it before the second real connector lands, not after.

**Registration is a decorator into a module-level registry, imported explicitly by
`core/clients/__init__.py`.** No entry-points, no dynamic import scanning — this project ships one
image and gains nothing from discovery machinery. Adding a connector is one file plus one import
line.

### 6.2 The conformance suite is what actually makes this cheap

A test parameterized over the whole registry, asserting of **every** registered connector:

- Every operation and field key in the enums is declared — no key silently missing.
- No field is declared that the connector cannot populate (§2.2).
- The phase mapping is total: no input status raises (§3).
- Only the three error types (§4.2) escape its methods.
- The declared config schema (§8.1) round-trips.

At two connectors this is mild insurance. At eight it is the difference between adding a client in
an afternoon and adding one over a week.

**Two of these checks cannot be genuinely registry-generic until there is a second connector.**
"Only three error types escape" and "no field declared that cannot be populated" both need a
connector whose responses and failures a test can drive. With only the fake registered (stage 0)
they are written as direct tests against it; they become registry-generic once stage 1's SABnzbd
fixture exists with its own controllable state. Noted so a later reader does not mistake the
narrower form for the intended one.

---

## 7. Identity

### 7.1 The *arr already hands us the client's own key

`docs/transfers-redesign-spec.md` §4.4, from real production evidence: `downloadId` **is** the
client's key — `b67924d8-c0f0-4901-8941-85ddbfef6179` (a SAB `nzo_id`) and
`12682AF0C00A061448BCFA16975A5D5F01A84A61` (a torrent hash), both observed in `arr_matched` events.
For any *arr-tracked item, matching is an exact key lookup, free.

**Infohashes are normalized to lowercase on storage and compared case-insensitively.** The *arr
hands them over uppercase; clients vary. Cheap now; a class of phantom-row bugs later.

### 7.2 Do not predict paths from names

Same section, same production evidence: Sonarr grabbed
`Married.At.First.Sight.S12E15.720p.WEB.h264-BAE-xpost`, it failed on the SAB side, and the
replacement landed as `...BAE[rarbg]-xpost`. SAB also renames on unpack. **Binding happens against
the client's reported `content_path`, never against a path predicted from a release name.** A wrong
guess leaves a phantom row that never reconciles.

### 7.3 Announce URLs are credentials

`docs/torrent-manager-spec.md` §5. Announce URLs embed per-user passkeys. **Store, match, log and
display the announce *hostname* only.** The full URL is never persisted, never logged, never
rendered, and never enters a support bundle. See §13.3 — the capture mechanism this spec introduces
is a new and very plausible way to leak one, and is redacted at the point of capture.

---

## 8. Instance configuration

### 8.1 The instance row, and connector-declared config schemas

Mirrors `arr_instance` (migration 018): `id`, `name`, `client_type`, `enabled` **defaulting to 0**
per project rule, secret encrypted at rest via `core/crypto.py`, `created_at`/`updated_at`. Plus
`capabilities_json`, `capabilities_probed_at`, `version` (§4.1).

**A common `base_url` + secret does not cover these clients.** rTorrent needs an SCGI path or an
httprpc mount; qBittorrent needs cert-verification and a `Referer`/`Origin` host that matches, or
naive clients simply fail; SABnzbd needs an API key. So:

> **Each connector declares its own connection-form schema**, and Settings renders one generic form
> from the declaration.

Decided with the user 2026-08-22. This is the same "UI driven by the declaration, never by the
client's name" rule (§4.4) applied to setup, and it is the difference between one form and ten
hand-written ones. Type-specific values live in a `config_json` blob whose shape the connector owns.

#### Where this is eventually going: one "API Clients" page for every connector

**The user's own framing, 2026-08-22 — recorded as a direction, deliberately not scoped now:**

> *"In my head I think of API Clients or something. In there we have a list of all api connectors
> we have set up, and when I click edit the edit section loads the right settings for the type, and
> when I say add I have a dropdown list of integrations we support — sonarr/radarr/sab/rtorrent
> etc. This creates a dynamic page where all integrations get configured."*

**The mechanism for this already exists** — it is exactly what §8.1's declared config schema plus
§6's registry produce. A unified page is a list of instances, an add-dropdown fed by the registry,
and an edit form rendered from the selected type's `ConfigField` list. Nothing new is required
structurally; the *arr instances would need to render through the same form machinery (whether by
registering as connector types or by declaring an equivalent schema), which is the only real work.

**Explicitly deferred.** The user's instruction is to leave Settings' layout alone until the
download-client work is actually functioning: *"I don't want to change things yet… after some time
testing/validating we can look at combining into a new config page."* Until then Settings → Clients
sits beside Settings → Integrations as two tabs, and the naming overlap between them is a known,
accepted rough edge rather than an oversight — see the same conversation for the rejected
`Media managers` / `Download clients` rename.

**Settings → Clients carries a "this section is new and may have issues" notice** (the user's
request, same conversation) stating that configuring a client changes no behaviour yet. It comes
out once the section has real use behind it.

### 8.2 Base paths are user-configured, browsed, and validated on save

**Decided with the user, 2026-08-22.** The scan's roots (§11) come from the instance's settings, not
from the client's own answer:

- **The user enters them in Settings, with the existing path-browse dialog.** `core/browse.py`
  already provides `resolve_remote_dir` over SFTP and `GET /api/browse/remote` is already the thin
  wrapper Settings → Queues uses for `remote_path`. This is reuse, not new machinery.
- **`list_base_paths` (§2.1) prefills the field where a client can answer**, and is never treated as
  complete. rTorrent's own configured directory says nothing about the completed folder it hardlinks
  into (§1.1), so a client-only answer would miss the tree that matters most.
- **Save-time validation is the half that matters here.** `core/browse.py.remote_directory_error`
  gives a real answer rather than a graceful fallback, precisely so a typo is caught at save. A
  mistyped base path is not cosmetic in this feature: it is what the §10.2 containment check
  authorises deletion *within*. A wrong root is a wrong safety boundary.
- Multiple roots per instance, since a seedbox routinely spreads content across several.

### 8.3 Binding: site-level instance, category → queue

`docs/transfers-redesign-spec.md` §4.5 — **a client instance is site-level, not per-queue.** One SAB
serves `ar-tv` and `ar-movies` both; copying the *arr's per-queue binding would mean configuring and
polling the same SAB twice. Queue attribution comes from a configured **category → queue** mapping.

**Observed on the live test system, 2026-08-22:** its two queues are
`/home/crzykidd/downloads/complete/ar-movies` and `/home/crzykidd/downloads/complete/ar-tv` — i.e.
**the queue remote paths already *are* the client's category folders.** The mapping is not a new
concept for the user to learn; it is the layout they already have. The setup UI should therefore
*offer to infer* the mapping from existing queue remote paths, with the user confirming rather than
typing.

**Inference reads the instance's own configured base paths (§8.2), not a live `list_base_paths`
probe.** An earlier wording here named the client's result and read as if a probe were required,
which contradicts §8.2's rule that the user's configuration is authoritative and the client's answer
is only ever a prefill. Stage 1b resolved it the right way. A future live-probe prefill would be
*additive* — one more source for the suggestion — and must not become a precondition for inferring
at all, or a client that cannot report its own paths loses a convenience it never needed.

---

## 9. Polling

`docs/transfers-redesign-spec.md` §4.8: `core/arrsync.py` already implements exactly the needed
shape — poll an external service, short timeout, capped exponential backoff per instance
(60 s → 30 min), one warning plus one audit event on failure, keep last-known state, never let
silence become a verdict.

> **Do not refactor `arrsync.py` in the same pass that introduces this.** It is battle-tested
> against real production incidents. Build the client poller alongside it, then extract the shared
> shape afterwards, once both exist and the seams are obvious.

### 9.1 Two cadences per instance

Decided with the user, 2026-08-22. One cadence cannot serve both consumers:

| Cadence | Call | Consumer |
|---|---|---|
| **Fast** (~10 s) | `list_transfers(active_only=True)` | The settle-gate skip, Preflight rows, withhold — #18 |
| **Slow** (minutes) | Full estate refresh, `list_trackers` where needed | The seeding overview — #21 |

Listing 500 seeding torrents every 10 seconds is waste; learning 60 seconds late that a download
finished defeats the point of the settle-gate skip.

### 9.2 Freshness and source precedence — a stale *arr reading must never overwrite a fresh one

**Raised by the user, 2026-08-22**, and it is a genuine hole in every preceding section:

> *"If data from sab/torrent etc is current, older stale status from arr shouldn't replace it. The
> *arr polls its download client at an interval, and we poll the *arr, and that can be a 60+ second
> round trip — but we'll see SAB/rTorrent directly for download status once this feature is on."*

**The lag is structural and compounds.** SAB reports to the *arr on the *arr's own schedule; lftpweb
then polls the *arr on its own (default 10 s since v0.3.1). An *arr-derived download status is
therefore two polling intervals removed from the fact it describes, and once a client connector
exists lftpweb is reading the *same fact* from the process actually performing the work.

Two sources reporting one fact with different lag means **last-write-wins is wrong**, and it is the
default behaviour of any naive merge.

#### The precedence rule

> **For a fact both sources can report, the direct client observation wins — always, not
> "if newer."**

This is deliberately *not* a timestamp comparison, because the timestamps do not exist to compare.
**The *arr does not tell us when it last polled its own client**, so lftpweb can only know when *it*
fetched the *arr's answer — which measures our own freshness, not the data's. A rule written on
fetch time would confidently prefer a just-fetched relay of a two-minute-old fact over a
ten-second-old direct reading. The client is the origin; the *arr is a relay of unknown lag. Origin
wins.

#### Three constraints that keep the rule from doing harm

1. **Precedence is per-field, never per-record.** The client wins on the fields it actually
   populates (§2.2's declaration is exactly the list). It must not blank a field it does not carry
   just because it won on another — an *arr's `timeleft` should survive a client that reports no
   ETA. A record-level "client wins" would silently strip data.
2. **It applies only where the client actually reported.** §4.2 is unchanged and outranks this:
   **absent from the client is not a verdict.** Silence from the client means the *arr's reading
   stands, unchallenged. A blank SAB queue response — the v0.2.4 incident — must never be read as
   "the client says this isn't downloading."
3. **An unreachable client does not demote its last-known reading to the *arr's.** Same shape as
   §4.2's "keep last known status": a client that stops answering keeps whatever it last said until
   it says otherwise, and the poller's backoff (§9) is what covers the gap.

#### Where this actually bites first: Preflight

`core/preflight.py` already merges multiple sources into one box, and §4.6a names the download
client as a planned third. **A release grabbed by SAB will be reported by both the *arr source and
the client source at once** — the same underlying thing, twice, with different lag and different
wording.

The identity to dedupe on already exists and is exact: **`downloadId` *is* the client's own key**
(§7.1), so the two rows are matchable without heuristics. The merge belongs at the point rows are
assembled for the box, and it must respect all three constraints above — in particular
`PreflightHold`'s flap tolerance is per-source today, and two sources holding the same identity
must not produce two rows.

**Not scoped into stage 2's poller by default.** Stage 2 adds the client as a Preflight source; this
section is the rule that stage's merge must implement, and it should be built with a test that
asserts a stale *arr row cannot overwrite a fresher client field — the failure is otherwise silent
and looks like flicker.

---

## 10. Deletion — client removes the entry, lftpweb deletes the bytes

**This is the design's largest simplification, decided with the user 2026-08-22:**

> **Remove the item in the client, then delete the data over SSH, then verify, then log the event.
> The client is never asked to delete data.**

### 10.1 What this buys

`docs/download-client-api-survey.md` §2 is the worst corner of the entire survey: rTorrent has no
"remove and delete data" primitive, and the ruTorrent sequence (`d.custom5.set=1` → `d.delete_tied`
→ `d.erase`, order load-bearing) only deletes anything because ruTorrent's `erasedata` plugin
installs an `event.download.erased` hook. A bare rTorrent **accepts all three calls and deletes
nothing, with no error.** The documented failure modes are worse: the plugin silently does nothing
on a filesystem-permission problem, and times out on torrents with 100+ files. Both are invisible to
the caller.

Deleting over SSH removes all of it:

- **`remove_with_data` leaves the operation vocabulary entirely.** `remove` means *unregister, leave
  the data*, which every surveyed client does natively and honestly.
- **The `erasedata` deployment probe is never written.** A capability that is a property of the
  user's plugin configuration stops existing.
- **Runtime capability degradation (§4.1 layer 3) loses most of its job.** It is kept for
  qBittorrent's `pause`→`stop` and the general case, but it is no longer load-bearing.
- **lftpweb already deletes on the seedbox reliably.** `core/remote.py.delete_path` issues
  `rm -rf --` over the pooled asyncssh connection, refuses empty/root-looking paths, and raises
  `RemoteDeleteError` on a nonzero exit — a real error when it fails, which is exactly what the hook
  path cannot offer. This is the survey's own §2 consequence 3, adopted.

The only remaining capability gate on deletion is the `content_path` **field** (§2.2). No path, no
delete.

### 10.2 The sequence

| Step | What | Why |
|---|---|---|
| 1 | **Pre-check before touching the client**: SSH reachable, path exists, and the path is **inside one of the client's declared base paths** | The containment check is what stops a wrong or hostile `content_path` from `rm -rf`-ing something catastrophic. `delete_path`'s own root-path refusal is defense in depth *behind* this, not instead of it |
| 2 | Shared-path check (§10.4) | |
| 3 | `remove(id)` in the client — unregister, data stays | |
| 4 | `delete_path()` over SSH | |
| 5 | **Verify**: re-stat the path, and measure free space before/after | `rm`'s exit code is not the same as "the bytes are gone" |
| 6 | One audit event either way; failure raises an in-app banner | |

**Order is client-first on purpose.** Deleting the data first leaves the client seeing vanished
files: it errors, may re-check, and briefly reports a broken torrent. Removing the entry first means
nothing is watching the files when they go.

### 10.3 The one bad failure window, and its backstop

**Client removed, SSH delete failed** — the seed is lost *and* the space is not reclaimed, the worst
of both outcomes. Three things address it:

1. **Step 1 shrinks the window** to "SSH died in the last few hundred milliseconds."
2. **A bounded retry**, reusing the temperament (not necessarily the code) of the existing
   `move`-mode delete ladder and its stranded-delete sweep — this project's house style for remote
   deletes, which §9 of the torrent-manager spec explicitly asks this feature to inherit rather than
   invent a looser one.
3. **§11's disk review scan is the structural backstop.** Data left orphaned by exactly this failure
   shows up in the orphan bucket on the next scan.

### 10.4 Cross-seeding

**Decided with the user 2026-08-22: they do not cross-seed, but others will, so it is built.**

The same files seeded on three trackers are three torrents sharing one save path. Deleting the data
for one breaks the other two. So: **detect shared save paths, and refuse the delete unless every
torrent on that path is unclaimed.** With SSH doing the deleting this is a core requirement, not a
safety rail — the bytes go regardless of what the client thinks.

**Named gap:** this ships correct-by-unit-test and **unwitnessed against a real cross-seeding
setup.** It belongs in README's "Known gaps" on the release that ships it, not presented as proven.

### 10.5 Hardlinks are the normal case, not an anomaly

`docs/torrent-manager-spec.md` §9 treats hardlinks as a divergence to detect: *"deleting may free
nothing… stop the pass if expected and actual diverge badly."* **In the reference workflow (§1.1)
that divergence is the designed state, not a fault**, and a check written as an anomaly detector
would fire on every single rTorrent release.

Every rTorrent item exists as **two links to one set of inodes** — the seeding directory and the
completed-folder copy. So:

| Delete | Effect |
|---|---|
| The completed-folder copy alone | Seeding continues unbroken. **Zero bytes reclaimed.** |
| The torrent's data alone | The completed copy survives intact. **Zero bytes reclaimed.** Seed lost |
| Both links | Seed lost, **bytes actually reclaimed** |

**Therefore link-awareness is a first-class requirement of the delete path, not a post-hoc check:**

- **Predicted freed space must count a file's bytes only when the deletion removes its last link.**
  "7 selected — 312 GB" is a lie if half of it is still linked from a seeding torrent. The inode map
  §11.1 builds is what makes this computable, and the same map serves both features.
- **A delete that frees nothing is not necessarily a failure.** Step 5's verification (§10.2) must
  distinguish *the path is gone* (success, the actual post-condition) from *free space moved*
  (informational, and expected to be zero for a link-removal). Conflating them reports every
  correct hardlink deletion as a failed one.
- **The user is told which kind of deletion they are about to perform.** "This removes the completed
  copy; the torrent keeps seeding and no space is reclaimed" is a genuinely different action from
  "this is the last link — 40 GB comes back," and the UI must not present them identically.
- **Expected-versus-actual divergence is still worth reporting**, but as an *unexpected* divergence
  only — actual reclaim materially below a link-aware prediction, not below a naive sum-of-sizes.

**This also settles a question about a feature that already ships.** `move`-mode source deletion
removes `<queue.remote_path>/<rel_path>` on confirmed *arr import — which in this workflow is the
**completed-folder copy**. For an rTorrent release that is the hardlink: **seeding is unaffected,
and no space is reclaimed until the torrent itself is removed.** For a SAB release the completed
folder holds the only copy, so the space comes back immediately. Same code, two different real
outcomes, and neither is currently explained anywhere.

### 10.6 Manual or automatic

The delete path is one code path with two triggers — a user clicking, or a scheduled job. This
mirrors `docs/torrent-manager-spec.md` §8a's "manual mode is the whole of auto mode minus the
trigger," and for the same reason: two implementations drift, and the one that drifts unwatched is
the one that deletes things. **Manual ships first** (§14).

---

## 11. The disk review scan

> *"Client shows all this on disk… what is in the base folders for the client that don't exist in
> the UI that could be cleaned up with a review option."* — the user, 2026-08-22

### 11.1 One reconciliation, two buckets

Three sets, over the declared base paths (§2.1 `list_base_paths`):

| Set | What |
|---|---|
| **A** | What **every configured client instance** claims — `content_path` across all their `list_transfers` results, **plus every inode reachable under those paths** (see below) |
| **B** | What is actually on disk under the base paths, via SSH — path, size, **inode, and link count** |
| **C** | Paths lftpweb itself is using — items, queued/running jobs, pipelines in flight |

- **`B − A − C` → orphaned data.** On disk, claimed by no client, not in use by lftpweb. This is the
  review list the user asked for.
- **`A − B` → broken seeds.** A client item whose data is gone.

### 11.1a Set A is a union across clients — this is a correctness requirement, not a nicety

**In the reference workflow SAB and rTorrent share the TV folder** (§1.1). A scan that evaluates one
client's claims against a shared folder sees *the other client's entire estate as orphaned*. With
deletion on the other end of that list, this is the most dangerous single mistake available in this
feature.

So:

- **The scan is per-base-path, and set A is the union of claims from every client instance that
  writes to that path.** Never per-client.
- **If any client contributing to a base path is unreachable, or is disabled, or has not reported
  successfully this pass, no orphans are proposed for that path at all.** This is §4.2's "absent
  from the client is not a verdict" applied to the scan: a client that did not answer has not told
  us its releases are unclaimed — it has told us nothing.
- Configuring a second client that shares a base path must therefore be *possible to declare* before
  it is possible to scan safely. The setup UI should surface a path claimed by a client lftpweb does
  not know about as a blocker on scanning, not as a pile of orphans.

### 11.1b Claiming is by inode, because rTorrent cannot report the hardlink

**rTorrent's `content_path` is its seeding directory, not the completed-folder copy** (§1.1). The
hardlinked entry the reference workflow creates is invisible to the client's own API. Path-matching
alone would therefore flag **every rTorrent-sourced release in the completed folder as an orphan** —
the same catastrophic outcome as §11.1a, arriving by a different route.

The fix is to match on **inode**, which is exactly what a hardlink shares:

- **The SSH walk collects inode number and link count per file**, not just path and size. GNU
  `find -printf` supplies both (`%i`, `%n`), and `core/remote.py` already drives `find -printf` for
  its remote scan — so this is an extension of an existing mechanism, not a new one. The
  BusyBox/fallback scan path must be checked for parity or the scan declared unavailable there
  rather than silently degrading to path-only matching.
- **A file is claimed if any link to its inode falls inside a client-claimed tree.**
- **A candidate is only proposed when *every* link to its inode is itself a candidate.** A file with
  `nlink > 1` and an unaccounted-for link is never proposed — the conservative default, and the one
  that keeps a seeding torrent's data off the list.
- **The same inode map produces the link-aware freed-space prediction** §10.5 requires. One walk,
  both answers.

### 11.1c Correcting an earlier reading of the *arr `move`-mode interaction

An earlier draft of this document asserted that `move`-mode source deletion on the live system is
breaking seeds today. **With the reference workflow's hardlinks, that is wrong**, and the correction
matters because it changes what the broken-seed bucket is expected to contain.

`move` mode deletes the completed-folder copy. For an rTorrent release that is one of two links:
**the torrent keeps seeding, unharmed** (§10.5). The hardlink workflow is precisely what makes
`move` mode and seeding coexist. `docs/torrent-manager-spec.md` §9's "a torrent can already be
seeding files lftpweb has removed underneath it" describes a real hazard for setups *without* the
hardlink step, and the `A − B` bucket is how any instance of it becomes visible — but on this
workflow it should be empty, and a non-empty one is a genuine finding rather than the expected
state.

**Confirmed by the user, 2026-08-22:** *"it hard links the file from the downloads directory to the
completed directory and that is it. we pick up from completed and delete on success import. the
torrent keeps seeding till I manually clean today."*

### 11.1d Two different piles — and only one of them is orphans

That confirmation separates two things this section could easily have conflated, and they want
different features:

| Pile | What it is | Whose problem |
|---|---|---|
| **Debris** | Data under a base path that **no client claims and lftpweb is not using** — failed extractions, aborted grabs, and the §10.3 window where the client entry was removed but the SSH delete failed | §11, the orphan scan. Genuinely unclaimed; safe to review and remove |
| **The seeding estate** | rTorrent's downloads directory, still **claimed** by live torrents, accumulating until the user cleans it by hand | Not orphans. This is #21 — eligibility by site rules, ranking, then §10's delete path |

**The manual cleanup the user does today is the second pile**, and naming it correctly keeps the
orphan scan from ever proposing a live seeding torrent's data as debris. The scan still *shows*
both — a review page that omits the seeding estate would be answering a question nobody asked — but
the two are labelled distinctly and only debris is selectable for removal before #21 exists.

**One useful property falls out of the workflow.** Once lftpweb's `move`-mode delete has removed the
completed-folder link on confirmed import, the torrent's own copy is the **only** remaining link.
Its `nlink` is back to 1, so from that point on:

- deleting the torrent's data reclaims its **full size**, exactly and predictably;
- the link-aware prediction (§10.5) and the naive sum-of-sizes agree, which is the easy case;
- and nothing else in either tree references those bytes.

So the state #21 acts on is the clean one. The hardlink complexity is confined to the window between
the torrent completing and the *arr importing — real, and the reason inode-matching is required
(§11.1b), but not the state most removal decisions are made in.

**The scan is where the user's phrasing becomes precise.** lftpweb's Files page shows only its
queues' remote paths. A torrent client's seeding folders generally sit *outside* those paths
entirely — on the live test system, the two queue paths are SAB-shaped category folders, so
rTorrent's estate is invisible to lftpweb today by construction. That is what "doesn't exist in the
UI" means, and it is why `list_base_paths` is load-bearing rather than convenient.

**Interpretation to confirm:** a candidate must be unclaimed by *both* the client and lftpweb — the
union reading, `B − A − C`. The narrower reading ("not in lftpweb's UI") alone would propose live
torrent data for deletion.

### 11.2 Guards

- **Age floor.** A release added two minutes ago, whose files are being written but which has not
  yet appeared in the client's list, must never be proposed. Same instinct as §7.3's removal grace
  period.
- **Containment.** Only paths under declared base paths are scanned or proposed. Ever.
- **The C set reuses `core/pipeline_flight.py`'s existing predicate and `in_flight_item_ids()` —
  never a second definition of "busy."** `docs/torrent-manager-spec.md` §9 names this specifically,
  and it is the v0.2.6 `REMOTE_GONE` defect class: deleting a seedbox source out from under a queued
  job.
- **Every client contributing to a base path must have reported successfully this pass**, or no
  orphans are proposed for that path at all (§11.1a). The single most dangerous mistake available
  here, and the shared TV folder makes it reachable on the reference setup.
- **Inode accounting, not path matching** (§11.1b): a file is claimed if *any* link to its inode is
  claimed, and a candidate is proposed only when *every* link to its inode is also a candidate.
- **Shared cross-seed paths** (§10.4) apply here identically.
- **Review-only. The scan never deletes.** It produces a list with per-candidate size and a
  **link-aware** reclaim total (§10.5); a human selects; the selection goes through §10's sequence.
- **Debris and the seeding estate are labelled distinctly, and only debris is selectable** before
  #21 exists (§11.1d).

### 11.3 Manual trigger

The user asked for a manual scan, and manual is right for a first version: the scan is an SSH walk
over potentially large trees and should not ride a page load. A scheduled cadence is a later
addition, not a launch requirement.

---

## 12. Free space, and what is *not* a connector's job

`free_space` is an operation (§2.1) and every surveyed client offers it. **No client reports
quota** — `docs/download-client-api-survey.md` §4.3 confirms this across all five, and there is an
open upstream Deluge ticket for it.

**Therefore quota is not part of this framework.** It is user-entered configuration plus a scheduled
`du` over the existing pooled SSH connection, per `docs/torrent-manager-spec.md` §6.0, and it
belongs to #21. A connector is never asked for it, and never asked to guess it.

Two traps to carry forward when #21 consumes `free_space`: qBittorrent reports free space for its
**default save path**, not necessarily where a given item lives; and rTorrent's `d.free_diskspace`
is the *minimum* across the devices an item's files span. The two sources must agree on which
filesystem they describe.

---

## 13. Testing

**The user's instruction, 2026-08-22: test locally for everything that can be tested locally.**
The repo already has the templates — `tests/fake_arr.py` is a real FastAPI app on a real uvicorn
socket with a mutable `FakeArrState` a test drives between poller passes, and the fake seedbox is a
real sshd container.

### 13.1 What is fully testable locally — including the riskiest parts

- **The delete sequence (§10) against the existing fake seedbox**: containment check, pre-check,
  client-remove, SSH delete, re-stat verify, the failure path and its retry — an e2e test over real
  sshd, the shape `test_delete_during_transfer_e2e.py` already uses.
- **The §11 reconciliation is pure set math** over three inputs. Every guard is a unit test,
  **including the cross-seed case the user's own setup cannot produce** (§10.4).
- Capability resolution, the three-layer merge, the "transport failure must not degrade" rule
  (§4.2), phase-mapping totality (§3), and the whole conformance suite (§6.2).
- `tests/fake_sabnzbd.py` and `tests/fake_rtorrent.py` in `fake_arr.py`'s shape — real
  request/response cycle, not a mocked transport.

### 13.2 What local tests cannot prove, and why it matters here specifically

**This repo has already been burned by exactly this failure.** `IMPORT_EVENT_TYPES = {3}` was wrong
— the *arr v3 API serializes `eventType` as a camelCase string in response bodies, and the numeric
codes exist only as query-parameter values. Two genuine live Sonarr imports were misclassified
`gone`. **Every test stayed green, because the fake *arr fixture encoded the identical wrong
numeric assumption.** The lesson recorded at the time:

> When a spec flags a vocabulary "unverified against a live instance," the test fixture that data
> drives must not itself be trusted as ground truth for that vocabulary — it can encode the
> identical wrong guess the production code does, and then prove nothing.

`docs/download-client-api-survey.md` opens by saying every claim in it must be re-confirmed against
a real instance. A fake SABnzbd authored from vendor documentation proves the code matches *a
reading of the documentation*, and nothing more.

### 13.3 Capture first, fixture second

**Stage 1's `test_connection` writes a redacted raw sample of each call's response to the log.**
Fixtures are then built from captured bytes rather than from documentation.

- **Redaction is mandatory, not polish.** Announce URLs embed passkeys (§7.3) and SAB's API key
  rides in the query string. Redaction happens at the point of capture, before anything is written.
- **Samples are capped**, the way the support bundle caps its *arr log fetch.
- **Every fixture carries its provenance in its docstring** — *recorded from a live SABnzbd 4.x,
  2026-08-22* versus *authored from vendor docs, UNVERIFIED*. An unverified vocabulary should be
  visible in the fixture that drives it, which is the one countermeasure that would have caught the
  `eventType` defect before production did.

The capture is independently useful to the user: a client that will not connect becomes diagnosable
from the log rather than by guesswork.

#### Open: the capture is at DEBUG, which defeats half its purpose

**Stage 1b writes the capture via `logger.debug`**, so seeing it requires setting
`LFTPWEB_LOG_LEVEL=DEBUG` on the deployment and turning it back afterwards. That is fine for
correcting §13.4 as a one-off exercise, and **wrong for the diagnostic promise made just above** —
"a client that will not connect becomes diagnosable from the log" does not hold if diagnosing it
first requires a log-level change and a restart, which is exactly the moment a user is least
inclined to keep going.

Options, none decided: emit the capture at INFO (it is bounded, redacted, and only fires on an
explicit user-triggered test, so the volume argument is weak); or return it in the
test-connection **response body** so the settings page can show it inline, which is where a user
looking at a failed test actually is. The second is probably right and costs little — but it makes
the redaction guarantee load-bearing in a second place, so it should be built with the same
log-content-style test rather than trusting the helper's return value (see the `httpx` finding
below for why that distinction is not academic).

#### The side door: `httpx` logs the full request URL, and the API key is in it

**Found during stage 1b (2026-08-22) by the test that asserts no API key reaches the log** — the
test failed even though the connector's own capture was correctly redacted.

`httpx` logs every outgoing request's full URL at INFO by default. SABnzbd authenticates with
`?apikey=…` **in the query string**, so a correctly redacted capture line was landing in the log
immediately after an *unredacted* `HTTP Request: GET …&apikey=<real key>` line emitted by the
library — from a code path this codebase's redaction never touches. `logsetup.py` now carries an
`httpx: WARNING` floor alongside `asyncssh` and the others.

**The generalisable lesson, and it is not "add httpx to the floors":** redaction discipline applied
to *your own* log calls is not sufficient when a dependency logs the same secret through its own
mechanism. Any future connector whose auth rides in a URL — and per
`docs/download-client-api-survey.md` §4, auth is the least portable part of this whole framework,
with a query-string API key being one of five distinct schemes — reopens this exact door. The test
that catches it is the one that asserts on **log content**, not on the capture helper's return
value; a helper-only test would have passed while the key leaked.

This is §7.3's "hostname only, never the full announce URL" rule arriving from a completely
different direction, which is the reason it is written down here rather than left in a commit
message.

### 13.4 The stage 1a correction list — every SABnzbd guess, in one place

§13.3 promises that the UNVERIFIED markers become "the list of things to go correct." This is that
list, produced by the stage 1a build (2026-08-22) and to be worked through once the capture runs
against the real instance. **Nothing here is confirmed; all of it is vendor-doc-derived.**

| # | Guess | Risk if wrong |
|---|---|---|
| 1 | **Queue status → phase** groupings: `Queued`/`Grabbing`→`QUEUED`; `Downloading`→`DOWNLOADING`; `Fetching`/`Propagating`/`Verifying`/`QuickCheck`/`Checking`/`Repairing`→`VERIFYING`; `Extracting`/`Moving`/`Running`→`EXTRACTING`; `Paused`→`PAUSED` | Cosmetic-to-moderate. Folding `Repairing`/`Fetching`/`Moving`/`Running` into neighbours is judgment, not observation |
| 2 | **History status → phase**: `Completed`→`COMPLETED`, `Failed`→`FAILED` | High — this is what the settle-gate skip keys on |
| 3 | **`Field.ADDED_AT` declared `NONE`** — the guess that neither mode exposes a queued/added timestamp | Low. A declared-absent field that turns out to exist is the safe direction |
| 4 | **`free_space` reads `diskspace2`/`diskspacetotal2`**, guessing index 2 = complete and index 1 = incomplete | Moderate; a one-character fix, but silently reports the wrong volume until noticed |
| 5 | **Queue byte/time parsing**: `mb`/`mbleft` as MB-denominated numeric strings, `timeleft` as `"H:MM:SS"` | Moderate |
| 6 | **History field names/shapes**: `bytes`/`completed`/`storage`/`fail_message`/`category`, including reusing `bytes` as both final size and `bytes_done` | High — `storage` is the identity source (§7.2) |
| 7 | **`list_base_paths`** via `mode=get_config&section=misc`, reading `complete_dir`/`download_dir` | Moderate — feeds §8.2's prefill only, and the user's own config is authoritative |
| 8 | **`list_files`** via `mode=get_files&value=<nzo_id>`, tolerant of a bare list or `{"files": [...]}` | Low |
| 9 | **Action call shapes and the `{"status": …}` contract** — specifically that `{"status": false}` alone means *not found* while `{"status": false, "error": …}` means a real failure | **Highest risk in the connector.** Wrong in one direction turns routine not-found into false errors; wrong in the other lets a real failure pass silently |
| 10 | **`test_connection` via `mode=version`**, plus "HTTP 200 even on auth failure, error signalled in the body" | High — a bad API key reading as success is a bad first-run experience |
| 11 | **`get_transfer` left `DERIVED`** (filter the merged list) rather than native by `nzo_id` | Low; §5 already flags this one |
| 12 | **`tests/fake_sabnzbd.py` inherits every guess above** rather than independently corroborating any of them | This is the §13.2 trap by construction, and why the list exists |

### 13.5 The live validation loop

The test system is **https://lftpweb.crzynet.com**, running `dev`. It exposes `/api/health`,
`/api/history/events`, `/api/logs/*`, `/api/settings/*` and `/api/files` — so a stage deployed there
can be validated by reading its events and logs directly, without asking the user to relay output.
**It runs SABnzbd and rTorrent today**, with more clients to be added as the framework grows.

---

## 14. Build order

Each stage is independently shippable. Nothing before stage 5 can delete anything.

| Stage | What | Notes |
|---|---|---|
| **0** | Interface, enums, capability declaration + profiles, registry, conformance suite, a fake adapter | Ships with nothing configured. **This is the piece the vocabulary must be right in**, so §13.3's capture ideally informs it |
| **1** | SABnzbd adapter, instance CRUD, declared config form, test-connection, capability readout, **the redacted capture** (§13.3), **and the README write-up of the reference workflow** (§1.1) | First real client contact. The README section is the user's explicit ask: document the *preferred* seedbox setup, so other workflows are recognisable as departures from a stated one |
| **2** | The poller (§9), SAB as a third Preflight source, the settle-gate skip | #18's first real user-facing payoff |
| **3** | Withhold on partial failure (`docs/transfers-redesign-spec.md` §4.3) | |
| **4** | The disk review scan (§11), both buckets, review-only | Looked at against the real box before anything may delete |
| **5** | The delete pipeline (§10), manual trigger, verification, banner | |

**Deletion is stage 5 deliberately.** The scan gets built and inspected against a real seeding
estate before any code path is allowed to remove. Auto mode is the same code minus the trigger, and
belongs to #21.

---

## 15. Open questions

1. ~~**Which client feeds `ar-tv` / `ar-movies` on the test system?**~~ **Answered 2026-08-22:
   both.** SAB and rTorrent share the TV completed folder; SAB extracts into it, rTorrent hardlinks
   into it. See §1.1, and §11.1a/§11.1b for the two correctness requirements this forces.
2. ~~**What are rTorrent's base paths on the test system?**~~ **Not an open question — the user
   sets them in Settings** (§8.2), and no implementation step needs to know them in advance. The
   scan discovers its roots from configuration at run time, and whether the SSH walk can actually
   read a configured root is answered by save-time validation
   (`core/browse.py.remote_directory_error`) and then by the scan itself, not by asking.
3. **Should `move`-mode source deletion consult the client before deleting?** Once a connector knows
   which client owns a release, the existing delete could ask — *"still seeding, withhold and
   explain."* That is §4.1's advisory vocabulary applied to a delete lftpweb already performs, and
   it is arguably a stronger argument for this framework than the settle-gate skip. **Not scoped
   in**; raised for a decision after stage 2. Note §10.5 lowers its urgency considerably: on the
   reference workflow the `move` delete removes a hardlink and the seed survives, so the question
   is about setups *without* the hardlink step.
4. **Does the ruTorrent-vs-rTorrent API surface question (`docs/torrent-manager-spec.md` §10.1)
   resolve to XML-RPC directly, or the ruTorrent HTTP plugin API?** Deferred to the rTorrent
   connector, and now much lower stakes: §10's SSH deletion removes the `erasedata` plugin from the
   critical path entirely, which was the main thing that made the choice consequential.
