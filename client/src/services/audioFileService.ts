/**
 * Renderer-side service for saving and loading audio files via IPC.
 */

import * as bridge from './bridge'

/**
 * Save PCM chunks (Int16 LE) as WAV file.
 * WAV header 编码在 Rust 侧完成，前端只需传 PCM 原始数据。
 * Returns the saved file path.
 */
export async function saveRecordingAudio(
  id: string,
  chunks: ArrayBuffer[],
  sampleRate = 16000,
): Promise<string | null> {
  const totalLen = chunks.reduce((sum, c) => sum + c.byteLength, 0)
  if (totalLen <= 0) return null

  // 安全检查：PCM 数据量是否合理（16kHz 16bit mono = 32000 bytes/sec）
  const expectedBytesPerSec = sampleRate * 2
  const durationSec = totalLen / expectedBytesPerSec
  if (durationSec > 600) {
    console.warn('[audio] saveRecordingAudio: unusually long audio', {
      totalLen,
      durationSec: durationSec.toFixed(1),
      sampleRate,
      chunkCount: chunks.length,
    })
  }

  // 合并 PCM chunks
  const merged = new Uint8Array(totalLen)
  let offset = 0
  for (const chunk of chunks) {
    merged.set(new Uint8Array(chunk), offset)
    offset += chunk.byteLength
  }

  // 原始字节直接传给 Rust（不再转 base64，见 bridge.framedBody），Rust 侧编码 WAV header 并写入文件
  return bridge.savePcmAsWavRaw(id, merged, sampleRate)
}

/**
 * 读录音并生成 Blob URL 供 <audio> 播放。调用方用完要 URL.revokeObjectURL。
 *
 * 原来是 base64 data URL：五分钟录音是 13 MB 的字符串，Rust 转一遍、IPC 传一遍、
 * 浏览器再解一遍。现在 Rust 直接回字节。
 */
export async function loadAudioAsObjectUrl(filePath: string): Promise<string | null> {
  const bytes = await bridge.readAudioFileBytes(filePath)
  if (!bytes) return null
  return URL.createObjectURL(new Blob([bytes], { type: 'audio/wav' }))
}

/**
 * Delete an audio file.
 */
export async function deleteAudioFileFromRenderer(filePath: string): Promise<boolean> {
  return bridge.deleteAudioFile(filePath).then(() => true)
}
