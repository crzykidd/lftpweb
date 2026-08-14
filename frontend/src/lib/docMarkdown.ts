// Structural parsing for the Docs section's Markdown files (2026-08-14,
// prompts/2026-08-14-docs-as-markdown-single-source.md). `docs/quick-start.md` and
// `docs/concepts.md` are the single source of the prose that used to live as JSX in
// `QuickStartPage.tsx`/`ConceptsPage.tsx` -- this module is the pure-function half of the
// renderer: everything *structural* (title, lede, the Jump nav, where one section/step ends and
// the next begins, a section's stable anchor id) is plain string parsing here, not markdown
// rendering. `MarkdownDoc.tsx` only ever hands a section's already-isolated `body` string to
// `react-markdown` -- the part that actually needs a real Markdown engine (inline formatting,
// GFM tables, links).
//
// Deliberately not a general Markdown parser: it knows exactly the shape these two files use --
// a `# Title` line, a one-paragraph lede, an optional ```jump fenced block, then `## ` headings
// -- because that shape is something this module also enforces (a malformed doc throws rather
// than silently rendering wrong), not something it needs to tolerate arbitrary input for.

export interface JumpItem {
  id: string
  label: string
}

export interface DocSection {
  /** Stable anchor id. Explicit for a `section` heading (`## Title {#id}`); a step has no
   * anchor target (nothing jumps to it) but still gets one for `key` stability. */
  id: string
  title: string
  /** The section's own Markdown, everything after its heading line up to the next `## ` (or
   * end of document) -- handed to `react-markdown` as-is by the caller. */
  body: string
  variant: 'step' | 'section'
  /** Set only for `variant: 'step'`. */
  stepNumber?: number
}

export interface ParsedDoc {
  title: string
  lede: string
  /** `null` when the doc has no ```jump block (quick-start.md doesn't; concepts.md does). */
  jump: JumpItem[] | null
  sections: DocSection[]
}

const TITLE_RE = /^#\s+(.+?)\s*$/
const HEADING_RE = /^##\s+(.+)$/
const STEP_RE = /^(\d+)\.\s+(.+)$/
const ANCHOR_ID_RE = /^(.+?)\s*\{#([\w-]+)\}\s*$/

function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

export function parseDocSource(source: string): ParsedDoc {
  const lines = source.replace(/\r\n/g, '\n').split('\n')

  let i = 0
  const skipBlank = () => {
    while (i < lines.length && lines[i].trim() === '') i++
  }

  skipBlank()
  const titleMatch = TITLE_RE.exec(lines[i] ?? '')
  if (!titleMatch) {
    throw new Error('parseDocSource: document must open with a "# Title" heading')
  }
  const title = titleMatch[1]
  i++

  skipBlank()
  const ledeLines: string[] = []
  while (i < lines.length && lines[i].trim() !== '') {
    ledeLines.push(lines[i].trim())
    i++
  }
  if (ledeLines.length === 0) {
    throw new Error('parseDocSource: expected a lede paragraph after the title')
  }
  const lede = ledeLines.join(' ')

  skipBlank()

  let jump: JumpItem[] | null = null
  if (lines[i]?.trim() === '```jump') {
    i++
    const items: JumpItem[] = []
    while (i < lines.length && lines[i].trim() !== '```') {
      const line = lines[i].trim()
      if (line !== '') {
        const sep = line.indexOf('|')
        if (sep === -1) {
          throw new Error(`parseDocSource: malformed jump line (want "label|#id"): "${line}"`)
        }
        items.push({
          label: line.slice(0, sep).trim(),
          id: line
            .slice(sep + 1)
            .trim()
            .replace(/^#/, ''),
        })
      }
      i++
    }
    if (i >= lines.length) {
      throw new Error('parseDocSource: unterminated ```jump block')
    }
    i++ // consume the closing fence
    jump = items
  }

  const sections = parseSections(lines.slice(i).join('\n'))
  return { title, lede, jump, sections }
}

function parseSections(text: string): DocSection[] {
  const trimmed = text.trim()
  if (trimmed === '') return []

  return trimmed
    .split(/\n(?=## )/)
    .map((chunk) => chunk.trim())
    .filter(Boolean)
    .map(parseSectionChunk)
}

function parseSectionChunk(chunk: string): DocSection {
  const newlineIndex = chunk.indexOf('\n')
  const rawHeading = newlineIndex === -1 ? chunk : chunk.slice(0, newlineIndex)
  const body = newlineIndex === -1 ? '' : chunk.slice(newlineIndex + 1).trim()

  const headingMatch = HEADING_RE.exec(rawHeading.trim())
  if (!headingMatch) {
    throw new Error(`parseDocSource: expected a "## " heading, got: "${rawHeading}"`)
  }
  const headingText = headingMatch[1].trim()

  const stepMatch = STEP_RE.exec(headingText)
  if (stepMatch) {
    return {
      id: slugify(headingText),
      title: stepMatch[2],
      body,
      variant: 'step',
      stepNumber: Number(stepMatch[1]),
    }
  }

  const anchorMatch = ANCHOR_ID_RE.exec(headingText)
  if (anchorMatch) {
    return { id: anchorMatch[2], title: anchorMatch[1], body, variant: 'section' }
  }

  return { id: slugify(headingText), title: headingText, body, variant: 'section' }
}
