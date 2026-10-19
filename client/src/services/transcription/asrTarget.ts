import type { AsrProfile } from '@/features/settings/asrProviderCatalog'
import type { ServerAiSource } from './serverAiSource'

export type AsrTarget =
  | { mode: 'server' }
  | { mode: 'local'; modelId: string }
  | { mode: 'cloud_api'; profileId: string }

export type ResolvedAsrTarget =
  | { mode: 'server'; url: string; aiSource: ServerAiSource }
  | { mode: 'local'; modelId: string }
  | { mode: 'cloud_api'; profile: AsrProfile }
