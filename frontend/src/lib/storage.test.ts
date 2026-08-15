import { afterEach, describe, expect, it, vi } from 'vitest'
import { readLocalStorage, writeLocalStorage } from './storage'

interface Pref {
  n: number
}

function isPref(value: unknown): value is Pref {
  return typeof value === 'object' && value != null && typeof (value as Record<string, unknown>).n === 'number'
}

describe('readLocalStorage / writeLocalStorage', () => {
  afterEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('round-trips a value written through the same wrapper', () => {
    writeLocalStorage('pref', { n: 42 })
    expect(readLocalStorage('pref', isPref)).toEqual({ n: 42 })
  })

  it('namespaces the key so raw localStorage sees the prefixed form', () => {
    writeLocalStorage('pref', { n: 1 })
    expect(localStorage.getItem('lftpweb.pref')).toBe('{"n":1}')
  })

  it('reads null for a key that was never written', () => {
    expect(readLocalStorage('never-written', isPref)).toBeNull()
  })

  it('reads null for corrupt JSON left over from a previous version of the key', () => {
    localStorage.setItem('lftpweb.pref', 'not json{{{')
    expect(readLocalStorage('pref', isPref)).toBeNull()
  })

  it('reads null for well-formed JSON that fails the type guard (foreign/old schema)', () => {
    localStorage.setItem('lftpweb.pref', JSON.stringify({ wrong: 'shape' }))
    expect(readLocalStorage('pref', isPref)).toBeNull()
  })

  it('reads null rather than throwing when localStorage.getItem itself throws (private browsing)', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError: access denied')
    })
    expect(() => readLocalStorage('pref', isPref)).not.toThrow()
    expect(readLocalStorage('pref', isPref)).toBeNull()
  })

  it('does not throw when localStorage.setItem throws (quota exceeded / storage disabled)', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    expect(() => writeLocalStorage('pref', { n: 1 })).not.toThrow()
  })
})
