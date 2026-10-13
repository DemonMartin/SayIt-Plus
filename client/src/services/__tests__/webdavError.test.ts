import { describe, it, expect } from 'vitest'
import { describeWebDavError, webDavErrorDetail } from '../webdavBackup'

// 真实出现过的原文（坚果云断网时的 MKCOL）
const NETWORK = 'MKCOL failed: error sending request for url (https://dav.jianguoyun.com/dav)'

describe('describeWebDavError', () => {
  it('连不上服务器的 reqwest 原文翻成一句话，带上主机名', () => {
    const text = describeWebDavError(NETWORK)
    expect(text).toContain('dav.jianguoyun.com')
    expect(text).not.toContain('error sending request')
  })

  it('已登记的错误码照旧翻译', () => {
    expect(describeWebDavError('WEBDAV_UNAUTHORIZED')).not.toBe('WEBDAV_UNAUTHORIZED')
  })

  it('没认出来的错误按原样显示', () => {
    expect(describeWebDavError('PUT failed: HTTP 507')).toBe('PUT failed: HTTP 507')
  })
})

describe('webDavErrorDetail', () => {
  it('翻译过的错误保留原文给「详情」', () => {
    expect(webDavErrorDetail(NETWORK)).toBe(NETWORK)
  })

  it('原样显示的错误不再重复一份详情', () => {
    expect(webDavErrorDetail('PUT failed: HTTP 507')).toBeNull()
  })
})
