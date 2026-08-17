import { describe, expect, it } from 'vitest'
import type { HealthResponse } from '../api/types'
import { versionBadge } from './versionBadge'

function health(overrides: Partial<HealthResponse> = {}): HealthResponse {
  return {
    status: 'ok',
    version: '0.1.1',
    db: true,
    uptime_s: 123,
    repo_url: '',
    host_reachable: null,
    scheduler_alive: true,
    build_sha: null,
    build_channel: null,
    ...overrides,
  }
}

describe('versionBadge', () => {
  it('is null while health has not loaded -- the caller keeps its own placeholder', () => {
    expect(versionBadge(null)).toBeNull()
  })

  describe('channel null (local uv run, compose dev stack -- no args baked)', () => {
    it('links to the in-app Release notes route even when repo_url is empty (2026-08-17: no longer a dead link)', () => {
      expect(versionBadge(health())).toEqual({ label: 'v0.1.1', dev: false, href: '/docs/release-notes' })
    })

    it('still links to the in-app Release notes route, not GitHub, when repo_url is present', () => {
      expect(versionBadge(health({ repo_url: 'https://github.com/crzykidd/lftpweb' }))).toEqual({
        label: 'v0.1.1',
        dev: false,
        href: '/docs/release-notes',
      })
    })
  })

  describe('channel release', () => {
    it('renders identically to channel null -- a build_sha present but channel != dev never triggers the badge', () => {
      expect(
        versionBadge(
          health({
            build_channel: 'release',
            build_sha: 'abc1234',
            repo_url: 'https://github.com/crzykidd/lftpweb',
          }),
        ),
      ).toEqual({
        label: 'v0.1.1',
        dev: false,
        href: '/docs/release-notes',
      })
    })
  })

  describe('channel dev', () => {
    it('shows the DEV badge with the short sha and links to the commit when both sha and repo_url are present', () => {
      expect(
        versionBadge(
          health({
            build_channel: 'dev',
            build_sha: 'abc1234',
            repo_url: 'https://github.com/crzykidd/lftpweb',
          }),
        ),
      ).toEqual({
        label: 'DEV: v0.1.1 · abc1234',
        dev: true,
        href: 'https://github.com/crzykidd/lftpweb/commit/abc1234',
      })
    })

    it('shows the DEV badge with no link when repo_url is absent (never a dead link)', () => {
      expect(versionBadge(health({ build_channel: 'dev', build_sha: 'abc1234' }))).toEqual({
        label: 'DEV: v0.1.1 · abc1234',
        dev: true,
        href: null,
      })
    })

    it('drops the sha suffix and falls back to the release link if build_sha is somehow absent', () => {
      expect(
        versionBadge(
          health({ build_channel: 'dev', repo_url: 'https://github.com/crzykidd/lftpweb' }),
        ),
      ).toEqual({
        label: 'DEV: v0.1.1',
        dev: true,
        href: 'https://github.com/crzykidd/lftpweb/releases/tag/v0.1.1',
      })
    })

    it('drops the sha suffix and has no link if both build_sha and repo_url are absent', () => {
      expect(versionBadge(health({ build_channel: 'dev' }))).toEqual({
        label: 'DEV: v0.1.1',
        dev: true,
        href: null,
      })
    })
  })
})
