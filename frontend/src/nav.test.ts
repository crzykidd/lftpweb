import { describe, expect, it } from 'vitest'
import { DOCS_TABS, NAV_ITEMS, SETTINGS_TABS, tabsForPath } from './nav'

describe('tabsForPath', () => {
  it('returns the Settings tabs for the settings root and its children', () => {
    expect(tabsForPath('/settings')).toBe(SETTINGS_TABS)
    expect(tabsForPath('/settings/queues')).toBe(SETTINGS_TABS)
  })

  it('returns the Docs tabs for the docs root and its children', () => {
    expect(tabsForPath('/docs')).toBe(DOCS_TABS)
    expect(tabsForPath('/docs/concepts')).toBe(DOCS_TABS)
  })

  it('returns null for a section with no tabs', () => {
    expect(tabsForPath('/files')).toBeNull()
    expect(tabsForPath('/transfers')).toBeNull()
    expect(tabsForPath('/')).toBeNull()
  })

  it('does not match a route that merely starts with a section name', () => {
    // The bug the old `pathname.startsWith('/settings')` check would have had if a
    // `/settings-export` page ever existed: a prefix match on the bare string, with no
    // separator, would have drawn the Settings tab strip over an unrelated page.
    expect(tabsForPath('/settings-export')).toBeNull()
    expect(tabsForPath('/docsomething')).toBeNull()
  })
})

describe('nav items', () => {
  it('has a Docs entry pointing at the docs section root', () => {
    expect(NAV_ITEMS.map((i) => i.path)).toContain('/docs')
  })

  it('gives every tab a path inside its own section', () => {
    for (const tab of SETTINGS_TABS) expect(tab.path.startsWith('/settings/')).toBe(true)
    for (const tab of DOCS_TABS) expect(tab.path.startsWith('/docs/')).toBe(true)
  })
})
