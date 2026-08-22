# Torrent manager — high-level spec

**Status: proposal. Nothing here is built.** This is a *proposal document*, not a description of
reality — the same convention as `docs/transfers-redesign-spec.md`. `DESIGN.md` continues to
describe what exists.

**Depends on [#18](https://github.com/crzykidd/lftpweb/issues/18)** (phase 2, advisory
download-client integration — SABnzbd then ruTorrent). This feature needs a working rTorrent
connection before any of it can be built, and should not start before that lands.

Written 2026-08-21 from the user's description, while other work was in flight. Sections are
numbered for reference.

---

## 1. What was asked for

> *"A torrent manager as a new menu option. It would give you a summary of how many torrents
> you're seeding, how old, break down per tracker. Allow you to associate trackers with the site
> name etc. And then allow you to set stop-seeding rules based on available space — so maybe you
> could do something like keep xxGigs free. When we get to that threshold then stop seeding items
> based on some rules with a delete source file. So you could set rules per site. Site 1: I want
> to seed to at least 14 days and a 1.0 ratio. For site 2 I want to seed to 28 days and a 1.5
> ratio. Then based on the rules we would build a list that rated them by extra days seeded and
> ratio, and stop seeding and delete files through the API for the ones that are the most over
> defined rules."*

Three separable things, in increasing order of risk: **see** the seeding estate, **describe** the
rules that govern it, **enforce** those rules by deleting data.

## 2. Why this is a different kind of feature from #18 — read this first

`docs/transfers-redesign-spec.md` §4.1 governs download-client integration with one rule:
**advisory only** — a client may skip work, withhold work, or explain, but may **never write
`item.state`**. That rule is about protecting lftpweb's own state machine from a client's
opinions, and it stays true here.

**But it is not the rule this feature needs.** Everything in #18 is, in effect, *read-only*:
lftpweb learns things from the client and changes its own mind. This feature **writes to the
seedbox and destroys data** — it stops torrents and deletes their files through the client's API.
That is a capability class lftpweb has never had pointed at the torrent client, and the failure
mode is not "a row looks wrong" but "your seedbox data is gone and the tracker counts it as a
dropped seed."

So this document adds a second governing rule alongside §4.1's:

> **Destructive action is proposed, ranked, and explained before it is ever taken — and the
> explanation is retained afterwards.** Every removal has a written reason naming the rule that
> permitted it, the score that selected it, and the space it was expected to free.

lftpweb already has one precedent for deleting on the seedbox: `move`-mode source deletion, which
fires **only on a confirmed *arr import** and is hedged with a delete ladder, a retry sweep, and
audit events. That conservatism is the house style for remote deletes, and this feature should
inherit its temperament, not invent a looser one. See §9.

## 3. Build it in three phases — each one shippable alone

**Phase A — see it (read-only, zero risk).** A new *Torrents* section: how many torrents are
seeding, total size, age distribution, breakdown per tracker, ratio distribution. No rules, no
writes, nothing configurable beyond the client connection itself.

**Phase B — describe the rules (still no deletion).** Tracker → site mapping, per-site seed rules,
and a **live preview**: "if the threshold tripped right now, these 14 torrents would be stopped,
in this order, freeing 340 GB." The preview is the whole deliverable — it is how the rules get
debugged before they can hurt anything.

**Phase C — enforce.** The free-space watermark, the ranked selection, and the actual
stop-and-delete through the client API.

**Do not build C until A and B have run against the real seedbox for a while.** The ranking
function (§8) is the part that cannot be designed correctly in the abstract — it needs to be
looked at against a real seeding estate and adjusted. And unlike every other feature in this
project, a wrong answer here has no undo.

## 4. Data model sketch

**The torrent list is a cache, and framing it that way is load-bearing** — the same framing
§4.6 of the redesign spec uses for pending entries. Everything about a torrent is re-fetchable
from rTorrent, nothing else reads it for correctness, and truncating the table is always safe.
That is what stops it quietly becoming a second source of truth about what is on the seedbox.

| Table | Key | Carries |
|---|---|---|
| `torrent` (cache) | infohash | name, size, save path, announce host(s), added-at, completed-at, ratio, uploaded bytes, state, label |
| `tracker_site` | announce host | display name, and which site's rules apply |
| `seed_rule` | site | `min_seed_days`, `min_ratio`, enabled flag |

**Site is the unit of policy, tracker host is the unit of identity.** One site can announce from
several hostnames, so the mapping is many-hosts-to-one-site. Getting this backwards means a site
whose announce URL changes silently loses its rules.

## 4a. Client capability declaration — a connector-framework concern, not a torrent-manager one

**Every client module declares what it supports, and lftpweb enables or disables features from that
declaration.** This is the user's requirement (2026-08-21) and it is deliberately written here as a
property of the **API connector framework** — the one #18 builds for SAB, rTorrent and whatever
follows — rather than as something the torrent manager invents for itself. When phase 2 lands, this
belongs in `docs/transfers-redesign-spec.md` §4 as much as here.

The shape:

- A connector exposes a **capability set** — machine-readable, not prose. Something like
  `{listing, ratio, seed_time, tracker_urls, free_space, stop, delete_data, labels}`.
- **The UI is driven by the declaration, never by the client's name.** No `if client == "rtorrent"`
  anywhere. A future client that can list but not delete should light up phase A and leave phase C's
  controls disabled, with a reason — "your client doesn't report seed time" is a good message, a
  greyed-out control with no explanation is not.
- **A missing capability disables a feature; it never fakes one.** If a client cannot report ratio,
  the rules that need ratio are unavailable for that client — lftpweb does not estimate it from
  uploaded bytes and pretend. This is §4.2's "absent from the client is not a verdict" applied to
  configuration rather than to status.
- **Declared is not the same as working.** A capability that errors at runtime should degrade to
  unavailable with an audit event, the same way `core/arrsync.py`'s per-instance backoff handles an
  *arr that stops answering. A declaration is a promise, and promises get broken.

This also decides how much of §8's ranking is even offerable: the score needs seed time **and**
ratio, so on a client declaring only one of them the honest answer is to offer single-dimension
rules and say why, not to silently drop a term from the formula.

## 5. Tracker identity, and the passkey problem

**Announce URLs embed per-user passkeys.** They are credentials. Store and match on the announce
**hostname only**; never persist, log, display, or bundle the full announce URL. This project
already has a support-bundle secret-hygiene standard (the bundle was checked for secrets against a
real one before shipping) and a torrent list is a new and very plausible way to leak a passkey into
a debug zip.

A torrent may have multiple announce URLs. Record them all, designate one as primary for
attribution, and make the primary visible so a mis-attributed torrent is diagnosable rather than
mysterious.

## 6. Free space — whose, and measured how

**The seedbox's, not the local disk.** You stop seeding to reclaim space where the torrents live.

**Nothing in lftpweb knows this today.** The only disk-usage code is `shutil.disk_usage` in
`core/supportbundle.py`, which reports the *local* queue paths. So this is a genuine new
capability — with two possible sources, and **the client's own answer is preferred**:

1. **Ask the client.** rTorrent very likely reports free space (and per-torrent ratio, age and
   tracker) through its API directly — determine this against the real API when the connector
   framework lands rather than designing around a guess. A client that reports it declares
   `free_space` per §4a and this needs no SSH at all.
2. **Fall back to `df` over the existing SSH connection.** `core/remote.py` owns a pooled asyncssh
   connection and already issues `conn.run("rm -rf …")` and `conn.run("true")` over it, so a `df`
   costs one command on a connection that is already open. This is the answer for a client that
   does not declare `free_space`, and the reason the feature is not blocked on any particular
   client's API surface.

The two must agree on **which filesystem** they are describing — a client reporting free space for
its own data directory and a `df` on the queue's remote path can easily be different mounts.

Three things to get right:

- **Per-path, not per-host.** A seedbox may spread torrents across mounts. Measure the filesystem
  holding the torrents, not `/`.
- **Two watermarks, not one.** "Keep 500 GB free" as a single threshold flaps: free one torrent's
  worth, drop back under, free another, forever. Trigger at the low mark, remove until the high
  mark, then stop. The gap between them is what makes a pass a *pass* rather than a permanent
  trickle of deletions.
- **The trigger is global; the selection is per-site.** Free space is one number for the whole
  disk, but eligibility and ranking are governed by each torrent's own site rules.

## 6a. Projected free space — the reason this belongs in lftpweb and not in a standalone tool

**The trigger should be what free space is *about to be*, not what it is right now.** The user's
case (2026-08-21): 200 GB free, the *arr just grabbed 190 GB of new releases, and by the time those
land the disk is full. A reactive threshold notices this only after the damage — and worse, notices
it *while transfers are running*, which is the worst moment to start deleting things.

**lftpweb is uniquely placed to know this, and already does.** A standalone torrent manager sees
only the disk. lftpweb sees the incoming commitment, from three sources it already tracks:

| Source | What it contributes | Already available? |
|---|---|---|
| Preflight rows | Releases the *arr has grabbed that have not landed yet | **Yes** — `PreflightRow` already carries a known total size and, where the source can compute it, how much is left to arrive |
| Queued jobs | Known remote size of everything waiting for a slot | Yes — the queue already knows remote sizes |
| Running jobs | Remaining bytes on transfers in flight | Yes — progress is derived from local-vs-remote bytes |

So: **`projected_free = current_free − committed_incoming`**, and the watermark is evaluated
against the projection. That turns "clean up after you run out" into "clean up *because* 190 GB is
coming", which is the actual request.

**Two things will go wrong if they aren't designed for:**

- **Double counting.** One release can be a Preflight row *and* a queued job *and* in flight,
  depending on timing — Preflight explicitly evicts on handover for exactly this reason. Committed
  bytes must be computed **once, in one place**, with a single definition of what counts, the same
  way `core/pipeline_flight.py` is one SQL string with three callers. Two encodings of "incoming"
  will drift, and a projection that double-counts will delete torrents that never needed deleting.
- **Unknown size is not zero.** `PreflightRow`'s size fields are `None` when the source cannot
  compute them, by deliberate design ("never a request to enrich one that lacks it"). A `None`
  silently coerced to 0 makes the projection quietly optimistic — the failure direction that
  deletes nothing and lets the disk fill. Count unknowns separately and **say so in the UI**: "190
  GB incoming, plus 4 releases of unknown size" is honest; a single confident number is not.

**Which disk this lands on matters.** *arr grabs are downloaded by SAB/rTorrent onto the **seedbox**
first, then transferred locally — so incoming commitment hits the same disk the torrent manager
frees, and the connection is direct. The local target disk fills too, on its own schedule, but
stopping a seed does nothing for it. If local space is also to be protected that is a **separate
projection with a separate remedy**, and should not be folded into this one (§10, question 2).

**This is also the best argument for phase A shipping alone.** A read-only panel that says "200 GB
free, 190 GB incoming, 47 torrents eligible to stop freeing 340 GB" is genuinely useful before a
single rule exists, and it is how the projection gets validated against reality before anything is
allowed to act on it.

## 7. The rules

A torrent becomes **eligible to stop** only once it has satisfied **both** of its site's
thresholds — seeded at least N days **and** reached at least R ratio. That is the conservative
reading of the request, and the safe default: an `or` would let a fast-ratio torrent leave before
the site's minimum seed time, which is exactly the thing that gets accounts warned.

Make the combine mode a per-site setting eventually (some trackers genuinely are "time *or*
ratio"), but ship `and` and only add `or` when a real site needs it.

**Eligible is not the same as selected.** Eligibility says a torrent *may* be removed; the ranking
says which of the eligible ones actually are, and only as many as the space target requires.

## 8. Ranking — the one genuinely undecided piece

"Most over the defined rules" has to combine two quantities in different units — excess days and
excess ratio. That comparison needs a stated formula, because there is no natural one.

The recommendation is **normalized excess**:

```
score = (days_seeded / min_seed_days - 1) + (ratio / min_ratio - 1)
```

Both terms are dimensionless multiples of that torrent's *own site's* threshold, so a 28-day site
and a 14-day site are comparable without either dominating. A torrent exactly at its threshold
scores 0; one at double both scores 2.

Then: **sort by score descending, remove until the high watermark is met.** Greedy, predictable,
explainable — not an optimizer. A tie-break on size descending means a pass reaches its target in
fewer removals, which is usually what you want.

**Show the score and its inputs in the UI.** A user who cannot see why torrent X was picked ahead
of torrent Y will not trust the feature enough to enable it, and will be right not to.

Alternatives considered and not recommended: lexicographic (ratio excess, then days) hides one
dimension entirely; a bytes-freed-per-regret optimizer is unpredictable in exactly the situation
where predictability matters most.

## 9. Safety rails — the non-negotiable list

- **Never remove a torrent whose data lftpweb still needs.** A torrent still being transferred, or
  queued, or with its pipeline in flight, must be excluded. **Reuse `core/pipeline_flight.py`'s
  predicate and `in_flight_item_ids()` — do not write a second definition of "busy."** This is
  precisely the v0.2.6 `REMOTE_GONE` defect class: deleting a seedbox source out from under a
  queued job.
- **Cross-seeding will bite.** The same files seeded on three trackers are three torrents sharing
  one save path. Deleting the data for one breaks the other two, and the other two may not be
  eligible. Detect shared save paths and either refuse, or require *every* torrent on that path to
  be eligible before any of them go.
- **Hardlinks mean deleting may free nothing.** Report space actually reclaimed against space
  expected, and stop the pass if they diverge badly — that divergence is the signal that the disk
  layout is not what the rules assume.
- **Off by default, with a per-pass cap.** A first run that removes 200 torrents because a rule was
  mistyped is unrecoverable.
- **Dry-run is a first-class mode, not a debug flag** — it is phase B, and it stays available after
  phase C ships.
- **One audit event per removal**, carrying the rule, the score, the thresholds, and the before/
  after free space. lftpweb's existing events machinery already does this well for source deletes.
- **Define the interaction with `move`-mode source deletion.** lftpweb *already* deletes seedbox
  sources on confirmed *arr import — which means a torrent can already be seeding files lftpweb
  has removed underneath it. That is a pre-existing rough edge, and this feature is the natural
  place to surface it (an "errored / files missing" bucket in phase A's summary would expose it
  for free, before any of the rules work is built).

## 10. Open questions — for the user, before phase B

1. **Which API surface** — ruTorrent's HTTP plugin API, or rTorrent's XML-RPC directly? Different
   auth, different reliability, and it decides whether this shares anything with #18's adapter.
   **Resolve this when the connector framework is built, not before** — the framework is where it
   gets settled which of §4a's capabilities each surface can actually honor, and how much of this
   feature's data comes from the client versus from SSH.
2. **Is the space to protect only the seedbox's**, or should local free space trigger anything too?
3. **Stop-and-delete in one action, or stop first and delete later** on a second pass? The latter
   gives a recovery window at the cost of not freeing space immediately.
4. **Do you cross-seed?** If yes, §9's shared-path rule moves from a safety rail to a core
   requirement and shapes phase A's data model.
5. **Should enforcement ever act on its own**, or always propose a list and wait for a click? This
   is the biggest product question in the document — everything else is mechanics.
