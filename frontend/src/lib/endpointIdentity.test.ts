import { describe, expect, it } from 'vitest'
import { sshIdentityKey, urlIdentityKey } from './endpointIdentity'

describe('urlIdentityKey', () => {
  it('ignores trailing slash, host case, default port and whitespace', () => {
    const base = urlIdentityKey('http://sab.example:8080')
    expect(urlIdentityKey('http://sab.example:8080/')).toBe(base)
    expect(urlIdentityKey('  http://SAB.Example:8080  ')).toBe(base)
    expect(urlIdentityKey('http://sab.example')).toBe(urlIdentityKey('http://sab.example:80'))
    expect(urlIdentityKey('https://sab.example/')).toBe(urlIdentityKey('HTTPS://sab.example:443'))
  })

  it('sees a different host, scheme, port, path or embedded credentials', () => {
    const base = urlIdentityKey('http://sab.example')
    expect(urlIdentityKey('http://evil.example')).not.toBe(base)
    expect(urlIdentityKey('https://sab.example')).not.toBe(base)
    expect(urlIdentityKey('http://sab.example:8080')).not.toBe(base)
    expect(urlIdentityKey('http://sab.example/other')).not.toBe(base)
    expect(urlIdentityKey('http://u:p@sab.example')).not.toBe(base)
  })

  it('counts an rpc path and falls back deterministically for unparseable text', () => {
    expect(urlIdentityKey('http://h', '/RPC2')).toBe(urlIdentityKey('http://h/', 'RPC2/'))
    expect(urlIdentityKey('http://h', '/RPC2')).not.toBe(urlIdentityKey('http://h', '/x'))
    expect(urlIdentityKey('not a url')).toBe(urlIdentityKey(' not a url/ '))
    expect(urlIdentityKey('not a url')).not.toBe(urlIdentityKey('other'))
  })
})

describe('sshIdentityKey', () => {
  it('normalises address case and whitespace, keeps port and username significant', () => {
    expect(sshIdentityKey(' Box.Example ', '22', ' u ')).toBe(sshIdentityKey('box.example', 22, 'u'))
    expect(sshIdentityKey('box', '22', 'u')).not.toBe(sshIdentityKey('box', '2222', 'u'))
    expect(sshIdentityKey('box', '22', 'u')).not.toBe(sshIdentityKey('box', '22', 'U'))
    expect(sshIdentityKey('box', '22', 'u')).not.toBe(sshIdentityKey('evil', '22', 'u'))
  })
})
