import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AsrFallbackProvider } from '../AsrFallbackProvider'
import type { FinalResult, TranscriptionCallbacks, TranscriptionProvider, WorkMode } from '../types'

const mocks = vi.hoisted(() => ({ load: vi.fn(), transcribe: vi.fn() }))
vi.mock('../asrFallback', () => ({ loadAsrFallback: (...args: unknown[]) => mocks.load(...args) }))
vi.mock('../transcribeRecording', () => ({ transcribeRecording: (...args: unknown[]) => mocks.transcribe(...args) }))
vi.mock('../../debugLog', () => ({ addRuntimeEvent: vi.fn() }))

const empty: FinalResult = { asrText: '', llmText: '', asrMs: 20, llmMs: 0, durationSec: 1 }
const recovered: FinalResult = { ...empty, asrText: 'Recovered', llmText: 'Recovered', asrModel: 'backup-model' }

class Primary implements TranscriptionProvider {
  callbacks: TranscriptionCallbacks = {}
  constructor(readonly mode: WorkMode = 'local') {}
  async connect(callbacks: TranscriptionCallbacks) {
    this.callbacks = callbacks
  }
  start() {
    return true
  }
  sendAudio() {}
  stop() {
    return true
  }
  cancel = vi.fn()
  disconnect() {}
  isReady() {
    return true
  }
}

async function setup(mode: WorkMode = 'local', sampleCount = 16000, amplitude = 3000) {
  const primary = new Primary(mode)
  const provider = new AsrFallbackProvider(primary)
  const callbacks = { onASR: vi.fn(), onFinal: vi.fn(), onDone: vi.fn(), onError: vi.fn(), onFallbackStart: vi.fn() }
  await provider.connect(callbacks)
  provider.start({ runId: 1, hotwords: ['SayIt'], disableAi: true })
  const audio = new Int16Array(sampleCount).fill(amplitude).buffer
  provider.sendAudio(audio)
  provider.stop()
  return { primary, provider, callbacks, audio }
}

function finish(primary: Primary, result = empty) {
  primary.callbacks.onASR?.({ text: result.asrText, asrMs: result.asrMs, durationSec: result.durationSec })
  primary.callbacks.onFinal?.(result)
  primary.callbacks.onDone?.()
}

describe('AsrFallbackProvider', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.load.mockResolvedValue([{ mode: 'cloud_api', profile: { id: 'backup' } }])
    mocks.transcribe.mockResolvedValue(recovered)
  })

  it.each<WorkMode>(['local', 'cloud_api', 'server'])('replays audio once after empty %s recognition', async (mode) => {
    const { primary, callbacks, audio } = await setup(mode)
    finish(primary)
    expect(callbacks.onASR).not.toHaveBeenCalled()
    expect(callbacks.onDone).not.toHaveBeenCalled()
    await vi.waitFor(() => expect(callbacks.onFinal).toHaveBeenCalledOnce())
    expect(mocks.transcribe).toHaveBeenCalledOnce()
    expect(mocks.transcribe.mock.calls[0][1]).toEqual([audio])
    expect(callbacks.onFinal).toHaveBeenCalledWith({
      ...recovered,
      asrMs: 40,
      asrFallback: true,
      workMode: 'cloud_api',
    })
    expect(callbacks.onDone).toHaveBeenCalledOnce()
    expect(callbacks.onFallbackStart).toHaveBeenCalledOnce()
    expect(primary.cancel).not.toHaveBeenCalled()
  })

  it('delivers a successful primary result without a backup request', async () => {
    const { primary, callbacks } = await setup()
    finish(primary, recovered)
    await vi.waitFor(() => expect(callbacks.onFinal).toHaveBeenCalledWith(recovered))
    expect(mocks.transcribe).not.toHaveBeenCalled()
  })

  it('continues when empty recognition preserves existing editor text', async () => {
    mocks.load.mockResolvedValue([
      { mode: 'cloud_api', profile: { id: 'one' } },
      { mode: 'cloud_api', profile: { id: 'two' } },
    ])
    mocks.transcribe.mockResolvedValueOnce({ ...empty, llmText: 'Existing selection' }).mockResolvedValueOnce(recovered)
    const { primary, callbacks } = await setup('server')
    finish(primary, { ...empty, llmText: 'Existing selection' })
    await vi.waitFor(() => expect(callbacks.onFinal).toHaveBeenCalledOnce())
    expect(mocks.transcribe).toHaveBeenCalledTimes(2)
    expect(callbacks.onFinal.mock.calls[0][0].asrText).toBe('Recovered')
  })

  it('forwards connection errors before a recording starts', async () => {
    const primary = new Primary('server')
    const provider = new AsrFallbackProvider(primary)
    const onError = vi.fn()
    await provider.connect({ onError })
    primary.callbacks.onError?.('connection failed')
    expect(onError).toHaveBeenCalledWith('connection failed')
  })

  it.each([
    [16000, 0],
    [16000, 100],
    [4000, 3000],
    [0, 0],
  ])('skips silent or short audio (%i samples, amplitude %i)', async (samples, amplitude) => {
    const { primary, callbacks } = await setup('local', samples, amplitude)
    finish(primary)
    await vi.waitFor(() => expect(callbacks.onFinal).toHaveBeenCalledWith(empty))
    expect(mocks.transcribe).not.toHaveBeenCalled()
  })

  it('handles a provider completing without ASR or final events', async () => {
    const { primary, callbacks } = await setup()
    primary.callbacks.onDone?.()
    await vi.waitFor(() => expect(callbacks.onFinal).toHaveBeenCalledOnce())
    expect(mocks.transcribe).toHaveBeenCalledOnce()
  })

  it('does not retry primary provider errors', async () => {
    const { primary, callbacks } = await setup()
    primary.callbacks.onError?.('bad key')
    primary.callbacks.onDone?.()
    expect(callbacks.onError).toHaveBeenCalledWith('bad key')
    expect(mocks.transcribe).not.toHaveBeenCalled()
  })

  it('does not treat a missing AI final as empty recognition after ASR produced text', async () => {
    const { primary, callbacks } = await setup()
    primary.callbacks.onASR?.({ text: 'Already recognized', asrMs: 20, durationSec: 1 })
    primary.callbacks.onDone?.()
    expect(callbacks.onDone).toHaveBeenCalledOnce()
    expect(mocks.transcribe).not.toHaveBeenCalled()
  })

  it('does not loop when the backup also returns nothing', async () => {
    mocks.transcribe.mockResolvedValue(empty)
    const { primary, callbacks } = await setup()
    finish(primary)
    await vi.waitFor(() => expect(callbacks.onDone).toHaveBeenCalledOnce())
    expect(mocks.transcribe).toHaveBeenCalledOnce()
  })

  it('retains normal empty-result behavior when fallback is unavailable', async () => {
    mocks.load.mockResolvedValue([])
    const { primary, callbacks } = await setup()
    finish(primary)
    await vi.waitFor(() => expect(callbacks.onFinal).toHaveBeenCalledWith(empty))
    expect(mocks.transcribe).not.toHaveBeenCalled()
  })

  it('preserves protocol-incomplete handling for a bare done with fallback off', async () => {
    mocks.load.mockResolvedValue([])
    const { primary, callbacks } = await setup()
    primary.callbacks.onDone?.()
    await vi.waitFor(() => expect(callbacks.onDone).toHaveBeenCalledOnce())
    expect(callbacks.onFinal).not.toHaveBeenCalled()
  })

  it.each(['cancel', 'disconnect', 'new run'] as const)('discards a pending backup after %s', async (action) => {
    let resolve!: (result: FinalResult) => void
    mocks.transcribe.mockReturnValue(
      new Promise<FinalResult>((done) => {
        resolve = done
      }),
    )
    const { primary, provider, callbacks } = await setup()
    finish(primary)
    await vi.waitFor(() => expect(mocks.transcribe).toHaveBeenCalledOnce())
    if (action === 'new run') provider.start({ runId: 2 })
    else provider[action]()
    resolve(recovered)
    await new Promise((done) => setTimeout(done, 0))
    expect(callbacks.onFinal).not.toHaveBeenCalled()
    expect(callbacks.onDone).not.toHaveBeenCalled()
  })

  it('reports backup errors once', async () => {
    mocks.transcribe.mockRejectedValue(new Error('offline'))
    const { primary, callbacks } = await setup()
    finish(primary)
    await vi.waitFor(() => expect(callbacks.onError).toHaveBeenCalledOnce())
    expect(callbacks.onFinal).not.toHaveBeenCalled()
    expect(callbacks.onDone).toHaveBeenCalledOnce()
  })
  it('walks a mixed chain in order and stops at the first text', async () => {
    mocks.load.mockResolvedValue([
      { mode: 'server', url: 'ws://server' },
      { mode: 'cloud_api', profile: { id: 'one' } },
      { mode: 'cloud_api', profile: { id: 'two' } },
      { mode: 'local', modelId: 'local-model' },
    ])
    mocks.transcribe
      .mockResolvedValueOnce(empty)
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(recovered)
    const { primary, callbacks } = await setup()
    finish(primary)
    await vi.waitFor(() => expect(callbacks.onFinal).toHaveBeenCalledOnce())
    expect(mocks.transcribe.mock.calls.map((call) => call[0].mode)).toEqual(['server', 'cloud_api', 'cloud_api'])
    expect(callbacks.onFinal).toHaveBeenCalledWith({
      ...recovered,
      asrMs: 60,
      asrFallback: true,
      workMode: 'cloud_api',
    })
    expect(callbacks.onError).not.toHaveBeenCalled()
    expect(callbacks.onFallbackStart.mock.calls).toEqual([['server'], ['cloud_api'], ['cloud_api']])
  })

  it('records the successful local fallback mode and cancels active attempts', async () => {
    mocks.load.mockResolvedValue([{ mode: 'local', modelId: 'local-model' }])
    const { primary, callbacks } = await setup('cloud_api')
    finish(primary)
    await vi.waitFor(() => expect(callbacks.onFinal).toHaveBeenCalledOnce())
    expect(callbacks.onFinal.mock.calls[0][0].workMode).toBe('local')
  })
})
