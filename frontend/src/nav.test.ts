import { describe, expect, it } from 'vitest'
import { DOCS_TABS, NAV_ITEMS, SETTINGS_TABS, TRANSFERS_TABS, tabsForPath } from './nav'

describe('tabsForPath', () => {
  it('returns the Transfers tabs for the transfers root and its children', () => {
    expect(tabsForPath('/transfers')).toBe(TRANSFERS_TABS)
    expect(tabsForPath('/transfers/queue')).toBe(TRANSFERS_TABS)
    expect(tabsForPath('/transfers/files')).toBe(TRANSFERS_TABS)
  })

  it('returns the Settings tabs for the settings root and its children', () => {
    expect(tabsForPath('/settings')).toBe(SETTINGS_TABS)
    expect(tabsForPath('/settings/queues')).toBe(SETTINGS_TABS)
  })

  it('returns the Docs tabs for the docs root and its children', () => {
    expect(tabsForPath('/docs')).toBe(DOCS_TABS)
    expect(tabsForPath('/docs/concepts')).toBe(DOCS_TABS)
  })

  it('returns null for a section with no tabs', () => {
    // `/files` is the old standalone route -- App.tsx now redirects it to `/transfers/files`
    // rather than rendering anything at it directly, so it never gets a tab strip of its own.
    // `/history` is the same shape, redirecting to `/events` (2026-08-20, phase 1 stage 7); like
    // `/events` itself, it has never had a tab strip.
    expect(tabsForPath('/files')).toBeNull()
    expect(tabsForPath('/history')).toBeNull()
    expect(tabsForPath('/events')).toBeNull()
    expect(tabsForPath('/')).toBeNull()
  })

  it('does not match a route that merely starts with a section name', () => {
    // The bug the old `pathname.startsWith('/settings')` check would have had if a
    // `/settings-export` page ever existed: a prefix match on the bare string, with no
    // separator, would have drawn the Settings tab strip over an unrelated page.
    expect(tabsForPath('/settings-export')).toBeNull()
    expect(tabsForPath('/docsomething')).toBeNull()
    expect(tabsForPath('/transfers-export')).toBeNull()
  })
})

describe('nav items', () => {
  it('has a Docs entry pointing at the docs section root', () => {
    expect(NAV_ITEMS.map((i) => i.path)).toContain('/docs')
  })

  it('has one Transfers entry, not a separate Files entry -- Files is a tab now', () => {
    expect(NAV_ITEMS.map((i) => i.path)).toContain('/transfers')
    expect(NAV_ITEMS.map((i) => i.path)).not.toContain('/files')
  })

  it('has an Events entry, not a History entry -- History became Events (phase 1 stage 7)', () => {
    expect(NAV_ITEMS.map((i) => i.path)).toContain('/events')
    expect(NAV_ITEMS.map((i) => i.path)).not.toContain('/history')
  })

  it('gives every tab a path inside its own section', () => {
    for (const tab of TRANSFERS_TABS) expect(tab.path.startsWith('/transfers/')).toBe(true)
    for (const tab of SETTINGS_TABS) expect(tab.path.startsWith('/settings/')).toBe(true)
    for (const tab of DOCS_TABS) expect(tab.path.startsWith('/docs/')).toBe(true)
  })

  it('puts Queue before Files -- Queue is the default/working-surface tab', () => {
    expect(TRANSFERS_TABS.map((t) => t.path)).toEqual(['/transfers/queue', '/transfers/files'])
  })
})
