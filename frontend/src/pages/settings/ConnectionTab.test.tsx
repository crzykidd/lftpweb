import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getHost } from '../../api/client'
import type { HostOut } from '../../api/types'
import { ConnectionTab } from './ConnectionTab'

// Audit C4: the saved seedbox password is only sent to the address/port/username it was saved
// for. Same createRoot/act harness as ClientsTab.test.tsx.

vi.mock('../../api/client', () => ({
  getHost: vi.fn(),
  putHost: vi.fn(),
  testHost: vi.fn(),
}))

const HOST: HostOut = {
  id: 1,
  name: 'seedbox',
  address: 'box.example',
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

function typeInto(el: HTMLInputElement, value: string) {
  const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
  nativeSetter?.call(el, value)
  el.dispatchEvent(new Event('input', { bubbles: true }))
}

describe('ConnectionTab secret re-entry', () => {
  let container: HTMLDivElement

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    vi.mocked(getHost).mockResolvedValue(HOST)
  })

  afterEach(() => {
    document.body.removeChild(container)
    vi.resetAllMocks()
  })

  it('requires the password after the address moves, not for host case', async () => {
    const root = createRoot(container)
    await act(async () => {
      root.render(<ConnectionTab />)
    })
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    const address = Array.from(container.querySelectorAll('input')).find(
      (i) => i.value === 'box.example',
    ) as HTMLInputElement
    const password = () => container.querySelector('input[type="password"]') as HTMLInputElement
    expect(password().getAttribute('aria-required')).toBeNull()

    await act(async () => typeInto(address, ' BOX.example '))
    expect(password().getAttribute('aria-required')).toBeNull()

    await act(async () => typeInto(address, 'evil.example'))
    expect(password().getAttribute('aria-required')).toBe('true')
    expect(container.textContent).toContain('so re-enter the password')

    await act(async () => typeInto(password(), 'fresh'))
    expect(password().getAttribute('aria-required')).toBeNull()

    root.unmount()
  })
})
