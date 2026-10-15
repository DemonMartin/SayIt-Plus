import { withLegacyServerTextContext } from '../contextAware'
import { getWSUrl } from '../runtimeConfig'
import { polishWithClientAi } from './clientAiPolish'
import { extractServerAiEvidence, policyFromSnapshot, resolveAndLogAiOutcome, serverShouldPolish } from './aiPolicy'
import type { FinalResult, StartOptions } from './types'

/** A private connection for recorded audio, independent of the live provider socket. */
export async function transcribeWithServer(
  chunk: ArrayBuffer,
  options: StartOptions,
  signal?: AbortSignal,
  wsUrl = getWSUrl(),
): Promise<FinalResult | null> {
  const ai = {
    snapshot: options.aiConfig,
    context: {
      operationId: options.operationId || `server-${options.runId}`,
      trigger: options.source === 'history_reprocess' ? ('history_reprocess' as const) : ('live' as const),
    },
  }
  const { systemPrompt, clientMeta } = options
  const hotwords = options.hotwords ?? []
  if (signal?.aborted) return null
  const audioDurationSec = chunk.byteLength / 2 / 16000
  const policy = policyFromSnapshot(ai.snapshot, 'server', audioDurationSec)
  const useManagedAi = serverShouldPolish(policy)
  const useCustomAi = policy.allowCall && policy.route === 'custom'

  const serverResult = await new Promise<FinalResult>((resolve, reject) => {
    const socket = new WebSocket(wsUrl)
    const timeout = window.setTimeout(() => fail(new Error('Server transcription timed out')), 60_000)
    socket.binaryType = 'arraybuffer'

    let resolved = false
    let started = false
    let legacyContext = false
    const cleanup = () => {
      clearTimeout(timeout)
      signal?.removeEventListener('abort', abort)
      socket.close()
    }
    const fail = (error: Error) => {
      if (resolved) return
      resolved = true
      cleanup()
      reject(error)
    }
    const abort = () => fail(new Error('Transcription cancelled'))
    signal?.addEventListener('abort', abort, { once: true })

    const sendRecording = (supportsTextContext: boolean) => {
      if (resolved || started) return
      started = true
      legacyContext = Boolean(useManagedAi && options.textContext && !supportsTextContext)
      const startMsg: Record<string, unknown> = {
        cmd: 'start',
        source: options.source || 'live',
        disable_ai: !useManagedAi,
      }
      if (useManagedAi && systemPrompt) {
        startMsg.system_prompt =
          legacyContext && options.textContext
            ? withLegacyServerTextContext(systemPrompt, options.textContext)
            : systemPrompt
      }
      if (useManagedAi && options.textContext) {
        startMsg.text_context = {
          source: options.textContext.source,
          text_before: options.textContext.textBefore,
          selected_text: options.textContext.selectedText,
          text_after: options.textContext.textAfter,
          selection_truncated: options.textContext.selectionTruncated,
        }
      }
      if (options.appContext) {
        startMsg.app_context = {
          process_name: options.appContext.processName,
          exe_path: options.appContext.exePath,
          window_class: options.appContext.windowClass,
          focus_class: options.appContext.focusClass,
          control_type: options.appContext.controlType,
        }
      }
      if (clientMeta) {
        startMsg.client_meta = {
          user_id: clientMeta.userId,
          device_id: clientMeta.deviceId,
          hostname: clientMeta.hostname,
          client_version: clientMeta.clientVersion,
          platform: clientMeta.platform,
          os_version: clientMeta.osVersion,
          local_ip: clientMeta.localIp,
          system_locale: clientMeta.systemLocale,
          cpu_cores: clientMeta.cpuCores,
          memory_mb: clientMeta.memoryMb,
        }
      }
      if (options.language) startMsg.language = options.language
      if (hotwords.length > 0) startMsg.hotwords = hotwords
      socket.send(JSON.stringify(startMsg))

      // 分片发送 PCM 数据
      const CHUNK_SIZE = 32000
      const totalBytes = chunk.byteLength
      for (let offset = 0; offset < totalBytes; offset += CHUNK_SIZE) {
        const end = Math.min(offset + CHUNK_SIZE, totalBytes)
        socket.send(chunk.slice(offset, end))
      }

      socket.send(JSON.stringify({ cmd: 'stop' }))
    }

    socket.onmessage = (ev) => {
      if (resolved || typeof ev.data !== 'string') return
      let msg
      try {
        msg = JSON.parse(ev.data)
      } catch {
        return
      }
      try {
        if (msg.type === 'ready') {
          sendRecording(msg.capabilities?.text_context === true)
        } else if (msg.type === 'final') {
          resolved = true
          cleanup()
          resolve({
            asrText: msg.asr_text || '',
            llmText: msg.llm_text || '',
            asrMs: msg.asr_ms || 0,
            llmMs: msg.llm_ms || 0,
            durationSec: Number(msg.duration_sec || audioDurationSec),
            contextApplied:
              typeof msg.context_applied === 'boolean'
                ? msg.context_applied
                : legacyContext && msg.llm_debug?.provider
                  ? true
                  : undefined,
            asrEngine: msg.asr_engine || undefined,
            asrModel: msg.asr_model || undefined,
            // 与实时路径同一个提取函数：重跑此前完全不看执行证据，于是服务端
            // 调用失败在重跑后的记录里和"没调用"分不开。
            serverAi: extractServerAiEvidence(msg.llm_debug),
          })
        } else if (msg.type === 'done' && !resolved) {
          // 没有 final 就 done 了（后端判定为静音/无结果）
          resolved = true
          cleanup()
          resolve({ asrText: '', llmText: '', asrMs: 0, llmMs: 0, durationSec: 0 })
        } else if (msg.type === 'error') {
          resolved = true
          cleanup()
          reject(new Error(msg.message || 'backend error'))
        }
      } catch (error) {
        fail(error instanceof Error ? error : new Error(String(error)))
      }
    }

    socket.onerror = () => fail(new Error('WebSocket connection error'))
    socket.onclose = (event) => fail(new Error(`WebSocket closed unexpectedly, code=${event.code}`))
  })

  if (signal?.aborted) return null
  if (!useCustomAi || !serverResult.asrText.trim()) {
    // 内置 AI 路线（含空识别、总开关关闭、低于门槛）：结论一律由共用判据给出，
    // 不再拿 llmMs > 0 反推成功失败。
    const outcome = resolveAndLogAiOutcome(ai.context, policy, {
      asrTextEmpty: !serverResult.asrText.trim(),
      serverError: serverResult.serverAi?.error,
      serverProvider: serverResult.serverAi?.provider,
      llmMs: serverResult.llmMs,
    })
    return {
      ...serverResult,
      ...(!serverResult.asrText.trim() ? { llmText: '' } : {}),
      aiSource: outcome.source,
      aiStatus: outcome.status,
      aiReason: outcome.reason,
      aiProvider: outcome.provider,
      aiModel: outcome.model,
    }
  }

  const polished = await polishWithClientAi({
    asrText: serverResult.asrText,
    startOptions: options,
    policy,
    outcomeContext: ai.context,
    logSource: 'server',
    isCurrent: () => !signal?.aborted,
  })
  if (signal?.aborted) return null
  return polished ? { ...serverResult, ...polished } : serverResult
}
