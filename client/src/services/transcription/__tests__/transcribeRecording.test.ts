import { beforeEach, describe, expect, it, vi } from 'vitest'
import { transcribeRecording } from '../transcribeRecording'
import { applyFallback } from '../../fallback'

const mocks = vi.hoisted(() => ({ invoke: vi.fn(), local: vi.fn(), polish: vi.fn(), server: vi.fn(), cloud: vi.fn() }))
vi.mock('@tauri-apps/api/core', () => ({ invoke: (...args: unknown[]) => mocks.invoke(...args) }))
vi.mock('../../bridge', () => ({ localTranscribeRaw: (...args: unknown[]) => mocks.local(...args) }))
vi.mock('../../store', () => ({ getSetting: async (_key: string, fallback: unknown) => fallback }))
vi.mock('../../debugLog', () => ({ addRuntimeEvent: vi.fn(), AI_LOG_SOURCE: 'ai', AI_EVENT_OUTCOME: 'outcome' }))
vi.mock('../serverTranscription', () => ({ transcribeWithServer: (...args: unknown[]) => mocks.server(...args) }))
vi.mock('../profileTranscription', () => ({ transcribeWithProfile: (...args: unknown[]) => mocks.cloud(...args) }))
vi.mock('../clientAiPolish', () => ({ polishWithClientAi: (...args: unknown[]) => mocks.polish(...args) }))

const options = {
  runId: 1,
  aiConfig: {
    workMode: 'cloud_api' as const,
    aiEnabled: true,
    aiMinDurationSec: 0,
    serverAiSource: 'managed' as const,
  },
}

describe('recorded audio target execution', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.polish.mockResolvedValue({ llmText: 'Recognized', llmMs: 0 })
    mocks.local.mockResolvedValue({ text: 'Recognized', elapsed_ms: 12 })
    mocks.invoke.mockImplementation(async (command) => {
      if (command === 'list_downloaded_models') return [{ id: 'backup-local', complete: true }]
      return undefined
    })
  })

  it('uses the requested downloaded model instead of the normal local selection', async () => {
    const result = await transcribeRecording(
      { mode: 'local', modelId: 'backup-local' },
      [new Int16Array(16000).buffer],
      options,
      new AbortController().signal,
    )
    expect(mocks.local).toHaveBeenCalledWith(new Uint8Array(32000), expect.objectContaining({ modelId: 'backup-local' }))
    expect(result).toMatchObject({ asrText: 'Recognized', asrModel: 'backup-local', durationSec: 1 })
    expect(mocks.polish.mock.calls[0][0].policy.workMode).toBe('local')
    expect(mocks.invoke.mock.calls.some((call) => call[0] === 'preload_local_model')).toBe(false)
  })

  it('cancels pending local work without polishing a late result', async () => {
    let complete!: (value: unknown) => void
    mocks.local.mockImplementation(() => new Promise((resolve) => { complete = resolve }))
    const abort = new AbortController()
    const promise = transcribeRecording(
      { mode: 'local', modelId: 'backup-local' },
      [new Int16Array(16000).buffer],
      options,
      abort.signal,
    )
    await vi.waitFor(() => expect(complete).toBeDefined())
    abort.abort()
    expect(await promise).toBeNull()
    complete({ text: 'Late', elapsed_ms: 12 })
    await Promise.resolve()
    expect(mocks.polish).not.toHaveBeenCalled()
  })

  it('uses the server AI configuration even when starting in cloud mode', async () => {
    mocks.server.mockResolvedValue(null)
    await transcribeRecording(
      { mode: 'server', url: 'ws://backup', aiSource: 'custom' },
      [new Int16Array(16000).buffer],
      options,
      new AbortController().signal,
    )
    expect(mocks.server.mock.calls[0][1].aiConfig).toMatchObject({ workMode: 'server', serverAiSource: 'custom' })
    expect(mocks.server.mock.calls[0][3]).toBe('ws://backup')
  })

  it('loads successive local targets on demand after an empty result', async () => {
    mocks.invoke.mockImplementation(async (command) => {
      if (command === 'list_downloaded_models')
        return [
          { id: 'first-local', complete: true },
          { id: 'second-local', complete: true },
        ]
    })
    mocks.local.mockImplementation(async (_pcm, args) => ({
      text: args.modelId === 'first-local' ? '' : 'Recognized', elapsed_ms: 12,
    }))
    const empty = { asrText: '', llmText: '', asrMs: 0, llmMs: 0, durationSec: 1 }
    const result = await applyFallback(empty, {
      shouldFallback: (value) => !value.asrText.trim(),
      isCurrent: () => true,
      getSteps: async () =>
        ['first-local', 'second-local'].map((modelId) => ({
          id: modelId,
          execute: async () =>
            (await transcribeRecording(
              { mode: 'local', modelId },
              [new Int16Array(16000).buffer],
              options,
              new AbortController().signal,
            )) ?? empty,
        })),
    })
    expect(result).toMatchObject({ asrText: 'Recognized', asrModel: 'second-local' })
    expect(
      mocks.local.mock.calls.map((call) => call[1].modelId),
    ).toEqual(['first-local', 'second-local'])
    expect(mocks.invoke.mock.calls.some((call) => call[0] === 'preload_local_model')).toBe(false)
  })
})
