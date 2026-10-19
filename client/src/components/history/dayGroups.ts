/**
 * 历史记录按「本地日期」分组，组按日期从新到旧排。
 *
 * 为什么按当天零点的时间戳排，而不是按分组标签排：标签是给人看的本地化字符串
 * （「2026年10月13日」「October 13, 2026」），按字符串比大小月份就乱了 ——
 * 「2026年8月…」排在「2026年10月…」前面，英文则按月份名的字母序。原来就是这么写的，
 * 收藏页一跨月，最新的十月反而沉到最底下。今天 / 昨天也不用再单独判断：它们的零点最大。
 *
 * 组内顺序保持传入顺序（数据库按 list_order 排好的，新的在前），这里不重排。
 */
export interface DayGroup<T> {
  /** 当天本地零点的毫秒时间戳，也当 React key 用 */
  dayStart: number
  label: string
  records: T[]
}

export function groupByDay<T extends { timestamp: number }>(
  records: readonly T[],
  labelFor: (timestamp: number) => string,
): DayGroup<T>[] {
  const byDay = new Map<number, DayGroup<T>>()
  for (const record of records) {
    const date = new Date(record.timestamp)
    const dayStart = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
    let group = byDay.get(dayStart)
    if (!group) {
      group = { dayStart, label: labelFor(record.timestamp), records: [] }
      byDay.set(dayStart, group)
    }
    group.records.push(record)
  }
  return [...byDay.values()].sort((a, b) => b.dayStart - a.dayStart)
}
