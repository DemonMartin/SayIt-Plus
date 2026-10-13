// AI 服务配置页面

import AIProviderSection from './AIProviderSection'
import { useT } from '@/i18n/useT'
import { PageTitle } from '@/components/ui/info-tip'

export default function AIServicePage() {
  const t = useT()
  return (
    <div className="mx-auto max-w-4xl">
      <PageTitle title={t('nav.aiService')} info={t('aiService.subtitle')} />
      <div className="space-y-6">
        <AIProviderSection />
      </div>
    </div>
  )
}
