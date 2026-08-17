import { describe, expect, it } from 'vitest'
import { breadcrumbSegments, descendPath, fallbackNote, remoteBrowseDisabled } from './pathBrowse'
import type { HostOut } from '../api/types'

const HOST: HostOut = {
  id: 1,
  name: 'seedbox',
  address: '1.2.3.4',
  port: 22,
  username: 'seeduser',
  auth_method: 'password',
  key_path: null,
  has_password: true,
  has_ssh_key: false,
  active_key_source: null,
  known_hosts_policy: 'accept-and-pin',
  credentials_need_reentry: false,
  net_connection_limit: null,
}

describe('remoteBrowseDisabled', () => {
  it('is disabled with no host configured', () => {
    expect(remoteBrowseDisabled(null)).toBe(true)
  })

  it('is disabled when credentials need re-entry', () => {
    expect(remoteBrowseDisabled({ ...HOST, credentials_need_reentry: true })).toBe(true)
  })

  it('is enabled for a healthy, configured host', () => {
    expect(remoteBrowseDisabled(HOST)).toBe(false)
  })
})

describe('descendPath', () => {
  it('joins onto root without a doubled slash', () => {
    expect(descendPath('/', 'data')).toBe('/data')
  })

  it('joins onto a non-root path', () => {
    expect(descendPath('/data/pickup', 'Release.One')).toBe('/data/pickup/Release.One')
  })

  it('tolerates a trailing slash on the parent', () => {
    expect(descendPath('/data/pickup/', 'Release.One')).toBe('/data/pickup/Release.One')
  })
})

describe('breadcrumbSegments', () => {
  it('root is a single crumb', () => {
    expect(breadcrumbSegments('/')).toEqual([{ label: '/', path: '/' }])
  })

  it('builds one crumb per path segment, each carrying its own absolute path', () => {
    expect(breadcrumbSegments('/data/pickup/Release')).toEqual([
      { label: '/', path: '/' },
      { label: 'data', path: '/data' },
      { label: 'pickup', path: '/data/pickup' },
      { label: 'Release', path: '/data/pickup/Release' },
    ])
  })

  it('a single top-level directory', () => {
    expect(breadcrumbSegments('/data')).toEqual([
      { label: '/', path: '/' },
      { label: 'data', path: '/data' },
    ])
  })
})

describe('fallbackNote', () => {
  it('is null when nothing fell back', () => {
    expect(fallbackNote(null)).toBeNull()
  })

  it('names what was actually requested when it did', () => {
    const note = fallbackNote('/downloads/rtor')
    expect(note).toContain('/downloads/rtor')
    expect(note).toContain('nearest existing directory')
  })
})
