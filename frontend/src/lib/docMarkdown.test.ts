import { describe, expect, it } from 'vitest'
import conceptsSource from '../../../docs/concepts.md?raw'
import quickStartSource from '../../../docs/quick-start.md?raw'
import { parseDocSource } from './docMarkdown'

const SAMPLE = `# Sample title

The lede paragraph, one line.

\`\`\`jump
First symptom|#first
Second symptom|#second
\`\`\`

## 1. First step

Step one body.

## Why it breaks {#first}

Section one body.

## Plain heading with no explicit id

Section two body.
`

describe('parseDocSource', () => {
  it('extracts the title and the lede', () => {
    const doc = parseDocSource(SAMPLE)
    expect(doc.title).toBe('Sample title')
    expect(doc.lede).toBe('The lede paragraph, one line.')
  })

  it('parses a jump block into ordered {label, id} pairs', () => {
    const doc = parseDocSource(SAMPLE)
    expect(doc.jump).toEqual([
      { label: 'First symptom', id: 'first' },
      { label: 'Second symptom', id: 'second' },
    ])
  })

  it('returns null jump when the document has no ```jump block', () => {
    const doc = parseDocSource(`# T\n\nlede\n\n## 1. Step\n\nbody\n`)
    expect(doc.jump).toBeNull()
  })

  it('reads a numbered heading as a step, with the number stripped from the title', () => {
    const doc = parseDocSource(SAMPLE)
    expect(doc.sections[0]).toMatchObject({ variant: 'step', stepNumber: 1, title: 'First step' })
  })

  it('reads a heading with a trailing {#id} as a section using that explicit id', () => {
    const doc = parseDocSource(SAMPLE)
    expect(doc.sections[1]).toMatchObject({
      variant: 'section',
      id: 'first',
      title: 'Why it breaks',
    })
  })

  it('falls back to a slugified id when a section heading has no explicit {#id}', () => {
    const doc = parseDocSource(SAMPLE)
    expect(doc.sections[2]).toMatchObject({
      variant: 'section',
      id: 'plain-heading-with-no-explicit-id',
      title: 'Plain heading with no explicit id',
    })
  })

  it('gives each section the right body text, trimmed', () => {
    const doc = parseDocSource(SAMPLE)
    expect(doc.sections[0].body).toBe('Step one body.')
    expect(doc.sections[1].body).toBe('Section one body.')
  })

  it('throws on a document missing the "# Title" heading', () => {
    expect(() => parseDocSource('not a title\n\nlede\n')).toThrow(/Title/)
  })

  it('throws on a malformed jump line', () => {
    const bad = '# T\n\nlede\n\n```jump\nno separator here\n```\n\n## 1. Step\n\nbody\n'
    expect(() => parseDocSource(bad)).toThrow(/jump/)
  })

  describe('against the real docs shipped in docs/', () => {
    it('parses quick-start.md into six numbered steps with no jump block', () => {
      const doc = parseDocSource(quickStartSource)
      expect(doc.title).toBe('Quick start')
      expect(doc.jump).toBeNull()
      expect(doc.sections).toHaveLength(6)
      expect(doc.sections.every((s) => s.variant === 'step')).toBe(true)
      expect(doc.sections.map((s) => s.stepNumber)).toEqual([1, 2, 3, 4, 5, 6])
    })

    it('parses concepts.md into thirteen anchored sections matching its jump block', () => {
      const doc = parseDocSource(conceptsSource)
      expect(doc.title).toBe('Concepts')
      expect(doc.sections).toHaveLength(13)
      expect(doc.sections.every((s) => s.variant === 'section')).toBe(true)
      const sectionIds = doc.sections.map((s) => s.id)
      const jumpIds = (doc.jump ?? []).map((j) => j.id)
      expect(sectionIds).toEqual(jumpIds)
      expect(sectionIds).toEqual([
        'pause',
        'pipeline',
        'manual-outcome',
        'settle',
        'removal-grace',
        'suppression',
        'blast-radius',
        'icons',
        'copy-move',
        'inherit',
        'arr-integration',
        'preflight',
        'support-bundle',
      ])
    })
  })
})
