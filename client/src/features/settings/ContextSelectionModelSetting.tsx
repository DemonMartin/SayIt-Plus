import { useEffect, useRef, useState } from 'react'
import { Feedback } from '@/components/ui/feedback'
import { Select, type SelectOption } from '@/components/ui/select'
import { useT } from '@/i18n/useT'
import { getSetting, setSetting } from '@/services/store'
import { CONTEXT_SELECTION_AI_PROFILE_KEY } from '@/services/transcription/selectionAi'
import { loadAiProfiles } from './aiProfileStore'
import { isProfileComplete, profileSubtitle, profileTitle, type AiProfile } from './aiProviderCatalog'

export default function ContextSelectionModelSetting() {
  const t = useT()
  const [profiles, setProfiles] = useState<AiProfile[]>([])
  const [profileId, setProfileId] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<'load' | 'save' | null>(null)
  const revision = useRef(0)
  const savingRef = useRef(false)

  useEffect(() => {
    let disposed = false
    const load = async () => {
      if (savingRef.current) return
      const current = ++revision.current
      try {
        const [state, selectedId] = await Promise.all([
          loadAiProfiles(),
          getSetting(CONTEXT_SELECTION_AI_PROFILE_KEY, ''),
        ])
        if (disposed || current !== revision.current) return
        setProfiles(state.profiles)
        setProfileId(selectedId)
        setError(null)
      } catch {
        if (!disposed && current === revision.current) setError('load')
      } finally {
        if (!disposed && current === revision.current) setLoading(false)
      }
    }
    void load()
    window.addEventListener('ai-profiles-changed', load)
    window.addEventListener('focus', load)
    return () => {
      disposed = true
      window.removeEventListener('ai-profiles-changed', load)
      window.removeEventListener('focus', load)
    }
  }, [])

  const save = async (nextId: string) => {
    if (savingRef.current) return
    savingRef.current = true
    revision.current += 1
    setSaving(true)
    try {
      await setSetting(CONTEXT_SELECTION_AI_PROFILE_KEY, nextId)
      setProfileId(nextId)
      setError(null)
    } catch {
      setError('save')
    } finally {
      savingRef.current = false
      setSaving(false)
    }
  }
  const selected = profiles.find((profile) => profile.id === profileId)
  const missing = Boolean(profileId && !selected)
  const usable = profiles.filter(isProfileComplete)

  // 用应用自己的 Select，不用原生 <select>：原生下拉在 WebView2 里弹的是系统列表，
  // 宽度跟着最长那一项走（「模型名 · 供应商 · 主机名」很长），会伸出卡片甚至窗口。
  // 这个组件的列表与触发器同宽，长名字截断，悬停看全称。
  // 它不支持禁用单项，所以没填齐的服务干脆不列；只有已经选中的那条例外，
  // 否则触发器上什么都不显示，用户看不出自己选过什么（下面另有警告说明原因）。
  const options: SelectOption[] = [
    { value: '', label: t('settings.contextAware.sameModel') },
    ...(missing ? [{ value: profileId, label: t('settings.contextAware.modelMissingOption') }] : []),
    ...profiles
      .filter((profile) => isProfileComplete(profile) || profile.id === profileId)
      .map((profile) => {
        const incomplete = isProfileComplete(profile) ? '' : ` · ${t('ai.status.incomplete')}`
        const hint = `${profileSubtitle(profile)}${incomplete}`
        return { value: profile.id, label: profileTitle(profile), hint, title: `${profileTitle(profile)} · ${hint}` }
      }),
  ]

  // 和卡片标题那一行同一个结构：左边名称 + 一行说明，右边控件。下拉框固定窄宽度，
  // 列表右对齐往左展开（menuAlign="end"），长模型名不会把控件撑宽。
  return (
    <div className="mt-4 border-t border-border pt-4">
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2">
        <div className="min-w-0 flex-[1_1_16rem]">
          <p id="context-selection-ai-label" className="text-sm text-foreground">
            {t('settings.contextAware.model')}
          </p>
          <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
            {!loading && !usable.length && !error
              ? t('settings.contextAware.modelEmpty')
              : t('settings.contextAware.modelDesc')}
          </p>
        </div>
        <Select
          size="sm"
          menuAlign="end"
          labelledBy="context-selection-ai-label"
          value={profileId}
          disabled={loading || saving || error === 'load'}
          onChange={(value) => void save(value)}
          options={options}
          className="w-56 shrink-0"
        />
      </div>
      {missing && !error && (
        <Feedback className="mt-3" tone="warning" message={t('settings.contextAware.modelMissing')} />
      )}
      {selected && !isProfileComplete(selected) && !error && (
        <Feedback className="mt-3" tone="warning" message={t('settings.contextAware.modelIncomplete')} />
      )}
      {error && (
        <Feedback className="mt-3" tone="error" message={t(error === 'load'
          ? 'settings.contextAware.modelLoadError'
          : 'settings.contextAware.modelSaveError')} />
      )}
    </div>
  )
}
