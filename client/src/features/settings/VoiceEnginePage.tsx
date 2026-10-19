// 语音引擎设置页面 — 工作模式 + ASR 识别服务配置 + 识别测试

import { useEffect, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { ChevronDown } from 'lucide-react'
import { getSetting, setSetting } from '@/services/store'
import { switchProvider, getWorkMode, type WorkMode } from '@/services/transcription'
import { refreshRecorderSettings, reconnectProvider } from '@/services/recorder'
import { refreshModeStatus } from '@/stores/modeStatus'
import { setEngineDraftDirty } from '@/stores/engineDraft'
import WorkModeSection from './WorkModeSection'
import CloudAPISection from './CloudAPISection'
import LocalModeSection, { LocalModeAdvancedSection } from './LocalModeSection'
import ServerSection from './ServerSection'
import AsrTestSection from './AsrTestSection'
import AsrFallbackSection from './AsrFallbackSection'
import { useT } from '@/i18n/useT'
import type { AsrTarget } from '@/services/transcription/asrTarget'
import { loadAsrProfiles, saveAsrProfiles } from './asrProfileStore'
import { describeAsrMissing } from './asrProviderCatalog'
import { addRuntimeEvent } from '@/services/debugLog'
import { PageTitle } from '@/components/ui/info-tip'

export default function VoiceEnginePage() {
  const t = useT()
  const [workMode, setWorkMode] = useState<WorkMode>(getWorkMode)
  const [selectionRevision, setSelectionRevision] = useState(0)
  // 本地模式的次级设置默认收起。原来这 4 张卡与"选模型"完全等权地平铺在一起，
  // 本地模式一屏 7 个同级标题、27 个控件，用户没法判断哪几个是必须做的。
  const [advancedOpen, setAdvancedOpen] = useState(false)

  useEffect(() => {
    getSetting('workMode', 'server').then((value) => {
      const v = value as WorkMode
      if (v === 'server' || v === 'cloud_api' || v === 'local') setWorkMode(v)
    })
  }, [])

  const handleWorkModeChange = async (mode: WorkMode) => {
    // 换模式会整体替换下方的配置卡，上一套里没保存的输入随之消失——脏标记要跟着清，
    // 否则「识别测试」会一直以为有未保存的改动
    await setSetting('workMode', mode)
    setEngineDraftDirty(false)
    setWorkMode(mode)
    await switchProvider(mode)
    await refreshRecorderSettings()
    reconnectProvider()
    void refreshModeStatus() // 同步左下角的引擎指示与右上角的就绪徽标
  }

  const handlePrimaryChange = async (target: AsrTarget) => {
    let selectionChange: { apply: () => Promise<void>; restore: () => Promise<void> } | undefined
    if (target.mode === 'local') {
      const models = await invoke<{ id: string; complete: boolean }[]>('list_downloaded_models')
      if (!models.some((model) => model.id === target.modelId && model.complete)) {
        throw new Error('Selected local model is unavailable')
      }
      const previousModelId = await getSetting('localAsr.modelId', 'sensevoice-small-gguf')
      selectionChange = {
        apply: () => setSetting('localAsr.modelId', target.modelId),
        restore: () => setSetting('localAsr.modelId', previousModelId),
      }
    } else if (target.mode === 'cloud_api') {
      const state = await loadAsrProfiles()
      const profile = state.profiles.find((item) => item.id === target.profileId)
      if (!profile || describeAsrMissing(profile)) {
        throw new Error('Selected recognition service is unavailable')
      }
      selectionChange = {
        apply: () => saveAsrProfiles({ ...state, activeId: target.profileId }),
        restore: () => saveAsrProfiles(state),
      }
    }
    try {
      await selectionChange?.apply()
      await handleWorkModeChange(target.mode)
    } catch (error) {
      try {
        await selectionChange?.restore()
        await handleWorkModeChange(workMode)
      } catch (restoreError) {
        addRuntimeEvent('warn', 'settings', 'Could not restore the previous recognition selection', {
          error: String(restoreError),
        })
      }
      throw error
    } finally {
      // The engine card owns its form state; refresh it after an external selection change.
      setSelectionRevision((revision) => revision + 1)
    }
  }

  return (
    <div className="mx-auto max-w-4xl">
      <PageTitle title={t('nav.voiceEngine')} info={t('voiceEngine.subtitle')} />

      <div className="space-y-6">
        <WorkModeSection value={workMode} onChange={(m) => void handleWorkModeChange(m)} />

        {/* id 供「工作模式」右上角的「待配置」徽标点击后滚动定位 */}
        <div id="engine-config" key={selectionRevision} className="space-y-6">
          {workMode === 'local' && <LocalModeSection />}
          {workMode === 'server' && <ServerSection />}
          {workMode === 'cloud_api' && <CloudAPISection />}
        </div>

        <AsrFallbackSection workMode={workMode} onPrimaryChange={handlePrimaryChange} />

        <AsrTestSection workMode={workMode} />

        {workMode === 'local' && (
          <section className="space-y-6">
            <button
              type="button"
              aria-expanded={advancedOpen}
              aria-controls="local-advanced"
              onClick={() => setAdvancedOpen(!advancedOpen)}
              className="flex w-full items-center gap-2 rounded-md px-1 py-2 text-left text-sm text-muted-foreground transition-colors hover:text-foreground"
            >
              <ChevronDown
                className={`h-4 w-4 shrink-0 transition-transform ${advancedOpen ? 'rotate-180' : ''}`}
                aria-hidden
              />
              <span className="font-medium">{t('voiceEngine.advanced')}</span>
              <span className="text-xs">{t('voiceEngine.advancedDesc')}</span>
              <span className="h-px flex-1 bg-border" aria-hidden />
            </button>

            {advancedOpen && (
              <div id="local-advanced" className="space-y-6">
                <LocalModeAdvancedSection />
              </div>
            )}
          </section>
        )}
      </div>
    </div>
  )
}
