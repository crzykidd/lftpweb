import { describe, expect, it } from 'vitest'
import { clientEditHref, parseClientEditParam } from './clientEditLink'

describe('clientEditHref', () => {
  it('builds a /settings/clients path carrying the client id', () => {
    expect(clientEditHref(7)).toBe('/settings/clients?edit=7')
  })
})

describe('parseClientEditParam', () => {
  it('round-trips through clientEditHref', () => {
    const href = clientEditHref(7)
    const [, query] = href.split('?')
    expect(parseClientEditParam(query)).toBe(7)
  })

  it('accepts a URLSearchParams instance directly, not just a string', () => {
    expect(parseClientEditParam(new URLSearchParams('edit=9'))).toBe(9)
  })

  it('reports no target when edit is absent', () => {
    expect(parseClientEditParam('')).toBeNull()
    expect(parseClientEditParam('other=3')).toBeNull()
  })

  it('degrades to no target on a malformed edit param rather than crashing', () => {
    expect(parseClientEditParam('edit=abc')).toBeNull()
    expect(parseClientEditParam('edit=-1')).toBeNull()
    expect(parseClientEditParam('edit=')).toBeNull()
  })
})
