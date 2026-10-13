import { beforeEach, describe, expect, it, vi } from 'vitest'
import { emptyAsrProfile } from '@/features/settings/asrProviderCatalog'
import { transcribeWithProfile } from '../profileTranscription'

const mocks = vi.hoisted(() => ({ invoke: vi.fn(), polish: vi.fn() }))
vi.mock('@tauri-apps/api/core', () => ({ invoke: (...args: unknown[]) => mocks.invoke(...args) }))
vi.mock('../../bridge', () => ({ notifyAsrCapabilityMaybeChanged: vi.fn() }))
vi.mock('../../debugLog', () => ({ addRuntimeEvent: vi.fn(), AI_LOG_SOURCE: 'ai', AI_EVENT_OUTCOME: 'outcome' }))
vi.mock('../clientAiPolish', () => ({ polishWithClientAi: (...args: unknown[]) => mocks.polish(...args) }))

describe('transcribeWithProfile', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.invoke.mockResolvedValue({ text: 'Hello', elapsed_ms: 12 })
    mocks.polish.mockResolvedValue({ llmText: 'Hello', llmMs: 0, aiSource: 'none', aiStatus: 'skipped' })
  })

  it('uses the chosen profile credentials, endpoint, model and hotwords', async () => {
    const profile = {
      ...emptyAsrProfile('openai_compat'),
      id: 'backup',
      apiUrl: 'https://backup.example/v1/audio/transcriptions',
      apiKey: 'test-backup-key',
      model: 'my-asr',
      protocol: 'transcriptions' as const,
    }
    const result = await transcribeWithProfile(
      profile,
      [new Int16Array(16000).buffer],
      {
        runId: 1,
        hotwords: ['SayIt'],
        aiConfig: { workMode: 'server', aiEnabled: false, aiMinDurationSec: 0, serverAiSource: 'managed' },
      },
      () => true,
    )
    expect(mocks.invoke).toHaveBeenCalledWith('cloud_transcribe', {
      request: expect.objectContaining({
        sample_rate: 16000,
        hotwords: ['SayIt'],
        asr_config: expect.objectContaining({
          api_key: 'test-backup-key',
          app_id: '',
          extra: expect.objectContaining({ model: 'my-asr', baseUrl: profile.apiUrl }),
        }),
      }),
    })
    expect(result).toMatchObject({ asrText: 'Hello', asrModel: 'my-asr', durationSec: 1 })
    expect(mocks.polish.mock.calls[0][0].policy.allowCall).toBe(false)
  })

  it('does not polish or deliver a result after cancellation during ASR', async () => {
    let current = true
    mocks.invoke.mockImplementation(async () => {
      current = false
      return { text: 'late', elapsed_ms: 1 }
    })
    expect(
      await transcribeWithProfile(
        emptyAsrProfile('groq'),
        [new Int16Array(16000).buffer],
        {
          runId: 1,
        },
        () => current,
      ),
    ).toBeNull()
    expect(mocks.polish).not.toHaveBeenCalled()
  })

  it('keeps empty recognition empty when editor text is selected', async () => {
    mocks.invoke.mockResolvedValue({ text: '', elapsed_ms: 12 })
    const result = await transcribeWithProfile(
      emptyAsrProfile('groq'),
      [new Int16Array(16000).buffer],
      {
        runId: 1,
        textContext: {
          source: 'uia',
          textBefore: '',
          selectedText: 'Original',
          textAfter: '',
          selectionTruncated: false,
        },
      },
      () => true,
    )
    expect(result).toMatchObject({ asrText: '', llmText: '' })
    expect(mocks.polish).not.toHaveBeenCalled()
  })
})
