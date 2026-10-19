import { describe, expect, it } from 'vitest'
import { groupByDay } from '../dayGroups'

const at = (y: number, m: number, d: number, h = 12) => new Date(y, m - 1, d, h).getTime()
// 故意用会被字符串排序排错的本地化标签：8 月排在 10 月前面、7 日排在 14 日前面
const zhLabel = (ts: number) => {
  const date = new Date(ts)
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`
}

describe('groupByDay', () => {
  it('跨月、跨日的分组按日期从新到旧，不按标签字符串', () => {
    const records = [
      { id: 'oct13', timestamp: at(2026, 10, 13) },
      { id: 'aug18', timestamp: at(2026, 8, 18) },
      { id: 'apr14', timestamp: at(2026, 4, 14) },
      { id: 'apr7', timestamp: at(2026, 4, 7) },
    ]
    // 传入顺序打乱也一样
    const groups = groupByDay([records[3], records[1], records[0], records[2]], zhLabel)
    expect(groups.map((g) => g.label)).toEqual(['2026年10月13日', '2026年8月18日', '2026年4月14日', '2026年4月7日'])
  })

  it('跨年时新年份在前', () => {
    const groups = groupByDay([
      { id: 'old', timestamp: at(2025, 12, 31) },
      { id: 'new', timestamp: at(2026, 1, 1) },
    ], zhLabel)
    expect(groups.map((g) => g.records[0].id)).toEqual(['new', 'old'])
  })

  it('同一天的记录归进一组，组内保持传入顺序', () => {
    const groups = groupByDay([
      { id: 'b', timestamp: at(2026, 10, 13, 22) },
      { id: 'a', timestamp: at(2026, 10, 13, 9) },
      { id: 'c', timestamp: at(2026, 10, 12, 23) },
    ], zhLabel)
    expect(groups).toHaveLength(2)
    expect(groups[0].records.map((r) => r.id)).toEqual(['b', 'a'])
  })
})
