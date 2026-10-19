import * as bridge from '../bridge'
import { addRuntimeEvent } from '../debugLog'
import { saveRecordingAudio } from '../audioFileService'
import { getSetting, updateHistoryRecord } from '../store'

/**
 * 等音频落盘的上限。
 *
 * 存档是为了兜底，不该反过来把结果处理挂死：写盘走一次大 payload IPC（长录音的 PCM 有
 * 好几 MB），一旦它不返回，等它的那条链路就永远出不来，界面停在「处理中」什么也不说。
 * 超时后先按无音频写记录，落盘真的完成时再把路径补回去。
 */
const AUDIO_ARCHIVE_WAIT_MS = 30_000

/**
 * 一次录音的音频存档。
 *
 * 关键点：写盘在**停止录音时立刻**开始，不等 ASR/AI 结果。以前音频只在某个终态分支里
 * 才落盘，于是任何一条没走到终态的路（超时后被新录音顶掉、连接断了在假等待、进程退出）
 * 都会让整段录音消失得无声无息。现在结果只决定历史记录里写什么，不再决定音频是否存在。
 */
interface AudioArchive {
  runId: number
  /** 本代唯一的历史记录 id。所有终态路径必须复用它，保证一次录音只有一条记录、一份文件。 */
  recordId: string
  /** 落盘任务；resolve 为 WAV 路径，null = 未保存（关了音频保留 / 无数据 / 写失败）。 */
  promise: Promise<string | null>
  /** 用户显式取消后置位；写盘可能还没结束，所以要等 promise 出来才知道删哪个文件。 */
  discarded: boolean
}

/**
 * 每一代录音的音频存档，按 runId 索引。从 RecorderOrchestrator 拆出（模块 6），行为不变。
 *
 * 必须按代保存、不能只留一份：超时兜底要等 15 秒宽限，这期间用户完全可能已经录完了
 * 下一段。只留一份的话，旧代来取存档时拿到的是新代的，只能放弃 —— 音频又成了孤儿文件、
 * 历史里依旧什么都没有。
 */
export class AudioArchiveService {
  private archives = new Map<number, AudioArchive>()

  /**
   * @param onLateAttached 落盘超时、之后又成功补进历史时调用（用来把失败卡上的
   *   「尚未确定」升级成明确去处）。
   */
  constructor(private readonly onLateAttached: (runId: number) => void) {}

  has(runId: number): boolean {
    return this.archives.has(runId)
  }

  /**
   * 停止录音后立刻把音频写盘，不等识别结果。
   *
   * 返回本代的 recordId：所有终态路径（正常 final / 空识别 / 报错 / 超时兜底）都必须用
   * take 取回它，绝不再自己生成 id、自己保存一遍。
   */
  begin(runId: number, chunks: ArrayBuffer[]): string {
    const recordId = Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
    const promise = (async (): Promise<string | null> => {
      try {
        if (chunks.length === 0) return null
        const [historyEnabled, retentionEnabled] = await Promise.all([
          getSetting('historyEnabled', true),
          getSetting('audioRetentionEnabled', true),
        ])
        if (!historyEnabled || !retentionEnabled) return null
        const savedPath = await saveRecordingAudio(recordId, chunks)
        addRuntimeEvent('info', 'recorder', 'Recording audio archived', {
          runId,
          recordId,
          saved: Boolean(savedPath),
          chunks: chunks.length,
        })
        return savedPath
      } catch (error) {
        addRuntimeEvent('warn', 'recorder', 'Failed to archive recording audio', {
          runId,
          recordId,
          error: String(error),
        })
        return null
      }
    })()
    this.archives.set(runId, { runId, recordId, promise, discarded: false })
    // 正常情况下每条存档都会被某个终态取走；关掉历史等分支不会取，这里兜一下上限。
    if (this.archives.size > 8) {
      const oldest = this.archives.keys().next()
      if (!oldest.done) this.archives.delete(oldest.value)
    }
    return recordId
  }

  /**
   * 确保本代音频已经在落盘。
   *
   * 正常路径由 stopRecording 发起；错误可能在**录音进行中**就到达（连接断开），那时还没
   * 走到 stopRecording，必须在这里补一次，否则这段音频没人保存。
   */
  ensure(runId: number, chunks: ArrayBuffer[]): void {
    if (this.archives.has(runId)) return
    this.begin(runId, chunks)
  }

  /** 取回本代已存档的音频；返回 null 表示这一代没有存档（例如时长不足没进 processing）。 */
  async take(
    runId: number,
  ): Promise<{ recordId: string; audioFilePath?: string; pendingLate?: boolean } | null> {
    const archive = this.archives.get(runId)
    if (!archive) return null
    // 取走即移交：一次录音只有一条终态路径会走到这里（finalHandled / settled 互斥保证）。
    this.archives.delete(runId)
    let timedOutWaiting = false
    const savedPath = await Promise.race([
      archive.promise,
      new Promise<null>((resolve) => setTimeout(() => {
        timedOutWaiting = true
        resolve(null)
      }, AUDIO_ARCHIVE_WAIT_MS)),
    ])
    if (archive.discarded) return null
    if (timedOutWaiting) {
      // 落盘 IPC 卡住时绝不能把整条结果处理一起挂死（那正是「悬浮条无限转圈、什么都不说」
      // 的成因之一）。记录先写出去，音频路径等落盘真的完成再补上。
      addRuntimeEvent('warn', 'recorder', 'Audio archive did not finish in time; writing history without the audio path', {
        runId,
        recordId: archive.recordId,
        waitedMs: AUDIO_ARCHIVE_WAIT_MS,
      })
      void archive.promise.then(async (latePath) => {
        if (!latePath || archive.discarded) return
        try {
          await updateHistoryRecord(archive.recordId, { audioFilePath: latePath })
          void bridge.emit('history-updated')
          addRuntimeEvent('info', 'recorder', 'Attached late audio archive to its history record', {
            recordId: archive.recordId,
          })
          // 落盘终于成功了：若那张失败卡还开着，把"尚未确定"升级成明确的去处。
          // updateFailureRecovery 自己校验 token 与当前状态，卡片已关时是空操作。
          this.onLateAttached(archive.runId)
        } catch (error) {
          addRuntimeEvent('warn', 'recorder', 'Failed to attach late audio archive', {
            recordId: archive.recordId,
            error: String(error),
          })
        }
      })
      // pendingLate：落盘还在继续，稍后大概率会成功并补进历史。调用方据此说
      // "尚未确定"，绝不能说成"没有留下录音" —— 那是错的，而且不可挽回地误导用户。
      return { recordId: archive.recordId, pendingLate: true }
    }
    return { recordId: archive.recordId, audioFilePath: savedPath ?? undefined }
  }

  /** 用户主动取消：把这份存档标为废弃，写盘完成后删掉文件。 */
  discard(runId: number) {
    const archive = this.archives.get(runId)
    if (!archive || archive.discarded) return
    archive.discarded = true
    this.archives.delete(runId)
    void archive.promise.then((savedPath) => {
      if (!savedPath) return
      void bridge.deleteAudioFile(savedPath).catch(() => { /* 孤儿文件删除失败不影响状态复位 */ })
    })
  }
}
