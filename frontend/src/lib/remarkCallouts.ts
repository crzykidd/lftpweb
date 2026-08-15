// A small remark (mdast) transform backing the Docs Markdown renderer's Warn/Note convention
// (2026-08-14, prompts/2026-08-14-docs-as-markdown-single-source.md). `prose.tsx`'s old `Warn`
// and `Note` components rendered a plain coloured box with no visible label -- the box's colour
// *was* the signal. The Markdown source marks the same two cases with a leading bold marker on
// a blockquote's first line (`> **Warning:** ...` / `> **Note:** ...`), and this plugin turns
// that into a `<div data-callout="warn"|"note">` (the marker text itself is stripped, never
// rendered) that `MarkdownDoc.tsx`'s `div` component override maps back onto `Warn`/`Note`.
//
// A hand-rolled AST walk rather than `unist-util-visit`: blockquotes only ever appear as direct
// children of the tree or of a heading-delimited section body in this doc set, so a shallow
// recursive walk over `.children` is the whole job and pulling in a traversal helper for it
// would be dead weight.

interface MdastNode {
  type: string
  children?: MdastNode[]
  value?: string
  data?: { hName?: string; hProperties?: Record<string, string> }
}

const MARKER = /^(Warning|Note):?$/i

function calloutKind(marker: string): 'warn' | 'note' | null {
  const match = MARKER.exec(marker.trim())
  if (!match) return null
  return match[1].toLowerCase() === 'warning' ? 'warn' : 'note'
}

function transformBlockquote(node: MdastNode): void {
  const firstParagraph = node.children?.[0]
  if (!firstParagraph || firstParagraph.type !== 'paragraph') return

  const firstChild = firstParagraph.children?.[0]
  if (!firstChild || firstChild.type !== 'strong') return

  const markerText = firstChild.children?.[0]
  if (!markerText || markerText.type !== 'text' || typeof markerText.value !== 'string') return

  const kind = calloutKind(markerText.value)
  if (!kind) return

  // Drop the marker (`**Warning:**` / `**Note:**`) and any leading whitespace it left behind --
  // the callout's colour is the label; the word itself was only ever a source-level signal.
  firstParagraph.children = (firstParagraph.children ?? []).slice(1)
  const next = firstParagraph.children[0]
  if (next && next.type === 'text' && typeof next.value === 'string') {
    next.value = next.value.replace(/^\s+/, '')
  }

  node.data = { ...node.data, hName: 'div', hProperties: { 'data-callout': kind } }
}

function walk(node: MdastNode): void {
  if (node.type === 'blockquote') transformBlockquote(node)
  for (const child of node.children ?? []) walk(child)
}

/** A unified/remark plugin: `remarkPlugins={[remarkGfm, remarkCallouts]}`. */
export function remarkCallouts() {
  return (tree: MdastNode) => {
    walk(tree)
  }
}
