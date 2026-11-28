import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getAuthSettings, listApiKeys, putAuthSettings } from '../../api/client'
import type { AuthSettingsOut } from '../../api/types'
import { AuthContext } from '../../hooks/authContext'
import { AuthTab } from './AuthTab'

// Audit C5 (F14): leaving password mode needs the current password, and the form must show the
// field and send it. Same `createRoot`/`act` harness as ClientsTab.test.tsx.

vi.mock('../../api/client', () => ({
  getAuthSettings: vi.fn(),
  putAuthSettings: vi.fn(),
  listApiKeys: vi.fn(),
  createApiKey: vi.fn(),
  deleteApiKey: vi.fn(),
  changePassword: vi.fn(),
}))

const mockGet = vi.mocked(getAuthSettings)
const mockPut = vi.mocked(putAuthSettings)
const mockKeys = vi.mocked(listApiKeys)

function settings(overrides: Partial<AuthSettingsOut> = {}): AuthSettingsOut {
  return {
    mode: 'password',
    proxy_header: 'Remote-User',
    proxy_trusted_cidrs: [],
    has_user: true,
    username: 'admin',
    ...overrides,
  }
}

function typeInto(el: HTMLInputElement, value: string) {
  const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
  nativeSetter?.call(el, value)
  el.dispatchEvent(new Event('input', { bubbles: true }))
}

async function mount(container: HTMLDivElement) {
  const root = createRoot(container)
  await act(async () => {
    root.render(
      <AuthContext.Provider
        value={{ session: null, login: vi.fn(), logout: vi.fn(), refresh: vi.fn() }}
      >
        <AuthTab />
      </AuthContext.Provider>,
    )
  })
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
  return root
}

function modeRadio(container: HTMLElement, index: number) {
  return container.querySelectorAll('input[type="radio"]')[index] as HTMLInputElement
}

function saveButton(container: HTMLElement) {
  return Array.from(container.querySelectorAll('button')).find(
    (b) => b.textContent === 'Save',
  ) as HTMLButtonElement
}

const currentPasswordFields = (c: HTMLElement) =>
  Array.from(c.querySelectorAll('label')).filter((l) =>
    l.textContent?.startsWith('Current password'),
  )

describe('AuthTab current password on leaving password mode', () => {
  let container: HTMLDivElement

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    mockKeys.mockResolvedValue([])
  })

  afterEach(() => {
    document.body.removeChild(container)
    vi.resetAllMocks()
  })

  it('shows the field only when switching away from password mode, and sends it', async () => {
    mockGet.mockResolvedValue(settings())
    mockPut.mockResolvedValue(settings({ mode: 'none' }))
    const root = await mount(container)

    // Stays in password mode: only the separate "Change password" card has such a field.
    expect(currentPasswordFields(container)).toHaveLength(1)

    await act(async () => {
      modeRadio(container, 0).click() // "None"
    })
    const fields = currentPasswordFields(container)
    expect(fields).toHaveLength(2)
    const input = fields[0].querySelector('input') as HTMLInputElement
    await act(async () => {
      typeInto(input, 'hunter2')
    })
    await act(async () => {
      saveButton(container).click()
    })

    expect(mockPut).toHaveBeenCalledTimes(1)
    expect(mockPut.mock.calls[0][0]).toMatchObject({ mode: 'none', current_password: 'hunter2' })
    root.unmount()
  })

  it('does not send it when the mode is not being left, and shows the server error inline', async () => {
    mockGet.mockResolvedValue(settings({ mode: 'none' }))
    mockPut.mockRejectedValue(new Error('current password is incorrect'))
    const root = await mount(container)

    expect(currentPasswordFields(container)).toHaveLength(1) // only the change-password card
    await act(async () => {
      modeRadio(container, 1).click() // "Password"
    })
    await act(async () => {
      saveButton(container).click()
    })
    expect(mockPut.mock.calls[0][0].current_password).toBeUndefined()
    expect(container.textContent).toContain('current password is incorrect')
    root.unmount()
  })
})
