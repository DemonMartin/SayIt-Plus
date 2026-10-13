import type { ReactNode } from 'react'
import { Info } from 'lucide-react'
import { Tooltip } from '@/components/ui/tooltip'
import { t } from '@/i18n'

/**
 * 标题旁的 ⓘ：说明文字平时收起，鼠标移上去才显示。
 *
 * 界面上大段灰字压缩以后，原来那些解释不能直接删（用户遇到不懂的地方要有地方查），
 * 统一挪到这里。提示框是 pointer-events-none 的，里面放不了可点的链接 —— 链接要留在正文里。
 */
export function InfoTip({ content, label }: { content: ReactNode; label: string }) {
  return (
    <Tooltip variant="light" content={content}>
      <Info
        aria-label={t('settings.helpAria', { label })}
        className="h-3.5 w-3.5 shrink-0 cursor-help text-muted-foreground/50 transition-colors hover:text-muted-foreground"
      />
    </Tooltip>
  )
}

/** 页面大标题。原来标题下面都跟一句副标题，现在副标题收进 ⓘ。 */
export function PageTitle({ title, info }: { title: string; info?: ReactNode }) {
  return (
    <div className="mb-6 flex items-center gap-2">
      <h1 className="text-2xl font-bold">{title}</h1>
      {info && <InfoTip content={info} label={title} />}
    </div>
  )
}
