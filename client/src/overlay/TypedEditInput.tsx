import { useEffect, useRef, useState } from 'react'
import { ArrowUp, X } from 'lucide-react'
import * as bridge from '@/services/bridge'
import { useT } from '@/i18n/useT'

export type TypedEditPhase = 'idle' | 'improving' | 'applying'

export default function TypedEditInput({
  token,
  phase,
  error,
}: {
  token: number
  phase: TypedEditPhase
  error: string
}) {
  const t = useT()
  const [text, setText] = useState('')
  const input = useRef<HTMLTextAreaElement>(null)
  const submitted = useRef(false)
  const busy = phase !== 'idle'

  useEffect(() => {
    if (busy) return
    submitted.current = false
    void bridge
      .focusTypedEdit(token)
      .then(() => input.current?.focus())
      .catch(() => {})
  }, [token, busy])

  const cancel = () => {
    void bridge.emit('typed-edit-cancel', { token })
  }
  const submit = (improveFirst: boolean) => {
    if (busy || submitted.current || !text.trim()) return
    submitted.current = true
    void bridge.emit('typed-edit-submit', { token, text, improveFirst }).catch(() => {
      submitted.current = false
    })
  }

  return (
    <section
      data-overlay-content
      role="dialog"
      aria-labelledby="typed-edit-title"
      aria-busy={busy}
      className="pointer-events-auto flex w-full max-w-[480px] flex-col gap-3 rounded-2xl border p-4"
      style={{ background: 'var(--overlay-bg)', color: 'var(--overlay-text)', borderColor: 'var(--overlay-border)' }}
      onKeyDown={(event) => {
        if (event.nativeEvent.isComposing) return
        if (event.key === 'Escape') {
          event.preventDefault()
          cancel()
        }
      }}
    >
      <header className="flex items-center justify-between gap-3">
        <h2 id="typed-edit-title" className="text-sm font-medium">
          {t('typedEdit.title')}
        </h2>
        <button
          type="button"
          onClick={cancel}
          aria-label={t('window.close')}
          className="rounded-lg p-1 text-white/50 transition-colors hover:bg-white/10 hover:text-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-400"
        >
          <X className="h-4 w-4" />
        </button>
      </header>
      <textarea
        ref={input}
        value={text}
        readOnly={busy}
        maxLength={6000}
        aria-label={t('typedEdit.title')}
        placeholder={t('typedEdit.placeholder')}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing || event.keyCode === 229) return
          if (event.key === 'Enter') {
            event.preventDefault()
            if (!event.repeat) submit(event.shiftKey)
          }
        }}
        className="h-20 w-full resize-none rounded-lg border border-white/10 bg-white/5 p-3 text-sm leading-5 text-white outline-none placeholder:text-white/35 focus:border-sky-400/50 read-only:opacity-60"
      />
      <footer className="flex min-h-7 items-center justify-between gap-3">
        <p role="status" aria-live="polite" className={`text-xs ${error ? 'text-rose-300' : 'text-white/50'}`}>
          {error ||
            (busy ? (
              <span className="loading-text">
                {t(phase === 'improving' ? 'typedEdit.improving' : 'typedEdit.applying')}
              </span>
            ) : (
              t('typedEdit.keyHint')
            ))}
        </p>
        <button
          type="button"
          disabled={busy || !text.trim()}
          onClick={() => submit(false)}
          aria-label={t('typedEdit.apply')}
          title={t('typedEdit.apply')}
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-white text-zinc-900 transition-opacity hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-400 disabled:opacity-25"
        >
          <ArrowUp className="h-4 w-4" />
        </button>
      </footer>
    </section>
  )
}
