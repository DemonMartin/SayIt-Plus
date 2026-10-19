import type { PromptPreset } from '../store'
import { CONTEXT_SELECTION_EDIT_PROMPT } from '../contextAware'
import type { AppPromptRule } from '../personalization/types'

/**
 * 录音器每次录音要读的设置。由 refreshRuntimeSettings 从存储整份读回，
 * 设置页的快速开关（AI 整理、润色模式、热词、流式显示）直接改这里，不必全量刷新。
 */
export interface RecordingSettings {
  micId: string
  /**
   * 是否让浏览器对麦克风做降噪。默认开（底噪大的机器需要），
   * 但降噪是按"人听得舒服"优化的，可能削掉 ASR 要的细节，所以可关。
   */
  noiseSuppression: boolean
  /** 是否在录音期间静音系统输出（防外放被麦克风回采）。默认关闭。 */
  muteSystemAudio: boolean
  /** 插入文本后是否自动还原剪贴板为插入前内容。默认开启。 */
  protectClipboard: boolean
  presets: PromptPreset[]
  activePresetId: string
  aiEnabled: boolean
  /** 0 = 所有语音都用 AI；正数 = 达到门槛才调用 AI。 */
  aiMinDurationSec: number
  /** Reads bounded editor text only when explicitly enabled; default is false. */
  contextAwareWriting: boolean
  /** User-visible selection-edit prompt. */
  contextSelectionEditPrompt: string
  appPromptRules: AppPromptRule[]
  hotwords: string[]
  /** 是否把热词注入 AI 提示词（默认关，用户可在热词页开启）。 */
  injectHotwords: boolean
  /** 服务器识别语言；空串 = 自动。 */
  language: string
  /** 是否开启流式实时显示（识别过程中把中间结果实时显示在悬浮窗）。默认关闭。 */
  streamingDisplay: boolean
}

/** 第一次读回存储之前用的值。 */
export function defaultRecordingSettings(): RecordingSettings {
  return {
    micId: '',
    noiseSuppression: true,
    muteSystemAudio: false,
    protectClipboard: true,
    presets: [],
    activePresetId: 'intent',
    aiEnabled: true,
    aiMinDurationSec: 0,
    contextAwareWriting: false,
    contextSelectionEditPrompt: CONTEXT_SELECTION_EDIT_PROMPT,
    appPromptRules: [],
    hotwords: [],
    injectHotwords: false,
    language: '',
    streamingDisplay: false,
  }
}
