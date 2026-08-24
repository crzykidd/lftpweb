// The per-client "do you even need this control" relevance copy (Part 3 of
// prompts/2026-08-23-category-tristate-and-exclusion.md). The user's own words: "the setting
// shows in SAB and the ui isn't clear that you don't need it in the current configuration."
//
// **Derived entirely from observed attribution counts, never from `client_type`.** Hardcoding
// "usenet clients don't need this, torrent clients do" would be exactly the client-name branching
// spec §4.4/§5.1 forbid, and it is the generalisation this feature has already got wrong four
// times (see docs/decisions.md's 2026-08-23 entries). `DownloadClientOut.attribution_sample_size`/
// `attribution_matched_by_path` (migration 031) are the two numbers `core.clientsync.
// ClientSyncScheduler._attribution_sample` observes on every poll pass -- this module only ever
// reads them, with no branch on which connector produced them. The examples that motivated the
// exact wording:
//
//   SAB:      "12 of 12 recent downloads matched by folder — no mapping needed unless a category
//              lands outside a queue folder."
//   rTorrent: "0 of 2 matched by folder — this client's downloads are matched by category, so a
//              mapping is required."
//
// Same sentence template, true for both, computed from the same two fields.

/** `null` for either field means "the poller has never had anything to observe yet" -- a fresh
 * instance, or one that has never reported a transfer with a `content_path` or a `category`.
 * Distinct from `sampleSize === 0`, which the backend never actually persists (a quiet pass
 * leaves the prior reading in place, see `core.clientsync._record_attribution_stats`'s own
 * docstring) but which this function still handles rather than assuming.
 */
export function describePathAttributionRelevance(
  sampleSize: number | null,
  matchedByPath: number | null,
): string {
  if (sampleSize == null || matchedByPath == null || sampleSize === 0) {
    return 'Not yet observed — once this client reports some downloads, this will say whether the category mapping below is actually needed.'
  }
  const downloadsWord = sampleSize === 1 ? 'download' : 'downloads'
  if (matchedByPath === sampleSize) {
    return (
      `${matchedByPath} of ${sampleSize} recent ${downloadsWord} matched by folder — no mapping ` +
      'needed unless a category lands outside a queue folder.'
    )
  }
  if (matchedByPath === 0) {
    return (
      `0 of ${sampleSize} recent ${downloadsWord} matched by folder — this client's downloads ` +
      'are matched by category, so a mapping is required.'
    )
  }
  return (
    `${matchedByPath} of ${sampleSize} recent ${downloadsWord} matched by folder — the rest need ` +
    'a category → queue mapping.'
  )
}
