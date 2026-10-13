import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { transcribeWithServer } from '../serverTranscription'
import type { StartOptions } from '../types'

vi.mock('../../runtimeConfig', () => ({ getWSUrl: () => 'ws://configured/ws' }))
vi.mock('../../debugLog', () => ({ addRuntimeEvent: vi.fn(), AI_LOG_SOURCE: 'ai', AI_EVENT_OUTCOME: 'outcome' }))
const polish = vi.hoisted(() => vi.fn())
vi.mock('../clientAiPolish', () => ({ polishWithClientAi: polish }))

class Socket {
  static instances: Socket[] = []
  binaryType = ''
  onmessage?: (event: { data: string }) => void
  onerror?: () => void
  onclose?: (event: { code: number }) => void
  send = vi.fn()
  close = vi.fn()
  constructor(readonly url: string) {
    Socket.instances.push(this)
  }
  message(value: unknown) {
    this.onmessage?.({ data: JSON.stringify(value) })
  }
}

const audio = new Int16Array(32000).buffer
const options: StartOptions = {
  runId: 1,
  hotwords: ['SayIt'],
  language: 'en',
  source: 'live',
  aiConfig: { workMode: 'server', aiEnabled: false, aiMinDurationSec: 0, serverAiSource: 'managed' },
}

describe('server recording transcription', () => {
  beforeEach(() => {
    Socket.instances = []
    vi.stubGlobal('WebSocket', Socket)
    vi.stubGlobal('window', globalThis)
    vi.clearAllMocks()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('uses a private socket and sends the original PCM after ready', async () => {
    const promise = transcribeWithServer(audio, options, undefined, 'ws://backup/ws')
    const socket = Socket.instances[0]
    expect(socket.url).toBe('ws://backup/ws')
    expect(socket.send).not.toHaveBeenCalled()
    socket.message({ type: 'ready' })
    expect(JSON.parse(socket.send.mock.calls[0][0])).toMatchObject({
      cmd: 'start',
      disable_ai: true,
      hotwords: ['SayIt'],
      language: 'en',
      source: 'live',
    })
    expect(socket.send.mock.calls.slice(1, 3).map((call) => call[0].byteLength)).toEqual([32000, 32000])
    socket.message({ type: 'final', asr_text: 'Recovered', asr_ms: 10 })
    expect(await promise).toMatchObject({ asrText: 'Recovered', durationSec: 2, aiStatus: 'skipped' })
    expect(socket.close).toHaveBeenCalledOnce()
    expect(polish).not.toHaveBeenCalled()
  })

  it('closes immediately on cancellation and never sends late audio', async () => {
    const abort = new AbortController()
    const promise = transcribeWithServer(audio, options, abort.signal)
    const rejection = expect(promise).rejects.toThrow('cancelled')
    abort.abort()
    Socket.instances[0].message({ type: 'ready' })
    await rejection
    expect(Socket.instances[0].close).toHaveBeenCalledOnce()
    expect(Socket.instances[0].send).not.toHaveBeenCalled()
  })

  it('bounds a stalled server attempt and closes the socket', async () => {
    vi.useFakeTimers()
    const promise = transcribeWithServer(audio, options)
    const rejection = expect(promise).rejects.toThrow('timed out')
    await vi.advanceTimersByTimeAsync(60_000)
    await rejection
    expect(Socket.instances[0].close).toHaveBeenCalledOnce()
  })

  it('preserves editor context on the managed AI route', async () => {
    const promise = transcribeWithServer(audio, {
      ...options,
      systemPrompt: 'Clean up text',
      aiConfig: { ...options.aiConfig!, aiEnabled: true },
      textContext: {
        source: 'uia',
        textBefore: 'Before',
        selectedText: '',
        textAfter: 'After',
        selectionTruncated: false,
      },
    })
    const socket = Socket.instances[0]
    socket.message({ type: 'ready', capabilities: { text_context: true } })
    expect(JSON.parse(socket.send.mock.calls[0][0])).toMatchObject({
      disable_ai: false,
      system_prompt: 'Clean up text',
      text_context: { text_before: 'Before', text_after: 'After' },
    })
    socket.message({ type: 'final', asr_text: 'Text', context_applied: true })
    expect(await promise).toMatchObject({ contextApplied: true })
  })

  it('fails promptly when sending audio throws', async () => {
    const promise = transcribeWithServer(audio, options)
    const rejection = expect(promise).rejects.toThrow('send failed')
    const socket = Socket.instances[0]
    socket.send.mockImplementation(() => {
      throw new Error('send failed')
    })
    socket.message({ type: 'ready' })
    await rejection
    expect(socket.close).toHaveBeenCalledOnce()
  })
})
