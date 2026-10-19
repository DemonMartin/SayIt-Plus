import { describe, expect, it, vi } from 'vitest'

vi.mock('@/services/runtimeConfig', () => ({
  getUpdateBaseUrl: () => 'https://sayitapp.site',
  getOfficialUpdateBaseUrl: () => 'https://sayitapp.site',
  isOfficialUpdateChannel: () => true,
}))

import { resolveDownloadUrl } from '../updateChecker'

describe('resolveDownloadUrl', () => {
  it('keeps paths on the manifest server', () => {
    expect(resolveDownloadUrl('https://sayitapp.site', '/api/desktop-updates/win32/x64/SayIt_0.2.2_x64-setup.exe'))
      .toBe('https://sayitapp.site/api/desktop-updates/win32/x64/SayIt_0.2.2_x64-setup.exe')
    expect(resolveDownloadUrl('https://my.server:8443', '/a.exe')).toBe('https://my.server:8443/a.exe')
  })

  it('rejects paths that would move the download to another host', () => {
    // 原来字符串拼接时，这一条会变成 https://sayitapp.site.attacker.example/x.exe
    expect(resolveDownloadUrl('https://sayitapp.site', '.attacker.example/x.exe')).toBeNull()
    expect(resolveDownloadUrl('https://sayitapp.site', '//attacker.example/x.exe')).toBeNull()
    expect(resolveDownloadUrl('https://sayitapp.site', 'https://attacker.example/x.exe')).toBeNull()
    expect(resolveDownloadUrl('https://sayitapp.site', '@attacker.example/x.exe')).toBeNull()
  })

  it('returns null when there is no path', () => {
    expect(resolveDownloadUrl('https://sayitapp.site', undefined)).toBeNull()
    expect(resolveDownloadUrl('https://sayitapp.site', '')).toBeNull()
  })
})
