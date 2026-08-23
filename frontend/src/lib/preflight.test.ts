import { describe, expect, it } from 'vitest'
import {
  isPreflightPageSize,
  preflightChipLabel,
  preflightChipState,
  preflightChipTooltip,
  preflightFillPercent,
  preflightRemainingLabel,
  preflightSizeLabel,
  preflightStatusLabel,
} from './preflight'

describe('preflightSizeLabel', () => {
  it('is null when the source gave neither figure -- never a placeholder', () => {
    expect(preflightSizeLabel({ size_bytes: null, size_remaining_bytes: null })).toBeNull()
  })

  it('is just the total when only a total is known -- the settle-gate follow-up\'s own expected shape', () => {
    expect(preflightSizeLabel({ size_bytes: 1_000_000_000, size_remaining_bytes: null })).toBe('953.7 MB')
  })

  it('is a percent-of-total once both figures are known -- an *arr row still downloading', () => {
    expect(preflightSizeLabel({ size_bytes: 1_000_000, size_remaining_bytes: 250_000 })).toBe(
      '75% of 976.6 KB',
    )
  })

  it('is null for a non-positive total -- defensive, matches every other size helper in this codebase', () => {
    expect(preflightSizeLabel({ size_bytes: 0, size_remaining_bytes: 0 })).toBeNull()
  })
})

describe('preflightStatusLabel', () => {
  it('capitalizes the first letter of the source\'s own free-form text', () => {
    expect(preflightStatusLabel('downloading')).toBe('Downloading')
  })

  it('is null straight through -- a row with nothing to say renders nothing, not a placeholder', () => {
    expect(preflightStatusLabel(null)).toBeNull()
  })

  it('is null for an empty string too', () => {
    expect(preflightStatusLabel('')).toBeNull()
  })
})

describe('preflightRemainingLabel', () => {
  it('is the size alone when there is no remaining-time estimate -- a settle row\'s own shape', () => {
    expect(
      preflightRemainingLabel({ size_bytes: 1_000_000_000, size_remaining_bytes: null, remaining_s: null }),
    ).toBe('953.7 MB')
  })

  it('appends the duration through the same formatEta shape transferLineValue uses for its own ETA', () => {
    expect(
      preflightRemainingLabel({ size_bytes: 1_000_000, size_remaining_bytes: 250_000, remaining_s: 180 }),
    ).toBe('75% of 976.6 KB · 3m left')
  })

  it('is just the duration when the source gave no size at all', () => {
    expect(preflightRemainingLabel({ size_bytes: null, size_remaining_bytes: null, remaining_s: 45 })).toBe(
      '45s left',
    )
  })

  it('is null when the source gave neither figure', () => {
    expect(preflightRemainingLabel({ size_bytes: null, size_remaining_bytes: null, remaining_s: null })).toBeNull()
  })
})

describe('preflightChipLabel', () => {
  it('translates the verified "downloading" trackedDownloadState into lftpweb\'s own perspective', () => {
    expect(preflightChipLabel({ source: 'arr', status_label: 'downloading' })).toBe('Waiting')
  })

  it('translates the verified "importing" trackedDownloadState into its own word', () => {
    expect(preflightChipLabel({ source: 'arr', status_label: 'importing' })).toBe('Importing')
  })

  it('falls through to the source\'s own verbatim wording for an unverified *arr state', () => {
    expect(preflightChipLabel({ source: 'arr', status_label: 'paused' })).toBe('Paused')
  })

  it('is null for a status-less *arr row', () => {
    expect(preflightChipLabel({ source: 'arr', status_label: null })).toBeNull()
  })

  it('leaves a settle row\'s own word untouched -- "Settling" is kept, never renamed', () => {
    expect(preflightChipLabel({ source: 'settle', status_label: 'Settling' })).toBe('Settling')
  })
})

describe('preflightChipTooltip', () => {
  it('names the download client and the reporting instance when both are known', () => {
    expect(
      preflightChipTooltip(
        {
          source: 'arr',
          status_label: 'downloading',
          download_client: 'SABnzbd',
          source_label: 'Sonarr',
          wait_scans: null,
          wait_since: null,
        },
        null,
      ),
    ).toBe('Downloading from "SABnzbd" — reported by Sonarr')
  })

  it('falls back to naming just the instance when there is no download client', () => {
    expect(
      preflightChipTooltip(
        {
          source: 'arr',
          status_label: 'downloading',
          download_client: null,
          source_label: 'Sonarr',
          wait_scans: null,
          wait_since: null,
        },
        null,
      ),
    ).toBe('Reported by Sonarr')
  })

  it('is null for a status-less *arr row', () => {
    expect(
      preflightChipTooltip(
        {
          source: 'arr',
          status_label: null,
          download_client: null,
          source_label: 'Sonarr',
          wait_scans: null,
          wait_since: null,
        },
        null,
      ),
    ).toBeNull()
  })

  it('reuses the shared settle-wait sentence for a settle row -- never null, unlike the *arr branch', () => {
    expect(
      preflightChipTooltip(
        {
          source: 'settle',
          status_label: 'Settling',
          download_client: null,
          source_label: 'TV',
          wait_scans: 1,
          wait_since: '2026-08-21T00:00:00.000000Z',
        },
        { enabled: true, client_skip_enabled: false, required_scans: 2, min_age_s: 60 },
      ),
    ).toMatch(/^Waiting for changes -- 1 of 2 scans, \d+s of 60s$/)
  })

  it('degrades to the bare sentence for a settle row with no progress yet', () => {
    expect(
      preflightChipTooltip(
        {
          source: 'settle',
          status_label: 'Settling',
          download_client: null,
          source_label: 'TV',
          wait_scans: null,
          wait_since: null,
        },
        { enabled: true, client_skip_enabled: false, required_scans: 2, min_age_s: 60 },
      ),
    ).toBe('Waiting for changes')
  })
})

describe('preflightFillPercent -- the "Waiting" chip\'s own bar', () => {
  it('is the done-over-total percent once both figures are known', () => {
    expect(preflightFillPercent({ size_bytes: 1_000_000, size_remaining_bytes: 250_000 })).toBe(75)
  })

  it('is null when the total is absent -- never a placeholder', () => {
    expect(preflightFillPercent({ size_bytes: null, size_remaining_bytes: 100 })).toBeNull()
  })

  it('is null when the remaining figure is absent -- a paused/stalled client item, per the *arr', () => {
    expect(preflightFillPercent({ size_bytes: 1_000_000, size_remaining_bytes: null })).toBeNull()
  })

  it('is null for a non-positive total -- never a divide-by-zero NaN%', () => {
    expect(preflightFillPercent({ size_bytes: 0, size_remaining_bytes: 0 })).toBeNull()
  })

  it('is null when remaining exceeds total -- a stale/inconsistent *arr record, never a negative bar', () => {
    expect(preflightFillPercent({ size_bytes: 1000, size_remaining_bytes: 2000 })).toBeNull()
  })

  it('is null for a negative remaining figure -- nonsensical, not clamped to a fake number', () => {
    expect(preflightFillPercent({ size_bytes: 1000, size_remaining_bytes: -1 })).toBeNull()
  })

  it('is 100 when the remaining figure has reached zero', () => {
    expect(preflightFillPercent({ size_bytes: 1000, size_remaining_bytes: 0 })).toBe(100)
  })
})

describe('preflightChipState -- which StateChip bucket a row\'s chip renders through', () => {
  it('is WAITING for the one *arr status this box translates to "Waiting"', () => {
    expect(preflightChipState({ source: 'arr', status_label: 'downloading' })).toBe('WAITING')
  })

  it('is SETTLING for an *arr row past downloading -- importing has nothing to fill', () => {
    expect(preflightChipState({ source: 'arr', status_label: 'importing' })).toBe('SETTLING')
  })

  it('is SETTLING for an *arr row with an unverified status shown verbatim', () => {
    expect(preflightChipState({ source: 'arr', status_label: 'paused' })).toBe('SETTLING')
  })

  it('is SETTLING for a settle row -- deliberately never fills', () => {
    expect(preflightChipState({ source: 'settle', status_label: 'Settling' })).toBe('SETTLING')
  })
})

describe('isPreflightPageSize', () => {
  it('accepts each of this box\'s own offered sizes -- 5/10/20, not the other boxes\' 10/20/50', () => {
    expect(isPreflightPageSize(5)).toBe(true)
    expect(isPreflightPageSize(10)).toBe(true)
    expect(isPreflightPageSize(20)).toBe(true)
  })

  it('rejects a size only the other two boxes offer -- 50 is not in this box\'s own list', () => {
    expect(isPreflightPageSize(50)).toBe(false)
  })

  it('rejects a hand-edited or out-of-range number', () => {
    expect(isPreflightPageSize(999)).toBe(false)
    expect(isPreflightPageSize(0)).toBe(false)
    expect(isPreflightPageSize(-5)).toBe(false)
  })

  it('rejects a non-number entirely -- a stale/foreign localStorage value', () => {
    expect(isPreflightPageSize('20')).toBe(false)
    expect(isPreflightPageSize('abc')).toBe(false)
    expect(isPreflightPageSize(null)).toBe(false)
    expect(isPreflightPageSize(undefined)).toBe(false)
    expect(isPreflightPageSize({})).toBe(false)
    expect(isPreflightPageSize([5])).toBe(false)
  })
})
