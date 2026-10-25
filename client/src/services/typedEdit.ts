import * as bridge from './bridge'
import { getSetting } from './store'
import { normalizeContextSelectionEditPrompt, usableTextContext } from './contextAware'
import { loadSelectionAiConfig } from './transcription/selectionAi'
import { isClientAiConfigComplete, loadClientAiConfig } from './transcription/clientAiPolish'
import type { ActiveAppContext, TextContext } from '@/types/appContext'
import type { ClientAiConfig } from './transcription/selectionAi'
import { PasteService, type ProbeResult } from './recorder/PasteService'
import { InsertionController } from './recorder/InsertionController'
import type { OverlayService } from './recorder/OverlayService'
import { t } from '@/i18n'
import { setTypedEditInputActive } from './webviewKeyboardFallback'
import { addRuntimeEvent } from './debugLog'

interface EditSession {
  token: number
  context?: TextContext
  probe?: ProbeResult
  config?: ClientAiConfig
  prompt?: string
  busy: boolean
}

export interface TypedEditPayload {
  token: number
  text?: string
  improveFirst?: boolean
}

/** Text-only selection editing. No microphone, ASR provider, or model preload is involved. */
export class TypedEditService {
  private session: EditSession | null = null
  private sequence = Date.now()
  private keepAlive: ReturnType<typeof setInterval> | undefined
  private readonly insertion = new InsertionController(new PasteService())

  constructor(
    private readonly overlay: OverlayService,
    private readonly showResult: (text: string, reason: string, token: number) => void,
  ) {}

  get active() {
    return this.session !== null
  }

  async open() {
    if (this.session) return
    const session: EditSession = { token: ++this.sequence, busy: false }
    this.session = session
    try {
      // Capture before showing a focusable window, including when context-aware dictation is off.
      const capture = await bridge.getRecordingContext(true)
      if (this.session !== session) return
      const context = usableTextContext((capture.appContext as ActiveAppContext).textContext)
      if (!context?.selectedText) throw new Error(t('typedEdit.selectText'))
      session.context = context
      session.probe = capture.probe as unknown as ProbeResult
      const [customConfig, defaultConfig, prompt] = await Promise.all([
        loadSelectionAiConfig(context),
        loadClientAiConfig(),
        getSetting('contextSelectionEditPrompt', ''),
      ])
      if (this.session !== session) return
      session.config = customConfig ?? defaultConfig
      session.prompt = normalizeContextSelectionEditPrompt(prompt)
      if (!isClientAiConfigComplete(session.config)) throw new Error(t('typedEdit.configureAi'))
      await bridge.setTypedEditActive(true)
      if (this.session !== session) {
        if (!this.session) await bridge.setTypedEditActive(false)
        return
      }
      this.keepAlive = setInterval(() => {
        void bridge.setTypedEditActive(true).catch(() => {})
      }, 8000)
      setTypedEditInputActive(true)
      await this.overlay.showTypedEdit(session.token)
    } catch (error) {
      if (this.session !== session) return
      this.finish()
      this.overlay.showError(error instanceof Error ? error.message : t('typedEdit.failed'))
    }
  }

  cancel(token: number) {
    if (this.session?.token !== token) return
    this.finish()
    this.overlay.hide()
  }

  async submit({ token, text, improveFirst }: TypedEditPayload) {
    const session = this.session
    const instruction = typeof text === 'string' ? text.trim().slice(0, 6000) : ''
    if (
      !session ||
      session.token !== token ||
      session.busy ||
      !instruction ||
      !session.context ||
      !session.config ||
      !session.prompt
    )
      return
    session.busy = true
    const current = () => this.session === session
    let completedText = ''
    try {
      await this.overlay.updateTypedEdit(token, improveFirst ? 'improving' : 'applying')
      if (!current()) return
      let finalInstruction = instruction
      if (improveFirst) {
        // The same contextual prompt edits the instruction as selected text in the first pass.
        const instructionContext: TextContext = {
          source: 'typed_instruction',
          textBefore: '',
          textAfter: '',
          selectedText: instruction,
          selectionTruncated: false,
        }
        // i18n-allow-start: model instruction, not UI copy
        finalInstruction = await this.edit(
          session,
          'Correct and improve this editing instruction. Make its goal and wording clear. Preserve its language, intent, and constraints. Return only the improved instruction; do not carry out its task.',
          instructionContext,
        )
        // i18n-allow-end
        if (!current()) return
        await this.overlay.updateTypedEdit(token, 'applying')
        if (!current()) return
      }
      const result = await this.edit(session, finalInstruction, session.context)
      if (!current()) return
      completedText = result
      const protectClipboard = await getSetting('protectClipboard', true)
      if (!current()) return
      const outcome = await this.insertion.insert(result, {
        runId: token,
        probeResult: session.probe ?? null,
        finalReceivedAt: Date.now(),
        protectClipboard: Boolean(protectClipboard),
        isCurrent: current,
        waitForModifierRelease: async () => {
          for (let attempt = 0; attempt < 40; attempt++) {
            const down = await bridge.getPTTPhysicalKeyStates([
              'ShiftLeft',
              'ShiftRight',
              'ControlLeft',
              'ControlRight',
              'AltLeft',
              'AltRight',
              'Enter',
            ])
            if (!current() || !down.some(Boolean)) return
            await new Promise((resolve) => setTimeout(resolve, 25))
          }
          throw new Error(t('typedEdit.releaseKeys'))
        },
        // Keep Escape available in the input until the final handoff to the captured target.
        beforeIrreversiblePaste: async () => { await bridge.hideOverlay() },
      })
      if (!current()) return
      this.finish()
      if (outcome.kind === 'fallback') this.showResult(result, outcome.reason, token)
    } catch {
      if (!current()) return
      addRuntimeEvent('warn', 'typed-edit', 'Typed edit did not complete', {
        stage: completedText ? 'insertion' : 'editing',
        provider: session.config.provider,
        model: session.config.model,
      })
      if (completedText) {
        this.finish()
        this.showResult(completedText, 'paste_failed', token)
        return
      }
      session.busy = false
      await this.overlay.updateTypedEdit(token, 'idle', t('typedEdit.failed'))
    }
  }

  private async edit(session: EditSession, text: string, context: TextContext): Promise<string> {
    const config = session.config!
    let timer: ReturnType<typeof setTimeout> | undefined
    try {
      const result = await Promise.race([
        bridge.invoke<{ text: string }>('cloud_polish', {
          request: {
            text,
            ai_config: {
              provider: config.provider,
              api_url: config.apiUrl,
              api_key: config.apiKey,
              model: config.model,
            },
            system_prompt: session.prompt,
            text_context: context,
          },
        }),
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new Error('Typed edit timed out')), 60_000)
        }),
      ])
      if (!result.text?.trim()) throw new Error('Empty editing result')
      return result.text
    } finally {
      if (timer) clearTimeout(timer)
    }
  }

  dispose() {
    this.finish()
  }

  private finish() {
    this.session = null
    clearInterval(this.keepAlive)
    this.keepAlive = undefined
    setTypedEditInputActive(false)
    void bridge.setTypedEditActive(false).catch(() => {})
  }
}
