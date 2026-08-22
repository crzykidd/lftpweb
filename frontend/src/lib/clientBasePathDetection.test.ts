import { describe, expect, it } from 'vitest'
import {
  acceptedPathFor,
  buildAcceptedBasePath,
  isDetectedRowAccepted,
  type BasePathDraft,
} from './clientBasePathDetection'

// spec §8.2 correction (2026-08-22): "re-running detection must not clobber manual rows or a
// translation the user already supplied" -- these are exactly the cases this module exists to
// get right, tested as pure predicates per this repo's settled pattern.

const manualRow: BasePathDraft = {
  path: '/mnt/manual/whatever',
  kind: 'unknown',
  client_path: null,
  source: 'manual',
}

describe('buildAcceptedBasePath', () => {
  it('accepting a verified path unmodified records no translation', () => {
    const draft = buildAcceptedBasePath(
      { client_path: '/downloads/complete', kind: 'content' },
      '/downloads/complete',
    )
    expect(draft).toEqual({
      path: '/downloads/complete',
      kind: 'content',
      client_path: null,
      source: 'detected',
    })
  })

  it('supplying a different SSH-visible path for a not_found row records the translation', () => {
    const draft = buildAcceptedBasePath(
      { client_path: '/complete', kind: 'content' },
      '/home/user/downloads/complete',
    )
    expect(draft).toEqual({
      path: '/home/user/downloads/complete',
      kind: 'content',
      client_path: '/complete',
      source: 'detected',
    })
  })

  it('trims the user-supplied path before comparing and storing', () => {
    const draft = buildAcceptedBasePath({ client_path: '/x', kind: 'working' }, '  /x  ')
    expect(draft.path).toBe('/x')
    expect(draft.client_path).toBeNull() // trimmed value equals client_path -- no translation
  })
})

describe('isDetectedRowAccepted / acceptedPathFor', () => {
  it('is false for a detected row nothing has accepted yet', () => {
    expect(isDetectedRowAccepted({ client_path: '/complete' }, [manualRow])).toBe(false)
    expect(acceptedPathFor({ client_path: '/complete' }, [manualRow])).toBeNull()
  })

  it('a manually-added row at the same path is never mistaken for an accepted detection', () => {
    const rows: BasePathDraft[] = [
      { path: '/complete', kind: 'unknown', client_path: null, source: 'manual' },
    ]
    expect(isDetectedRowAccepted({ client_path: '/complete' }, rows)).toBe(false)
  })

  it('recognizes a verified path already accepted as-is (client_path null)', () => {
    const rows: BasePathDraft[] = [
      { path: '/downloads/complete', kind: 'content', client_path: null, source: 'detected' },
    ]
    expect(isDetectedRowAccepted({ client_path: '/downloads/complete' }, rows)).toBe(true)
    expect(acceptedPathFor({ client_path: '/downloads/complete' }, rows)).toBe(
      '/downloads/complete',
    )
  })

  it('recognizes a not_found path already resolved via a recorded translation', () => {
    const rows: BasePathDraft[] = [
      {
        path: '/home/user/downloads/complete',
        kind: 'content',
        client_path: '/complete',
        source: 'detected',
      },
    ]
    expect(isDetectedRowAccepted({ client_path: '/complete' }, rows)).toBe(true)
    expect(acceptedPathFor({ client_path: '/complete' }, rows)).toBe(
      '/home/user/downloads/complete',
    )
  })

  it('re-running detection does not clobber a manual row or an existing translation', () => {
    // The scenario this whole module exists for: a user has one manual row and one already-
    // resolved detected translation; Test is clicked again and the same server-reported paths
    // come back. Neither existing row is disturbed, and the already-resolved one is recognized
    // rather than re-prompted for.
    const draft: BasePathDraft[] = [
      manualRow,
      {
        path: '/home/user/downloads/complete',
        kind: 'content',
        client_path: '/complete',
        source: 'detected',
      },
    ]
    expect(isDetectedRowAccepted({ client_path: '/complete' }, draft)).toBe(true)
    expect(draft).toContainEqual(manualRow) // untouched
    expect(draft).toHaveLength(2) // nothing duplicated, nothing removed
  })
})
