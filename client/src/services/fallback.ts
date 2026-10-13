/** The caller owns eligibility and execution; this module knows nothing about ASR or AI. */
export interface FallbackStep<T> {
  id: string
  execute: () => Promise<T>
}

export interface FallbackPolicy<T> {
  shouldFallback: (result: T) => boolean
  isCurrent: () => boolean
  /** Lazy so a successful primary result never waits for backup configuration. */
  getSteps: () => Promise<readonly FallbackStep<T>[]>
  onAttempt?: (id: string) => void
  onResult?: (id: string, result: T) => void
  /** Opt in to skipping a failed step, e.g. an unavailable backup service. */
  onError?: (id: string, error: unknown) => void
}

/** Tries each configured step at most once. Errors propagate unless explicitly handled; stale results are discarded. */
export async function applyFallback<T>(primary: T, policy: FallbackPolicy<T>): Promise<T | null> {
  if (!policy.isCurrent()) return null
  if (!policy.shouldFallback(primary)) return primary
  const steps = await policy.getSteps()
  if (!policy.isCurrent()) return null
  let result = primary
  for (const step of steps) {
    if (!policy.isCurrent()) return null
    policy.onAttempt?.(step.id)
    if (!policy.isCurrent()) return null
    try {
      result = await step.execute()
    } catch (error) {
      if (!policy.isCurrent()) return null
      if (!policy.onError) throw error
      policy.onError(step.id, error)
      continue
    }
    if (!policy.isCurrent()) return null
    policy.onResult?.(step.id, result)
    if (!policy.shouldFallback(result)) break
  }
  return policy.isCurrent() ? result : null
}
