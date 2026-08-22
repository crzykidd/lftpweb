import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getHealth } from '../api/client'
import type { HealthResponse } from '../api/types'
import { WhatsNewDialog } from './WhatsNewDialog'

// The decision logic (what to show, when to store silently) is `lib/releaseNotes.ts`'s own
// suite; this file only covers the glue this component adds on top of it -- reading/writing
// `whatsnew.lastSeenVersion`, and that Dismiss (button or backdrop) writes storage the same way
// a no-op does. Real `localStorage` under happy-dom, not a mock -- `lib/storage.ts` already has
// its own coverage for the failure-handling wrapper itself.

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

async function mount(container: HTMLDivElement) {
  const root = createRoot(container)
  await act(async () => {
    root.render(
      <MemoryRouter>
        <WhatsNewDialog />
      </MemoryRouter>,
    )
  })
  return root
}

describe('WhatsNewDialog', () => {
  let container: HTMLDivElement

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    localStorage.clear()
  })

  afterEach(() => {
    document.body.removeChild(container)
    vi.resetAllMocks()
    localStorage.clear()
  })

  it('renders nothing and stores the version silently on a fresh browser (no lastSeenVersion yet)', async () => {
    mockGetHealth.mockResolvedValue(health({ version: '0.2.1' }))
    const root = await mount(container)

    expect(container.querySelector('[aria-label="Close"]')).toBeNull()
    expect(localStorage.getItem('lftpweb.whatsnew.lastSeenVersion')).toBe('"0.2.1"')

    root.unmount()
  })

  it('shows the popup for an upgrade and Dismiss (Got it) writes the new version to storage', async () => {
    localStorage.setItem('lftpweb.whatsnew.lastSeenVersion', JSON.stringify('0.1.0'))
    mockGetHealth.mockResolvedValue(health({ version: '0.2.1' }))
    const root = await mount(container)

    const backdrop = container.querySelector('[aria-label="Close"]')
    expect(backdrop).not.toBeNull()
    expect(container.textContent).toContain('v0.2.1')

    const gotIt = Array.from(container.querySelectorAll('button')).find((b) => b.textContent === 'Got it')
    expect(gotIt).toBeDefined()
    await act(async () => {
      gotIt?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })

    expect(localStorage.getItem('lftpweb.whatsnew.lastSeenVersion')).toBe('"0.2.1"')
    expect(container.querySelector('[aria-label="Close"]')).toBeNull()

    root.unmount()
  })

  it('renders nothing when the stored version already matches the current one', async () => {
    localStorage.setItem('lftpweb.whatsnew.lastSeenVersion', JSON.stringify('0.2.1'))
    mockGetHealth.mockResolvedValue(health({ version: '0.2.1' }))
    const root = await mount(container)

    expect(container.querySelector('[aria-label="Close"]')).toBeNull()

    root.unmount()
  })
})
