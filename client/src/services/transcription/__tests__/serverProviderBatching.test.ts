import { beforeEach, describe, expect, it, vi } from 'vitest'

const wsMock = vi.hoisted(() => ({
  sent: [] as Array<{ kind: 'audio'; bytes: number[] } | { kind: 'stop' }>,
}))

vi.mock('../../websocket', () => ({
  connect: vi.fn().mockResolvedValue(undefined),
  sendStart: vi.fn(() => true),
  sendAudio: vi.fn((buffer: ArrayBuffer) => {
    wsMock.sent.push({ kind: 'audio', bytes: Array.from(new Uint8Array(buffer)) })
  }),
  sendStop: vi.fn(() => {
    wsMock.sent.push({ kind: 'stop' })
    return true
  }),
  disconnect: vi.fn(),
  isConnected: vi.fn(() => true),
}))
vi.mock('../../store', () => ({ getSetting: vi.fn(async (_k: string, fallback: unknown) => fallback) }))
vi.mock('../../debugLog', () => ({ addRuntimeEvent: vi.fn() }))
vi.mock('../serverAiSource', () => ({
  getRuntimeServerAiSource: () => 'managed',
  loadServerAiSource: async () => 'managed',
}))

import { ServerProvider } from '../ServerProvider'

function frame(byteLength: number, fill: number): ArrayBuffer {
  return new Uint8Array(byteLength).fill(fill).buffer
}

describe('ServerProvider audio batching', () => {
  beforeEach(() => {
    wsMock.sent = []
  })

  it('sends 200 ms batches instead of every frame, in order', () => {
    const provider = new ServerProvider()
    provider.start({ runId: 1 } as never)
    // 256 字节一帧（AudioWorklet 一个渲染块），25 帧正好 6400 字节 = 200 ms
    for (let i = 0; i < 24; i++) provider.sendAudio(frame(256, i))
    expect(wsMock.sent).toHaveLength(0)
    provider.sendAudio(frame(256, 24))
    expect(wsMock.sent).toHaveLength(1)
    const first = wsMock.sent[0]
    expect(first.kind).toBe('audio')
    if (first.kind === 'audio') {
      expect(first.bytes).toHaveLength(6400)
      expect(first.bytes[0]).toBe(0)
      expect(first.bytes[256]).toBe(1)
      expect(first.bytes[6399]).toBe(24)
    }
  })

  it('flushes the tail before stop', () => {
    const provider = new ServerProvider()
    provider.start({ runId: 1 } as never)
    provider.sendAudio(frame(256, 7))
    provider.sendAudio(frame(256, 8))
    provider.stop()
    expect(wsMock.sent.map((e) => e.kind)).toEqual(['audio', 'stop'])
    const tail = wsMock.sent[0]
    if (tail.kind === 'audio') expect(tail.bytes).toHaveLength(512)
  })

  it('drops pending audio on cancel and on a new start', () => {
    const provider = new ServerProvider()
    provider.start({ runId: 1 } as never)
    provider.sendAudio(frame(256, 1))
    provider.cancel()
    provider.start({ runId: 2 } as never)
    provider.sendAudio(frame(256, 2))
    provider.stop()
    const audio = wsMock.sent.filter((e) => e.kind === 'audio')
    expect(audio).toHaveLength(1)
    if (audio[0].kind === 'audio') expect(audio[0].bytes.every((b) => b === 2)).toBe(true)
  })
})
