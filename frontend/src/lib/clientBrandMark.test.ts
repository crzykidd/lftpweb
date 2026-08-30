import { describe, expect, it } from 'vitest'
import { clientBrandLabel } from './clientBrandMark'

// `ClientBrandMark`'s own render decision, pulled out here so it's testable without mounting a
// component (2026-08-30, prompts/2026-08-30-downloader-icon-on-rows.md) -- see this module's own
// docstring for why. Three cases the handoff prompt itself names: a known kind, an unknown/future
// kind, and `null`.

describe('clientBrandLabel', () => {
  it('renders nothing (returns null) for a null instanceKind -- no data, no mark', () => {
    expect(clientBrandLabel(null)).toBeNull()
  })

  it('renders the short label for a known kind', () => {
    expect(clientBrandLabel('sabnzbd')).toBe('SAB')
    expect(clientBrandLabel('rtorrent')).toBe('rT')
  })

  it('falls back to the kind itself, truncated and uppercased, for an unrecognized/future kind', () => {
    expect(clientBrandLabel('deluge')).toBe('DEL')
    expect(clientBrandLabel('qbittorrent')).toBe('QBI')
  })

  it('never returns null for a non-null instanceKind -- "never render nothing for a tracked item"', () => {
    // A short, unusual kind still produces a truthy label rather than an empty string.
    expect(clientBrandLabel('x')).toBe('X')
  })
})
