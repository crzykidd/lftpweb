// Pure changelog parsing shared by the what's-new popup (`components/WhatsNewDialog.tsx`) and
// its own Vitest suite (2026-08-17, prompts/2026-08-17-whats-new-popup-and-release-notes.md).
// The Docs -> Release notes page does *not* use this module -- it renders `CHANGELOG.md`
// verbatim through `MarkdownDoc.tsx`'s section renderer, unstructured, because the changelog is
// the single source of truth (`release-prep-and-cut` standard) and must never be reshaped. This
// module exists only to answer "what changed since the last version this browser saw."

/** One `## [X.Y.Z] — YYYY-MM-DD` release section. `body` is the section's raw Markdown,
 * verbatim from the file (blank `### Heading` subsections included) -- `trimEmptySubsections`
 * below is a separate, opt-in step for a renderer that wants to hide them, never baked in here,
 * so this function's output always matches the file it read.
 */
export interface ChangelogSection {
  version: string
  /** `YYYY-MM-DD`, or `null` for a section header with no date (shouldn't happen for a real
   * release, but a malformed or hand-edited header must degrade rather than throw). */
  date: string | null
  body: string
}

// `— ` between the version and date is an em dash (U+2014), matching `release-prep`'s own
// header format; the date group is optional so a headerless variant still parses as *some*
// section rather than throwing away the whole file over one line.
const SECTION_HEADER_RE = /^##\s+\[(Unreleased|\d+\.\d+\.\d+)\](?:\s*—\s*(\d{4}-\d{2}-\d{2}))?\s*$/

/** Splits `CHANGELOG.md`'s raw text into its release sections, newest-first (the file's own
 * order), `[Unreleased]` dropped. HTML comments are stripped first -- the file keeps a
 * commented-out skeleton for the next roll (`<!-- ... ## [Unreleased] ... -->`) as an authoring
 * aid, and that example heading would otherwise be mistaken for a real section boundary by the
 * same line-based split that finds the real ones.
 */
export function parseChangelog(raw: string): ChangelogSection[] {
  const withoutComments = raw.replace(/<!--[\s\S]*?-->/g, '')
  const lines = withoutComments.replace(/\r\n/g, '\n').split('\n')

  const sections: ChangelogSection[] = []
  let current: { version: string; date: string | null; bodyLines: string[] } | null = null

  const flush = () => {
    if (current && current.version !== 'Unreleased') {
      sections.push({ version: current.version, date: current.date, body: current.bodyLines.join('\n').trim() })
    }
    current = null
  }

  for (const line of lines) {
    const match = SECTION_HEADER_RE.exec(line)
    if (match) {
      flush()
      current = { version: match[1], date: match[2] ?? null, bodyLines: [] }
    } else if (current) {
      current.bodyLines.push(line)
    }
  }
  flush()

  return sections
}

/** Integer-triple semver compare -- no pre-release/build metadata handling, because this
 * project's own versions (`standards.md`'s `release-prep-and-cut`) are always bare
 * `MAJOR.MINOR.PATCH`. Returns the usual -1 / 0 / 1. */
export function compareVersions(a: string, b: string): number {
  const pa = a.split('.').map(Number)
  const pb = b.split('.').map(Number)
  for (let i = 0; i < 3; i++) {
    const diff = (pa[i] ?? 0) - (pb[i] ?? 0)
    if (diff !== 0) return diff > 0 ? 1 : -1
  }
  return 0
}

/** The sections the what's-new popup should show, newest first. Settled rules
 * (2026-08-17, prompts/2026-08-17-whats-new-popup-and-release-notes.md):
 *
 * - `lastSeenVersion == null` -- a fresh browser, not an upgrade -- shows nothing; the caller
 *   stores the current version and moves on.
 * - `lastSeenVersion == currentVersion` -- nothing changed since this browser last looked.
 * - Otherwise, every section with `lastSeen < version <= current` -- an upgrade that skipped a
 *   release (or a browser that hadn't been opened in a while) shows all of them, not just the
 *   latest. If that range is empty (a downgrade, since no version can satisfy `lastSeen < v` and
 *   `v <= current` when `lastSeen >= current`; or every matching release having since been
 *   archived out of `CHANGELOG.md` entirely) the result is `[]` the same as the first two cases
 *   -- the caller always stores silently on an empty result, never leaving `lastSeenVersion`
 *   stale.
 */
export function whatsNewSections(
  currentVersion: string,
  lastSeenVersion: string | null,
  sections: ChangelogSection[],
): ChangelogSection[] {
  if (lastSeenVersion == null) return []
  if (lastSeenVersion === currentVersion) return []

  return sections
    .filter((s) => compareVersions(lastSeenVersion, s.version) < 0 && compareVersions(s.version, currentVersion) <= 0)
    .sort((a, b) => compareVersions(b.version, a.version))
}

/** Drops a `### Heading` subsection that has nothing under it before the next `### ` heading (or
 * the end of the body) -- most releases don't touch every Keep-a-Changelog category, and a
 * popup listing five empty headings under one real one reads as broken. Opt-in and popup-only:
 * the Docs -> Release notes page never calls this, since it renders the file verbatim. */
export function trimEmptySubsections(body: string): string {
  const lines = body.split('\n')
  const out: string[] = []

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (/^###\s+/.test(line)) {
      let j = i + 1
      let hasContent = false
      while (j < lines.length && !/^###\s+/.test(lines[j])) {
        if (lines[j].trim() !== '') hasContent = true
        j++
      }
      if (!hasContent) {
        i = j - 1
        continue
      }
    }
    out.push(line)
  }

  return out.join('\n').trim()
}
