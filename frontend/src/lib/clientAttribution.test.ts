import { describe, expect, it } from 'vitest'
import { describePathAttributionRelevance } from './clientAttribution'

// Part 3 of prompts/2026-08-23-category-tristate-and-exclusion.md: "derive the per-client 'do
// you even need this control' copy from OBSERVED attribution counts ... never from client_type."
// The exact wording examples from the prompt, asserted verbatim, plus the fail-safe "not yet
// observed" case and the mixed-result case in between.

describe('describePathAttributionRelevance', () => {
  it('reads "not yet observed" when neither count has ever been recorded', () => {
    const text = describePathAttributionRelevance(null, null)
    expect(text).toContain('Not yet observed')
  })

  it('treats a null sample size and a null matched count independently as "not yet observed"', () => {
    expect(describePathAttributionRelevance(5, null)).toContain('Not yet observed')
    expect(describePathAttributionRelevance(null, 0)).toContain('Not yet observed')
  })

  it('treats a zero sample size as "not yet observed" too, defensively', () => {
    // The backend never actually persists this (a quiet pass leaves the prior reading alone),
    // but this function must not divide-by-zero or render a broken sentence if it ever did.
    expect(describePathAttributionRelevance(0, 0)).toContain('Not yet observed')
  })

  it('the SABnzbd-shaped example: everything matched by folder, no mapping needed', () => {
    const text = describePathAttributionRelevance(12, 12)
    expect(text).toBe(
      '12 of 12 recent downloads matched by folder — no mapping needed unless a category lands ' +
        'outside a queue folder.',
    )
  })

  it('the rTorrent-shaped example: nothing matched by folder, mapping required', () => {
    const text = describePathAttributionRelevance(2, 0)
    expect(text).toBe(
      "0 of 2 recent downloads matched by folder — this client's downloads are matched by " +
        'category, so a mapping is required.',
    )
  })

  it('a partial match names the remainder as still needing a mapping', () => {
    const text = describePathAttributionRelevance(10, 4)
    expect(text).toBe(
      '4 of 10 recent downloads matched by folder — the rest need a category → queue mapping.',
    )
  })

  it('uses the singular "download" for a sample size of exactly one', () => {
    expect(describePathAttributionRelevance(1, 1)).toContain('1 of 1 recent download matched')
  })

  it('never branches on client_type -- no such parameter exists in its signature', () => {
    // The load-bearing assertion this task's own instruction asked for: the function's only
    // inputs are the two observed counts. Calling it with the same two numbers must always
    // produce the same sentence, whatever connector "reported" them.
    expect(describePathAttributionRelevance(12, 12)).toBe(describePathAttributionRelevance(12, 12))
    expect(describePathAttributionRelevance.length).toBe(2)
  })
})
