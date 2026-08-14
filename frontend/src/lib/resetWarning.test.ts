import { describe, expect, it } from 'vitest'
import { resetWarningLines, type ResetQueueContext } from './resetWarning'

const ALWAYS_TRUE_LINES = [
  'Local files are not deleted -- this only resets tracking, not your data.',
  "Transfer history for these items goes too: their job records are deleted outright, and any " +
    'audit-log entries about them stay in History but lose the link back to them.',
]

function ctx(overrides: Partial<ResetQueueContext> = {}): ResetQueueContext {
  return { syncMode: 'copy', autoQueueEnabled: false, scanIntervalS: null, ...overrides }
}

describe('resetWarningLines', () => {
  it('always appends the two always-true consequence lines, regardless of counts', () => {
    const lines = resetWarningLines(3, 3, ctx())
    expect(lines.slice(1)).toEqual(ALWAYS_TRUE_LINES)
  })

  describe('remote count zero', () => {
    it('reads as plural for more than one item', () => {
      expect(resetWarningLines(3, 0, ctx())[0]).toBe(
        'None of these 3 items still exist on the seedbox, so nothing will be re-downloaded.',
      )
    })

    it('reads as singular for exactly one item', () => {
      expect(resetWarningLines(1, 0, ctx())[0]).toBe(
        'This item no longer exists on the seedbox, so it will not be re-downloaded.',
      )
    })
  })

  describe('auto-queue off', () => {
    it('says nothing re-downloads automatically, but a manual queue or turning it on would', () => {
      const line = resetWarningLines(2, 2, ctx({ autoQueueEnabled: false }))[0]
      expect(line).toContain('Auto-queue is off for this queue, so nothing')
      expect(line).toContain('queueing them manually, or turning auto-queue on, will fetch them again')
    })

    it('uses the singular pronoun for exactly one remaining item', () => {
      const line = resetWarningLines(1, 1, ctx({ autoQueueEnabled: false }))[0]
      expect(line).toContain('queueing it manually')
    })
  })

  describe('auto-queue on', () => {
    it('states it will start downloading again within the scan interval', () => {
      const line = resetWarningLines(2, 2, ctx({ autoQueueEnabled: true, scanIntervalS: 45 }))[0]
      expect(line).toContain('auto-queue is on for this queue, so they will start downloading again')
      expect(line).toContain("within about 45s (this queue's scan interval)")
    })

    it('defaults the scan-interval phrasing to the site default (30s) when null', () => {
      const line = resetWarningLines(2, 2, ctx({ autoQueueEnabled: true, scanIntervalS: null }))[0]
      expect(line).toContain('within about 30s')
    })

    it('names on-demand-only scanning (no timer) when scanIntervalS is 0', () => {
      const line = resetWarningLines(2, 2, ctx({ autoQueueEnabled: true, scanIntervalS: 0 }))[0]
      expect(line).toContain('the next time this queue is scanned (on-demand only, no timer)')
    })

    it('uses the singular pronoun and verb for exactly one remaining item', () => {
      const line = resetWarningLines(1, 1, ctx({ autoQueueEnabled: true, scanIntervalS: 30 }))[0]
      expect(line).toContain('it will start downloading again')
    })
  })

  describe('move vs. copy sync mode', () => {
    it('adds the move-queue caveat about remote copies already being removed', () => {
      const line = resetWarningLines(2, 2, ctx({ syncMode: 'move' }))[0]
      expect(line).toContain('a move queue -- most completed items already had their remote copy removed')
    })

    it('adds no caveat for a copy queue', () => {
      const line = resetWarningLines(2, 2, ctx({ syncMode: 'copy' }))[0]
      expect(line).not.toContain('move queue')
    })

    it('adds no caveat for a sync queue either -- the note is move-specific', () => {
      const line = resetWarningLines(2, 2, ctx({ syncMode: 'sync' }))[0]
      expect(line).not.toContain('move queue')
    })
  })

  describe('partial remote survival', () => {
    it('names the exact subset still on the seedbox when it is neither all nor none', () => {
      const line = resetWarningLines(5, 2, ctx())[0]
      expect(line).toContain('2 of these 5 items still exist')
    })

    it('says "All N of these items" when every target still exists remotely, plural', () => {
      const line = resetWarningLines(4, 4, ctx())[0]
      expect(line).toContain('All 4 of these items still exist')
    })

    it('says "This item still exists" for a single fully-surviving item', () => {
      const line = resetWarningLines(1, 1, ctx())[0]
      expect(line).toContain('This item still exists')
    })
  })
})
