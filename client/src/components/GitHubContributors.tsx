import { useEffect, useState } from 'react'
import { open as shellOpen } from '@tauri-apps/plugin-shell'
import { useT } from '@/i18n/useT'
import { CONTRIBUTORS_URL, REPOSITORY_OWNER, getGitHubContributors, type GitHubContributor } from '@/services/githubContributors'

export default function GitHubContributors() {
  const t = useT()
  const [contributors, setContributors] = useState<GitHubContributor[] | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let active = true
    void getGitHubContributors().then(
      (list) => { if (active) setContributors(list) },
      () => { if (active) setFailed(true) },
    )
    return () => { active = false }
  }, [])

  return (
    <section className="mt-5 border-t border-border pt-5" aria-labelledby="contributors-heading">
      <h3 id="contributors-heading" className="mb-3 text-sm font-medium">{t('about.contributors')}</h3>
      {!contributors && !failed && (
        <p role="status" className="text-sm text-muted-foreground">
          <span className="loading-text">{t('common.loading')}</span>
        </p>
      )}
      {failed && <p role="status" className="text-sm text-muted-foreground">{t('about.contributorsUnavailable')}</p>}
      {contributors && contributors.length > 0 && (
        <ul
          tabIndex={0}
          aria-labelledby="contributors-heading"
          className="grid max-h-44 grid-cols-1 gap-1 overflow-y-auto overscroll-contain rounded-lg p-0.5 [scrollbar-gutter:stable] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:grid-cols-2 lg:grid-cols-3"
        >
          {contributors.map(({ login, avatar_url }) => (
            <li key={login} className="min-w-0">
              <a
                href={`https://github.com/${login}`}
                onClick={(event) => {
                  event.preventDefault()
                  void shellOpen(`https://github.com/${login}`)
                }}
                className="flex items-center gap-2.5 rounded-lg px-2 py-2 text-sm text-muted-foreground transition-colors hover:bg-muted/50 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <span className="relative flex h-8 w-8 shrink-0 items-center justify-center overflow-hidden rounded-full bg-muted text-xs" aria-hidden="true">
                  {login[0].toUpperCase()}
                  <img
                    src={`${avatar_url}${avatar_url.includes('?') ? '&' : '?'}s=64`}
                    alt=""
                    loading="lazy"
                    decoding="async"
                    referrerPolicy="no-referrer"
                    className="absolute inset-0 h-full w-full object-cover"
                    onError={(event) => { event.currentTarget.style.display = 'none' }}
                  />
                </span>
                <span className="truncate">{login}</span>
                {login.toLowerCase() === REPOSITORY_OWNER && (
                  <span className="shrink-0 rounded-full border border-border px-1.5 py-0.5 text-[10px] font-medium leading-none text-muted-foreground">
                    {t('about.contributorOwner')}
                  </span>
                )}
              </a>
            </li>
          ))}
        </ul>
      )}
      {(failed || contributors?.length === 0) && (
        <a
          href={CONTRIBUTORS_URL}
          onClick={(event) => {
            event.preventDefault()
            void shellOpen(CONTRIBUTORS_URL)
          }}
          className="mt-2 inline-block text-sm text-muted-foreground underline underline-offset-4 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {t('about.contributorsOnGitHub')}
        </a>
      )}
    </section>
  )
}
