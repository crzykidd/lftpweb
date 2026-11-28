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

### Minor, noticed while diagnosing (open)

- **`_UNPACK_<name>` folders** (SAB's in-progress extraction dirs) appear on the Files tree as
  `REMOTE_ONLY` items. For example, `_UNPACK_Chicago.Fire.S11E05…` (id 29674136) and
  `_UNPACK_…S11E06…` are still listed after SAB renamed them. The settle gate rightly never
  queued them. Check whether they age out, or should be ignored like the `.nfs*` tombstones.
- **The rTorrent settle-gate skip didn't fire for S11E07.** The ordinary 60 s gate queued it
  instead. That is harmless, but worth knowing why, given finished torrents report `SEEDING`.
