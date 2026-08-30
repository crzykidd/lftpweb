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
| `list_base_paths` | The client's own configured download/complete directories, each with its own role (content vs. working) | **Detected, then SSH-verified, then confirmed** (§8.2) — the connector's own answer proposes both the path and its role; whether lftpweb sees that path at the same spot over SSH is a separate question the settings UI verifies before anything is saved. Never saved on the strength of the client's report alone |
| `list_categories` | The client's own categories/labels, by name | **Joined the vocabulary 2026-08-23** (§8.3 correction, `prompts/2026-08-23-category-binding-redesign.md`) — `Field.CATEGORY` only ever reports a category *in use* on an existing transfer, so a client with an empty queue and empty history (precisely the fresh-setup case the binding UI is for) reports none. This operation asks the client directly. **Detected, then proposed, then confirmed**, same shape as `list_base_paths` — except there is nothing to SSH-verify, since a category is a name the client owns, not a filesystem path |
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
| `list_categories` | native | native — **joined 2026-08-23**; both baselines assume a real, enumerable category list (true for most torrent clients too), so a connector without one (rTorrent) is expected to override down itself, same pattern `Field.SEED_TIME_S` already uses |
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

### 8.2 Base paths are detected from the client and SSH-verified, not typed in

**Correcting an earlier reading of this section (decided with the user, 2026-08-22).** The
original wording here said base paths are user-configured and `list_base_paths` is a prefill
only, justified by "rTorrent's `directory.default` will never mention the completed folder it
hardlinks into." **That reasoning was wrong.** True as a fact, but irrelevant: that folder is
the queue's `remote_path`, which lftpweb already knows on its own — it never needed a
connector to name it. The correction, mirroring the same "earlier reading, why it was wrong,
what's right instead" style §11.1c uses:

- **`list_base_paths` (§2.1) already answers two things at once, not one.** `SabnzbdClient.
  list_base_paths` returns `complete_dir` and `download_dir`, and the **role each one plays is
  already known** because the connector knows which config key it read each path from
  (`core.clients.models.BasePathKind` — `content` vs `working`, migration 028). Asking the user
  to classify a path the API already answers was pushing a solved question back out to them.
- **The one thing a connector genuinely cannot know is whether lftpweb sees a path at the same
  spot over SSH.** A containerised client reports paths in its own filesystem view (`/complete`)
  which need not match the SSH-visible view lftpweb actually scans and deletes within
  (`/home/user/downloads/complete`). This repo already solved the identical problem for the
  *arr with `path_queue.arr_visible_path` (migration 018) — this feature mirrors that design,
  inverted: `download_client_base_path.path` is the SSH-visible, authoritative path (same role
  `arr_visible_path`'s *counterpart* plays there); `client_path` records the client's own view,
  present only when it differs, for display and diagnosis alone.
- **So the flow is detect → verify → confirm, not type → browse → validate.** `POST
  /api/settings/clients/{id}/test` calls the connector's own `list_base_paths` (when declared;
  a connector that doesn't is not an error, it simply contributes nothing) and SSH-verifies each
  reported path via `core/browse.py.remote_directory_error`, returning one of three states that
  are **deliberately not collapsed**: `verified` (client and lftpweb agree), `not_found` (the
  seedbox clearly reports it missing — the namespace mismatch, detected rather than asked
  about; the user supplies the SSH-visible equivalent, with the existing browse dialog to find
  it), and `unverified` (the stat failed for any other reason — permission, protocol, no SSH
  connection to try at all — never presented as a failure, per `remote_directory_error`'s own
  docstring). **Detection proposes; it never saves** — the settings page shows what was found,
  and the user accepts or translates each one before Save persists anything, same rule as the
  existing category → queue inference (§8.3).
- **What did not change: the SSH-visible path is still authoritative for scanning and
  deletion**, because it is the §10.2 containment boundary. Save-time validation against
  `core/browse.py.remote_directory_error` still applies to whatever ends up in `path` — a wrong
  root is still a wrong safety boundary, detected-and-confirmed or typed by hand through the
  manual-add escape hatch (for a path no connector exposes). What changed is *where the
  proposal comes from*, not which path wins once confirmed.
- **A detection failure must never fail the connection test.** Reachability and detection are
  different questions (§4.2's temperament) — a client that answers but whose base paths can't be
  SSH-checked right now (no host configured, credentials not decryptable) still tests `ok`, with
  every detected path reported `unverified`.
- Multiple roots per instance, since a seedbox routinely spreads content across several.

**Save also tests, for an enabled instance.** `POST`/`PUT /api/settings/clients` now test the
*submitted* configuration before persisting anything when `enabled: true` — mirroring how
Sonarr/Radarr's own Download Clients page behaves, per the user's own instruction, 2026-08-22:
*"we should test at save and not save if enabled and test failed."* A failing test on an enabled
save persists nothing at all (not the instance, not a partial row) and reports the real error;
`enabled: false` never tests and always saves — the deliberate escape hatch so a temporarily
broken client never locks the user out of editing (or disabling) their own instance. A
successful save persists the probed capabilities and version from that same test, so a freshly
saved instance never needs a separate Test click to show them.

### 8.2 correction, 2026-08-23 — a `~` client_path is offered, expanded, never applied blind

**Found on the live test system, 2026-08-23** (finding #1, `prompts/test-findings-2026-08-23.md`):
rTorrent's `directory.default` answered `~/downloads/rtorrent` — a real, existing directory
(`/home/crzykidd/downloads/rtorrent`) — and detection reported it `not_found`. Not a namespace
mismatch; `remote_directory_error`'s literal `stat` never expands `~` (no SFTP server does), so it
correctly reported "missing" for a path it never actually looked at.

**The fix, the user's own proposed shape:** *"You are always connecting from a user context. If
we can get home dir and pwd, we should give an option in the box with a note that says: It
appears your ~ path pwd is xxx."* Offered, not applied — the same propose-don't-apply rule this
section's `not_found` box already follows for every other translation. `core.clients.detection.
_resolve_tilde_candidate` expands a `~`/relative `client_path` via `sftp.realpath` (the identical
primitive `core.browse.resolve_remote_dir` already uses for the browse dialog) and re-verifies the
result over SSH before ever offering it — a wrong guess is worse than no guess. The result rides
along as `DetectedBasePath.resolved_candidate`, still `None` for an already-absolute path or one
whose expansion doesn't check out either (a genuine miss must not turn into a false suggestion).
The settings UI pre-fills the `not_found` (and, for the same reason, the `unverified`) box's input
with the candidate and states it plainly, still requiring an explicit Add before anything saves.
**Decided deliberately to layer this at the detection layer** (`core.clients.detection._verify_one`
calling `_resolve_tilde_candidate`), not by making `core.browse.resolve_remote_dir` and
`remote_directory_error` behave identically — the two differ on purpose (the first falls back
gracefully for a half-typed browse-dialog field; the second exists specifically to give a real,
blocking answer at save time), and collapsing that distinction to fix one caller's need would have
been the wrong-shaped fix.

**The constraint that matters most: a `~` path must never be what gets stored.** Every downstream
consumer — the disk-review walk roots, and stage 5's containment check that authorises `rm -rf` —
inherits the expansion problem otherwise; a containment check comparing `~/downloads/rtorrent`
against `/home/crzykidd/downloads/rtorrent` matches nothing, and a delete boundary that silently
matches nothing is a bad way to fail. This holds structurally, not just for the `not_found` box:
`unverified`'s own "Accept anyway" (a `~` path reaches this state too, on an ambiguous stat
failure) no longer offers a direct accept for a non-absolute `client_path` either — it falls back
to the same ask-for-the-SSH-visible-equivalent box, pre-filled with the resolved candidate when
one exists. `client_path` still records the client's own literal `~` form once a translation is
accepted, exactly as this section's `client_path` column already exists to do.

`not_found` and `unverified` remain deliberately distinct through this change — the resolution
above only ever adds a suggestion to whichever state the literal stat produces; it never turns one
into the other.

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

**Inference reads the instance's own *saved* base paths (§8.2) — whatever was accepted after
detect → verify → confirm, or added manually — not a fresh live `list_base_paths` probe of its
own.** An earlier wording here named the client's result and read as if a probe were required at
inference time specifically; that would make inference a precondition on the connector
answering, which it must never be. §8.2's own detect-then-confirm step already is the place a
live probe belongs; inference simply reuses whatever base paths that step produced, the same way
it would read a manually-added one.

### 8.3 correction, 2026-08-23 — the control is redesigned; path arithmetic is now the fallback

**The above was wrong as a *primary* mechanism, and real use proved it within one session**
(findings #10, #11a-c, `prompts/test-findings-2026-08-23.md`). Path arithmetic is a proxy for the
category mapping that only ever holds for SABnzbd's `<base>/<category>` layout, and can never
work for rTorrent, whose `d.custom1` labels have no relationship to any directory. On the live
test system it also proposed nothing for SAB, because no base path had been configured yet — the
exact fresh-setup case the binding UI exists for. Compounding it, the free-text category
`<input>` carried `placeholder="ar-tv"` — greyed text that read as a filled-in recommendation but
was not a value — and the save silently filtered rows whose `category` was blank, so the user's
mappings vanished on every edit (#11b/#11c).

**The fix, the user's own design (2026-08-22/23):** `list_categories` (§2.1) joins the operation
vocabulary. The settings UI now shows one row per category the client actually reports — the
category name as **text, not an input** — bound to a queue dropdown defaulting to **"— not
used —"**. A binding is suggested (pre-selected, never placeholder text) when a queue's name or
the trailing segment of its `remote_path` matches the category. **There is no free-text category
field anywhere in this control any more** — #11b/#11c's defect class, not merely its symptom, is
gone: with nothing to type, there is no blank row the save could ever silently drop.

Path-arithmetic inference (this section's original text, above) is **retained only as a labelled
fallback** for a client that reports no categories at all (a fresh SAB with nothing configured
yet, an rTorrent with nothing labelled) — it can still propose something from an empty queue,
where a category list genuinely cannot. The UI states which mechanism produced a row's
suggestion; the two are never blurred together.

**Two decisions made explicitly, not by default (`docs/decisions.md`, 2026-08-23):**

- **Uncategorised items are never given a bindable pseudo-row.** An rTorrent torrent with no
  `d.custom1` label, or a SAB item under no category, is simply not attributable — the same
  silent-omission rule §8.3's own mapping already applies to any unmatched item, not a new
  exception carved out for "no category."
- **A category that appears later is surfaced by re-testing, not by a background poll.** Clicking
  Test while an instance is open in the edit form recomputes its category rows against the
  freshly detected list immediately; nothing auto-probes on page load, matching §8.2's own
  base-path detection, which also only ever runs on an explicit Test click.

### 8.3 correction, round 4, 2026-08-23 — path attribution is primary; the mapping is a fallback

**The above was still wrong, one level up from the round-3 correction.** Round 3 fixed the
*control* (no free-text field, nothing to silently drop) but left the underlying rule unchanged:
`core/clientsync.py._update_preflight` attributed a transfer to a queue **only** through the
category mapping, dropping anything without a category — or with an unmapped one — before ever
looking at where its bytes actually are. Live use (`prompts/test-findings-2026-08-23.md` #2/#10)
surfaced the user's own words twice: *"the category is ar-tv and the dir for that is ar-tv"*, and
*"this makes zero sense to me"* — they were made to configure something the filesystem already
answers. SABnzbd's history `storage` field, and rTorrent's `content_path`, both land inside (or
at) a queue's own `remote_path` in the reference layout; a queue's `remote_path` **is** the
on-disk root a connector's finished items land under, for a connector whose reported path sits
there at all (see the caveat below — it does not for every connector).

**Attribution order, decided:**

1. `content_path` matches an enabled queue's `remote_path` — component-boundary containment or
   equality, **never a bare prefix** (`/complete/ar-tv` must not match `/complete/ar-tv-extra`) —
   → that queue. No category mapping consulted, no configuration needed.
2. Otherwise (most commonly: nothing on disk yet, still queued at the client with no
   `content_path` to check) — the configured category → queue mapping, if one exists. This is now
   that mapping's **whole remaining job**.
3. Otherwise — silently omitted, unchanged from every earlier round.

**Path wins on disagreement.** A transfer whose path matches one queue but whose category is
mapped to a different one is not a tie: the path is where the bytes actually are, a stale or wrong
category mapping is not. The mismatch is logged, not silently resolved in the mapping's favour —
a disagreement is a signal the user's configuration is wrong, not noise to suppress.

**Reused, not reimplemented.** The component-boundary matching rule is exactly
`core/settle.py._client_content_path_matches` — already built for the stage 2b settle-gate skip
and the stage 3 withhold gate, which match a transfer's `content_path` against one *item's*
remote path (`queue.remote_path` + `rel_path`) the identical way. This section's own attribution
matches against a *queue's* remote path directly (there is no single item yet — the question is
"which queue," not "is this settled"), but the underlying rule — equality or containment at a `/`
boundary — is the same fact about paths either way, so it is imported and reused rather than
forked into a second copy that could drift from the first.

**This does not make the category mapping optional for every connector — corrected, 2026-08-23,
same day, on further live evidence.** An earlier draft of this correction claimed most setups
would need the mapping "not at all" once path attribution shipped. That is true for SABnzbd,
whose history `storage` field points inside the queue's own folder, and **false for rTorrent**:
per §1.1, rTorrent reports its own *seeding* directory as `content_path`, not the hardlinked copy
under the queue's `remote_path` (`/home/crzykidd/downloads/rtorrent/...` vs.
`/home/crzykidd/downloads/complete/ar-tv`) — two different trees under the common hardlink
layout. An rTorrent transfer's path therefore essentially never matches a queue root, and the
category mapping remains **rTorrent's only attribution route**, exactly as before this task. Path
attribution's real win is narrower than first stated: it removes the mapping requirement entirely
for a usenet-style connector whose reported path already lands in the queue's folder, and it
still helps a torrent client only insofar as its `content_path` happens to coincide with a
queue's `remote_path` (an unusual layout where the queue points at the seeding directory itself).
The settings UI states this per-connector, not as a blanket "most setups won't need this."

**Open question this correction surfaces, deliberately left unresolved by this task:** an
rTorrent torrent with no `d.custom1` label *and* a `content_path` that doesn't match any queue
(the ordinary case under a hardlink layout) has **no attribution route at all** — not by path, not
by category — and is silently omitted, identically to before this task. §8.3's own prior decision
("uncategorised items are never given a bindable pseudo-row," above) may need revisiting in light
of this — a torrent client's unlabelled item is now the one case with genuinely no way to ever
become visible, where a SAB item under no category at least has an on-disk path a future
mechanism could reach. Not decided here; see `docs/decisions.md`.

### 8.3 correction, round 5 (2026-08-23) — categories are three-state; exclusion is a safety
boundary, not a preference

**The deployment shape that forces this:** the user runs **two lftpweb instances against one
seedbox** — one SABnzbd, one rTorrent, both serving both instances, each lftpweb with its own
*arr pair and its own subset of the download locations. Each instance permanently sees work that
is not its business; that is the steady state, not a misconfiguration
(`prompts/2026-08-23-category-tristate-and-exclusion.md`, findings #15/#16).

**A category is now three-state, saved explicitly** (migration 031,
`download_client_category.excluded`):

| State | Persisted as | Does the unattributed-clients banner warn? |
|---|---|---|
| **Bound** to a queue | `queue_id` set, `excluded = 0` | No |
| **Explicitly "not used by this instance"** | `queue_id = NULL`, `excluded = 1` | **Never again** |
| **Undecided** — never looked at | `queue_id = NULL`, `excluded = 0` | Yes |

Mutually exclusive by construction — `DownloadClientCategoryIn`'s own validator rejects a row
that sets both `queue_id` and `excluded`. `core.clientsync.ClientSyncScheduler._update_preflight`
consults the excluded set per instance and skips it before it can ever reach
`unattributed_clients`'s own count — a client whose every category is bound or explicitly
excluded now produces a **silent** banner, which is the entire point: a warning that can never be
resolved stops carrying information (the same failure §2's silence named, with the opposite
sign).

**Exclusion is a hard scan/delete boundary, not merely a silenced banner (finding #16's own
load-bearing half).** The disk review scan (§11) proposes `B − A − C`; the other instance's
content is protected only by set A, and that protection expires the moment the other instance's
release drops out of its own client's history or active list. "Not used by this instance" must
therefore also mean: never scanned, never proposed as debris, never inside §10.2's containment
boundary. Two mechanisms, deliberately layered:

- **`download_client_excluded_path` (migration 031) is the enforceable primitive** — a path (or
  sub-path) typed directly, exactly expressing "this tree belongs to the other instance." It is
  what `core/disk_review.py.reconcile`'s new `excluded_paths` parameter consumes, and the only
  thing that works when a category has no relationship to any path at all.
- **An excluded *category* is a convenience that resolves into a path wherever spec §1.1's
  `<base>/<category>` layout holds** — `core/disk_review.py.resolve_category_exclusion_paths`,
  called by `run_scan` against the owning client's `content`-kind base paths only.
- **Where it cannot resolve — FAIL CLOSED.** A client with no `content`-kind base path at all
  (rTorrent: its only declared base path is its seeding/`working` directory, unrelated to any
  category folder) cannot have its excluded category translated into a path. `run_scan` then
  suppresses debris for **every one of that client's declared base paths**, via `unavailable_
  roots`, with a stated reason — never a guess at a path arithmetic cannot produce. "A scan that
  proposes less is always preferable to one that proposes someone else's data."

**The per-client relevance copy is computed from observation, never from `client_type`**
(`core.clientsync._attribution_sample`, migration 031's `attribution_sample_size`/
`attribution_matched_by_path` columns, `frontend/src/lib/clientAttribution.ts`) — "12 of 12
recent downloads matched by folder, no mapping needed" or "0 of 2 matched, a mapping is
required," the same sentence template for SABnzbd and rTorrent alike, driven by what was actually
observed rather than a hardcoded "usenet doesn't need it, torrent does." That generalisation had
already been wrong four times before this correction (§8.3's own round-3/round-4 history above).

### 8.3 correction, round 6 (2026-08-23) — every observed category is recorded, defaulting to
excluded; the banner and the scan's fail-closed rule are both narrowed to what's actually true

Four defects reported the same day, against a real seedbox, in the code round 5 above just
shipped (`prompts/2026-08-23-auto-add-categories-default-excluded.md`):

1. **A category the poller sees never reached Settings.** Only a manual **Test**'s own
   `detected_categories` were ever written to `download_client_category` — the poller observed a
   category on every pass and discarded the observation. `core.clientsync.
   persist_observed_categories` (migration 032) is now called from **both** routes (a poll pass,
   in `_process_instance`, and `test_client_instance`) — one function, so "is this category new"
   is never answered twice. Only ever **inserts** a category never seen before; an already-decided
   row (bound, excluded, or a queue-deleted-back-to-undecided one, below) is never touched.
2. **This reverses round 5's own default.** A newly recorded category now lands **`excluded = 1`**
   ("not used here"), not undecided — the safer default, not merely the quieter one, for the
   two-instances-one-seedbox shape this whole correction exists for: arriving excluded means the
   content is never walked, never proposed as debris, and never inside the §10.2 containment
   boundary until a person deliberately opts it in, rather than sitting exposed until someone
   happens to notice and act.
3. **The "undecided" state is kept, not removed** — it is not actually unreachable: `download_
   client_category.queue_id REFERENCES path_queue (id) ON DELETE SET NULL` (migration 027) means
   deleting a bound category's queue produces exactly this state as a side effect, and the banner
   correctly should still warn about it (a broken mapping the user needs to know about). What
   changed is only which state a *fresh observation* lands in.
4. **Consequence handled, not ignored: the banner going quiet for excluded categories means one of
   the user's own new categories could do nothing unnoticed.** Not solved with a second banner —
   `download_client.categories_acknowledged_at` (migration 032) plus each category's own
   `first_seen_at` drive a calm count on the Clients row (`newCategoryCount`, "+N new"), cleared
   the instant the instance is opened for edit (`POST .../acknowledge-categories`), no button, no
   confirmation.

**Two more defects surfaced in the same live session, both about staleness in the code this
correction touches:**

5. **The unattributed-clients banner was computed and cached at *poll* time**, so excluding a
   category in Settings didn't clear it until the next poll pass happened to run.
   `ClientSyncScheduler.unattributed_clients` now mirrors `core/arrsync.py.ArrSyncScheduler.
   preflight_rows`'s own 2026-08-21 "eviction latency" fix exactly: `_update_preflight` caches
   only the **raw**, unfiltered per-category breakdown; `unattributed_clients` re-applies a
   **freshly read** exclusion set on every call. One predicate, one place the question is asked,
   never baked into a once-per-poll-interval cache.
6. **Round 5's fail-closed rule (`### round 5` above) was too blunt.** Suppressing debris for a
   client's *entire* declared base path when a category couldn't resolve to a path also hid that
   root's **seeding estate** — legitimate, already-claimed content that was never at risk, live
   evidence: *"there are things in there in ar-tv that it doesn't show now."* Narrowed to a
   per-file rule: a **claimed** file is resolved directly off its own transfer's category
   (`ClientClaim.category`, universal, no path arithmetic needed) — bound survives normally,
   excluded is dropped outright, for every client, not only the ones a category can't resolve a
   path for. Only a genuinely **unclaimed** file under such a root remains ambiguous (it might be
   the leftover of a since-vanished excluded-category claim) and is fail-closed —
   `core/disk_review.py`'s new `debris_ambiguous_roots`/`SuppressedDebrisItem`, reported as "N
   items suppressed," never "N base paths skipped." The root is still walked; only debris
   proposals for its unclaimed remainder are narrowed.

### 8.4 Which client fetched an item — persisted forward-only, drawn as a row icon

2026-08-30 (`prompts/2026-08-30-downloader-icon-on-rows.md`, migration 033): the user's own ask —
*"add another icon to the list, right next to the ARR icon... the SAB or rtorrent icon based on
what downloader was used."* The match already existed, computed and thrown away every pass:
`core/settle.py.find_client_completion`'s own `content_path`↔item-remote-path component-boundary
check (§8.3's own path-attribution rule), run by `core/autoqueue.py.on_scan` purely to decide
whether to skip the settle wait. This task persists it as a fact about the item, not merely a
transient signal for one gate: `item.download_client_id`/`download_client_matched_at`, joined into
`JobOut`/`HistoryJobOut` the identical way `item.arr_status` already joins `arr_instance` (§8.1's
own precedent) and drawn as a small `ClientBrandMark` chip beside the existing *arr chip.

**Written from `core/clientsync.py.ClientSyncScheduler._update_preflight`, and from nowhere
else.** `find_client_completion`/`find_client_failure`'s only real callers
(`core/autoqueue.py.on_scan`) run behind `SettleSettings.client_skip_enabled` and
`WithholdSettings.enabled` — both off for some installs (§14, §17.8). Writing attribution from
either path would make the row icon's mere *presence* silently depend on a setting that has
nothing to do with it — this is the one mistake this task's own handoff prompt names directly as
the most likely to be made here. `_update_preflight` is the one call in this whole subsystem that
runs unconditionally on every successful poll pass, so it is the only place this can be written
from and still be true for every install regardless of what the user has switched off.

**Item-level, not queue-level — a new pass over the same data, not a new matching notion.**
§8.3's path attribution already resolves a transfer to a *queue*; this adds a second pass, over
the same `transfers` list, against every `item` row under an enabled queue not already attributed
to *this* instance, reusing the identical `_client_content_path_matches` component-boundary rule.
A transfer with no `content_path` can only ever identify a queue (via the category mapping, when
one exists) — never a specific item inside it — so a category-only transfer writes nothing,
silently, the same "no information, no write" instinct every other attribution path in this spec
already follows. An item that doesn't exist yet (not yet discovered by a remote scan) is equally
silent this pass; a later pass, once the item exists, catches it — there is nothing to retry or
remember having tried.

**Write once and leave it.** An item already attributed to *this* instance is excluded from the
candidate query outright, so a quiet repeat match issues no write and `download_client_matched_at`
never drifts on an unchanged pass. An item currently attributed to a *different* instance (or
never attributed) remains a candidate and IS overwritten on a fresh match — a release genuinely
re-fetched by a different client is a real fact worth recording, not noise to suppress.

**Forward-only, by explicit, informed user decision — no backfill.** The alternative considered
and rejected was resolving this live, at *read* time, from the poller's own in-memory transfer
cache instead of persisting it: rejected because both connectors age old jobs out of their own
history/queue (§9.1), which would make a History row's icon silently vanish the moment the client
forgets a job lftpweb still remembers forever. Persisting once means the icon is either right or
absent — never flickering. Every item downloaded before migration 033 shipped has no recorded
client and never will; see `docs/decisions.md`.

**`client_instance_kind` choosing the chip's label is a display switch, not the client-name
branching §4.4/§5.1 forbid.** That rule governs *behaviour* only — capability gating, field
support, what gets sent over the wire — never which picture (or short text label) a row draws.
**No brand logo**: simple-icons, the CC0 dataset this project's Sonarr/Radarr row-line marks copy
path data from verbatim, ships neither a `sabnzbd` nor an `rtorrent` mark (checked directly
against the dataset for this task). The chip is therefore always the same text-fallback treatment
this project already uses for an unrecognized *arr `kind`, never an invented or approximated logo.

**Extended to Preflight and the Files tree the same day** (`prompts/2026-08-30-client-chip-on-
files-tree.md`), from the user's own follow-up report: *"I don't see the SAB tag in the list on
preflight, but I do see it in active/pending... we should show the chip for SAB in all if it was a
SAB process."* Three surfaces now draw this chip, and each resolves `client_instance_kind`
differently, on purpose:

- **Transfers/History** — `client_instance_name`/`client_instance_kind` on `JobOut`/
  `HistoryJobOut` directly, as this section describes.
- **Preflight** (`PreflightBox.tsx.Badge`) — was already drawing a badge per row source (§8.3's
  own *arr/client merge); this task only changes *which label* a client-sourced badge shows, from
  the instance's free-text configured name to `lib/clientBrandMark.clientBrandLabel` — the
  identical function and short label (`'SAB'`/`'rT'`) the row-line chip uses — gated on
  `badge.source === 'client'` so an *arr badge is never affected. The configured instance name
  survives as the hover `title`, unchanged.
- **Files tree** (`FileTree.tsx`) — the one surface with no such field until now:
  `item.download_client_id` joined to `download_client.name`/`client_type` in `core/itemview.py.
  item_view`, the identical `client_instance_name`/`client_instance_kind` names and shape, added
  to `FileNode`. **Resolved differently from the *arr chip on the same row, deliberately**: the
  *arr *kind* isn't a per-item fact (`FileNode` carries only `arr_status`/`arr_status_at`), so
  `FilesPage.tsx` resolves it from the item's *queue* binding and threads it down as a prop
  (§16's own precedent). The download-client kind *is* a per-item fact already sitting on the
  node, so `FileTree.tsx` reads it straight off `entry` — no prop, no queue-level resolution step.
  Recorded in `docs/decisions.md` so a future reader does not "unify" the two into one shape.

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

**Correction, stage 2b build (2026-08-23): the table above is wrong about which cadence actually
feeds the settle-gate skip.** `list_transfers(active_only=True)` (the fast row) excludes every
terminal transfer by both connectors' own contract (SABnzbd's queue slots never reach `COMPLETED`
at all -- that status only exists in history; rTorrent's `active_only=True` explicitly filters
`COMPLETED` out, §13.6's own `list_transfers` note). The only cache that ever holds a `COMPLETED`
verdict is `core/clientsync.py.ClientSyncScheduler._full_estate` -- the **slow** cadence's
`active_only=False` result. Stage 2b's implementation therefore reads `_full_estate`
(`finished_transfers()`, renamed 2026-08-24 from `completed_transfers`), not the fast cadence's
own Preflight cache, so a client-verdict skip is
bound by `SLOW_INTERVAL_S` (5 minutes) freshness, not `FAST_INTERVAL_S` (10s) as this table implies.
This does not defeat the feature -- the skip only ever *shortens* a wait that is already `>=
SETTLE_MIN_AGE_S` (60s) under the plain settle gate, so a same-cadence-as-Preflight skip was never
load-bearing the way a same-cadence Preflight *row* is -- but the table's own "Consumer" column
should be read as aspirational for this row, not as what stage 2b actually built. Left uncorrected
above (rather than rewritten) so this note stands as the record of the discrepancy; see
`docs/decisions.md` (2026-08-23) for the fuller reasoning.

**Fixed, stage 3 build (2026-08-23): the table's split was drawn along the wrong axis, and this
correction is superseded by an actual fix, not just a documented discrepancy.** The table above
frames the split as *active-vs-everything* (`active_only=True` vs `active_only=False`). The
correction just above diagnosed the resulting bug correctly (a terminal verdict is structurally
invisible to `active_only=True`) but the fix is not "wait longer" -- it is that the split should
never have been drawn on `active_only` at all. The right axis is **cheap-vs-expensive**, and
which side a connector falls on is a fact `Operation.LIST_HISTORY`'s own NATIVE/DERIVED
declaration (§5) already carries:

- **NATIVE** (SABnzbd, `USENET_BASELINE`) -- a real, independent, trivial call. Every non-slow
  tick now calls `list_transfers(active_only=True)` **and** `list_history()`, and the terminal
  results of the latter are merged into `core/clientsync.py.ClientSyncScheduler._full_estate`
  immediately -- no more waiting for `SLOW_INTERVAL_S`.
- **DERIVED** (rTorrent, `TORRENT_BASELINE`: "a torrent never leaves the list") -- `list_history()`
  is not a second cheap call; it re-fetches the identical expensive full listing
  `list_transfers(active_only=False)` already pays for. Calling it every fast tick would double
  the exact cost this section's own "waste" framing warns against, so it stays on the slow
  cadence, unchanged from stage 2a.

`ClientSyncScheduler._process_instance` decides which case applies via
`client.capabilities.supports(Operation.LIST_HISTORY)` alone -- no `client_type` branch anywhere
in the scheduler (§4.4/§5.1's rule, applied here as this task's own explicit requirement). The
"Consumer" column above is therefore accurate again for the settle-gate skip and the withhold gate
(§14 stage 3) both: a terminal verdict is now visible within one `FAST_INTERVAL_S` tick for any
connector whose history is cheap, exactly as this table originally promised. See
`docs/decisions.md` (2026-08-23, "cadence split fixed to cheap-vs-expensive") for the fuller
reasoning and the rejected alternative (a connector-authored boolean flag, rejected as a second
truth sitting next to a capability declaration that already says the same thing).

**Two more corrections, 2026-08-29 (prompts/2026-08-29-preflight-poll-freshness.md), live use:
*"when things are in preflight we should update from SAB or rtorrent more often."***

**Defect 1 -- the "within one `FAST_INTERVAL_S` tick" promise just above was still broken for
rTorrent, in a commit (`cc5f75d`) that shipped the very same day it widened `settle.
FINISHED_TRANSFER_PHASES` to `{COMPLETED, SEEDING}`.** Two independent misses stacked:
`_process_instance`'s own fast-tick merge into `_full_estate` still filtered on the old, narrower
`(COMPLETED, FAILED)` pair (a third hand-restated copy of "what counts as terminal," never
updated to match); and the merge itself only ran `elif cheap_history:` -- i.e. only for a
`NATIVE`-history connector (SABnzbd) -- which structurally excluded rTorrent (`DERIVED`) from
ever merging *any* fast-tick data at all, even though rTorrent's own `active_only=True` result
already reports `SEEDING` torrents at zero extra cost ("a torrent never leaves the list,"
`active_only` excludes only `COMPLETED`). Both are fixed together: the filter is now derived from
the two consumers' own constants (`settle.FINISHED_TRANSFER_PHASES` plus `TransferPhase.FAILED`,
`core/clientsync.py._MERGEABLE_TERMINAL_PHASES`) rather than hand-restated a fourth time, and the
merge now runs on every non-slow-due tick unconditionally -- it costs nothing extra either way,
since `transfers` at that point is exactly what the tick already fetched.

**Defect 2 -- a new, shorter cadence for an instance that currently has something in Preflight.**
`ACTIVE_POLL_INTERVAL_S` (4.0s, `core/clientsync.py`) applies only to the fast, active-only call
for one instance, and only while `_update_preflight`'s own `seen` rows for that instance are
non-empty (`ClientSyncScheduler._active_instances`) -- reusing what that method already computes
rather than adding a second, independently-drifting notion of "busy." `SLOW_INTERVAL_S` is
completely untouched: Preflight's own data never comes from the full-estate call in the first
place, so there is nothing this constant could speed up by also shortening the slow cadence, only
cost to add. An instance with nothing currently in Preflight is never sped up -- it falls back to
`FAST_INTERVAL_S`, via a new per-instance due-check in `_process_instance` (`_last_active_poll_at`)
that did not exist before this stage; previously, cadence was governed purely by how often the
*caller* invoked `run_once` (in production, `_loop`'s own sleep), with no gate inside
`_process_instance` itself. `_loop` now wakes every `min(FAST_INTERVAL_S, ACTIVE_POLL_INTERVAL_S)`
so a busy instance *can* be polled that often; the due-check is what keeps a quiet one from being
dragged along for free. **The backoff ladder wins regardless** -- the existing backoff check in
`_process_instance` runs, and returns, before the active-poll due-check is ever consulted, so an
instance backing off is never polled faster merely because it has Preflight rows cached from
before it broke.

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

#### The provenance-display gap (found 2026-08-23, finding #3)

**This section specifies *precedence* — which value wins per field — and says nothing about
*provenance display*: that a row deduped across two sources should still show both of them.**
That is a real, separate gap, not a restatement of the rule above. It surfaced because the
original symptom it was mistaken for — Preflight showing the *arr's status rather than the
client's — was measured with **zero** client rows reaching the merge at all (§9.3's own root
cause, finding #2): with nothing on the other side of the merge, this section's precedence had
nothing to prefer, so it looked broken when it had simply never been exercised. Once client rows
flowed, the precedence itself was already correct (`tests/test_preflight_client_merge.py::
test_client_status_label_always_wins_when_present` predates this note and passed unmodified) —
but the user's own request, verbatim, was never only about which value wins: *"We should show a
sonarr AND a SAB icon."* `PreflightRow` carried a single `source`/`source_label`/`source_kind`
triple, so a merged row could only ever display one badge, regardless of how correct its
precedence was.

**Resolved 2026-08-23** — `core/preflight.py.PreflightContributor` (a new, minimal dataclass: the
same six display fields `PreflightRow` itself carries per-source, nothing else) and `PreflightRow.
contributors: tuple[PreflightContributor, ...]`, `()` for every row a source constructs for
itself, populated only by `api/jobs.py._merge_client_field_into_arr` with both pre-merge views
(*arr, then client) when a row folds together. The frontend's `lib/preflight.ts.preflightBadges`/
`preflightDetailEntries` render one badge — and, on expand, one detail line — per contributor,
falling back to the row's own top-level fields for a standalone row so no row ever shows an empty
second slot. Provenance display and field precedence are now both covered, by two different,
independently testable mechanisms — this section's own rule for the latter, `PreflightRow.
contributors` for the former.

### 9.3 Visibility — a working client must never look identical to a broken one

**Found on the live test system, 2026-08-23** (finding #2 and its reinforcing observation,
`prompts/test-findings-2026-08-23.md`): two enabled, authenticating instances (SAB 5.1.1, rTorrent
0.9.8) produced **zero** Preflight rows and **zero** events between them, because neither had a
category → queue mapping (§8.3) yet, and an unattributable row is *silently omitted* — the correct
rule for the *arr source (§4.2's own "promising a release that never arrives is worse than showing
nothing"), applied here too by inheritance. **The rule is right for the *arr and wrong here.** For
the *arr, an unattributable queue record genuinely is noise. For a configured, authenticating,
explicitly-enabled download client, silence is the worst outcome: a fully-working instance is
indistinguishable from a broken one, and nothing anywhere says why. Compounding it, `core.
clientsync.ClientSyncScheduler` mirrors `core/arrsync.py`'s transition-only event rule exactly —
right for the *arr, which earns its silence by emitting `arr_matched`/`arr_notified`/`arr_imported`/
`arr_cleanup` at real lifecycle points — but the client poller has no such equivalent, so a
correctly-configured, fully-working instance was **completely invisible**: no rows, no events, no
status anywhere. The only thing that ever proved the integration was alive was it breaking.

**Three additions, none of them a per-poll event** (a 10 s cadence would bury the log — precisely
why `arrsync.py` doesn't emit one either; events mark transitions, not heartbeats):

- **The unattributed-clients banner.** `ClientSyncScheduler.unattributed_clients` counts, per
  pass, how many Preflight-eligible-phase transfers an enabled instance reported that could not be
  attributed to any queue (no path match and no category, or a category with no enabled mapping)
  — never `0` (a quiet, fully-attributed client has nothing to say). `GET /api/queue/preflight`'s
  `unattributed_clients` surfaces it, the mount-gate banner's own shape (§9.2's box, one line per
  affected thing, never one row per dropped item): *"SABnzbd: reports 2 items, none attributable to
  a queue — check its category → queue mapping."* **Widened, round 4 (2026-08-23, live evidence):**
  the count alone left a user with `ar-tv` already mapped guessing what else needed one, so the
  banner now names which categories the unattributable items actually carried, and calls out "no
  category at all" as its own distinct clause rather than folding it into the same count — *"reports
  2 items in ar-movies, 1 with no category, none attributable to a queue."* Two different problems
  (an unmapped category vs. a client not labelling its downloads at all) with two different fixes,
  so the copy no longer blurs them into one number.
- **Per-instance poll status, not just last Test.** Migration 029 adds `last_poll_at`/
  `last_poll_ok`/`last_poll_message`/`last_success_at` to `download_client`, written on **every**
  actual poll attempt (a single-row status column, not a log — the "not per failed pass" rule
  governs the event log, not this). `last_poll_message` reuses `_FAILURE_VERB`'s own wording
  ("rejected the configured credential" vs. "unreachable"), so a credential problem reads as that on
  the Clients row, never as a generic network failure. `last_success_at` is independent of the other
  three, so a currently-failing instance that worked yesterday still shows when.
- **One positive signal.** The first time an instance's poll succeeds in a given process's
  lifetime, one `client_poll_first_success` audit event marks the transition — a fact worth a line
  in the log exactly once. Every later successful pass writes to the status row above and nothing
  to the event log at all.

Also addressed here: finding #5 noted that "we went into settling and SAB said nothing" is
indistinguishable from the settle-gate skip (§14 stage 2b, `settle.SettleSettings.
client_skip_enabled`, off by default) simply being off. `lib/format.ts.settleWaitLabel` — the one
shared sentence the Files tree, the lifecycle icon tooltip, and the Preflight chip tooltip all
already render through — now appends "(download-client verdict skip is off)" whenever that setting
is off, rather than leaving a user watching an item settle to infer it.

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
| 1 | **Pre-check before touching the client**: SSH reachable, path exists, and the path is **inside one of the client's declared base paths and outside every excluded path** | The containment check is what stops a wrong or hostile `content_path` from `rm -rf`-ing something catastrophic. `delete_path`'s own root-path refusal is defense in depth *behind* this, not instead of it |
| 2 | Shared-path check (§10.4) | |
| 3 | `remove(id)` in the client — unregister, data stays | |
| 4 | `delete_path()` over SSH | |
| 5 | **Verify**: re-stat the path, and measure free space before/after | `rm`'s exit code is not the same as "the bytes are gone" |
| 6 | One audit event either way; failure raises an in-app banner | |

**Step 1's containment check is two-sided (added 2026-08-23, §8.3 round 5, finding #16).**
`core/disk_review.py.is_authorized_delete_target(path, base_paths, excluded_paths)` is the seed
of this check, built and unit-tested against `reconcile()`'s own excluded-path set now, ahead of
stage 5's own build — a target must sit inside a declared base path **and** outside every
excluded path (manual `download_client_excluded_path` rows, plus any category exclusion
`run_scan` resolved into a path). Whichever build implements this step must call that function
(or its stage-5 equivalent) rather than re-deriving containment a second time.

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

**Inode numbers are unique per *filesystem*, not globally — and stage 4 shipped without a device
component (2026-08-23).** `find -printf` offers `%D` (device number) alongside `%i`, and the key
should eventually be `(device, inode)` rather than `inode` alone. Worth fixing, but **not urgent,
because every consequence of a collision fails safe:**

| Rule | Effect of a false inode match | Direction |
|---|---|---|
| "claimed if *any* link to its inode is claimed" | an unclaimed file looks claimed | **fewer** debris proposals |
| "propose only when *every* link is a candidate" | a phantom link looks unaccounted-for | **fewer** debris proposals |
| link-aware freed bytes (§10.5) | a phantom link suppresses the last-link case | **understates** reclaim |

All three err toward proposing less and promising less, which is the correct direction for a
feature whose next stage deletes things. A collision additionally requires base paths spanning
filesystems *and* a number collision between them — plausible on a seedbox, but the cost when it
happens is a missed candidate, not a wrongly-deleted one. Fix it with `%D` when convenient; do not
treat it as a blocker.

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

### 11.1d Four piles — and only one of them is orphans

That confirmation separates things this section could easily have conflated, and they want
different features:

| Pile | What it is | Whose problem |
|---|---|---|
| **Debris** | Data under a base path that **no client claims and lftpweb is not using** — failed extractions, aborted grabs, and the §10.3 window where the client entry was removed but the SSH delete failed | §11, the orphan scan. Genuinely unclaimed, in a resolvable path; safe to review and select for removal |
| **The seeding estate** | rTorrent's downloads directory, still **claimed** by live torrents, accumulating until the user cleans it by hand. Each row carries its claim's own `attribution` (bound/excluded/undecided) | Not orphans. This is #21 — eligibility by site rules, ranking, then §10's delete path |
| **Excluded content** *(2026-08-24)* | Under an excluded path, **no claim currently covers it** — §17.7's "latent data-loss path" made visible instead of silent | §11, shown but never selectable, never counted toward a reclaim total |
| **Unclaimed** *(finding #17, 2026-08-23)* | Unclaimed by any client, in a tree where an exclusion cannot be resolved to a path — ownership is **genuinely undeterminable**, not merely unproven | §11, shown but not selectable through the ordinary debris flow — see below |

**The manual cleanup the user does today is the seeding-estate pile**, and naming it correctly
keeps the orphan scan from ever proposing a live seeding torrent's data as debris. The scan still
*shows* all four — a review page that omits any of them would be answering a question nobody
asked, and finding #17 corrects an earlier reading that treated "cannot resolve to a path" as
license to hide the unclaimed pile entirely — but they are labelled distinctly and only debris is
selectable for removal before #21 exists.

**The unclaimed pile, and why it exists as a pile rather than a count (finding #17).** §11.2's own
fail-closed rule (below) still refuses to let a genuinely unclaimed file under an ambiguous root
land in debris — the safety property is unchanged. What changed is what happens to it instead:
earlier, it was tallied into a bare "N items suppressed" count and never shown. That is the same
failure as finding #2 — content that exists and is never surfaced is indistinguishable from
content that is not there — applied to the exact material a disk review exists to find (*"things
can show up in weird categories etc — we might want to clean up"*, the user, 2026-08-23). So:

- **Fail-closed now means "never act without an explicit gate," not "never display."** The file
  is shown, grouped by directory the same way debris is (a genuinely unclaimed item has no
  torrent to group under either), with the reason it landed here.
- **It states plainly why it is abnormal.** In a single-lftpweb setup this pile should be empty or
  near-empty. A populated one usually means either debris left behind by an interrupted operation,
  or **another lftpweb instance's content** sharing this seedbox (finding #16) — say both, not a
  generic warning.
- **Its reclaim figure is link-aware**, exactly like debris's (§10.5) — a naive sum would
  reintroduce the lie that section exists to prevent.
- **It is not reachable through the ordinary select-and-remove flow.** No checkbox exists for it
  at all in this task's own implementation — see §11.4 below for the gate stage 5 must build
  before anything can act on this pile.
- **The line that must stay sharp:** a file claimed by an **excluded** category is *known* to
  belong to the other lftpweb instance — it is never eligible for `debris` or `unclaimed`, ever.
  The unclaimed pile is only for ownership that is genuinely *unknown*. **2026-08-24 correction —
  see §11.1e below:** this used to also mean the claim was dropped outright, before any
  claim/debris logic ran, so the file appeared in *no* pile at all. That guaranteed the line above
  but at the cost of hiding legitimate content; the claim is now retained instead, so the file
  shows in the **seeding estate**, tagged `attribution="excluded"` — the same "claimed always
  wins" per-entry order that already keeps a claimed file out of `unclaimed` now keeps it out of
  `excluded_content` too, structurally rather than by removing it from consideration.

### 11.1e Exclusion is a delete-safety boundary, not a visibility boundary (2026-08-24)

Findings #16 and #17 already taught this lesson twice — a manually excluded path must be dropped
from *delete authorization*, not from the screen (#16), and an ambiguous-root unclaimed file must
be shown, not silently counted (#17). This task applies the same correction a third time, to the
one path that still got it wrong: **a claim whose own category was marked "not used by this
instance" was dropped outright, before any claim/candidate logic ever ran.** That kept it out of
every pile correctly, but "every pile" included the seeding estate, where a claimed file
legitimately belongs — the same file, still actively seeding on the other instance's client, had
become invisible to a page whose whole purpose is showing what is on disk.

**What changed:** the claim is now retained. Its files are still claimed, so they still land in
the seeding estate — shown, tagged `attribution="excluded"` (migration 031's three-state
category, copied onto the row purely for display; `reconcile()` never branches on the value) —
and structurally unreachable by `debris` or `unclaimed`, because "claimed" is checked first in the
per-entry order, before either of those branches runs at all. A manually excluded path with **no
claim currently covering it** — the moment the other instance's client drops its history entry or
removes the torrent, which is exactly the "latent data-loss path" §17.7 names — now lands in a new
**excluded content** pile instead of vanishing: visible, never selectable, never counted toward a
reclaim total.

**What did not change, at all:** `core/disk_review.py.is_authorized_delete_target(path,
base_paths, excluded_paths)` still receives the same resolved excluded-path set and still refuses
everything under it, unconditionally — visibility and delete authorization are two different
questions, and this task only ever touches the first one. `resolve_category_exclusion_paths` and
`_resolve_client_exclusions` (the category-to-path resolution machinery §11.2 and §17.7 describe)
are untouched too. The response gained two other shapes the same day, neither a safety change: a
`torrents` array (one row per claim — the client's own reported figures plus disk-derived
`file_count`/`size_on_disk`, superseding the old separate "broken seeds" list) and a `clients`
roster (which instances reported this pass, and their declared field capabilities, so the page can
section by client and choose columns from the declaration rather than from `client_type`).

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
- **Exclusion is a *delete-safety* boundary, never a *debris-eligibility* one, and — as of
  2026-08-24 — no longer a visibility boundary either (§11.1e).** A path under
  `download_client_excluded_path`, or resolved there from an excluded category, is **never
  eligible for `debris`, unconditionally** — that guarantee is unchanged and is what
  `is_authorized_delete_target` also enforces independently. What *is* different since 2026-08-24:
  such a path's content is no longer dropped from the walk before any pile logic runs. A claim
  still covering it is shown in the **seeding estate**, tagged `attribution="excluded"` (its own
  category is *known* to belong to another instance, not merely unproven — the same fact that used
  to justify dropping it now just gets copied onto the row as a label); content with **no claim
  covering it** is shown in the new **excluded content** pile instead, never `debris`, never
  `unclaimed`. Where an excluded category cannot be resolved to a path at all (no `content`-kind
  base path for that client, rTorrent under the reference layout), the root is still walked and
  its seeding estate stays populated normally — **only a genuinely unclaimed file** under that
  root is held back from debris, and it is shown in the **unclaimed** pile (§11.1d), not silently
  suppressed. (Two earlier versions of this rule suppressed the file entirely, and — earlier
  still — the client's whole base path; all three are superseded.)
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
- **All four piles are labelled distinctly, and only debris is selectable** before #21 exists
  (§11.1d) — the unclaimed and excluded-content piles are visible but gated off the ordinary
  select-and-remove flow by construction (no checkbox exists for either), not merely by a UI
  convention that could be bypassed.

### 11.3 Manual trigger

The user asked for a manual scan, and manual is right for a first version: the scan is an SSH walk
over potentially large trees and should not ride a page load. A scheduled cadence is a later
addition, not a launch requirement.

### 11.4 The unclaimed pile's own gate — deferred to stage 5, deliberately

Finding #17 makes the unclaimed pile visible but explicitly builds no action for it — stage 5
(§14) doesn't exist yet, so there is nothing to gate. Building a confirmation flow now would be
speculative UI, designed against guesses about a delete sequence not yet written. What this task
*does* do is record the shape stage 5 should implement, so that stage rather than a future guess
decides it:

- **This project has a standing preference against confirmation dialogs.** The pause and
  bandwidth controls (§8) were deliberately built as a checkbox plus a debounced auto-commit plus
  a result banner, *never* a modal, on the recorded principle that fewer clicks beat more
  confirmation.
- **The user's own request was "a confirmation dialog or something"** — not a mandate for a modal,
  an acknowledgment that *some* extra friction is warranted here specifically, because the failure
  mode is deleting another lftpweb instance's data.
- **The recommendation: a distinct, separately-reachable action, not a modal.** Keep the unclaimed
  pile permanently unreachable from the ordinary debris checkbox-and-remove flow (as this task
  already builds), and give it its own explicit action — visually separated, naming what is
  unresolvable and why before it can be used — rather than a confirm dialog bolted onto the same
  flow debris uses. This is accident-proof (it cannot be reached by habit, the way clicking through
  a modal can) without being repetitive (it is not a second click on every ordinary action, only
  on the one action that is genuinely unusual). See `docs/decisions.md`'s 2026-08-23 entry for this
  task for the full reasoning; stage 5 should treat this as the starting design, not reopen the
  question from scratch.

**The excluded content pile (2026-08-24, §11.1e) is a different case, not a second instance of
this one — it needs no gate at all, deferred or otherwise.** The unclaimed pile is *this
instance's own* ownership question, genuinely unresolved, and a future gated action on it is
plausible (it might turn out to be this instance's own debris). Excluded content is the opposite:
its owner is *known* — the other lftpweb instance sharing this seedbox — which is exactly why it
is excluded in the first place. There is no future world in which this instance should offer any
action on it; it stays permanently informational, same as the seeding estate, and stage 5 should
not build a gate for it under the assumption this section merely forgot to ask for one.

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

> **The list is already earning its keep.** Guess #10 — the one ranked highest-risk — was falsified
> within hours of the Clients page reaching a real SABnzbd, by a user typing a deliberately bad API
> key and watching the test pass ([#23](https://github.com/crzykidd/lftpweb/issues/23)). The suite
> was green throughout, because `tests/fake_sabnzbd.py` encodes the same wrong assumption the
> connector does. **That is the second occurrence in this repo of the §13.2 failure**, after
> `IMPORT_EVENT_TYPES = {3}`. The lesson to draw is not "we guessed badly" — it is that a
> self-authored fixture cannot falsify a self-authored assumption, and that **the remaining eleven
> rows deserve the same suspicion this one has now earned.** When fixing any of them, correct the
> fixture *first* and watch the test fail before touching the connector; a fixture edited only to
> match new code repeats the original mistake exactly.

| # | Guess | Risk if wrong |
|---|---|---|
| 1 | **Queue status → phase** groupings: `Queued`/`Grabbing`→`QUEUED`; `Downloading`→`DOWNLOADING`; `Fetching`/`Propagating`/`Verifying`/`QuickCheck`/`Checking`/`Repairing`→`VERIFYING`; `Extracting`/`Moving`/`Running`→`EXTRACTING`; `Paused`→`PAUSED` | Cosmetic-to-moderate. Folding `Repairing`/`Fetching`/`Moving`/`Running` into neighbours is judgment, not observation |
| 2 | **History status → phase**: `Completed`→`COMPLETED`, `Failed`→`FAILED` | High — this is what the settle-gate skip keys on |
| 3 | **`Field.ADDED_AT` declared `NONE`** — the guess that neither mode exposes a queued/added timestamp | Low. A declared-absent field that turns out to exist is the safe direction |
| 4 | **`free_space` reads `diskspace2`/`diskspacetotal2`**, guessing index 2 = complete and index 1 = incomplete | Moderate; a one-character fix, but silently reports the wrong volume until noticed |
| 5 | **Queue byte/time parsing**: `mb`/`mbleft` as MB-denominated numeric strings, `timeleft` as `"H:MM:SS"` | Moderate |
| 6 | **History field names/shapes**: `bytes`/`completed`/`storage`/`fail_message`/`category`, including reusing `bytes` as both final size and `bytes_done` | High — `storage` is the identity source (§7.2) |
| 7 | **`list_base_paths`** via `mode=get_config&section=misc`, reading `complete_dir`/`download_dir` | Moderate → now load-bearing rather than a mere prefill (§8.2 correction, 2026-08-22): a wrong guess here surfaces as a base path the settings UI proposes, which the user must actively confirm (or reject) rather than silently absorbing — the SSH verification step is exactly the guard that turns "is SAB's reported path even valid over SSH" from an unverified guess into something the UI states outright (`verified`/`not_found`/`unverified`) instead of assuming |
| 8 | **`list_files`** via `mode=get_files&value=<nzo_id>`, tolerant of a bare list or `{"files": [...]}` | Low |
| 9 | ~~**Action call shapes and the `{"status": …}` contract**~~ **CORRECTED, 2026-08-22, measured against SAB 5.1.1 ([#23](https://github.com/crzykidd/lftpweb/issues/23)).** An **authentication** failure is not a `{"status": false}` JSON envelope at all: it is **HTTP 403, `text/html`, body `API Key Incorrect`**. `sabnzbd.py`'s `_get` now recognises that body and raises the new `ClientAuthenticationFailed` (`core/clients/errors.py`) on every authenticated call (`list_transfers`, `list_history`, `list_base_paths`, `pause`/`resume`/`remove`/`set_label`), keyed off the body text so an unrelated 403 still falls through to a plain `ClientError`. The `{"status": …}` contract for *non-auth* action failures remains unverified | Corrected and tested (`tests/test_clients_sabnzbd.py`); the non-auth `{"status": …}` shape is still the connector's riskiest remaining area |
| 10 | ~~**`test_connection` via `mode=version`**, plus "HTTP 200 even on auth failure, error signalled in the body"~~ **CORRECTED, 2026-08-22, by real use within hours — [#23](https://github.com/crzykidd/lftpweb/issues/23)).** `mode=version` is **unauthenticated**: SAB answers it for any key, so an invalid API key tested as success. `test_connection` now makes two calls: `mode=version` still supplies reachability + the version string (and is still where the redacted capture fires), and a second, authenticated `self._get("queue")` actually validates the key, raising `ClientAuthenticationFailed` on a bad one. The save-on-test *flow* in `api/settings_clients.py` (when a test runs, what blocks a save) was left untouched, per the user's explicit deferral pending the Settings rework — only the connector's own error handling and endpoint choice changed | Corrected and tested; was ranked highest-risk here, and was the first guess reality falsified |
| 11 | **`get_transfer` left `DERIVED`** (filter the merged list) rather than native by `nzo_id` | Low; §5 already flags this one |
| 12 | **`tests/fake_sabnzbd.py` inherits every guess above** rather than independently corroborating any of them | This is the §13.2 trap by construction, and why the list exists |
| 13 | **`list_categories`** via `mode=get_config&section=categories`, reading `config.categories` as a list of `{"name": ...}` dicts, excluding SAB's own `"*"` "Default" pseudo-category (added 2026-08-23, §2.1/§8.3, `prompts/2026-08-23-category-binding-redesign.md`) | Moderate — now load-bearing for the category → queue binding UI's *primary* proposal mechanism, same load-bearing shift `list_base_paths` (row 7) got in the §8.2 correction. A wrong section name or shape means this silently returns `[]`, which the UI reads as "the client reported no categories" and falls back to the (also fallible) path-arithmetic guess rather than erroring loudly |

### 13.5 The live validation loop

The test system is **https://lftpweb.crzynet.com**, running `dev`. It exposes `/api/health`,
`/api/history/events`, `/api/logs/*`, `/api/settings/*` and `/api/files` — so a stage deployed there
can be validated by reading its events and logs directly, without asking the user to relay output.
**It runs SABnzbd and rTorrent today**, with more clients to be added as the framework grows.

### 13.6 The rTorrent correction list — every guess, risk-ranked

Produced by `core/clients/rtorrent.py`'s build (2026-08-22,
`prompts/2026-08-22-rtorrent-connector.md`), the same discipline §13.4 established for SABnzbd:
**nothing here is confirmed; there are no credentials for the live rTorrent, so everything below
the endpoint choice is vendor-doc guesswork.** `tests/fake_rtorrent.py` inherits every one of
these guesses (see its own docstring) — a green suite proves internal consistency with this
module's own reading of the docs, not correctness. Ranked by what breaks if the guess is wrong:
a mistake that flips terminal/non-terminal phase or corrupts what a delete targets ranks above
one that only misdraws a cosmetic label.

| # | Guess | Risk if wrong |
|---|---|---|
| 1 | **XML-RPC fault classification** (`_looks_like_missing_method`/`_looks_like_unknown_hash` in `_call`/`remove`): substring heuristics against fault text this module has never seen from a live rTorrent, deciding `ClientError` vs. `CapabilityUnavailable` (§4.2's load-bearing split) and whether `remove` reports a routine "already gone" outcome or raises | **High.** A wrong classification here could let a genuine transport/protocol failure silently degrade a capability the client actually has (§4.2's own "must never" rule), or make `remove` raise on a case that should read as a benign no-op |
| 2 | **`map_phase`'s PAUSED-vs-QUEUED split** (incomplete + inactive, disambiguated by `d.state`) — this connector's own elaboration on top of the simpler guidance it was handed | Low-moderate. Both outcomes are non-terminal; §4.2's "unknown never blocks anything" already bounds the damage a wrong split can do, unlike a terminal/non-terminal confusion |
| 3 | **Infohash case sent back to rTorrent** (`_to_rtorrent_hash` uppercasing every per-item call) — no live confirmation rTorrent's own hash lookup is actually case-sensitive | **High if the underlying premise is wrong the other way** (i.e. if lowercase would have worked and uppercase doesn't) — every `pause`/`resume`/`remove`/`list_trackers`/`list_files`/`free_space` call would silently fail to find the item. Not a data-safety issue (`remove` never deletes data itself), but an operational one large enough to make every per-item control appear broken |
| 4 | **`Field.CATEGORY` reads `d.custom1=`** on the unconfirmed assumption this deployment's ruTorrent (if any) follows the label-plugin convention of storing label text there, and that nothing else writes into the same generic slot | Moderate. A wrong or colliding value feeds spec §8.3's category → queue inference with bad data; the inference step is user-confirmed before anything saves, which is the backstop this guess leans on |
| 5 | ~~**`TORRENT_BASELINE` declares `Field.SEED_TIME_S` `NATIVE` with no note**~~ **NOT A DEFECT — entry retracted 2026-08-22.** The baseline is the *common* case for torrent clients, and native seed time genuinely is common: qBittorrent (`seeding_time`), Transmission (`secondsSeeding`) and Deluge (`seeding_time`) all report it directly. **rTorrent is the exception**, and it overriding to `DERIVED` with a note is §5's inheritance mechanism working exactly as designed, not a workaround for a broken default. §4.3 uses rTorrent as the example of *why a derived capability needs a caveat*, not as a claim about what the baseline should say. **Do not "fix" the baseline** — setting it `DERIVED` would make three connectors understate a capability they have | None — retained only so the retraction is visible, since acting on the original entry would have been the actual defect |
| 6 | **`free_space(path)`** matched by finding any currently-listed transfer whose `content_path` sits under the requested path, then reading that transfer's `d.free_diskspace` | Low-moderate today (no caller uses it yet — #21 is `free_space`'s real consumer and is out of scope here); will need re-examination once #21 lands, especially combined with §12's own "minimum across devices" trap |
| 7 | **`d.base_path=` chosen over `d.directory=` for `content_path`**, on the doc-derived reasoning that they differ only for single-file torrents | Moderate. If the two are not equivalent the way vendor docs suggest for multi-file torrents either, every delete/inode-match downstream (spec §11.1b) inherits a wrong path — but §11.1b already matches by inode rather than trusting path equality, which bounds the blast radius |
| 8 | **`d.pause`/`d.resume` chosen for the `pause`/`resume` operations, `d.stop` reserved for `remove` alone** — an unconfirmed reading that rTorrent actually distinguishes a lightweight pause from a full stop the way this module assumes | Low. Worst case, "pause" behaves more like "stop" than a user expects; does not affect data safety or terminal-phase classification |
| 9 | **`d.multicall2`'s leading empty-string "call id" argument** (`("", "main", *commands)`) — copied from common client-library convention, not confirmed against this deployment's rTorrent version | Low. If wrong, the whole listing call fails outright (loud, not silent) rather than returning subtly wrong data |
| 10 | **`ADDED_AT` reads `d.timestamp.started=`** — vendor docs are ambiguous on whether this timestamp resets when a torrent is restarted/resumed after being fully stopped, which would make it "last started," not "first added" | Low, per spec §13.4 #3's own precedent: a field that turns out to mean something narrower than its name is the safe direction relative to a field silently returning `None` |
| 11 | **`tests/fake_rtorrent.py` inherits every guess above** rather than independently corroborating any of them | This is the §13.2 trap by construction, and why this list exists |
| 12 | **`list_categories`** implemented as the distinct, non-empty `d.custom1` values off the same `d.multicall2` listing call every other method already issues, declared `Support.DERIVED` (added 2026-08-23, §2.1/§8.3, `prompts/2026-08-23-category-binding-redesign.md`) | Moderate — inherits guess #4's own uncertainty about whether `d.custom1` genuinely holds ruTorrent's label text on this deployment, one level up: this can only ever report labels *currently in use*, never a closed list, so a deployment with real categories that happen to have nothing currently labelled reports none and the category-binding UI falls back to path arithmetic instead. The inference step is user-confirmed before anything saves, the same backstop guess #4 already leans on |
| 13 | **CONFIRMED LIVE, 2026-08-23** — `directory.default` (feeding `list_base_paths`'s `working` entry, guess row 7's own reasoning) returns a `~`-relative form (`~/downloads/rtorrent`) rather than an absolute path, on this deployment's rTorrent 0.9.8. Not a guess any more, and not a namespace mismatch either — §8.2 correction (2026-08-23) handles it: offered, expanded, never applied blind (finding #1, `prompts/test-findings-2026-08-23.md`) | None remaining — confirmed and handled. Named here because it is exactly the kind of thing §13.2 says would never have surfaced without a live instance, and did not appear in any pre-live guess above |

---

## 14. Build order

Each stage is independently shippable. Nothing before stage 5 can delete anything.

| Stage | What | Notes |
|---|---|---|
| **0** | Interface, enums, capability declaration + profiles, registry, conformance suite, a fake adapter | Ships with nothing configured. **This is the piece the vocabulary must be right in**, so §13.3's capture ideally informs it |
| **1** | SABnzbd adapter, instance CRUD, declared config form, test-connection, capability readout, **the redacted capture** (§13.3), **and the README write-up of the reference workflow** (§1.1) | First real client contact. The README section is the user's explicit ask: document the *preferred* seedbox setup, so other workflows are recognisable as departures from a stated one |
| **2** | The poller (§9), SAB as a third Preflight source, the settle-gate skip | #18's first real user-facing payoff. **2a (the poller + Preflight source) landed 2026-08-23** (`prompts/done/2026-08-23-client-poller.md`). **2b (the settle-gate skip itself) landed 2026-08-23** (`prompts/done/2026-08-23-settle-gate-skip.md`) -- ships **off** (`settle.SettleSettings.client_skip_enabled`, default `False`) pending live confirmation of §13.4 guess #2 against a real SABnzbd; every uncertain path (setting off, no client-sync source wired, unreachable client, blank/empty response, a queue-side or `UNKNOWN` phase, a near-miss path) falls back to running the settle gate exactly as it ran before this stage. **A terminal `COMPLETED` verdict no longer satisfies the gate the instant it's seen** (2026-08-23, `prompts/done/2026-08-23-client-completion-delay.md`, finding #9) -- it now holds `settle.CLIENT_COMPLETION_HOLD_S` (10s) first, measured from the client's own `completed_at` (a completion already older than the hold satisfies it immediately; falls back to lftpweb's own first-observation time only when a connector reports no `completed_at`). **2026-08-24, `prompts/done/2026-08-24-client-shortened-settle.md`: two corrections landed together.** First, `find_client_completion`/`ClientSyncScheduler.completed_transfers` (renamed `finished_transfers`) accepted only `TransferPhase.COMPLETED`, which made the settle-gate skip (and the withhold gate's own self-lift check) **structurally unreachable for an ordinary seeding rTorrent torrent** -- `core/clients/rtorrent.py._classify_token` maps a finished, actively-seeding torrent to `SEEDING`, not `COMPLETED`. Both now also accept `SEEDING` (`settle.FINISHED_TRANSFER_PHASES`); `VERIFYING` stays excluded regardless. Second, and superseding `client_skip_enabled` for the common case: a new, **default-ON** client-shortened settle (`AutoQueue`'s own background ticker) re-fingerprints the item's remote subtree twice, `settle.CLIENT_RECHECK_INTERVAL_S` (10s) apart, and queues only on a match -- verifying on the filesystem rather than trusting the client's status string, which is why it can ship on where the SABnzbd-status-mapping features above can't. `client_skip_enabled` itself is untouched and still off by default, evaluated only after the new mechanism, as the escape hatch for anyone who wants no recheck at all. Two overlapping mechanisms for one job, left unconsolidated on purpose -- named in `docs/decisions.md` and `README.md`'s "Known gaps". **2026-08-29, `prompts/done/2026-08-29-settle-verify-under-existing-toggle.md`: the two mechanisms above are consolidated into one, under the existing toggle.** The user's own words: *"There is a toggle already for Skip the wait on a download client's own verdict. This setting should be the one that still does the 5s verify."* `settle.CLIENT_COMPLETION_HOLD_S`/`client_completion_ready` (the pure time-hold from 2026-08-23) are deleted outright, not kept as a degraded fallback; `client_skip_enabled` now gates the re-fingerprint verify directly, and `CLIENT_RECHECK_INTERVAL_S` drops from 10s to 5s (the user's explicit instruction, with `AutoQueue.RECHECK_TICK_S` recalibrated from 5s to 2.5s alongside it so the observed window stays close to 5s rather than routinely rounding up to 10s). **The toggle's own default flips from `False` to `True` in the same task** -- the `False` default only ever protected against the deleted time-hold's failure mode (trusting an unconfirmed status mapping); a verified skip carries none of that risk, so the user's own call was to default it on: *"yes, make it on by default since it verifies."* One toggle, one meaning: skipping the wait now means verifying that nothing moved, never trusting a client's word alone |
| **3** | Withhold on partial failure (`docs/transfers-redesign-spec.md` §4.3), and the §9.1 poll-cadence fix | **Landed 2026-08-23** (`prompts/done/2026-08-23-withhold-and-cadence.md`). The cadence split is corrected to cheap-vs-expensive, read per-connector off `Operation.LIST_HISTORY`'s own capability declaration (§9.1's own correction note). The withhold gate ships **off** (`autoqueue.WithholdSettings.enabled`, default `False`) pending live confirmation of §13.4 guess #2 against a real SABnzbd, for the identical reason stage 2b's `client_skip_enabled` shipped off -- every uncertain path (setting off, no client-sync source wired, unreachable client, blank/empty response, a queue-side or `UNKNOWN` phase, an outright failure with no `content_path`, a near-miss path) falls back to today's behavior unchanged. No API/UI surface shipped this stage -- `AutoQueue.withheld` is public and readable, but nothing reads it yet; named as an open gap, not hidden |
| **4** | The disk review scan (§11), three piles, review-only | **Landed 2026-08-23** (`prompts/done/2026-08-23-disk-review-scan.md`), **extended the same day to a third pile** by finding #17 (`prompts/done/2026-08-23-unclaimed-pile.md`). `core/disk_review.py.reconcile()` is pure set math over Set A/B/C, unit-tested exhaustively without SSH -- the §11.1a union-across-clients catastrophe and the §11.1b inode-claiming catastrophe are both asserted directly. `core/remote.py.RemoteConnectionPool.scan_with_inodes` extends the existing GNU-`find`/BusyBox-fallback scan with `%i`/`%n`; the fallback (`remote_agent/scan_fs.py --inodes`) supplies inode/nlink too (`os.lstat`, stdlib-only), so there is no "BusyBox can't do this" case needing the unavailable-declaration path -- it exists (`RemoteScanError` propagates rather than degrading) but is untriggered by inode support itself, only by a genuine walk failure. **The third pile (finding #17):** a genuinely unclaimed file under a root where an excluded category cannot be resolved to a path is no longer a silent count (`SuppressedDebrisItem`) -- it is now `UnclaimedItem`, shown as its own pile (§11.1d), grouped by directory, link-aware reclaim figure, gated off the ordinary select-and-remove flow by construction (no checkbox rendered for it at all). A claim whose category is excluded is folded into the same hard-exclusion set a manually excluded path uses, so it never falls through to "unclaimed" by accident (`test_excluded_category_claim_appears_in_no_pile_not_even_unclaimed`). `POST /api/disk-review/scan`, manual trigger only (§11.3); a Transfers → Disk review tab shows all three piles with a link-aware running total each. Not yet looked at against the real box -- see this task's own final report for what remains named as a gap (multi-filesystem inode collisions, prefix-vs-exact mount-sentinel matching, empty-directory debris) before stage 5 |
| **5** | The delete pipeline (§10), manual trigger, verification, banner | **Findings #15/#16's gate is cleared** (2026-08-23, `prompts/done/2026-08-23-category-tristate-and-exclusion.md`, §8.3 round 5, **corrected in round 6 the same day** by live use against the real box, `prompts/done/2026-08-23-auto-add-categories-default-excluded.md`). Categories are three-state and persisted (migration 031, every observation now recorded via migration 032, not just a manual Test); the unattributed-clients banner counts only the undecided state, re-derived at **request time** against a live exclusion read (round 6 fixed a poll-interval staleness bug here); excluded categories are dropped per-file off their own claim's category (round 6, universal, no path arithmetic needed) with a narrower fail-closed rule for the genuinely unclaimed remainder only — a whole base path is never blanket-suppressed anymore, only ambiguous unclaimed files (round 5's version of this rule hid legitimate, already-claimed content that was never at risk); `core/disk_review.py.is_authorized_delete_target` gives stage 5 a ready-made, already-tested two-sided containment check (base path **and** outside every excluded path). **Finding #17 (2026-08-23) adds a requirement to this stage's own scope**: the unclaimed pile is now visible (§11.1d, §11.4) but deliberately inert, so stage 5 must build its own gate before that pile can be acted on at all -- §11.4 records the recommended shape (a distinct, separately-reachable action, not a confirm dialog) for stage 5 to implement deliberately rather than invent. **What still stands in the way of building stage 5 itself:** (1) none of this has been exercised against the user's real two-instance deployment beyond the live findings rounds 5/6/17 already fixed — a scan, an excluded category, the narrowed fail-closed rule, and the unclaimed pile itself all still need a full live run before anything is trusted to delete; (2) `is_authorized_delete_target` is unit-tested but unused — stage 5's own delete sequence (§10.2) must actually call it, not re-derive containment; (3) the manual excluded-paths UI (`ClientsTab.tsx`), the new "N new categories" signal, and the unclaimed pile's own display are all unverified in a real browser, same as every other layout change in this feature; (4) §11.1c/§10.4/§10.5's own named gaps (cross-seeding, multi-filesystem inode collisions) remain open regardless of this task; (5) the unclaimed pile's own gate (§11.4) does not exist yet — stage 5 must design and build it, not merely wire a delete call to the pile that already renders. Stage 4's own real-box verification (named in its own §14 row) is still outstanding too and should happen before, not after, stage 5 |

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
4. ~~**Does the ruTorrent-vs-rTorrent API surface question (`docs/torrent-manager-spec.md` §10.1)
   resolve to XML-RPC directly, or the ruTorrent HTTP plugin API?**~~ **Answered 2026-08-22,
   MEASURED: direct XML-RPC at `/RPC2`.** All four candidate URLs on the live seedbox sit behind
   HTTP Basic auth; `/RPC2` and the ruTorrent httprpc plugin both answered with the same realm,
   `/xmlrpc` answered with a different one (a separate nginx location, not assumed to share a
   backend), and the ruTorrent `rpc.php` plugin mount 404s. `/RPC2` is the default and the path is
   a `ConfigField` (`rpc_path`), so a deployment can point elsewhere without a code change — see
   `core/clients/rtorrent.py`'s module docstring and `docs/decisions.md` (2026-08-22).
