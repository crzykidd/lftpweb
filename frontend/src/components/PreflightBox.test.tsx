import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getSettleSettings } from '../api/client'
import type { PreflightResponse } from '../api/types'
import { PreflightBox } from './PreflightBox'

// Finding #13 (2026-08-23, prompts/2026-08-23-category-control-and-banner-link.md): the
// unattributed-clients banner used to name a settings path that doesn't exist ("Settings →
// Integrations → API Clients") and wasn't a link. It now deep-links straight to the specific
// instance (`lib/clientEditLink.ts`). `MemoryRouter` is required here -- the banner renders a
// react-router `Link`, which throws outside a Router context.

vi.mock('../api/client', () => ({
  getSettleSettings: vi.fn(),
}))

const mockGetSettleSettings = vi.mocked(getSettleSettings)

function baseResponse(overrides: Partial<PreflightResponse> = {}): PreflightResponse {
  return {
    source_configured: false,
    rows: [],
    gated_queues: [],
    unattributed_clients: [],
    ...overrides,
  }
}

async function mount(container: HTMLDivElement, response: PreflightResponse | undefined) {
  const root = createRoot(container)
  await act(async () => {
    root.render(
      <MemoryRouter>
        <PreflightBox response={response} />
      </MemoryRouter>,
    )
  })
  await act(async () => {
    await Promise.resolve()
  })
  return root
}

describe('PreflightBox unattributed-clients banner', () => {
  let container: HTMLDivElement

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    mockGetSettleSettings.mockRejectedValue(new Error('not needed for this test'))
  })

  afterEach(() => {
    document.body.removeChild(container)
    vi.resetAllMocks()
  })

  it('links straight to the specific client, not a hand-navigated settings path', async () => {
    const root = await mount(
      container,
      baseResponse({
        unattributed_clients: [
          {
            client_id: 7,
            client_name: 'ultracc rtorrent',
            count: 2,
            categories: [],
            no_category_count: 0,
          },
        ],
      }),
    )

    const link = container.querySelector('a')
    expect(link).not.toBeNull()
    expect(link?.getAttribute('href')).toBe('/settings/clients?edit=7')

    // The old, nonexistent breadcrumb must be gone.
    expect(container.textContent).not.toContain('Settings → Integrations → API Clients')
    expect(container.textContent).toContain('ultracc rtorrent')
    expect(container.textContent).toContain('2')

    root.unmount()
  })

  it('renders nothing when there is nothing unattributable', async () => {
    const root = await mount(container, baseResponse())
    expect(container.querySelector('a')).toBeNull()
    root.unmount()
  })

  // Round 4 (2026-08-23, live evidence): the banner must name *which* categories went
  // unmatched, and call out "no category at all" distinctly from an unmapped category.

  it('names the unmatched categories, not just the count', async () => {
    const root = await mount(
      container,
      baseResponse({
        unattributed_clients: [
          {
            client_id: 7,
            client_name: 'ultracc rtorrent',
            count: 2,
            categories: ['ar-movies'],
            no_category_count: 0,
          },
        ],
      }),
    )

    expect(container.textContent).toContain('in ar-movies')
    root.unmount()
  })

  it('calls out items with no category at all distinctly from an unmapped category', async () => {
    const root = await mount(
      container,
      baseResponse({
        unattributed_clients: [
          {
            client_id: 7,
            client_name: 'ultracc rtorrent',
            count: 3,
            categories: ['ar-movies'],
            no_category_count: 2,
          },
        ],
      }),
    )

    expect(container.textContent).toContain('in ar-movies')
    expect(container.textContent).toContain('2 with no category')
    root.unmount()
  })
})
