import { describe, expect, it } from 'vitest'
import { parseFallbackSettings } from '../fallbackSettings'

const parse = (raw: unknown) => parseFallbackSettings(raw, (step) => (typeof step === 'string' ? step : null))

describe('fallback settings', () => {
  it('defaults to off for absent or malformed settings', () => {
    for (const raw of [null, undefined, '', 42, {}, { enabled: 'true' }]) {
      expect(parse(raw)).toEqual({ enabled: false, steps: [] })
    }
  })
  it('preserves order, repeated steps, and settings while switched off', () => {
    expect(parse({ enabled: false, steps: ['a', null, 'b', 'a'] })).toEqual({ enabled: false, steps: ['a', 'b', 'a'] })
  })
  it('accepts explicit opt-in only', () => {
    expect(parse({ enabled: true, steps: ['a'] })).toEqual({ enabled: true, steps: ['a'] })
  })
})
