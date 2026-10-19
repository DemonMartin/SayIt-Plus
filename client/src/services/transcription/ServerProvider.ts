// 服务器模式 Provider：服务器负责 ASR，AI 可由服务器或客户端自定义供应商完成。

import { getSetting } from '../store'
import { addRuntimeEvent } from '../debugLog'
import * as ws from '../websocket'
import { polishWithClientAi } from './clientAiPolish'
import {
  resolveAiPolicy,
  resolveAndLogAiOutcome,
  type AiOutcomeContext,
  type AiPolicy,
} from './aiPolicy'
import {
  getRuntimeServerAiSource,
  loadServerAiSource,
  type ServerAiSource,
} from './serverAiSource'
import { MID_SESSION_DISCONNECT_ERROR } from './types'
import type {
  FinalResult,
  TranscriptionProvider,
  TranscriptionCallbacks,
  StartOptions,
  StopOptions,
} from './types'

/**
 * 攒够这么多字节才发一个 WebSocket 包：16 kHz、16 bit 单声道下正好 200 ms。
 *
 * 采集端每 8 ms 左右给一帧（AudioWorklet 一个渲染块），原来收到就发，每秒 125 个
 * 256 字节的小包，回落路径更碎（每秒约 375 个 85 字节）。服务端松键之前只是把音频攒着，
 * 没有流式结果，所以合批不影响识别结果和速度；云 API 模式一直就是 200 ms 一批。
 * 松键时剩下的不足 200 ms 一定在 stop 之前发掉（见 flushAudio）。
 */
const SEND_BATCH_BYTES = 6400

export class ServerProvider implements TranscriptionProvider {
  readonly mode = 'server' as const

  private callbacks: TranscriptionCallbacks = {}
  private activeRunId = 0
  private activeStartOpts: Readonly<StartOptions> | undefined
  private aiSource: ServerAiSource = 'managed'
  private customAiReady = false
  private customFinalPending = false
  private serverDonePending = false
  /** 松键时随 stop 传入的本次策略；与线上那个 disable_ai 布尔同出一源。 */
  private activePolicy: AiPolicy | undefined
  private activeConnectionId: string | undefined
  private pendingAudio: ArrayBuffer[] = []
  private pendingAudioBytes = 0

  async connect(callbacks: TranscriptionCallbacks): Promise<void> {
    this.callbacks = callbacks
    const [storedSource, customProvider, customUrl, customKey, customModel] = await Promise.all([
      loadServerAiSource(),
      getSetting('cloudAi.provider', '') as Promise<string>,
      getSetting('cloudAi.apiUrl', '') as Promise<string>,
      getSetting('cloudAi.apiKey', '') as Promise<string>,
      getSetting('cloudAi.model', '') as Promise<string>,
    ])
    this.aiSource = storedSource === 'custom' ? 'custom' : 'managed'
    this.customAiReady = Boolean(
      customUrl.trim()
      && customModel.trim()
      && (customKey.trim() || customProvider === 'ollama'),
    )

    await ws.connect({
      onStateChange: (state) => {
        callbacks.onStateChange?.(state)
        // 连接在一次录音进行中掉了：必须当场报出去。
        //
        // 以前这里只是转发状态、没人处理，于是录音继续跑（音频被 sendAudio 静静丢掉），
        // 停止时 sendStop 也发不出去，录音器照旧进入 45 秒超时等待 —— 用户看着悬浮条转
        // 一分钟，最后什么提示都没有。实测 2026-09-07 两段长录音都是这么没的。
        if (state !== 'disconnected' && state !== 'error') return
        const runId = this.activeRunId
        if (runId === 0) return
        addRuntimeEvent('error', 'server', 'Connection dropped during an active recording', {
          runId,
          state,
        })
        this.resetRun()
        callbacks.onError?.(MID_SESSION_DISCONNECT_ERROR)
      },
      onReady: (data) => {
        // 留着服务端连接标识：ai.outcome 带上它，才能把客户端日志和服务器日志
        // （每行有 cid=/sid=）直接对起来，不必再靠时间戳手工对齐。
        this.activeConnectionId = data.connectionId
        callbacks.onReady?.({
          connectionId: data.connectionId,
          asr: data.asr,
          // 自定义 AI 在本机执行；服务端 llm=false 不应把整条模式标成不可用。
          llm: this.aiSource === 'custom' ? this.customAiReady : data.llm,
        })
      },
      onASR: (result) => {
        if (this.activeRunId === 0) return
        const isEmpty = !result.text.trim()
        callbacks.onASR?.({
          text: result.text,
          asrMs: result.asrMs,
          durationSec: result.durationSec,
          // 空识别不会执行任何 AI；它会在录音器的专用空结果分支先于 final 落库。
          aiSource: isEmpty ? 'none' : undefined,
          aiStatus: isEmpty ? 'skipped' : undefined,
        })
      },
      onFinal: (result) => {
        const runId = this.activeRunId
        if (runId === 0) return
        if (this.aiSource === 'custom') {
          this.customFinalPending = true
          void this.handleCustomFinal(runId, result)
          return
        }

        // 服务器内置 AI 路线。此前这里按 `llmMs > 0` 反推成功失败 —— 服务端异常时
        // 返回原文 + 0ms，于是"调用失败"被误记成"不可用"，两者在界面和日志里长得一样。
        // 现在用服务端回传的证据判断（error / provider），耗时只作辅助。
        const policy = this.resolvePolicy(result.durationSec)
        const outcome = resolveAndLogAiOutcome(this.outcomeContext(), policy, {
          asrTextEmpty: !result.asrText.trim(),
          serverError: result.serverAi?.error,
          serverProvider: result.serverAi?.provider,
          llmMs: result.llmMs,
        })
        callbacks.onFinal?.({
          asrText: result.asrText,
          // 仍原样转发服务端的 llm_text：跳过/失败时服务端本来就回原文，而选区编辑的
          // 安全回退由录音器按 contextApplied 处理。这里改写它会动到那条路径。
          llmText: result.llmText,
          asrMs: result.asrMs,
          llmMs: result.llmMs,
          durationSec: result.durationSec,
          asrEngine: result.asrEngine,
          asrModel: result.asrModel,
          contextApplied: result.contextApplied,
          aiSource: outcome.source,
          aiStatus: outcome.status,
          aiProvider: outcome.provider,
        })
      },
      onDone: () => {
        const runId = this.activeRunId
        if (runId === 0) return
        if (this.customFinalPending) {
          // 服务端 final 后会立刻发 done；客户端 AI 尚未完成时必须暂存，避免录音器先收尾。
          this.serverDonePending = true
          return
        }
        this.finishRun(runId)
      },
      onError: (msg) => {
        const runId = this.activeRunId
        if (runId === 0) return
        callbacks.onError?.(msg)
        if (this.activeRunId === runId) this.resetRun()
      },
    })
  }

  start(opts: StartOptions): boolean {
    // 选区模型只覆盖本次录音；普通录音沿用开始时冻结的来源或当前运行时设置。
    this.aiSource = opts.aiConfig?.selectionAiOverride
      ? 'custom'
      : opts.aiConfig?.serverAiSource ?? getRuntimeServerAiSource()
    this.activeRunId = opts.runId
    this.activeStartOpts = {
      ...opts,
      hotwords: opts.hotwords ? [...opts.hotwords] : undefined,
      textContext: opts.textContext ? { ...opts.textContext } : undefined,
    }
    this.customFinalPending = false
    this.serverDonePending = false
    this.dropPendingAudio()

    const wireOptions = this.aiSource === 'custom'
      ? {
        ...opts,
        // 自定义 AI 由本机调用：服务端只做 ASR，也不需要收到 Prompt 或编辑器正文。
        disableAi: true,
        selectionAiConfig: undefined,
        systemPrompt: undefined,
        textContext: undefined,
      }
      : opts
    const started = ws.sendStart(wireOptions)
    if (!started) this.resetRun()
    return started
  }

  cancel(): void {
    this.resetRun()
    ws.disconnect()
  }

  sendAudio(buffer: ArrayBuffer): void {
    this.pendingAudio.push(buffer)
    this.pendingAudioBytes += buffer.byteLength
    if (this.pendingAudioBytes >= SEND_BATCH_BYTES) this.flushAudio()
  }

  private flushAudio(): void {
    if (this.pendingAudio.length === 0) return
    const chunks = this.pendingAudio
    const total = this.pendingAudioBytes
    this.pendingAudio = []
    this.pendingAudioBytes = 0
    if (chunks.length === 1) {
      ws.sendAudio(chunks[0])
      return
    }
    const merged = new Uint8Array(total)
    let offset = 0
    for (const chunk of chunks) {
      merged.set(new Uint8Array(chunk), offset)
      offset += chunk.byteLength
    }
    ws.sendAudio(merged.buffer)
  }

  private dropPendingAudio(): void {
    this.pendingAudio = []
    this.pendingAudioBytes = 0
  }

  stop(opts?: StopOptions): boolean {
    // 留住完整策略，别再把它压成 activeStartOpts.disableAi —— 那样一来"路由是自配"
    // 和"低于门槛"就都变成同一个布尔，结束时再也分不出这次为什么没整理。
    if (opts?.aiPolicy) this.activePolicy = opts.aiPolicy
    // 尾巴必须先于 stop 发出：服务端收到 stop 就开始识别，之后到的音频不算数。
    this.flushAudio()
    return ws.sendStop(this.aiSource === 'custom' ? { ...opts, disableAi: true } : opts)
  }

  /**
   * 取本次策略。正常路径由 stop 传入；缺失时（理论上不该发生）用 start 冻结的配置
   * 快照就地重算一份，仍然走同一个判据函数，绝不退回旧的布尔推断。
   */
  private resolvePolicy(durationSec: number): AiPolicy {
    if (this.activePolicy) return this.activePolicy
    const snapshot = this.activeStartOpts?.aiConfig
    const fallback = resolveAiPolicy({
      workMode: 'server',
      aiEnabled: snapshot?.aiEnabled ?? !(this.activeStartOpts?.disableAi ?? false),
      aiMinDurationSec: snapshot?.aiMinDurationSec ?? this.activeStartOpts?.aiMinDurationSec ?? 0,
      serverAiSource: snapshot?.serverAiSource ?? this.aiSource,
      selectionAiOverride: snapshot?.selectionAiOverride,
      audioDurationSec: durationSec,
    })
    this.activePolicy = fallback
    return fallback
  }

  private outcomeContext(): AiOutcomeContext {
    return {
      operationId: this.activeStartOpts?.operationId || `run-${this.activeRunId}`,
      trigger: this.activeStartOpts?.source === 'history_reprocess' ? 'history_reprocess' : 'live',
      serverRef: this.activeConnectionId,
    }
  }

  disconnect(): void {
    this.resetRun()
    ws.disconnect()
  }

  isReady(): boolean {
    return ws.isConnected()
  }

  private async handleCustomFinal(runId: number, result: FinalResult): Promise<void> {
    const startOptions = this.activeStartOpts
    // 自配路线：服务端的 final 只代表识别完成，整次整理还没结束，最终状态必须取
    // 客户端润色的结果，不能被服务端的 llm_ms=0 覆盖。
    const polished = await polishWithClientAi({
      asrText: result.asrText,
      startOptions,
      policy: this.resolvePolicy(result.durationSec),
      outcomeContext: this.outcomeContext(),
      logSource: 'server',
      isCurrent: () => this.activeRunId === runId,
    })
    if (!polished || this.activeRunId !== runId) return

    this.callbacks.onFinal?.({
      ...result,
      ...polished,
    })
    this.customFinalPending = false

    if (this.serverDonePending) {
      this.finishRun(runId)
    }
  }

  private finishRun(runId: number): void {
    if (this.activeRunId !== runId) return
    this.callbacks.onDone?.()
    if (this.activeRunId === runId) this.resetRun()
  }

  private resetRun(): void {
    this.dropPendingAudio()
    if (this.activeRunId !== 0 && this.customFinalPending) {
      addRuntimeEvent('info', 'server', 'Discarded pending custom AI result for stale run', {
        runId: this.activeRunId,
      })
    }
    this.activeRunId = 0
    this.activeStartOpts = undefined
    this.activePolicy = undefined
    this.customFinalPending = false
    this.serverDonePending = false
  }
}
