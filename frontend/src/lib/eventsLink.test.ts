import { describe, expect, it } from 'vitest'
import { itemEventsHref, parseItemEventsFilter } from './eventsLink'

describe('itemEventsHref', () => {
  it('builds an /events path carrying item_id and a display label', () => {
    expect(itemEventsHref(42, 'Show.S01E01.mkv')).toBe('/events?item_id=42&item=Show.S01E01.mkv')
  })

  it('URL-encodes a label with special characters', () => {
    const href = itemEventsHref(7, 'a & b/c')
    const [, query] = href.split('?')
    const params = new URLSearchParams(query)
    expect(params.get('item_id')).toBe('7')
    expect(params.get('item')).toBe('a & b/c')
  })
})

describe('parseItemEventsFilter', () => {
  it('round-trips through itemEventsHref', () => {
    const href = itemEventsHref(42, 'Show.S01E01.mkv')
    const [, query] = href.split('?')
    expect(parseItemEventsFilter(query)).toEqual({ itemId: 42, itemLabel: 'Show.S01E01.mkv' })
  })

  it('accepts a URLSearchParams instance directly, not just a string', () => {
    const params = new URLSearchParams('item_id=9&item=movie')
    expect(parseItemEventsFilter(params)).toEqual({ itemId: 9, itemLabel: 'movie' })
  })

  it('reports no filter when item_id is absent', () => {
    expect(parseItemEventsFilter('')).toEqual({ itemId: null, itemLabel: null })
    expect(parseItemEventsFilter('queue_id=3')).toEqual({ itemId: null, itemLabel: null })
  })

  it('degrades to no filter on a malformed item_id rather than crashing', () => {
    expect(parseItemEventsFilter('item_id=abc')).toEqual({ itemId: null, itemLabel: null })
    expect(parseItemEventsFilter('item_id=-1')).toEqual({ itemId: null, itemLabel: null })
    expect(parseItemEventsFilter('item_id=')).toEqual({ itemId: null, itemLabel: null })
  })

  it('never surfaces a label without a valid item_id behind it', () => {
    expect(parseItemEventsFilter('item=orphaned+label')).toEqual({ itemId: null, itemLabel: null })
  })

  it('itemLabel is null when the label param is simply absent', () => {
    expect(parseItemEventsFilter('item_id=5')).toEqual({ itemId: 5, itemLabel: null })
  })
})
