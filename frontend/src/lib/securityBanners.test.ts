import { describe, expect, it } from 'vitest'
import type { AuthSessionOut, HealthResponse } from '../api/types'
import { lftpSettingsIgnoredMessage, showHostAllowlistBanner } from './securityBanners'

function session(overrides: Partial<AuthSessionOut> = {}): AuthSessionOut {
  return {
    mode: 'none',
    authenticated: true,
    username: null,
    csrf_token: null,
    allowed_hosts_set: false,
    ...overrides,
  }
}

describe('showHostAllowlistBanner', () => {
  it('shows in none and proxy mode while LFTPWEB_ALLOWED_HOSTS is unset', () => {
    expect(showHostAllowlistBanner(session({ mode: 'none' }))).toBe(true)
    expect(showHostAllowlistBanner(session({ mode: 'proxy', username: 'alice' }))).toBe(true)
  })

  it('hides once the allow-list is set', () => {
    expect(showHostAllowlistBanner(session({ allowed_hosts_set: true }))).toBe(false)
    expect(showHostAllowlistBanner(session({ mode: 'proxy', allowed_hosts_set: true }))).toBe(false)
  })

  it('never shows in password mode', () => {
    expect(showHostAllowlistBanner(session({ mode: 'password', username: 'admin' }))).toBe(false)
  })

  it('stays hidden while the session is unknown or not authenticated', () => {
    expect(showHostAllowlistBanner(null)).toBe(false)
    expect(showHostAllowlistBanner(session({ authenticated: false }))).toBe(false)
  })
})

describe('lftpSettingsIgnoredMessage', () => {
  const health = (lftp_settings_ignored: string | null) =>
    ({ lftp_settings_ignored }) as unknown as HealthResponse

  it('returns the summary while lines are being ignored', () => {
    expect(lftpSettingsIgnoredMessage(health("1 extra lftp setting line(s) are being ignored: line 2 ('!id'): nope"))).toContain(
      'line 2',
    )
  })

  it('returns null otherwise', () => {
    expect(lftpSettingsIgnoredMessage(null)).toBeNull()
    expect(lftpSettingsIgnoredMessage(health(null))).toBeNull()
    expect(lftpSettingsIgnoredMessage(health(''))).toBeNull()
  })
})
