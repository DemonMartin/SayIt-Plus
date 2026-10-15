import { useEffect, useRef, useState } from 'react'
import { Info } from 'lucide-react'
import { Feedback } from '@/components/ui/feedback'
import { Tooltip } from '@/components/ui/tooltip'
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
  const [infoFocused, setInfoFocused] = useState(false)
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

  return (
    <div className="mt-4 space-y-3 border-t border-border pt-4">
      <div className="flex flex-wrap items-center justify-between gap-3 sm:gap-6">
        <div className="flex min-w-0 flex-[1_1_14rem] items-center gap-1.5">
          <label htmlFor="context-selection-ai-profile" className="text-sm font-medium">
            {t('settings.contextAware.model')}
          </label>
          <Tooltip
            variant="light"
            forceVisible={infoFocused}
            content={(
              <div className="w-72 max-w-[calc(100vw-3rem)]">
                {t('settings.contextAware.modelDesc')}
              </div>
            )}
          >
            <button
              type="button"
              aria-label={t('settings.helpAria', { label: t('settings.contextAware.model') })}
              aria-describedby="context-selection-ai-help"
              onFocus={() => setInfoFocused(true)}
              onBlur={() => setInfoFocused(false)}
              className="shrink-0 cursor-help rounded-sm text-muted-foreground/50 transition-colors hover:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <Info className="h-3.5 w-3.5" aria-hidden />
            </button>
          </Tooltip>
          <span id="context-selection-ai-help" className="sr-only">
            {t('settings.contextAware.modelDesc')}
          </span>
        </div>
        <select
          id="context-selection-ai-profile"
          aria-describedby="context-selection-ai-help"
          value={profileId}
          disabled={loading || saving || error === 'load'}
          onChange={(event) => void save(event.target.value)}
          className="h-9 w-full min-w-0 max-w-full rounded-md border border-input-border bg-input-bg px-2 text-sm focus:border-input-focus-border disabled:opacity-50 sm:w-64"
        >
          <option value="">{t('settings.contextAware.sameModel')}</option>
          {missing && <option value={profileId}>{t('settings.contextAware.modelMissingOption')}</option>}
          {profiles.map((profile) => (
            <option key={profile.id} value={profile.id} disabled={!isProfileComplete(profile)}>
              {profileTitle(profile)} · {profileSubtitle(profile)}
              {!isProfileComplete(profile) ? ` — ${t('ai.status.incomplete')}` : ''}
            </option>
          ))}
        </select>
      </div>
      {!loading && !profiles.length && !error && (
        <p className="text-xs text-muted-foreground">{t('settings.contextAware.modelEmpty')}</p>
      )}
      {missing && !error && (
        <Feedback tone="warning" message={t('settings.contextAware.modelMissing')} />
      )}
      {selected && !isProfileComplete(selected) && !error && (
        <Feedback tone="warning" message={t('settings.contextAware.modelIncomplete')} />
      )}
      {error && (
        <Feedback tone="error" message={t(error === 'load'
          ? 'settings.contextAware.modelLoadError'
          : 'settings.contextAware.modelSaveError')} />
      )}
    </div>
  )
}
