// The `components/LifecycleIcons.tsx.ClientBrandMark` row chip's own label-selection logic
// (2026-08-30, prompts/2026-08-30-downloader-icon-on-rows.md), pulled out to a pure function so
// it is directly testable -- this project's frontend test suite is plain `vitest run` over pure
// functions (no `@testing-library/react`, no jsdom component rendering anywhere in this repo), so
// the "render nothing / render the known label / render the fallback" decision has to live
// somewhere a test can reach without mounting a component. `ClientBrandMark` itself stays a thin
// wrapper: call this, render nothing when it returns `null`, render the string it returns
// otherwise.
//
// **No brand logo, by design, not by omission.** simple-icons -- the CC0 dataset
// `LifecycleIcons.tsx`'s `SonarrLogo`/`RadarrLogo` copy path data from, verbatim, unmodified --
// ships neither a `sabnzbd` nor an `rtorrent` mark (checked directly against the dataset for this
// task, not assumed and not recalled from memory -- this file's own "copied verbatim or not at
// all" rule, restated in `EventsLinkButton.tsx`, leaves no other option). Every recognized `kind`
// below is therefore a short text label, never a logo choice.
const CLIENT_CHIP_LABELS: Record<string, string> = {
  sabnzbd: 'SAB',
  rtorrent: 'rT',
}

/** `null` in, `null` out -- "no data, no mark," the identical rule `lib/fileTree.ts.
 * arrIconVariant` applies for a `null` `arr_status`: an item with no recorded download client
 * (every item downloaded before migration 033 shipped, or one the poller hasn't matched a
 * transfer's own path to yet) must not get a placeholder or a question mark.
 *
 * A recognized `instanceKind` returns its short label (`'SAB'`/`'rT'`). An unrecognized or future
 * one still returns something -- the kind string itself, truncated to three characters and
 * uppercased -- rather than `null`, the same "never render nothing for a tracked item just
 * because the mark is missing" instinct `ArrBrandMark`'s own text-chip fallback follows.
 */
export function clientBrandLabel(instanceKind: string | null): string | null {
  if (instanceKind === null) return null
  return CLIENT_CHIP_LABELS[instanceKind] ?? instanceKind.slice(0, 3).toUpperCase()
}
