import { describe, expect, it } from 'vitest'
import { filterLogLines, logFilterSummary } from './logFilter'

describe('filterLogLines', () => {
  const lines = [
    '2026-08-17 12:00:00,000 INFO     lftpweb.core.queue: starting job 1',
    '2026-08-17 12:00:01,000 ERROR    lftpweb.core.queue: job 1 failed',
    '2026-08-17 12:00:02,000 INFO     httpx: HTTP Request: GET /api/v3/queue',
  ]

  it('returns every line unchanged when the query is empty', () => {
    expect(filterLogLines(lines, '')).toBe(lines)
  })

  it('returns every line unchanged when the query is only whitespace', () => {
    expect(filterLogLines(lines, '   ')).toBe(lines)
  })

  it('matches a case-insensitive substring', () => {
    expect(filterLogLines(lines, 'FAILED')).toEqual([lines[1]])
  })

  it('matches regardless of where the substring falls in the line', () => {
    expect(filterLogLines(lines, 'httpx')).toEqual([lines[2]])
  })

  it('returns an empty array when nothing matches', () => {
    expect(filterLogLines(lines, 'nope, not present')).toEqual([])
  })

  it('trims surrounding whitespace off the query before matching', () => {
    expect(filterLogLines(lines, '  job 1 failed  ')).toEqual([lines[1]])
  })
})

describe('logFilterSummary', () => {
  it('returns null when the query is empty', () => {
    expect(logFilterSummary(3, 10, '')).toBeNull()
  })

  it('returns null when the query is only whitespace', () => {
    expect(logFilterSummary(3, 10, '   ')).toBeNull()
  })

  it('reports shown vs. total while a filter is active', () => {
    expect(logFilterSummary(3, 10, 'error')).toBe('Showing 3 of 10 lines')
  })

  it('reports a zero-match filter the same way', () => {
    expect(logFilterSummary(0, 10, 'nope')).toBe('Showing 0 of 10 lines')
  })
})
