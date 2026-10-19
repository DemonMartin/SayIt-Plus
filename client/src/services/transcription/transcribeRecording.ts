import { LocalProvider } from './LocalProvider'
import { transcribeWithProfile } from './profileTranscription'
import { transcribeWithServer } from './serverTranscription'
import type { ResolvedAsrTarget } from './asrTarget'
import type { FinalResult, StartOptions, TranscriptionProvider } from './types'

/** Runs a provider against existing PCM without changing the application's active provider. */
async function transcribeWithProvider(
  provider: TranscriptionProvider,
  chunks: ArrayBuffer[],
  options: StartOptions,
  signal: AbortSignal,
): Promise<FinalResult | null> {
  if (signal.aborted) return null
  return new Promise((resolve, reject) => {
    let settled = false
    const finish = (result: FinalResult | null, error?: unknown) => {
      if (settled) return
      settled = true
      signal.removeEventListener('abort', abort)
      provider.disconnect()
      if (error) reject(error)
      else resolve(result)
    }
    const abort = () => finish(null)
    signal.addEventListener('abort', abort, { once: true })
    void provider
      .connect({
        onFinal: (result) => finish(result),
        onError: (message) => finish(null, new Error(message)),
        onDone: () => finish({ asrText: '', llmText: '', asrMs: 0, llmMs: 0, durationSec: 0 }),
      })
      .then(() => {
        if (settled || signal.aborted) {
          provider.disconnect()
          return
        }
        if (!provider.start(options)) throw new Error('Backup provider is unavailable')
        for (const chunk of chunks) provider.sendAudio(chunk)
        if (!provider.stop()) throw new Error('Backup provider could not process the recording')
      })
      .catch((error) => finish(null, error))
  })
}

export async function transcribeRecording(
  target: ResolvedAsrTarget,
  chunks: ArrayBuffer[],
  options: StartOptions,
  signal: AbortSignal,
): Promise<FinalResult | null> {
  const startOptions = {
    ...options,
    aiConfig: options.aiConfig
      ? {
          ...options.aiConfig,
          workMode: target.mode,
          serverAiSource: target.mode === 'server' ? target.aiSource : options.aiConfig.serverAiSource,
        }
      : undefined,
  }
  switch (target.mode) {
    case 'cloud_api':
      return transcribeWithProfile(target.profile, chunks, startOptions, () => !signal.aborted)
    case 'local': {
      const result = await transcribeWithProvider(
        new LocalProvider({ modelId: target.modelId, preload: false }),
        chunks,
        startOptions,
        signal,
      )
      return result ? { ...result, asrModel: target.modelId } : null
    }
    case 'server': {
      const bytes = new Uint8Array(chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0))
      let offset = 0
      for (const chunk of chunks) {
        bytes.set(new Uint8Array(chunk), offset)
        offset += chunk.byteLength
      }
      return transcribeWithServer(bytes.buffer, startOptions, signal, target.url)
    }
  }
}
