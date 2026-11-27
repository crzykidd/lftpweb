import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  REQUIRED_HEADER,
  REQUIRED_HEADER_VALUE,
  backupNow,
  getHealth,
  putHost,
  requestHeaders,
  setCsrfToken,
} from './client'
import type { HostIn } from './types'

// Audit batch C3: the backend 403s any mutating API request without REQUIRED_HEADER, so the
// one fetch wrapper must attach it to everything it sends.

function headersOf(call: unknown[]): Record<string, string> {
  const init = (call[1] ?? {}) as RequestInit
  return (init.headers ?? {}) as Record<string, string>
}

describe('api client headers', () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    fetchMock.mockResolvedValue(new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    fetchMock.mockReset()
    setCsrfToken(null)
  })

  it('sends the required header on a bodiless POST (the audit F2 shape)', async () => {
    await backupNow()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(headersOf(fetchMock.mock.calls[0])[REQUIRED_HEADER]).toBe(REQUIRED_HEADER_VALUE)
  })

  it('sends it on a JSON PUT alongside Content-Type', async () => {
    await putHost({} as HostIn)
    const headers = headersOf(fetchMock.mock.calls[0])
    expect(headers[REQUIRED_HEADER]).toBe(REQUIRED_HEADER_VALUE)
    expect(headers['Content-Type']).toBe('application/json')
  })

  it('sends it on reads too', async () => {
    await getHealth()
    expect(headersOf(fetchMock.mock.calls[0])[REQUIRED_HEADER]).toBe(REQUIRED_HEADER_VALUE)
  })

  it('adds the CSRF token on mutating calls only, and keeps the required header', () => {
    setCsrfToken('tok')
    expect(requestHeaders('POST', false)).toEqual({
      [REQUIRED_HEADER]: REQUIRED_HEADER_VALUE,
      'X-CSRF-Token': 'tok',
    })
    expect(requestHeaders('GET', false)).toEqual({ [REQUIRED_HEADER]: REQUIRED_HEADER_VALUE })
  })
})
