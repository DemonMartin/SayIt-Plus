import type { ResolvedAsrTarget } from './asrTarget'
import { addRuntimeEvent } from '../debugLog'
import { applyFallback } from '../fallback'
import { loadAsrFallback } from './asrFallback'
import { transcribeRecording } from './transcribeRecording'
import type {
  ASRResult,
  FinalResult,
  StartOptions,
  StopOptions,
  TranscriptionCallbacks,
  TranscriptionProvider,
} from './types'

interface Run {
  options: StartOptions
  targets: Promise<ResolvedAsrTarget[]>
  abort: AbortController
  chunks: ArrayBuffer[]
  captureAudio: boolean
  stopped: boolean
  settling: boolean
  failed: boolean
  asr?: ASRResult
}

/** Holds empty terminal events until the configured chain has finished. */
export class AsrFallbackProvider implements TranscriptionProvider {
  private callbacks: TranscriptionCallbacks = {}
  private run: Run | null = null

  constructor(private readonly primary: TranscriptionProvider) {}

  get mode() {
    return this.primary.mode
  }
  isReady() {
    return this.primary.isReady()
  }

  async connect(callbacks: TranscriptionCallbacks): Promise<void> {
    this.callbacks = callbacks
    await this.primary.connect({
      onStateChange: (state) => callbacks.onStateChange?.(state),
      onReady: (info) => callbacks.onReady?.(info),
      onPartialASR: (text) => {
        if (this.run && !this.run.settling) callbacks.onPartialASR?.(text)
      },
      onASR: (result) => {
        const run = this.run
        if (!run || run.settling) return
        run.asr = result
        if (result.text.trim()) callbacks.onASR?.(result)
      },
      onFinal: (result) => {
        const run = this.run
        if (run && !run.settling) void this.settle(run, result)
      },
      onError: (message) => {
        if (this.run?.settling) return
        if (this.run) this.run.failed = true
        callbacks.onError?.(message)
      },
      onDone: () => {
        const run = this.run
        if (!run || run.settling) return
        if (run.stopped && !run.failed && !run.asr?.text.trim()) {
          void this.settle(
            run,
            {
              asrText: '',
              llmText: '',
              llmMs: 0,
              asrMs: run.asr?.asrMs ?? 0,
              durationSec: run.asr?.durationSec ?? 0,
            },
            true,
          )
        } else {
          this.run = null
          callbacks.onDone?.()
        }
      },
    })
  }

  start(options: StartOptions): boolean {
    // ServerProvider.cancel disconnects its socket; preserve an idle ready connection.
    if (this.run) this.cancel()
    const run: Run = {
      options: {
        ...options,
        hotwords: options.hotwords?.slice(),
        aiConfig: options.aiConfig ? { ...options.aiConfig } : undefined,
        textContext: options.textContext ? { ...options.textContext } : undefined,
      },
      targets: loadAsrFallback().catch((error) => {
        addRuntimeEvent('warn', 'asr_fallback', 'Could not load fallback settings', { error: String(error) })
        return []
      }),
      abort: new AbortController(),
      chunks: [],
      captureAudio: true,
      stopped: false,
      settling: false,
      failed: false,
    }
    this.run = run
    void run.targets.then((targets) => {
      if (this.run === run && !targets.length) {
        run.captureAudio = false
        run.chunks = []
      }
    })
    if (this.primary.start(options)) return true
    this.run = null
    return false
  }

  sendAudio(buffer: ArrayBuffer): void {
    if (this.run?.captureAudio && !this.run.stopped) this.run.chunks.push(buffer.slice(0))
    this.primary.sendAudio(buffer)
  }

  stop(options?: StopOptions): boolean {
    if (this.run) this.run.stopped = true
    return this.primary.stop(options)
  }

  cancel(): void {
    this.run?.abort.abort()
    if (this.run) this.run.chunks = []
    this.run = null
    this.primary.cancel()
  }

  disconnect(): void {
    this.run?.abort.abort()
    if (this.run) this.run.chunks = []
    this.run = null
    this.primary.disconnect()
  }

  private async settle(run: Run, original: FinalResult, doneOnly = false): Promise<void> {
    run.settling = true
    const isCurrent = () => this.run === run
    let result: FinalResult | null
    let lastError: unknown
    let totalAsrMs = original.asrMs
    try {
      result = await applyFallback(original, {
        isCurrent,
        shouldFallback: (value) => !value.asrText.trim(),
        getSteps: async () => {
          if (!run.stopped) return []
          const targets = await run.targets
          // Require at least 300 ms and actual signal. Silence must not cause an upload.
          const samples = run.chunks.reduce((sum, chunk) => sum + chunk.byteLength / 2, 0)
          const hasSignal = run.chunks.some((chunk) =>
            new Int16Array(chunk).some((sample) => Math.abs(sample) / 32768 >= 0.01),
          )
          if (!targets.length || samples < 4800 || !hasSignal) return []
          return targets.map((target, index) => ({
            id: `${index}:${target.mode}`,
            execute: async () => {
              this.callbacks.onFallbackStart?.(target.mode)
              const fallback = await transcribeRecording(
                target,
                run.chunks,
                {
                  ...run.options,
                  operationId: `${run.options.operationId || run.options.runId}-fallback-${index + 1}`,
                },
                run.abort.signal,
              )
              if (!fallback) return original
              totalAsrMs += fallback.asrMs
              return { ...fallback, asrMs: totalAsrMs, asrFallback: true, workMode: target.mode }
            },
          }))
        },
        onAttempt: (stepId) => {
          addRuntimeEvent('info', 'asr_fallback', 'Empty recognition; trying next engine', {
            runId: run.options.runId,
            primaryMode: this.mode,
            stepId,
          })
        },
        onError: (stepId, error) => {
          lastError = error
          addRuntimeEvent('warn', 'asr_fallback', 'Backup unavailable; continuing chain', {
            stepId,
            error: String(error),
          })
        },
        onResult: (_id, value) => {
          addRuntimeEvent('info', 'asr_fallback', 'Backup recognition completed', {
            runId: run.options.runId,
            hasText: Boolean(value.asrText.trim()),
          })
        },
      })
      if (result === original && lastError) throw lastError
    } catch (error) {
      if (!isCurrent()) return
      addRuntimeEvent('warn', 'asr_fallback', 'Backup recognition failed', { error: String(error) })
      this.callbacks.onError?.(String(error))
      if (isCurrent()) {
        this.run = null
        this.callbacks.onDone?.()
      }
      return
    }
    if (!result || !isCurrent()) return
    run.chunks = []
    // Preserve protocol-incomplete handling when no backup was available for a bare done.
    if (!doneOnly || run.asr || result !== original) this.callbacks.onFinal?.(result)
    if (isCurrent()) {
      this.run = null
      this.callbacks.onDone?.()
    }
  }
}
