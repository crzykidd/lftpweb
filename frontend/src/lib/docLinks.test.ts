import { describe, expect, it } from 'vitest'
import { classifyLink } from './docLinks'

describe('classifyLink', () => {
  it('classifies an app route as internal', () => {
    expect(classifyLink('/settings/queues')).toBe('internal')
    expect(classifyLink('/docs/concepts')).toBe('internal')
    expect(classifyLink('/')).toBe('internal')
  })

  it('classifies a same-page fragment as anchor', () => {
    expect(classifyLink('#settle')).toBe('anchor')
    expect(classifyLink('#blast-radius')).toBe('anchor')
  })

  it('classifies everything else as external', () => {
    expect(classifyLink('https://example.com')).toBe('external')
    expect(classifyLink('http://example.com/x')).toBe('external')
    expect(classifyLink('mailto:someone@example.com')).toBe('external')
    expect(classifyLink('example.com')).toBe('external')
  })
})
