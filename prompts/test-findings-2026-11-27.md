# Test findings: 0.4.0 pre-release, test system (2026-11-27)

The `:dev` image with C1–C5 is running on the test system (`lftpweb.crzynet.com`, auth mode
`none`, behind Traefik). Findings are collected here and nothing is fixed until the user says
go. Then they are grouped by control.

Status key: **open**, **prompted**, **done**.

## Confirmed working

- **Startup:** the image starts cleanly. The C3 DNS-rebinding warning appears as designed while
  `LFTPWEB_ALLOWED_HOSTS` is unset. Both download clients (SAB, rTorrent) poll successfully.
- **SAB release, end to end (S11E05):**
  - The settle-gate skip verified SAB's "finished" and queued the release within seconds.
  - The transfer took 58 s, followed by verify (skipped, no sidecar), extract (no archives) and
    the prefix rename.
  - Sonarr was notified, the import was confirmed on two passes, and the remote and local copies
    were cleaned up.
- **Torrent (S11E07):** queued through the ordinary settle gate. The remote fingerprint had been
  stable for 60 s, so the torrent had stopped writing, and all 4.25 GB transferred in 111 s.
- **API calls:** all answer in 25–125 ms from the dev host. The C3–C5 middleware adds no
  measurable latency.

## Findings

### F1: Preflight keeps showing a finished download's last status for up to 150 s (done)

> "it seems to be stuck at showing sab extracting even thought it has been done extracting for
> almost 2 minutes" … "it went right from extracting to downloaded" … "now shows extracting but
> the file is queued even though sab shows extracting"

- **Seen three times:**
  - S11E05 showed "Extracting" through its whole transfer.
  - S11E06 still showed "Extracting" about 60 s after it had been queued.
  - S11E07 still showed rTorrent "downloading, 285 MB left" after lftpweb had already copied it.
- **Cause:** `core/clientsync.py` keeps a client Preflight row in `PreflightHold` for
  `PREFLIGHT_HOLD_S` (150 s, `core/preflight.py:178`) after it stops appearing. A **finished**
  transfer also stops appearing: SAB moves it to history, and rTorrent turns it to `SEEDING`,
  which is outside the Preflight-eligible phases. So its last status sticks for the full 150 s.
  The module docstring calls this an "accepted cost". It is from 2026-08-23, not this release.
- **Not a data risk.** In all three cases the queue decision was correct: either SAB history said
  completed and the 5 s re-fingerprint held, or the ordinary 60 s settle held. Only the label was
  stale.
- **Proposed fix:** retire a row immediately when the same poll gives a *positive* terminal
  signal for that `client_id`. For SAB that's a COMPLETED/FAILED history entry, which is already
  fetched every fast tick. For rTorrent it's the transfer appearing as `SEEDING`/`COMPLETED`.
  A blank or flaky response carries no such signal, so the anti-flicker hold still covers the
  v0.2.4 incident. Consider also retiring a row once its item is queued.

### F2: a Files page refresh takes 5–6 s (open; part 1, gzip, done)

> "the interface doesn't feel snappy.. a refresh of the page takes 5-6 seconds"

- **What `GET /api/files` returns:**
  - 3.1 MB of **uncompressed** JSON, 0.48 s just to download from the dev host.
  - 3,017 rows, of which **2,496 (83%) are `REMOVED_BOTH`** tombstones (gone from both sides).
    Every tombstone ever recorded is sent, so the response grows without bound.
  - About 1.1 KB per row.
- **Likely cost:** parsing and rendering about 3,000 rows in the browser. Not yet measured in a
  browser; it needs DevTools, or confirmation of which page was refreshed.
- **Candidate fixes:**
  1. **Done 2026-11-27.** Gzip API responses. Measured on the test system's real `/api/files` response: 3,154,794 B became 142,540 B, about 22× smaller. The synthetic fixture went from 2,783,009 B to 29,891 B. The change is non-breaking: one middleware line.
  2. Leave `REMOVED_BOTH` out by default and fetch it only when the Files filter asks for
     "Removed". This changes the default view, so it is the user's call.
  3. Check whether `FileTree` virtualises its rows.
- **Not a regression from C3–C5:** the per-request middleware adds nothing measurable.

### Minor, noticed while diagnosing (partly done)

- **`_UNPACK_<name>` folders — done 2026-11-27 with F3 (same cause; they now resolve to `REMOVED_BOTH` on the next full scan).** (SAB's in-progress extraction dirs) appear on the Files tree as
  `REMOTE_ONLY` items. For example, `_UNPACK_Chicago.Fire.S11E05…` (id 29674136) and
  `_UNPACK_…S11E06…` are still listed after SAB renamed them. The settle gate rightly never
  queued them. Check whether they age out, or should be ignored like the `.nfs*` tombstones.
- **The rTorrent settle-gate skip didn't fire for S11E07.** The ordinary 60 s gate queued it
  instead. That is harmless, but worth knowing why, given finished torrents report `SEEDING`.

### F3: things that vanish from the seedbox before they're downloaded stay `REMOTE_ONLY` forever (done 2026-11-27)

> "what are the 3000 lines? there were only 26 items in the complete list"

- **Seedbox vs. Files tree:** the seedbox `complete/ar-tv` folder held **one** entry, checked
  through `/api/browse/remote`. The Files tree lists **199** top-level `REMOTE_ONLY` items for
  `ar-tv`, plus 307 nested rows under them.
- **What those 199 are:**
  - 197 are SAB `_UNPACK_<name>` staging folders, first seen between 2026-08-15 and today.
  - The other four are real releases no longer on the seedbox: SNL S52E05 and S52E06, The
    Mentalist S06 (job 573 succeeded), and Chicago Fire S11E07.
- **Cause:** `core/engine.py`'s vanished-path loop (around line 1446) leaves any `prev_state`
  that `resolve_absence` has no opinion about "exactly as it was". `REMOTE_ONLY` is one of
  those. So a path that disappears from **both** trees while `REMOTE_ONLY` is never written
  again. SAB renames every `_UNPACK_` folder away, so each extraction leaves one ghost behind.
- **Effect:** ghost rows on Files, and a bigger `/api/files` payload (part of F2). Nothing is
  queued, because auto-queue already skips `_UNPACK_`/`_FAILED_`.
- **Proposed fix:**
  - A vanished `REMOTE_ONLY` path, together with its nested rows, resolves to `REMOVED_BOTH`
    like any other gone item. Or the row is deleted outright, if it was never downloaded.
  - A one-time cleanup of the existing ghosts.
  - Check this doesn't fire on a partial or failed remote scan. The existing partial-scan
    guard must apply.

### F2 follow-up: what the 3,017 rows actually are

The rows include every file inside a release folder (nested rows), not only releases.

| | ar-movies | ar-tv |
|---|---|---|
| `REMOVED_BOTH` top-level | 20 | 473 |
| `REMOVED_BOTH` nested | 125 | 1,878 |
| `REMOTE_ONLY` top-level (mostly F3 ghosts) | 2 | 199 |
| `REMOTE_ONLY` nested | 2 | 307 |
| Live (`DOWNLOADED`/`LOCAL_ONLY`/`FAILED`), all levels | 3 | 8 |

The oldest `REMOVED_BOTH` dates from 2026-08-15, so nothing ever ages out. Pruning tombstones
older than N days is a candidate alongside hiding them by default; that is the user's call.
