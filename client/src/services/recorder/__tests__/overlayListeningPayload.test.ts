import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  updateOverlay: vi.fn(),
  presentOverlay: vi.fn(),
}))

vi.mock('../../bridge', () => ({
  updateOverlay: mocks.updateOverlay,
  presentOverlay: mocks.presentOverlay,
  hideOverlay: vi.fn(() => Promise.resolve()),
  setEscapeActionMode: vi.fn(() => Promise.resolve()),
  copyText: vi.fn(() => Promise.resolve()),
}))
vi.mock('../../debugLog', () => ({ addRuntimeEvent: vi.fn() }))
vi.mock('../../store', () => ({
  getSetting: vi.fn((_key: string, fallback: unknown) => Promise.resolve(fallback)),
}))
vi.mock('@/i18n', () => ({
  getLocale: () => 'zh-CN',
  t: (key: string) => key,
}))

// vi.mock 会被提升到 import 之前，所以这里用普通 import 也拿到的是上面那些替身。
// 刻意不用 `await import()`：顶层 await 会让 vitest 在收集阶段丢掉 describe 的上下文
// （报 "Cannot read properties of undefined (reading 'config')"，看着像配置坏了）。
import { OverlayService } from '../OverlayService'

type Payload = Record<string, unknown>

function payloadsFrom(): Payload[] {
  return mocks.updateOverlay.mock.calls.map((call) => call[0] as Payload)
}

/**
 * 2026-09「开着实时字幕、不说话时悬浮窗一闪一闪」的回归测试。
 *
 * 机制：`streaming` 是原生侧判断悬浮窗布局的唯一依据。低音量警告每 5 秒发一次
 * listening 更新，那几个 payload 各自手写、全都漏了这个字段，于是窗口从流式尺寸
 * 收缩一帧再被下一次心跳撑回去。
 *
 * 所以这里断言的不是"警告能显示"，而是"每一次 listening 更新都带着布局判据" ——
 * 前者在出 bug 的那个版本上也全绿。
 */
describe('listening 更新必须始终携带布局判据', () => {
  const emitters: Array<[string, (service: InstanceType<typeof OverlayService>) => void]> = [
    ['showLowVolumeWarning', (s) => s.showLowVolumeWarning()],
    ['showNoSignalWarning', (s) => s.showNoSignalWarning()],
    ['showMicMutedAlert', (s) => s.showMicMutedAlert()],
    ['clearWarning', (s) => s.clearWarning()],
    ['showTimeoutWarning', (s) => s.showTimeoutWarning()],
    ['pushListeningBars', (s) => s.pushListeningBars([3, 5, 8], true)],
  ]

  beforeEach(() => {
    mocks.updateOverlay.mockClear()
    mocks.presentOverlay.mockClear()
  })

  it.each(emitters)('%s 带上 streaming 与 streamingText', (_name, emit) => {
    const service = new OverlayService(() => 7)
    service.setStreamingActive(true)
    service.setStreamingText('实时字幕中')

    emit(service)

    const payloads = payloadsFrom()
    expect(payloads.length).toBeGreaterThan(0)
    for (const payload of payloads) {
      expect(payload).toMatchObject({
        state: 'listening',
        streaming: true,
        streamingText: '实时字幕中',
      })
    }
  })

  it.each(emitters)('%s 在未开启流式时不硬塞 streaming 字段', (_name, emit) => {
    const service = new OverlayService(() => 7)
    service.setStreamingActive(false)

    emit(service)

    for (const payload of payloadsFrom()) {
      expect(payload.state).toBe('listening')
      expect('streaming' in payload).toBe(false)
    }
  })

  /**
   * 钉的是"走了统一入口"，不只是"这一版字段恰好对"。
   *
   * 差别在于：某个方法哪天被改回手写字面量、当时抄得还挺全，上面那批断言照样全绿，
   * 但它从此不再跟着 listeningPayload 演进 —— 下次往 payload 里加字段又会漏掉它一个。
   * 这条能当场把那种退化照出来。
   */
  it('所有 listening 出口都经由 listeningPayload，而非各自复制一份', () => {
    const service = new OverlayService(() => 7)
    const spy = vi.spyOn(
      service as unknown as { listeningPayload: (overrides?: Record<string, unknown>) => unknown },
      'listeningPayload',
    )
    service.setStreamingActive(true)

    for (const [name, emit] of emitters) {
      spy.mockClear()
      emit(service)
      expect(spy, `${name} 绕过了 listeningPayload`).toHaveBeenCalled()
    }
  })
})

/**
 * 录音时悬浮窗只该收到约 30 次/秒的更新：PCM 在流动时由波形那一路驱动，
 * 定时器只在 33ms 内没人发过时补位。原来两路各自 33ms，合起来接近 60 次/秒。
 */
describe('listening 更新频率', () => {
  beforeEach(() => {
    mocks.updateOverlay.mockClear()
    mocks.presentOverlay.mockClear()
    vi.useFakeTimers()
  })

  it('PCM 流动时定时器不再重复发送', () => {
    const service = new OverlayService(() => 7)
    service.startListeningTicker(0)
    mocks.updateOverlay.mockClear()
    // 一秒钟：每 8ms 一帧 PCM（AudioWorklet 一个渲染块）
    for (let t = 0; t < 1000; t += 8) {
      vi.advanceTimersByTime(8)
      service.pushListeningBars([3, 5, 8])
    }
    const count = mocks.updateOverlay.mock.calls.length
    expect(count).toBeGreaterThanOrEqual(24)
    expect(count).toBeLessThanOrEqual(32)
    service.stopListeningTicker()
    vi.useRealTimers()
  })

  it('没有 PCM 时定时器照样推进计时', () => {
    const service = new OverlayService(() => 7)
    service.startListeningTicker(0)
    mocks.updateOverlay.mockClear()
    vi.advanceTimersByTime(1000)
    expect(mocks.updateOverlay.mock.calls.length).toBeGreaterThanOrEqual(24)
    service.stopListeningTicker()
    vi.useRealTimers()
  })
})

/**
 * 提示音播完必须把 AudioContext 挂起：running 状态的 AudioContext 空闲时也持续耗 CPU，
 * 正式版从第一次录音起一直多耗约 1.8% 个核（2026-10 实测）。
 */
describe('就绪提示音的 AudioContext', () => {
  class FakeOscillator {
    onended: (() => void) | null = null
    frequency = { value: 0 }
    type = ''
    connect() {}
    start() {}
    stop() {}
  }
  class FakeAudioContext {
    static instances: FakeAudioContext[] = []
    state: 'running' | 'suspended' | 'closed' = 'running'
    currentTime = 0
    destination = {}
    oscillators: FakeOscillator[] = []
    suspend = vi.fn(async () => { this.state = 'suspended' })
    resume = vi.fn(async () => { this.state = 'running' })
    constructor() { FakeAudioContext.instances.push(this) }
    createOscillator() { const o = new FakeOscillator(); this.oscillators.push(o); return o }
    createGain() { return { connect() {}, gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} } } }
  }

  beforeEach(() => {
    FakeAudioContext.instances = []
    vi.stubGlobal('AudioContext', FakeAudioContext)
  })

  it('播完挂起，下次播放前恢复，只建一个 AudioContext', async () => {
    const service = new OverlayService(() => 0)
    service.startListeningTicker(0)
    service.stopListeningTicker()
    const ctx = FakeAudioContext.instances[0]
    expect(ctx).toBeDefined()
    ctx.oscillators[0].onended?.()
    expect(ctx.suspend).toHaveBeenCalledTimes(1)
    expect(ctx.state).toBe('suspended')

    service.startListeningTicker(0)
    service.stopListeningTicker()
    await Promise.resolve()
    await Promise.resolve()
    expect(ctx.resume).toHaveBeenCalledTimes(1)
    expect(FakeAudioContext.instances).toHaveLength(1)
    expect(ctx.oscillators).toHaveLength(2)
    vi.unstubAllGlobals()
  })
})
