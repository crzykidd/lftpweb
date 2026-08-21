import { describe, expect, it } from 'vitest'
import {
  preflightChipLabel,
  preflightChipTooltip,
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
    expect(preflightChipLabel({ source: 'arr', status_label: 'downloading' })).toBe('Waiting for download')
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
      preflightChipTooltip({
        source: 'arr',
        status_label: 'downloading',
        download_client: 'SABnzbd',
        source_label: 'Sonarr',
      }),
    ).toBe('Downloading from "SABnzbd" — reported by Sonarr')
  })

  it('falls back to naming just the instance when there is no download client', () => {
    expect(
      preflightChipTooltip({
        source: 'arr',
        status_label: 'downloading',
        download_client: null,
        source_label: 'Sonarr',
      }),
    ).toBe('Reported by Sonarr')
  })

  it('is null for a settle row -- nothing *arr to attribute', () => {
    expect(
      preflightChipTooltip({
        source: 'settle',
        status_label: 'Settling',
        download_client: null,
        source_label: 'TV',
      }),
    ).toBeNull()
  })

  it('is null for a status-less *arr row', () => {
    expect(
      preflightChipTooltip({ source: 'arr', status_label: null, download_client: null, source_label: 'Sonarr' }),
    ).toBeNull()
  })
})
