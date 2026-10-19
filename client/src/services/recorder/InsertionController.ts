import { addRuntimeEvent } from '../debugLog'
import { isUnconfirmedPaste } from './helpers'
import type { PasteService, ProbeResult } from './PasteService'

/** 插字的结果。stale = 这一代中途失效了，调用方什么都不用做。 */
export type InsertionOutcome =
  | { kind: 'stale' }
  | { kind: 'inserted' }
  | { kind: 'fallback'; reason: string }

const STALE: InsertionOutcome = { kind: 'stale' }

export interface InsertionOptions {
  runId: number
  /** 录音开始时拍的目标窗口快照；没有才现查（现查拿到的可能已经是别的窗口）。 */
  probeResult: ProbeResult | null
  finalReceivedAt: number
  protectClipboard: boolean
  /** 每次 await 之后都要验：这一代还是不是当前代。 */
  isCurrent: () => boolean
  waitForModifierRelease: () => Promise<void>
  /** 真正粘贴之前的最后一步（关掉 Esc 取消）。粘贴一旦发出就撤不回来了。 */
  beforeIrreversiblePaste: () => Promise<void>
}

/**
 * 把识别出的文字送进目标窗口：拿探测结果、判断能不能写、等修饰键松开、粘贴、给结果分类。
 * 从 RecorderOrchestrator.handleTextInsertion 拆出（模块 6），行为不变；
 * 根据结果改状态、弹卡片仍由编排器做。
 */
export class InsertionController {
  constructor(private readonly paste: PasteService) {}

  async insert(text: string, opts: InsertionOptions): Promise<InsertionOutcome> {
    const insertionStartedAt = Date.now()
    const { runId } = opts
    // 始终使用本次 final 携带的 probe 快照，不能读取可能已被下一次录音覆盖的实例字段。
    const capturedProbe = opts.probeResult
    const probe = capturedProbe ?? await this.paste.getProbeResult()
    if (!opts.isCurrent()) return STALE

    const usedCapturedProbe = capturedProbe !== null
    const probeAgeMs = typeof probe.completedAt === 'number' ? Date.now() - probe.completedAt : undefined
    const probeDurationMs = (
      typeof probe.completedAt === 'number'
      && typeof probe.startedAt === 'number'
    )
      ? probe.completedAt - probe.startedAt
      : undefined
    addRuntimeEvent('info', 'recorder', 'Paste decision', {
      runId,
      probeId: probe.probeId,
      editable: probe.editable,
      gate: probe.gate,
      hwnd: probe.hwnd,
      focusHwnd: probe.focusHwnd,
      pid: probe.pid,
      process: probe.process,
      verdict: probe.verdict,
      isCurrentAppProcess: probe.isCurrentAppProcess,
      windowClass: probe.windowClass,
      focusClass: probe.focusClass,
      usedCapturedProbe,
      probeAgeMs,
      probeDurationMs,
      finalToDecisionMs: opts.finalReceivedAt > 0 ? insertionStartedAt - opts.finalReceivedAt : undefined,
      detail: probe.detail,
      textLen: text.length,
    })

    if (probe.isCurrentAppProcess) {
      // SayIt 自身也是 Chromium 窗口，renderer 直插对 React 受控组件无效
      // （DOM value 被设置但 React state 不同步，下次 re-render 会覆盖）。
      // 所以不走 renderer 直插，而是和外部窗口一样走 Rust paste（Ctrl+V）。
      addRuntimeEvent('info', 'recorder', 'Target is SayIt; using native paste instead of renderer insertion', {
        probeId: probe.probeId,
        editable: probe.editable,
        hwnd: probe.hwnd,
        focusHwnd: probe.focusHwnd,
      })
    }

    if (!probe.editable) {
      // 目标不可编辑时只展示主动操作卡片，不提前改写用户剪贴板。
      addRuntimeEvent('info', 'recorder', 'Target is not editable; showing fallback card', {
        probeId: probe.probeId,
        pid: probe.pid,
        process: probe.process,
        verdict: probe.verdict,
        gate: probe.gate,
        isCurrentAppProcess: probe.isCurrentAppProcess,
        detail: probe.detail,
      })
      if (!opts.isCurrent()) return STALE
      return { kind: 'fallback', reason: 'not_editable' }
    }

    await opts.waitForModifierRelease()
    if (!opts.isCurrent()) return STALE

    await opts.beforeIrreversiblePaste()
    if (!opts.isCurrent()) return STALE

    // Target was editable → paste (pass the captured probe so Rust uses the original hwnd)
    const pasteStartedAt = Date.now()
    const result = await this.paste.pasteText(text, probe, opts.protectClipboard)
    if (!opts.isCurrent()) return STALE
    const pasteExecMs = Date.now() - pasteStartedAt

    // 报成功但拿不准的：交给兜底卡片，文字可能已经进去了，也可能没有（判据见 isUnconfirmedPaste）。
    if (result.ok && isUnconfirmedPaste(result.strategy, pasteExecMs)) {
      addRuntimeEvent('warn', 'recorder', 'External text insertion unconfirmed; showing fallback card', {
        strategy: result.strategy,
        gate: probe.gate,
        detail: result.detail,
        finalToPasteDoneMs: opts.finalReceivedAt > 0 ? Date.now() - opts.finalReceivedAt : undefined,
        pasteExecMs,
      })
      return { kind: 'fallback', reason: 'paste_unconfirmed' }
    }

    if (result.ok) {
      addRuntimeEvent('info', 'recorder', 'External text insertion succeeded', {
        strategy: result.strategy,
        // 成功侧也要留 gate：SendInput 返回成功并不代表文本真的落进了输入框
        // （UIPI 拦截是静默的），用户报「显示成功但没插进去」时，这一个字段就能
        // 说明当初是靠哪一层判据放行的，不必再让他复现一次。
        gate: probe.gate,
        detail: result.detail,
        attempts: result.attempts,
        finalToPasteDoneMs: opts.finalReceivedAt > 0 ? Date.now() - opts.finalReceivedAt : undefined,
        pasteExecMs,
      })

      return { kind: 'inserted' }
    }

    // Paste command failed (SendInput error, timeout, etc.). The card now lets the user
    // explicitly copy or dismiss; do not overwrite the clipboard automatically here.
    const level = result.reason === 'paste_exception' ? 'error' : 'warn'
    addRuntimeEvent(level, 'recorder', 'External text insertion failed; showing fallback card', {
      strategy: result.strategy,
      reason: result.reason,
      gate: probe.gate,
      detail: result.detail,
      attempts: result.attempts,
      finalToPasteDoneMs: opts.finalReceivedAt > 0 ? Date.now() - opts.finalReceivedAt : undefined,
      pasteExecMs: Date.now() - pasteStartedAt,
    })
    return { kind: 'fallback', reason: result.reason || 'paste_failed' }
  }
}
