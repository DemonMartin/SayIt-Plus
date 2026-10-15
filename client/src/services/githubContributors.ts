export const CONTRIBUTORS_URL = 'https://github.com/crosswk/SayIt/graphs/contributors'

const API_URL = 'https://api.github.com/repos/crosswk/SayIt/contributors'
const CACHE_KEY = 'sayit.github-contributors'
export const REPOSITORY_OWNER = 'crosswk'

export interface GitHubContributor {
  login: string
  avatar_url: string
  // Older caches already follow GitHub's contribution order but omit the count.
  contributions?: number
}

interface ContributorsCache {
  version: string
  contributors: GitHubContributor[]
}

function isContributor(value: unknown): value is GitHubContributor {
  if (!value || typeof value !== 'object') return false
  const contributor = value as Record<string, unknown>
  if (typeof contributor.login !== 'string'
    || !/^[a-z\d](?:[a-z\d-]*[a-z\d])?(?:\[bot\])?$/i.test(contributor.login)
    || typeof contributor.avatar_url !== 'string'
    || (contributor.contributions !== undefined
      && (typeof contributor.contributions !== 'number'
        || !Number.isInteger(contributor.contributions) || contributor.contributions < 0))) return false
  try {
    const avatar = new URL(contributor.avatar_url)
    return avatar.protocol === 'https:' && avatar.hostname === 'avatars.githubusercontent.com'
  } catch {
    return false
  }
}

function readCache(): ContributorsCache | null {
  try {
    const cache = JSON.parse(localStorage.getItem(CACHE_KEY) || 'null') as ContributorsCache | null
    if (cache && typeof cache.version === 'string'
      && Array.isArray(cache.contributors) && cache.contributors.every(isContributor)) return cache
  } catch {
    // A missing or unreadable cache should not prevent loading the list.
  }
  return null
}

async function loadContributors(): Promise<GitHubContributor[]> {
  const cache = readCache()
  if (cache?.version === __APP_VERSION__) return cache.contributors

  try {
    const contributors: GitHubContributor[] = []
    const signal = AbortSignal.timeout(15_000)
    for (let page = 1; ; page++) {
      const response = await fetch(`${API_URL}?per_page=100&page=${page}`, {
        headers: { Accept: 'application/vnd.github+json' },
        credentials: 'omit',
        referrerPolicy: 'no-referrer',
        signal,
      })
      if (!response.ok) throw new Error(`GitHub contributors: HTTP ${response.status}`)
      const data: unknown = response.status === 204 ? [] : await response.json()
      if (!Array.isArray(data) || !data.every(isContributor)) {
        throw new Error('Invalid GitHub contributors response')
      }
      contributors.push(...data.map(({ login, avatar_url, contributions }) => ({ login, avatar_url, contributions })))
      if (!response.headers.get('Link')?.includes('rel="next"')) break
    }

    try {
      localStorage.setItem(CACHE_KEY, JSON.stringify({ version: __APP_VERSION__, contributors }))
    } catch {
      // Keep the result in memory if persistent storage is unavailable.
    }
    return contributors
  } catch (error) {
    if (cache) return cache.contributors
    throw error
  }
}

// Share the request across mounts, including React Strict Mode. Successful lists
// persist across launches; an older list remains usable when an update is offline.
let contributorsRequest: Promise<GitHubContributor[]> | undefined

export function getGitHubContributors(): Promise<GitHubContributor[]> {
  contributorsRequest ??= loadContributors().then((contributors) => (
    [...contributors].sort((a, b) => (
      (Number(b.login.toLowerCase() === REPOSITORY_OWNER)
        - Number(a.login.toLowerCase() === REPOSITORY_OWNER))
      || (b.contributions ?? 0) - (a.contributions ?? 0)
    ))
  ))
  return contributorsRequest
}
