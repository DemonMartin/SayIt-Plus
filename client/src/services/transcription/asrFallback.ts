import { describeAsrMissing, parseAsrProfiles } from '@/features/settings/asrProviderCatalog'
import { parseFallbackSettings, type FallbackSettings } from '../fallbackSettings'
import { getSetting } from '../store'
import type { AsrTarget, ResolvedAsrTarget } from './asrTarget'
import { SERVER_AI_SOURCE_KEY } from './serverAiSource'
import { getWSUrl } from '../runtimeConfig'

export const ASR_FALLBACK_KEY = 'asr.fallback'

export type AsrFallbackSettings = FallbackSettings<AsrTarget>

export function parseAsrFallback(raw: unknown): AsrFallbackSettings {
  return parseFallbackSettings(raw, (value): AsrTarget | null => {
    if (!value || typeof value !== 'object') return null
    const step = value as Record<string, unknown>
    if (step.mode === 'server') return { mode: 'server' }
    if (step.mode === 'local' && typeof step.modelId === 'string' && step.modelId.trim()) {
      return { mode: 'local', modelId: step.modelId }
    }
    if (step.mode === 'cloud_api' && typeof step.profileId === 'string' && step.profileId.trim()) {
      return { mode: 'cloud_api', profileId: step.profileId }
    }
    return null
  })
}

/** Freeze the ordered targets at recording start without changing primary configuration. */
export async function loadAsrFallback(): Promise<ResolvedAsrTarget[]> {
  const settings = parseAsrFallback(await getSetting<unknown>(ASR_FALLBACK_KEY, null))
  if (!settings.enabled || !settings.steps.length) return []
  const profiles = parseAsrProfiles(await getSetting<unknown>('cloudAsr.profiles', []))
  const serverAiSource = await getSetting(SERVER_AI_SOURCE_KEY, 'managed')
  return settings.steps.flatMap((target): ResolvedAsrTarget[] => {
    if (target.mode === 'server')
      return [{ ...target, url: getWSUrl(), aiSource: serverAiSource === 'custom' ? 'custom' : 'managed' }]
    if (target.mode === 'local') return [target]
    const profile = profiles.find((item) => item.id === target.profileId)
    return profile && !describeAsrMissing(profile) ? [{ mode: 'cloud_api', profile }] : []
  })
}
