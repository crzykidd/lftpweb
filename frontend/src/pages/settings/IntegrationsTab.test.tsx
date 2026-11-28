import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getArrPollSettings, listArrInstances } from '../../api/client'
import type { ArrInstanceOut } from '../../api/types'
import { IntegrationsTab } from './IntegrationsTab'

// Audit C4: the saved *arr key is only sent to the URL it was saved for. Same createRoot/act
// harness as ClientsTab.test.tsx.

vi.mock('../../api/client', () => ({
  getArrPollSettings: vi.fn(),
  putArrPollSettings: vi.fn(),
  listArrInstances: vi.fn(),
  createArrInstance: vi.fn(),
  updateArrInstance: vi.fn(),
  deleteArrInstance: vi.fn(),
  testArrInstance: vi.fn(),
}))

const INSTANCE: ArrInstanceOut = {
  id: 7,
  name: 'Sonarr',
  kind: 'sonarr',
  base_url: 'http://sonarr.example:8989',
  has_api_key: true,
  enabled: true,
  notify_on_complete: false,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
}

function typeInto(el: HTMLInputElement, value: string) {
  const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
  nativeSetter?.call(el, value)
  el.dispatchEvent(new Event('input', { bubbles: true }))
}

describe('IntegrationsTab secret re-entry', () => {
  let container: HTMLDivElement

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    vi.mocked(getArrPollSettings).mockResolvedValue({ poll_interval_s: 60 })
    vi.mocked(listArrInstances).mockResolvedValue([INSTANCE])
  })

  afterEach(() => {
    document.body.removeChild(container)
    vi.resetAllMocks()
  })

  it('requires the API key after the URL moves, not for a trailing slash or host case', async () => {
    const root = createRoot(container)
    await act(async () => {
      root.render(<IntegrationsTab />)
    })
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    const edit = Array.from(container.querySelectorAll('button')).find((b) => b.textContent === 'Edit')
    await act(async () => {
      edit?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })

    const urlInput = Array.from(container.querySelectorAll('input')).find(
      (i) => i.value === INSTANCE.base_url,
    ) as HTMLInputElement
    const key = () => container.querySelector('input[type="password"]') as HTMLInputElement
    expect(key().getAttribute('aria-required')).toBeNull()

    await act(async () => typeInto(urlInput, 'http://Sonarr.Example:8989/'))
    expect(key().getAttribute('aria-required')).toBeNull()

    await act(async () => typeInto(urlInput, 'http://evil.example'))
    expect(key().getAttribute('aria-required')).toBe('true')
    expect(container.textContent).toContain('The URL changed, so re-enter the API key')

    await act(async () => typeInto(key(), 'fresh'))
    expect(key().getAttribute('aria-required')).toBeNull()

    root.unmount()
  })
})
