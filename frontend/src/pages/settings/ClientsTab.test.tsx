import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  getHost,
  listClientInstances,
  listClientTypes,
  listQueues,
  testClientInstance,
} from '../../api/client'
import type { ClientTypeOut, DownloadClientOut, DownloadClientTestResponse } from '../../api/types'
import { ClientsTab } from './ClientsTab'

// Covers this task's own four required cases (docs/download-client-framework-spec.md §4.3,
// §4.4; this task's handoff prompt): the generic connector form rendering from a declared
// schema (no per-connector hand-written form), a derived capability's label + note, a missing
// capability's stated disabled reason, and a failed test never blanking a previously known
// capability set. Mounted with `createRoot`/`act`, the same harness `WhatsNewDialog.test.tsx`
// established for this repo's component-render tests -- no testing-library dependency here.

vi.mock('../../api/client', () => ({
  listClientTypes: vi.fn(),
  listClientInstances: vi.fn(),
  listQueues: vi.fn(),
  getHost: vi.fn(),
  createClientInstance: vi.fn(),
  updateClientInstance: vi.fn(),
  deleteClientInstance: vi.fn(),
  testClientInstance: vi.fn(),
}))

const mockListClientTypes = vi.mocked(listClientTypes)
const mockListClientInstances = vi.mocked(listClientInstances)
const mockListQueues = vi.mocked(listQueues)
const mockGetHost = vi.mocked(getHost)
const mockTestClientInstance = vi.mocked(testClientInstance)

const USENET_TYPE: ClientTypeOut = {
  client_type: 'sabnzbd',
  family: 'usenet',
  config_schema: [
    { key: 'base_url', label: 'Base URL', kind: 'str', required: true, default: null, help_text: null },
    { key: 'api_key', label: 'API key', kind: 'secret', required: true, default: null, help_text: null },
  ],
}

const TORRENT_TYPE: ClientTypeOut = {
  client_type: 'rtorrent',
  family: 'torrent',
  config_schema: [
    { key: 'port', label: 'SCGI port', kind: 'int', required: true, default: 5000, help_text: null },
    {
      key: 'verify_cert',
      label: 'Verify TLS certificate',
      kind: 'bool',
      required: false,
      default: true,
      help_text: null,
    },
  ],
}

function instance(overrides: Partial<DownloadClientOut> = {}): DownloadClientOut {
  return {
    id: 1,
    name: 'Main SABnzbd',
    client_type: 'sabnzbd',
    config: { base_url: 'http://seedbox:8080' },
    has_secret: true,
    enabled: true,
    capabilities: null,
    capabilities_probed_at: null,
    version: null,
    base_paths: [],
    categories: [],
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
    ...overrides,
  }
}

async function mount(container: HTMLDivElement) {
  const root = createRoot(container)
  await act(async () => {
    root.render(<ClientsTab />)
  })
  // Flush the microtasks `Promise.all` in the component's mount effect needs to resolve and
  // re-render with the fetched data.
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
  return root
}

describe('ClientsTab', () => {
  let container: HTMLDivElement

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    mockGetHost.mockResolvedValue(null)
    mockListQueues.mockResolvedValue([])
  })

  afterEach(() => {
    document.body.removeChild(container)
    vi.resetAllMocks()
  })

  it('renders the connection form generically from the declared schema, and switches fields with the type', async () => {
    mockListClientTypes.mockResolvedValue([USENET_TYPE, TORRENT_TYPE])
    mockListClientInstances.mockResolvedValue([])
    const root = await mount(container)

    // The usenet type is selected by default (first registered type) -- its declared fields
    // render with their own labels, sourced from the schema, not written by hand here.
    const labels = Array.from(container.querySelectorAll('label span')).map((el) => el.textContent)
    expect(labels).toContain('Base URL *')
    expect(labels).toContain('API key *')
    expect(container.querySelector('input[type="password"]')).not.toBeNull()

    // Switching the type re-renders an entirely different field set -- driven purely by the
    // second type's own schema (int + bool), never a hand-picked list per connector.
    const select = container.querySelector('select') as HTMLSelectElement
    await act(async () => {
      select.value = 'rtorrent'
      select.dispatchEvent(new Event('change', { bubbles: true }))
    })
    const labelsAfter = Array.from(container.querySelectorAll('label span')).map((el) => el.textContent)
    expect(labelsAfter).toContain('SCGI port *')
    expect(labelsAfter).toContain('Verify TLS certificate')
    expect(container.querySelector('input[type="password"]')).toBeNull()
    expect(container.querySelector('input[type="number"]')).not.toBeNull()
    expect(container.querySelector('input[type="checkbox"]')).not.toBeNull()

    root.unmount()
  })

  it('labels a derived capability as derived and shows its note', async () => {
    mockListClientTypes.mockResolvedValue([USENET_TYPE])
    mockListClientInstances.mockResolvedValue([
      instance({
        capabilities: {
          operations: {},
          fields: {
            seed_time_s: {
              support: 'derived',
              note: 'wall-clock since completion — a stopped torrent still accrues',
            },
          },
        },
      }),
    ])
    const root = await mount(container)

    expect(container.textContent).toContain('derived')
    expect(container.textContent).toContain(
      'wall-clock since completion — a stopped torrent still accrues',
    )

    root.unmount()
  })

  it('states a reason for a missing capability instead of a bare disabled control', async () => {
    mockListClientTypes.mockResolvedValue([USENET_TYPE])
    mockListClientInstances.mockResolvedValue([
      instance({
        capabilities: {
          operations: {},
          fields: { ratio: { support: 'none', note: 'no ratio (spec §5)' } },
        },
      }),
    ])
    const root = await mount(container)

    expect(container.textContent).toContain('Not available')
    expect(container.textContent).toContain('no ratio (spec §5)')

    root.unmount()
  })

  it('preserves the last-known capability set on the page when a test fails, and reports the failure separately', async () => {
    const lastKnown: DownloadClientOut['capabilities'] = {
      operations: { pause: { support: 'native', note: null } },
      fields: {},
    }
    mockListClientTypes.mockResolvedValue([USENET_TYPE])
    mockListClientInstances.mockResolvedValue([instance({ capabilities: lastKnown })])
    const failedResult: DownloadClientTestResponse = {
      ok: false,
      error_class: 'ClientUnreachable',
      message: 'connection refused',
      version: null,
      capabilities: lastKnown,
      detected_base_paths: [],
      detected_categories: [],
    }
    mockTestClientInstance.mockResolvedValue(failedResult)
    const root = await mount(container)

    expect(container.textContent).toContain('Pause')

    const testButton = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === 'Test',
    )
    expect(testButton).toBeDefined()
    await act(async () => {
      testButton?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })

    // The failure is reported...
    expect(container.textContent).toContain('connection refused')
    // ...and the previously known capability is still rendered, not blanked.
    expect(container.textContent).toContain('Pause')

    root.unmount()
  })

  // Findings #10/#11 (prompts/2026-08-23-category-binding-redesign.md): the category ->
  // queue control is redesigned so there is no free-text field anywhere, and a suggested
  // binding is a pre-selected dropdown value rather than placeholder text.

  it('renders one row per category the client reports, with no free-text input and a pre-selected suggestion', async () => {
    mockListClientTypes.mockResolvedValue([USENET_TYPE])
    const queue = { id: 5, name: 'ar-tv', remote_path: '/data/complete/ar-tv', local_path: '/local/tv' }
    mockListQueues.mockResolvedValue([queue] as never)
    mockListClientInstances.mockResolvedValue([instance()])
    mockTestClientInstance.mockResolvedValue({
      ok: true,
      error_class: null,
      message: 'connected',
      version: '4.0.0',
      capabilities: { operations: {}, fields: {} },
      detected_base_paths: [],
      detected_categories: ['ar-tv'],
    })
    const root = await mount(container)

    const testButton = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === 'Test',
    )
    await act(async () => {
      testButton?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })

    const editButton = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === 'Edit',
    )
    await act(async () => {
      editButton?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })

    // The category renders as plain text -- no input anywhere on the page carries it as a value.
    expect(container.textContent).toContain('ar-tv')
    const inputValues = Array.from(container.querySelectorAll('input')).map((el) => el.value)
    expect(inputValues).not.toContain('ar-tv')

    // The suggested binding (queue name matches the category) is already selected, not blank --
    // saving without touching it is expected to persist that value.
    const selects = Array.from(container.querySelectorAll('select'))
    const categorySelect = selects.find((s) =>
      Array.from(s.options).some((o) => o.textContent?.includes('ar-tv (')),
    )
    expect(categorySelect).toBeDefined()
    expect(categorySelect?.value).toBe(String(queue.id))

    root.unmount()
  })

  it('falls back to a labelled path-arithmetic guess when the client reports no categories', async () => {
    mockListClientTypes.mockResolvedValue([USENET_TYPE])
    mockListClientInstances.mockResolvedValue([
      instance({
        base_paths: [
          { id: 1, path: '/data/complete', kind: 'content', client_path: null, source: 'detected' },
        ],
      }),
    ])
    mockListQueues.mockResolvedValue([
      { id: 7, name: 'tv-queue', remote_path: '/data/complete/ar-tv', local_path: '/local/tv' },
    ] as never)
    mockTestClientInstance.mockResolvedValue({
      ok: true,
      error_class: null,
      message: 'connected',
      version: '4.0.0',
      capabilities: { operations: {}, fields: {} },
      detected_base_paths: [],
      detected_categories: [],
    })
    const root = await mount(container)

    const testButton = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === 'Test',
    )
    await act(async () => {
      testButton?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })

    const editButton = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent === 'Edit',
    )
    await act(async () => {
      editButton?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })

    expect(container.textContent).toContain('guessed from your')
    expect(container.textContent).toContain('ar-tv')

    root.unmount()
  })
})
