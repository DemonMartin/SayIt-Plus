import { parseProfiles } from '@/features/settings/aiProviderCatalog'
import type { TextContext } from '@/types/appContext'
import { addRuntimeEvent } from '../debugLog'
import { getSetting } from '../store'

export const CONTEXT_SELECTION_AI_PROFILE_KEY = 'contextSelectionAiProfileId'

/** Request-only configuration. Never persist this snapshot in history or logs. */
export interface ClientAiConfig {
  provider: string
  apiUrl: string
  apiKey: string
  model: string
}

/** Resolve once at recording start so profile edits cannot change an in-flight request. */
export async function loadSelectionAiConfig(
  textContext: TextContext | null | undefined,
): Promise<ClientAiConfig | null> {
  if (!textContext?.selectedText || textContext.selectionTruncated) return null
  try {
    const profileId = await getSetting(CONTEXT_SELECTION_AI_PROFILE_KEY, '')
    if (!profileId) return null
    const profiles = parseProfiles(await getSetting<unknown>('cloudAi.profiles', []))
    const profile = profiles.find((item) => item.id === profileId)
    if (profile) {
      const { provider, apiUrl, apiKey, model } = profile
      return { provider, apiUrl, apiKey, model }
    }
    addRuntimeEvent('warn', 'ai', 'Selection-edit profile is unavailable; using current AI')
  } catch {
    addRuntimeEvent('warn', 'ai', 'Could not load selection-edit profile; using current AI')
  }
  return null
}
