# Download-client API survey — what each client can actually tell us

**Status: research, not a plan.** Gathered 2026-08-21 from vendor documentation to give
[#18](https://github.com/crzykidd/lftpweb/issues/18) (the connector framework) and
[`docs/torrent-manager-spec.md`](torrent-manager-spec.md) a factual basis instead of assumptions.

**Everything here must be re-confirmed against a real instance at implementation time.** Vendor docs
drift, seedbox providers ship old builds, and several of these APIs changed method names between
major versions. Treat this as "what to expect and what to watch for", not as a contract.

Clients surveyed: **rTorrent/ruTorrent**, **qBittorrent**, **Transmission**, **Deluge**, **SABnzbd**
— the ones actually common on seedboxes.

---

## 1. The capability matrix

Three states, not a boolean — this is the refinement §4a of the torrent-manager spec needs:
**native** (the client reports it directly), **derived** (lftpweb can compute it from what the client
does report, with caveats), **none**.

| Capability | rTorrent / ruTorrent | qBittorrent | Transmission | Deluge | SABnzbd |
|---|---|---|---|---|---|
| List items | native (`d.multicall2`) | native (`/torrents/info`) | native (`torrent-get`) | native (`core.get_torrents_status`) | native (`mode=queue`/`mode=history`) |
| Ratio | native (`d.ratio`, **per-mille**) | native (`ratio`) | native (`uploadRatio`) | native (`ratio`) | **none** (not a torrent client) |
| Seed time | **derived only** — no native field; compute from `d.timestamp.finished` | native (`seeding_time`) | native (`secondsSeeding`) | native (`seeding_time`) | **none** |
| Trackers | native (`t.multicall` → `t.url`) | native (`/torrents/trackers`, plus `tracker` = first working) | native (`trackers` / `trackerStats`, **includes `sitename`**) | native (`tracker_host`) | **none** |
| Size / path | native (`d.size_bytes`, `d.directory`, `d.base_path`) | native (`size`, `save_path`) | native (`totalSize`, `downloadDir`) | native (`total_size`, `save_path`) | native (history `storage`) |
| Free space | native (`d.free_diskspace`) | native (`server_state.free_space_on_disk`) | native (`free-space`, **takes a path, returns free *and* total**) | native (`core.get_free_space(path)`) | native (`diskspace1`/`diskspace2` + totals, **on the queue call itself**) |
| Stop | native (`d.stop` / `d.close`) | native (`/torrents/stop`) | native (`torrent-stop`) | native (`core.pause_torrent`) | n/a |
| **Delete + data** | **via hook, not core — see §2.** `d.custom5.set=1` + `d.delete_tied` + `d.erase`, in that order, in one `system.multicall` | native (`/torrents/delete?deleteFiles=true`) | native (`torrent-remove` + `delete-local-data`) | native (`core.remove_torrent(…, remove_data)`) | n/a |
| Labels/categories | via `d.custom*` slots | native (`category`, `tags`) | native (`labels`) | native (`label` plugin) | native (category) |

## 2. rTorrent deletes data through a *hook*, not through the client

**rTorrent core has no "remove and delete data" primitive.** `d.erase` unregisters the item and
leaves the files on disk. But that is not the end of the story, and an earlier draft of this document
was wrong to stop there.

**There is a documented, scriptable API sequence** — the one the ruTorrent UI itself issues. Sent as
a single `system.multicall`, in this order, with the info-hash as the parameter:

| Step | Call | Why |
|---|---|---|
| 1 | `d.custom5.set` = `1` | The flag the `erasedata` plugin's hook reads to mean "delete the physical files too" |
| 2 | `d.delete_tied` | Removes the tied `.torrent` from the session folder |
| 3 | `d.erase` | Unregisters the item — **and fires `event.download.erased`**, which is what actually triggers file removal |

**Order is load-bearing.** The flag must be set *before* `d.erase`, because `d.erase` is what fires
the hook that reads it. Reversed, the torrent is unregistered and the data stays.

### What this actually means for the connector

**The deletion is performed by an `event.download.erased` handler that ruTorrent's `erasedata`
plugin installs — not by rTorrent.** So the capability depends on the *deployment*, not on the
client version:

- **It must be detected, never assumed.** A bare rTorrent with no ruTorrent, or a ruTorrent with
  `erasedata` disabled, will accept all three calls happily and delete nothing. There is no error.
  This is a capability whose presence is an environment property — exactly the case §4a's runtime
  degradation rule exists for.
- **`d.custom5` is a general-purpose user slot.** ruTorrent's plugin claims it by convention, not by
  reservation. If anything else in the user's setup writes `custom5`, there is a collision — worth a
  glance at what a real seedbox has in there before relying on it.
- **The known failure modes are failures of this hook path**, and they are well documented: the
  plugin **silently does nothing** when filesystem permissions are wrong (torrent vanishes from the
  UI, data stays, no error surfaced), and it **times out on torrents with many files** (reports
  cluster around 100+). Both are invisible to the caller.

**So the three consequences stand, and the first one gets stronger, not weaker:**

1. **Never trust a delete's return value. Re-measure.** All three calls will report success on a
   deployment where nothing was deleted. Free space — or better, the item's path — must be checked
   afterward, and a delete that freed nothing reported as a failure. Same instinct as §9's hardlink
   rule, with a far more likely trigger.
2. **`delete_data` can be declared and still not work.** The strongest argument for §4a's "declared
   is not the same as working," and for degrading a capability to unavailable after a runtime
   failure.
3. **A stop-only mode is worth having**, both as a fallback and possibly as the default here.
   lftpweb already deletes on the seedbox over SSH (`core/remote.py` issues `rm -rf`) and does it
   reliably, with a real error when it fails. Stopping the torrent through the API and deleting the
   files over SSH may simply be the more honest path than trusting a hook that fails silently —
   worth considering whether the connector's delete capability should be *optional*, with SSH as the
   executor.

## 3. Other per-client traps worth knowing before designing the adapter

**rTorrent**
- **`d.ratio` is per-mille** — divide by 1000. A rule comparing it directly against `1.0` would treat
  every torrent as wildly over-seeded.
- **No seeding-time field.** Deriving it from `d.timestamp.finished` measures *wall-clock since
  completion*, which is **not the same thing** — a torrent stopped for a month still accrues. If a
  site's rule means "actually seeding for 14 days," that rule cannot be honored faithfully on
  rTorrent. Say so in the UI rather than quietly redefining the rule.
- `d.free_diskspace` returns the *minimum* free space across the devices the item's files live on.

**qBittorrent**
- **Auth is a session cookie** (`/api/v2/auth/login` → `SID`), and it requires a `Referer`/`Origin`
  header matching the host — a detail that breaks naive clients.
- **`pause` was renamed `stop` in 5.0** (API v2.11). An adapter must handle both, which makes API
  version detection a requirement, not a nicety.
- `free_space_on_disk` is free space **on the default save path** — not necessarily where a given
  torrent lives — and is documented as unreliable when full disk pre-allocation is enabled mid-download.

**Transmission** — the most complete API of the five for this purpose.
- **CSRF: a 409 response carries the correct `X-Transmission-Session-Id`**; the correct behavior is to
  update the header and *resend*. An adapter that treats 409 as an error will appear broken.
- **`free-space` takes a path and returns both free and total capacity** — the only client that
  gives total, which is exactly what a quota-style readout wants.
- **`trackers` includes a `sitename`** — free tracker→site normalization, the very thing §5 of the
  torrent-manager spec has to build by hand elsewhere.

**Deluge**
- JSON-RPC v1 at `/json`; core methods are namespaced (`core.*`). `core.get_free_space(path)` returns
  free bytes, defaulting to the configured download location.
- Documentation is notably thinner than the others; expect to read source. There is an open upstream
  ticket for **system quota support**, which is corroboration that no client models quota today (§4).

**SABnzbd** — not a torrent client, and its role in the framework is different: queue/history for the
settle gate and Preflight, per §4 of the redesign spec. It contributes **no** ratio, seed time, or
tracker data, which is precisely why capabilities must be per-function rather than per-client.
- **Disk space rides the queue call** (`diskspace1`/`diskspace2` free, plus `diskspacetotal1/2`), so it
  costs nothing extra — the cheapest free-space source of the five.

## 4. What this means for the connector framework

1. **Capabilities are per-function and tri-state** (native / derived / none), not a per-client flag.
   The matrix above is the proof: no two clients have the same shape, and SABnzbd shares almost
   nothing with the torrent clients.
2. **A derived capability must declare itself as derived**, because the semantics differ (rTorrent's
   seed time is the case in point). A rule built on a derived value should say so where the user sets
   it.
3. **Every client reports free space — none reports quota.** Confirms `docs/torrent-manager-spec.md`
   §6.0: on a shared seedbox the quota is user-configured and the client's free-space number is the
   *secondary* constraint. No amount of API work changes this.
4. **Auth is the least portable part** — cookie+Referer, CSRF-token-via-409, JSON-RPC session,
   SCGI/httprpc, and a query-string API key, one per client. Budget for it; it will not generalize.
5. **Version detection is mandatory**, not optional — qBittorrent's pause→stop rename alone forces it.
6. **Transmission is the reference implementation to design against**, because it has the richest and
   best-specified surface. Design the interface to Transmission's shape, then find out what each other
   client cannot do — rather than designing to rTorrent's limits and under-serving everything else.

## Sources

- [qBittorrent WebUI API (5.0)](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0))
- [qBittorrent sync/maindata `free_space_on_disk` discussion](https://github.com/qbittorrent/qBittorrent/discussions/15490) and [issue #16602](https://github.com/qbittorrent/qBittorrent/issues/16602)
- [Transmission RPC specification](https://github.com/alvistack/transmission-transmission/blob/main/docs/rpc-spec.md)
- [rTorrent commands reference](https://rtorrent-docs.readthedocs.io/en/latest/cmd-ref.html)
- [ruTorrent `erasedata` plugin source](https://github.com/Novik/ruTorrent/tree/master/plugins/erasedata) and its issue history — [#1147](https://github.com/Novik/ruTorrent/issues/1147), [#1675](https://github.com/Novik/ruTorrent/issues/1675), [#1758](https://github.com/Novik/ruTorrent/issues/1758), [#2148](https://github.com/Novik/ruTorrent/issues/2148). The `d.custom5` / `event.download.erased` mechanism in §2 was supplied by the user (2026-08-21) and corroborated against these.
- [Deluge Web JSON-RPC API](https://deluge.readthedocs.io/en/latest/reference/webapi.html) · [Deluge ticket #3276, system quota support](https://dev.deluge-torrent.org/ticket/3276)
- [SABnzbd API reference](https://sabnzbd.org/wiki/configuration/5.1/api)
