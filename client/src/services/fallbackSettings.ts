export interface FallbackSettings<T> {
  enabled: boolean
  steps: T[]
}

/** Validate persisted data at the boundary before it becomes executable configuration. */
export function parseFallbackSettings<T>(raw: unknown, parseStep: (value: unknown) => T | null): FallbackSettings<T> {
  if (!raw || typeof raw !== 'object') return { enabled: false, steps: [] }
  const value = raw as Record<string, unknown>
  return {
    enabled: value.enabled === true,
    steps: Array.isArray(value.steps)
      ? value.steps.flatMap((step) => {
          const parsed = parseStep(step)
          return parsed ? [parsed] : []
        })
      : [],
  }
}
