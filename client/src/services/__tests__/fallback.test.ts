import { describe, expect, it, vi } from 'vitest'
import { applyFallback } from '../fallback'

describe('applyFallback', () => {
  it('does not load backup configuration after a successful primary result', async () => {
    const getSteps = vi.fn()
    expect(
      await applyFallback('hello', {
        shouldFallback: (text) => !text.trim(),
        isCurrent: () => true,
        getSteps,
      }),
    ).toBe('hello')
    expect(getSteps).not.toHaveBeenCalled()
  })

  it('supports typed AI outcomes and stops at the first acceptable result', async () => {
    const unused = vi.fn()
    const result = await applyFallback(
      { text: '', status: 'empty' },
      {
        shouldFallback: (value) => value.status !== 'applied',
        isCurrent: () => true,
        getSteps: async () => [
          { id: 'first', execute: async () => ({ text: '', status: 'empty' }) },
          { id: 'second', execute: async () => ({ text: 'Corrected text', status: 'applied' }) },
          { id: 'third', execute: unused },
        ],
      },
    )
    expect(result).toEqual({ text: 'Corrected text', status: 'applied' })
    expect(unused).not.toHaveBeenCalled()
  })

  it('does not execute a backup if canceled while resolving configuration', async () => {
    let current = true
    const execute = vi.fn()
    const result = await applyFallback('', {
      shouldFallback: () => true,
      isCurrent: () => current,
      getSteps: async () => {
        current = false
        return [{ id: 'backup', execute }]
      },
    })
    expect(result).toBeNull()
    expect(execute).not.toHaveBeenCalled()
  })

  it('discards a stale result without reporting completion or advancing the chain', async () => {
    let current = true
    const onResult = vi.fn()
    const unused = vi.fn()
    expect(
      await applyFallback('', {
        shouldFallback: () => true,
        isCurrent: () => current,
        onResult,
        getSteps: async () => [
          {
            id: 'backup',
            execute: async () => {
              current = false
              return 'late'
            },
          },
          { id: 'unused', execute: unused },
        ],
      }),
    ).toBeNull()
    expect(onResult).not.toHaveBeenCalled()
    expect(unused).not.toHaveBeenCalled()
  })

  it('propagates errors to the caller instead of silently hiding failed attempts', async () => {
    await expect(
      applyFallback('', {
        shouldFallback: () => true,
        isCurrent: () => true,
        getSteps: async () => [
          {
            id: 'backup',
            execute: async () => {
              throw new Error('offline')
            },
          },
        ],
      }),
    ).rejects.toThrow('offline')
  })
})
