import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ServerProvider } from '../ServerProvider'
import type { WSCallbacks } from '../../websocket'
import type { StartOptions } from '../types'

const mocks = vi.hoisted(() => ({ invoke: vi.fn(), start: vi.fn(), stop: vi.fn(), connect: vi.fn() }))
vi.mock('@tauri-apps/api/core', () => ({ invoke: (...args: unknown[]) => mocks.invoke(...args) }))
vi.mock('../../store', () => ({ getSetting: async (_key: string, fallback: unknown) => fallback }))
vi.mock('../../debugLog', () => ({ addRuntimeEvent: vi.fn(), AI_LOG_SOURCE: 'ai', AI_EVENT_REQUEST: 'request', AI_EVENT_OUTCOME: 'outcome' }))
vi.mock('../serverAiSource', () => ({ getRuntimeServerAiSource: () => 'managed', loadServerAiSource: async () => 'managed' }))
vi.mock('../../websocket', () => ({
  connect: (...args: unknown[]) => mocks.connect(...args),
  sendStart: (...args: unknown[]) => mocks.start(...args),
  sendStop: (...args: unknown[]) => mocks.stop(...args),
  disconnect: vi.fn(), isConnected: () => true,
}))

const options: StartOptions = {
  runId: 1,
  aiConfig: { workMode: 'server', aiEnabled: true, aiMinDurationSec: 0, serverAiSource: 'managed', selectionAiOverride: true },
  selectionAiConfig: { provider: 'ollama', apiUrl: 'http://localhost:11434', apiKey: '', model: 'editor' },
  textContext: { source: 'uia', textBefore: '', selectedText: 'Original', textAfter: '', selectionTruncated: false },
  systemPrompt: 'Edit selected text',
}
const result = { asrText: 'Shorten this', llmText: 'Shorten this', asrMs: 10, llmMs: 0, durationSec: 4 }

describe('live server selection editing', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.start.mockReturnValue(true)
    mocks.stop.mockReturnValue(true)
    mocks.invoke.mockResolvedValue({ text: 'Edited', elapsed_ms: 20 })
  })

  async function setup(startOptions = options) {
    const provider = new ServerProvider()
    const callbacks = { onFinal: vi.fn(), onDone: vi.fn() }
    await provider.connect(callbacks)
    provider.start(startOptions)
    provider.stop()
    return { provider, callbacks, wire: mocks.connect.mock.calls[0][0] as WSCallbacks }
  }

  it('keeps the managed server on ASR and waits for the chosen client model before completing', async () => {
    let finish!: (value: unknown) => void
    mocks.invoke.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    const { callbacks, wire } = await setup()
    expect(mocks.start).toHaveBeenCalledWith(expect.objectContaining({ disableAi: true, textContext: undefined, systemPrompt: undefined }))
    expect(mocks.stop).toHaveBeenCalledWith(expect.objectContaining({ disableAi: true }))
    wire.onFinal?.(result)
    wire.onDone?.()
    expect(callbacks.onDone).not.toHaveBeenCalled()
    finish({ text: 'Edited', elapsed_ms: 20 })
    await vi.waitFor(() => expect(callbacks.onFinal).toHaveBeenCalledWith(expect.objectContaining({
      llmText: 'Edited', contextApplied: true, aiSource: 'custom', aiModel: 'editor',
    })))
    expect(callbacks.onDone).toHaveBeenCalledOnce()
    expect(mocks.invoke.mock.calls[0][1].request.ai_config.model).toBe('editor')
  })

  it('preserves the managed path when no separate model was selected', async () => {
    const normal = { ...options, selectionAiConfig: null, aiConfig: { ...options.aiConfig!, selectionAiOverride: false } }
    const { callbacks, wire } = await setup(normal)
    expect(mocks.start).toHaveBeenCalledWith(expect.objectContaining({ textContext: options.textContext, systemPrompt: options.systemPrompt }))
    wire.onFinal?.({ ...result, llmText: 'Server edit', llmMs: 20, contextApplied: true })
    wire.onDone?.()
    expect(callbacks.onFinal).toHaveBeenCalledWith(expect.objectContaining({ llmText: 'Server edit', aiSource: 'server' }))
    expect(callbacks.onDone).toHaveBeenCalledOnce()
    expect(mocks.invoke).not.toHaveBeenCalled()
  })

  it('discards late editor output after cancellation', async () => {
    let finish!: (value: unknown) => void
    mocks.invoke.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    const { provider, callbacks, wire } = await setup()
    wire.onFinal?.(result)
    provider.cancel()
    finish({ text: 'Late', elapsed_ms: 20 })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(callbacks.onFinal).not.toHaveBeenCalled()
    expect(callbacks.onDone).not.toHaveBeenCalled()
  })
})
