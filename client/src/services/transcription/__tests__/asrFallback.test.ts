import { beforeEach, describe, expect, it, vi } from 'vitest'
import { emptyAsrProfile } from '@/features/settings/asrProviderCatalog'
import { ASR_FALLBACK_KEY, loadAsrFallback, parseAsrFallback } from '../asrFallback'

const settings = vi.hoisted(() => new Map<string, unknown>())
vi.mock('../../store', () => ({
  getSetting: vi.fn(async (key: string, fallback: unknown) => settings.get(key) ?? fallback),
}))
vi.mock('../../runtimeConfig', () => ({ getWSUrl: () => 'ws://configured-server/ws' }))

describe('ASR fallback configuration', () => {
  beforeEach(() => settings.clear())

  it('is off by default and does not activate the former single-backup setting', async () => {
    settings.set('recognition.fallbackProfileId', 'old-backup')
    expect(await loadAsrFallback()).toEqual([])
  })

  it('preserves explicit order and repeated targets, ignoring malformed entries', () => {
    expect(
      parseAsrFallback({
        enabled: true,
        steps: [
          { mode: 'local', modelId: 'model' },
          { mode: 'server' },
          { mode: 'cloud_api', profileId: 'a' },
          { mode: 'cloud_api', profileId: 'a' },
          null,
          { mode: 'local' },
          { mode: 'unknown' },
        ],
      }).steps,
    ).toEqual([
      { mode: 'local', modelId: 'model' },
      { mode: 'server' },
      { mode: 'cloud_api', profileId: 'a' },
      { mode: 'cloud_api', profileId: 'a' },
    ])
  })

  it('resolves cloud profiles without changing or excluding the active profile', async () => {
    const profile = { ...emptyAsrProfile('groq'), id: 'active', apiKey: 'test-key' }
    settings.set('cloudAsr.profiles', [profile])
    settings.set('cloudAsr.activeProfileId', profile.id)
    settings.set(ASR_FALLBACK_KEY, {
      enabled: true,
      steps: [
        { mode: 'cloud_api', profileId: profile.id },
        { mode: 'server' },
        { mode: 'cloud_api', profileId: 'deleted' },
        { mode: 'local', modelId: 'model' },
      ],
    })
    const result = await loadAsrFallback()
    expect(result.map((item) => item.mode)).toEqual(['cloud_api', 'server', 'local'])
    expect(result[1]).toEqual({ mode: 'server', url: 'ws://configured-server/ws', aiSource: 'managed' })
    expect(settings.get('cloudAsr.activeProfileId')).toBe('active')
  })

  it('keeps a saved chain inactive while switched off', async () => {
    settings.set(ASR_FALLBACK_KEY, { enabled: false, steps: [{ mode: 'server' }] })
    expect(await loadAsrFallback()).toEqual([])
  })
})
