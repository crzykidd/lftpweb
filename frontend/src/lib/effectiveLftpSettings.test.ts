import { describe, expect, it } from 'vitest'
import {
  findLftpSettingCollisions,
  parseExtraLftpSettings,
  type EffectiveLftpJobKind,
} from './effectiveLftpSettings'

describe('parseExtraLftpSettings', () => {
  it('extracts key and value from a well-formed set line', () => {
    expect(parseExtraLftpSettings('set net:socket-buffer 262144;')).toEqual([
      { key: 'net:socket-buffer', value: '262144', raw: 'set net:socket-buffer 262144;' },
    ])
  })

  it('trims a trailing semicolon and surrounding whitespace from the value', () => {
    expect(parseExtraLftpSettings('  set sftp:max-packets-in-flight   16  ;  ')).toEqual([
      {
        key: 'sftp:max-packets-in-flight',
        value: '16',
        raw: 'set sftp:max-packets-in-flight   16  ;',
      },
    ])
  })

  it('handles multiple lines, ignoring blanks and non-set commands', () => {
    const text = [
      'set net:socket-buffer 262144;',
      '',
      'debug 3',
      'set mirror:parallel-directories yes;',
    ].join('\n')
    expect(parseExtraLftpSettings(text).map((l) => l.key)).toEqual([
      'net:socket-buffer',
      'mirror:parallel-directories',
    ])
  })

  it('is case-insensitive on the leading "set" keyword, matching lftp itself', () => {
    expect(parseExtraLftpSettings('SET net:timeout 10s;')[0].key).toBe('net:timeout')
  })

  it('ignores a bare "set key" with no value (that unsets, it does not collide)', () => {
    expect(parseExtraLftpSettings('set net:timeout')).toEqual([])
  })

  it('preserves a quoted value containing spaces, untouched', () => {
    expect(parseExtraLftpSettings('set sftp:connect-program "ssh -a -x";')[0].value).toBe(
      '"ssh -a -x"',
    )
  })

  it('keeps a value with an internal semicolon-looking sequence rather than truncating early', () => {
    // Only a semicolon at the very end of the line is stripped.
    expect(parseExtraLftpSettings('set some:key a;b;')[0].value).toBe('a;b')
  })
})

function kinds(): EffectiveLftpJobKind[] {
  return [
    {
      kind: 'mirror',
      argv: "mirror -c --parallel=4 --use-pget-n=4 '<remote>' '<local>/'",
      argv_why: 'why mirror',
      rc_settings: [
        { key: 'pget:min-chunk-size', value: '1048576', why: 'w', configurable: false },
        { key: 'mirror:use-pget-n', value: '4', why: 'w', configurable: true },
        { key: 'pget:default-n', value: '4', why: 'w', configurable: true },
      ],
    },
    {
      kind: 'pget',
      argv: "pget -c -n 6 '<remote>' -o '<local>'",
      argv_why: 'why pget',
      rc_settings: [
        { key: 'pget:min-chunk-size', value: '1048576', why: 'w', configurable: false },
        { key: 'mirror:use-pget-n', value: '6', why: 'w', configurable: true },
        { key: 'pget:default-n', value: '6', why: 'w', configurable: true },
      ],
    },
  ]
}

describe('findLftpSettingCollisions', () => {
  it('returns nothing when the box is empty', () => {
    expect(findLftpSettingCollisions('', kinds())).toEqual([])
  })

  it('returns nothing when no user-set key matches an lftpweb key', () => {
    expect(findLftpSettingCollisions('set net:socket-buffer 262144;', kinds())).toEqual([])
  })

  it('flags a key lftpweb always sets, regardless of job kind', () => {
    const collisions = findLftpSettingCollisions('set pget:min-chunk-size 999999;', kinds())
    expect(collisions).toHaveLength(1)
    expect(collisions[0]).toMatchObject({
      key: 'pget:min-chunk-size',
      userValue: '999999',
      userLine: 'set pget:min-chunk-size 999999;',
    })
    expect(collisions[0].lftpwebOccurrences).toEqual([
      { kind: 'mirror', value: '1048576' },
      { kind: 'pget', value: '1048576' },
    ])
  })

  it('reports both kinds when a key differs in value between mirror and pget', () => {
    const collisions = findLftpSettingCollisions('set mirror:use-pget-n 8;', kinds())
    expect(collisions).toHaveLength(1)
    expect(collisions[0].lftpwebOccurrences).toEqual([
      { kind: 'mirror', value: '4' },
      { kind: 'pget', value: '6' },
    ])
  })

  it('returns one collision per matching line when the box repeats a key', () => {
    const text = 'set pget:min-chunk-size 111;\nset pget:min-chunk-size 222;'
    const collisions = findLftpSettingCollisions(text, kinds())
    expect(collisions.map((c) => c.userValue)).toEqual(['111', '222'])
  })

  it('matches keys case-sensitively, same as lftp itself', () => {
    expect(findLftpSettingCollisions('set PGET:MIN-CHUNK-SIZE 1;', kinds())).toEqual([])
  })
})
