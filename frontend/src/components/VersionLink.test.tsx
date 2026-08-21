import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getHealth } from '../api/client'
import type { HealthResponse } from '../api/types'
import { VersionLink } from './VersionLink'

// A real mount (createRoot + act), not `renderToStaticMarkup` (MarkdownDoc.test.tsx's own
// pattern) -- this component's whole point is a `useEffect`-driven health fetch, which a static
// server render never runs at all. No new test dependency: React 19 exports `act` itself, and
// `react-dom/client` is already a dependency (2026-08-17,
// prompts/2026-08-17-whats-new-popup-and-release-notes.md -- "check VersionLink.tsx renders an
// internal route correctly").

vi.mock('../api/client', () => ({ getHealth: vi.fn() }))
const mockGetHealth = vi.mocked(getHealth)

function health(overrides: Partial<HealthResponse> = {}): HealthResponse {
  return {
    status: 'ok',
    version: '0.2.1',
    db: true,
    uptime_s: 123,
    repo_url: '',
    host_reachable: null,
    scheduler_alive: true,
    queue_paused: false,
    queue_paused_until: null,
    build_sha: null,
    build_channel: null,
    ...overrides,
  }
}

describe('VersionLink', () => {
  let container: HTMLDivElement

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
  })

  afterEach(() => {
    document.body.removeChild(container)
    vi.resetAllMocks()
  })

  it('renders the in-app Release notes route as a router Link -- no target/rel, so it never full-page-navigates', async () => {
    mockGetHealth.mockResolvedValue(health())
    const root = createRoot(container)
    await act(async () => {
      root.render(
        <MemoryRouter>
          <VersionLink />
        </MemoryRouter>,
      )
    })

    const link = container.querySelector('a')
    expect(link?.getAttribute('href')).toBe('/docs/release-notes')
    expect(link?.getAttribute('target')).toBeNull()
    expect(link?.textContent).not.toContain('↗') // no "↗" external-link glyph

    root.unmount()
  })

  it('still renders a dev build\'s commit link as a plain external <a target="_blank">', async () => {
    mockGetHealth.mockResolvedValue(
      health({ build_channel: 'dev', build_sha: 'abc1234', repo_url: 'https://github.com/crzykidd/lftpweb' }),
    )
    const root = createRoot(container)
    await act(async () => {
      root.render(
        <MemoryRouter>
          <VersionLink />
        </MemoryRouter>,
      )
    })

    const link = container.querySelector('a')
    expect(link?.getAttribute('href')).toBe('https://github.com/crzykidd/lftpweb/commit/abc1234')
    expect(link?.getAttribute('target')).toBe('_blank')

    root.unmount()
  })
})
