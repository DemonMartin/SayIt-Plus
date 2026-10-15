import { invoke } from '@tauri-apps/api/core'
import {
  asrEndpointUrl,
  effectiveAsrCredentials,
  resolveAsrApiModel,
  resolveAsrModelOption,
  resolveAsrRuntimeProvider,
  type AsrProfile,
} from '@/features/settings/asrProviderCatalog'
import { buildAsrExtra } from '@/lib/asrModels'
import { uint8ArrayToBase64 } from '@/lib/encoding'
import { notifyAsrCapabilityMaybeChanged } from '../bridge'
import { restoreHotwordSpacing } from '../textPostProcess'
import { policyFromSnapshot, resolveAndLogAiOutcome } from './aiPolicy'
import { polishWithClientAi } from './clientAiPolish'
import type { FinalResult, StartOptions } from './types'

/** One file transcription using a frozen profile; never switches the primary engine. */
export async function transcribeWithProfile(
  profile: AsrProfile,
  chunks: ArrayBuffer[],
  options: StartOptions,
  isCurrent: () => boolean,
): Promise<FinalResult | null> {
  if (!isCurrent()) return null
  const bytes = new Uint8Array(chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0))
  let offset = 0
  for (const chunk of chunks) {
    bytes.set(new Uint8Array(chunk), offset)
    offset += chunk.byteLength
  }
  const durationSec = bytes.length / 32000
  const provider = resolveAsrRuntimeProvider(profile)
  const model = resolveAsrApiModel(profile)
  const integrated = Boolean(resolveAsrModelOption(profile)?.omni)
  const credentials = effectiveAsrCredentials(profile)
  const extra = buildAsrExtra(provider, {
    model,
    instructions: integrated ? profile.omniPrompt : '',
    baseUrl: asrEndpointUrl(profile),
    protocol: profile.protocol,
  })
  const result = await invoke<{ text: string; elapsed_ms: number }>('cloud_transcribe', {
    request: {
      audio_b64: uint8ArrayToBase64(bytes),
      sample_rate: 16000,
      asr_config: { provider, api_key: credentials.apiKey, app_id: credentials.appId, extra },
      hotwords: options.hotwords ?? [],
    },
  })
  notifyAsrCapabilityMaybeChanged()
  if (!isCurrent()) return null
  const asrText = restoreHotwordSpacing(result.text, options.hotwords ?? [])
  const policy = policyFromSnapshot(
    options.aiConfig ? { ...options.aiConfig, workMode: 'cloud_api' } : undefined,
    'cloud_api',
    durationSec,
    integrated,
  )
  const outcomeContext = {
    operationId: options.operationId || `profile-${options.runId}`,
    trigger: options.source === 'history_reprocess' ? ('history_reprocess' as const) : ('live' as const),
  }
  if (!asrText.trim()) {
    const outcome = resolveAndLogAiOutcome(outcomeContext, policy, { asrTextEmpty: true })
    return {
      asrText: '',
      llmText: '',
      asrMs: result.elapsed_ms,
      llmMs: 0,
      durationSec,
      asrEngine: provider,
      asrModel: model,
      aiSource: outcome.source,
      aiStatus: outcome.status,
    }
  }
  const polish = await polishWithClientAi({
    asrText,
    startOptions: options,
    policy,
    outcomeContext,
    logSource: 'cloud_api',
    isCurrent,
  })
  if (!polish || !isCurrent()) return null
  return {
    asrText,
    asrMs: result.elapsed_ms,
    durationSec,
    ...polish,
    asrEngine: provider,
    asrModel: model,
  }
}
