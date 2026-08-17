import { describe, expect, it } from 'vitest'
import changelogSource from '../../../CHANGELOG.md?raw'
import { compareVersions, parseChangelog, trimEmptySubsections, whatsNewSections } from './releaseNotes'

describe('parseChangelog', () => {
  it('splits sections newest-first and skips [Unreleased]', () => {
    const raw = `# Changelog\n\n## [Unreleased]\n\n### Added\n\n## [0.2.0] — 2026-08-16\n\nSecond.\n\n## [0.1.0] — 2026-08-14\n\nFirst.\n`
    expect(parseChangelog(raw)).toEqual([
      { version: '0.2.0', date: '2026-08-16', body: 'Second.' },
      { version: '0.1.0', date: '2026-08-14', body: 'First.' },
    ])
  })

  it('ignores an example "## [Unreleased]" heading that lives inside an HTML comment', () => {
    const raw = `# Changelog\n\nIntro.\n\n<!--\nSkeleton:\n## [Unreleased]\n\n### Added\n-->\n\n## [Unreleased]\n\n### Added\n\n## [0.1.0] — 2026-08-14\n\nOnly real release.\n`
    expect(parseChangelog(raw)).toEqual([{ version: '0.1.0', date: '2026-08-14', body: 'Only real release.' }])
  })

  it('degrades to a null date for a malformed/headerless section rather than throwing', () => {
    const raw = `# Changelog\n\n## [0.1.0]\n\nNo date given.\n`
    expect(parseChangelog(raw)).toEqual([{ version: '0.1.0', date: null, body: 'No date given.' }])
  })

  it('parses the actual CHANGELOG.md?raw content -- a future format drift fails here, not silently in the popup', () => {
    const sections = parseChangelog(changelogSource)
    expect(sections.length).toBeGreaterThan(0)
    expect(sections.some((s) => s.version === '0.1.0')).toBe(true)
    expect(sections.every((s) => s.version !== 'Unreleased')).toBe(true)
    for (const section of sections) {
      expect(section.version).toMatch(/^\d+\.\d+\.\d+$/)
    }
    // File order is newest-first; parseChangelog must preserve that, since whatsNewSections'
    // own re-sort is a belt-and-suspenders step, not something callers should rely on alone.
    const versions = sections.map((s) => s.version)
    const sorted = [...versions].sort((a, b) => compareVersions(b, a))
    expect(versions).toEqual(sorted)
  })
})

describe('compareVersions', () => {
  it('compares major, minor, then patch in order', () => {
    expect(compareVersions('1.0.0', '0.9.9')).toBe(1)
    expect(compareVersions('0.2.0', '0.10.0')).toBe(-1)
    expect(compareVersions('0.2.1', '0.2.0')).toBe(1)
    expect(compareVersions('0.2.1', '0.2.1')).toBe(0)
  })
})

const S = (version: string, date = '2026-01-01', body = `Notes for ${version}.`) => ({ version, date, body })

describe('whatsNewSections', () => {
  const sections = [S('0.3.0'), S('0.2.1'), S('0.2.0'), S('0.1.0')]

  it('shows nothing on a fresh browser (lastSeenVersion is null) -- not an upgrade', () => {
    expect(whatsNewSections('0.3.0', null, sections)).toEqual([])
  })

  it('shows nothing when nothing changed since last seen', () => {
    expect(whatsNewSections('0.3.0', '0.3.0', sections)).toEqual([])
  })

  it('accumulates every skipped release, newest first', () => {
    expect(whatsNewSections('0.3.0', '0.1.0', sections)).toEqual([S('0.3.0'), S('0.2.1'), S('0.2.0')])
  })

  it('shows just the one release for a normal single-version upgrade', () => {
    expect(whatsNewSections('0.2.1', '0.2.0', sections)).toEqual([S('0.2.1')])
  })

  it('shows nothing on a downgrade (lastSeen newer than current)', () => {
    expect(whatsNewSections('0.2.0', '0.3.0', sections)).toEqual([])
  })

  it('handles an unknown/very old stored version by accumulating everything up to current', () => {
    expect(whatsNewSections('0.2.0', '0.0.1', sections)).toEqual([S('0.2.0'), S('0.1.0')])
  })

  it('shows nothing when every release between last-seen and current has been archived out of CHANGELOG.md', () => {
    // lastSeen and current both postdate everything actually present in `sections` here.
    expect(whatsNewSections('0.9.0', '0.5.0', sections)).toEqual([])
  })
})

describe('trimEmptySubsections', () => {
  it('drops a heading with nothing under it before the next heading', () => {
    const body = '### Added\n\n- A thing.\n\n### Changed\n\n### Fixed\n\n- Another thing.\n'
    expect(trimEmptySubsections(body)).toBe('### Added\n\n- A thing.\n\n### Fixed\n\n- Another thing.')
  })

  it('drops a trailing empty heading with nothing after it at all', () => {
    const body = '### Added\n\n- A thing.\n\n### Removed\n'
    expect(trimEmptySubsections(body)).toBe('### Added\n\n- A thing.')
  })

  it('keeps every heading when all of them have content', () => {
    const body = '### Added\n\n- A.\n\n### Fixed\n\n- B.'
    expect(trimEmptySubsections(body)).toBe(body)
  })

  it('is a no-op on a body with no ### headings at all', () => {
    const body = 'Just prose, no subsections.'
    expect(trimEmptySubsections(body)).toBe(body)
  })
})
