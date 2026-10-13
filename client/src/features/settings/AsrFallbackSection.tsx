import { useEffect, useState, useSyncExternalStore } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { ArrowDown, ArrowUp, Plus, X } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Feedback } from '@/components/ui/feedback'
import { localModelDisplayName } from '@/i18n/displayNames'
import type { AsrTarget } from '@/services/transcription/asrTarget'
import { Switch } from '@/components/ui/switch'
import { getSetting, setSetting } from '@/services/store'
import { ASR_FALLBACK_KEY, parseAsrFallback, type AsrFallbackSettings } from '@/services/transcription/asrFallback'
import type { WorkMode } from '@/services/transcription'
import { getModeStatus, subscribeModeStatus } from '@/stores/modeStatus'
import { useT } from '@/i18n/useT'
import { asrCardTitle, describeAsrMissing, resolveAsrModel, type AsrProfile } from './asrProviderCatalog'
import { loadAsrProfiles } from './asrProfileStore'
import CloudAPISection from './CloudAPISection'
import LocalModeSection from './LocalModeSection'
import ServerSection from './ServerSection'

interface LocalModel {
  id: string
  name: string
  complete: boolean
}

function targetKey(target: AsrTarget): string {
  if (target.mode === 'cloud_api') return `cloud:${target.profileId}`
  if (target.mode === 'local') return `local:${target.modelId}`
  return 'server'
}

interface Props {
  workMode: WorkMode
  onPrimaryChange: (target: AsrTarget) => Promise<void>
}

export default function AsrFallbackSection({ workMode, onPrimaryChange }: Props) {
  const t = useT()
  const primary = useSyncExternalStore(subscribeModeStatus, getModeStatus)
  const [profiles, setProfiles] = useState<AsrProfile[]>([])
  const [models, setModels] = useState<LocalModel[]>([])
  const [primaryTarget, setPrimaryTarget] = useState<AsrTarget | null>(null)
  const [settings, setSettings] = useState<AsrFallbackSettings>({ enabled: false, steps: [] })
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(false)
  const [adding, setAdding] = useState(false)
  const [addTarget, setAddTarget] = useState('server')
  const [configure, setConfigure] = useState<WorkMode | null>(null)

  useEffect(() => {
    let disposed = false
    getSetting<unknown>(ASR_FALLBACK_KEY, null)
      .then((raw) => {
        if (!disposed) setSettings(parseAsrFallback(raw))
      })
      .catch(() => {
        if (!disposed) setError(true)
      })
      .finally(() => {
        if (!disposed) setLoading(false)
      })
    return () => {
      disposed = true
    }
  }, [])

  useEffect(() => {
    let disposed = false
    const load = async () => {
      try {
        const [state, downloaded, modelId] = await Promise.all([
          loadAsrProfiles(),
          invoke<LocalModel[]>('list_downloaded_models'),
          getSetting('localAsr.modelId', 'sensevoice-small-gguf'),
        ])
        if (disposed) return
        setProfiles(state.profiles)
        setModels(downloaded.filter((model) => model.complete))
        setPrimaryTarget(
          workMode === 'server'
            ? { mode: 'server' }
            : workMode === 'local'
              ? { mode: 'local', modelId }
              : { mode: 'cloud_api', profileId: state.activeId },
        )
      } catch {
        if (!disposed) setError(true)
      }
    }
    void load()
    window.addEventListener('asr-profiles-changed', load)
    window.addEventListener('focus', load)
    window.addEventListener('sayit:models-dir-changed', load)
    return () => {
      disposed = true
      window.removeEventListener('asr-profiles-changed', load)
      window.removeEventListener('focus', load)
      window.removeEventListener('sayit:models-dir-changed', load)
    }
  }, [primary, workMode])

  const modeLabel = (mode: WorkMode) =>
    t(mode === 'local' ? 'mode.local' : mode === 'server' ? 'mode.server' : 'mode.cloudApi')
  const targets: { value: string; target: AsrTarget; label: string; unavailable?: string }[] = [
    { value: 'server', target: { mode: 'server' }, label: t('fallback.configuredServer') },
    ...profiles.map((profile) => ({
      value: `cloud:${profile.id}`,
      target: { mode: 'cloud_api' as const, profileId: profile.id },
      label: `${asrCardTitle(profile, profiles.length)} · ${resolveAsrModel(profile)}`,
      unavailable: describeAsrMissing(profile) || undefined,
    })),
    ...models.map((model) => ({
      value: `local:${model.id}`,
      target: { mode: 'local' as const, modelId: model.id },
      label: localModelDisplayName(model) || model.id,
    })),
  ]
  const chain = primaryTarget ? [primaryTarget, ...settings.steps] : []
  const localModelIds = new Set(chain.flatMap((target) => (target.mode === 'local' ? [target.modelId] : [])))
  const save = async (next: AsrFallbackSettings, nextPrimary?: AsrTarget) => {
    if (saving) return
    setSaving(true)
    try {
      await setSetting(ASR_FALLBACK_KEY, next)
      if (nextPrimary && primaryTarget && targetKey(nextPrimary) !== targetKey(primaryTarget)) {
        try {
          await onPrimaryChange(nextPrimary)
        } catch (error) {
          await setSetting(ASR_FALLBACK_KEY, settings)
          throw error
        }
        setPrimaryTarget(nextPrimary)
      }
      setSettings(next)
      setError(false)
      return true
    } catch {
      setError(true)
      return false
    } finally {
      setSaving(false)
    }
  }
  const saveChain = (next: AsrTarget[]) => {
    if (!next.length) return
    return save({ ...settings, steps: next.slice(1) }, next[0])
  }
  const move = (index: number, direction: number) => {
    const steps = [...chain]
    const other = index + direction
    const moved = steps[index]
    steps[index] = steps[other]
    steps[other] = moved
    void saveChain(steps)
  }
  const selectClass =
    'h-9 min-w-0 flex-1 rounded-md border border-input-border bg-input-bg px-2 text-sm focus:border-input-focus-border disabled:opacity-50'
  const iconClass =
    'rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-30'
  const options = (selected?: string) => (
    <>
      {selected && !targets.some((item) => item.value === selected) && (
        <option value={selected}>{t('fallback.missing')}</option>
      )}
      {(['server', 'cloud_api', 'local'] as const).map((mode) => (
        <optgroup key={mode} label={modeLabel(mode)}>
          {targets
            .filter((item) => item.target.mode === mode)
            .map((item) => (
              <option key={item.value} value={item.value} disabled={Boolean(item.unavailable)}>
                {item.label}
                {item.unavailable ? ` — ${item.unavailable}` : ''}
              </option>
            ))}
        </optgroup>
      ))}
    </>
  )

  return (
    <Card>
      <CardContent className="p-6">
        <div className="flex items-center justify-between gap-3">
          <h2 id="asr-fallback-title" className="text-base font-semibold">
            {t('fallback.title')}
          </h2>
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">
              {settings.enabled ? t('fallback.on') : t('fallback.off')}
            </span>
            <Switch
              checked={settings.enabled}
              disabled={loading || saving}
              labelledBy="asr-fallback-title"
              onChange={() => {
                setAdding(false)
                setConfigure(null)
                void save({ ...settings, enabled: !settings.enabled })
              }}
            />
          </div>
        </div>
        <p className="mt-1 text-sm text-muted-foreground">{t('fallback.description')}</p>
        {settings.enabled && (
          <div className="mt-4 space-y-3">
            <div className="space-y-2">
              {chain.map((step, index) => {
                const key = targetKey(step)
                const selected = targets.find((item) => item.value === key)
                return (
                  <div key={index} className="space-y-1">
                    <div className="flex items-center gap-2">
                      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-border text-xs text-muted-foreground">
                        {index + 1}
                      </span>
                      <select
                        aria-label={`${t('fallback.step')} ${index + 1}`}
                        className={selectClass}
                        value={key}
                        disabled={saving}
                        onChange={(event) => {
                          const next = targets.find((item) => item.value === event.target.value)
                          if (next) void saveChain(chain.map((item, i) => (i === index ? next.target : item)))
                        }}
                      >
                        {options(key)}
                      </select>
                      <button
                        type="button"
                        className={iconClass}
                        aria-label={`${t('fallback.moveUp')} ${index + 1}`}
                        title={t('fallback.moveUp')}
                        disabled={saving || index === 0}
                        onClick={() => move(index, -1)}
                      >
                        <ArrowUp className="h-3.5 w-3.5" />
                      </button>
                      <button
                        type="button"
                        className={iconClass}
                        aria-label={`${t('fallback.moveDown')} ${index + 1}`}
                        title={t('fallback.moveDown')}
                        disabled={saving || index === chain.length - 1}
                        onClick={() => move(index, 1)}
                      >
                        <ArrowDown className="h-3.5 w-3.5" />
                      </button>
                      <button
                        type="button"
                        className={iconClass}
                        aria-label={`${t('fallback.remove')} ${index + 1}`}
                        title={t('fallback.remove')}
                        disabled={saving || chain.length === 1}
                        onClick={() => void saveChain(chain.filter((_, i) => i !== index))}
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </div>
                    {(!selected || selected.unavailable) && (
                      <p className="pl-8 text-xs text-warning-strong">
                        {selected?.unavailable || t('fallback.missing')} ·{' '}
                        {t(index === 0 ? 'fallback.mainUnavailable' : 'fallback.skipped')}
                      </p>
                    )}
                  </div>
                )
              })}
            </div>
            {localModelIds.size > 1 && <Feedback tone="warning" message={t('fallback.localMemoryWarning')} />}
            {adding ? (
              <div className="space-y-3 rounded-lg border border-border bg-muted/20 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <select
                    autoFocus
                    aria-label={t('fallback.addTarget')}
                    className={selectClass}
                    value={addTarget}
                    disabled={saving}
                    onChange={(event) => setAddTarget(event.target.value)}
                  >
                    {options(addTarget)}
                  </select>
                  <Button
                    size="sm"
                    disabled={saving || !targets.some((item) => item.value === addTarget && !item.unavailable)}
                    onClick={() => {
                      const next = targets.find((item) => item.value === addTarget)
                      if (next)
                        void save({ ...settings, steps: [...settings.steps, next.target] }).then((saved) => {
                          if (saved) {
                            setAdding(false)
                            setConfigure(null)
                          }
                        })
                    }}
                  >
                    {t('fallback.add')}
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={saving}
                    onClick={() => {
                      setAdding(false)
                      setConfigure(null)
                    }}
                  >
                    {t('common.cancel')}
                  </Button>
                </div>
                <div className="flex flex-wrap items-center gap-3 text-xs">
                  <span className="text-muted-foreground">{t('fallback.configure')}</span>
                  {(['cloud_api', 'server', 'local'] as const)
                    .filter((mode) => mode !== workMode)
                    .map((mode) => (
                      <button
                        key={mode}
                        type="button"
                        aria-expanded={configure === mode}
                        className="font-medium text-primary hover:underline"
                        onClick={() => setConfigure(configure === mode ? null : mode)}
                      >
                        {modeLabel(mode)}
                      </button>
                    ))}
                </div>
              </div>
            ) : (
              <Button
                variant="outline"
                size="sm"
                onClick={() => setAdding(true)}
                disabled={loading || saving || !primaryTarget}
              >
                <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                {t('fallback.add')}
              </Button>
            )}
            {configure && configure !== workMode && (
              <div className="border-t border-border pt-4">
                {configure === 'cloud_api' ? (
                  <CloudAPISection />
                ) : configure === 'server' ? (
                  <ServerSection />
                ) : (
                  <LocalModeSection />
                )}
              </div>
            )}
          </div>
        )}
        {error && (
          <p role="alert" className="mt-3 text-xs text-destructive">
            {t('fallback.saveError')}
          </p>
        )}
      </CardContent>
    </Card>
  )
}
