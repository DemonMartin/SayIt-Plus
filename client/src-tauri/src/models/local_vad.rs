//! 本地 ASR 前置语音检测。
//!
//! Qwen3-ASR 这类生成式模型遇到纯静音或环境底噪时，可能生成并不存在的文字。
//! 这里参考 Handy 的做法，用独立的 Silero VAD 在推理前确认音频里确实有人声。
//! 与 Handy 不同的是，我们只用 VAD 确定首尾边界，保留句子中间的停顿；历史记录
//! 保存的也仍然是未经裁剪的原始录音。

use std::ops::Range;

use silero_vad_crs::{
    get_timestamps_from_probs_with_config, SileroVad, TimestampConfig,
};

/// 与 Handy 一致使用较宽松的 0.3，优先避免漏掉轻声说话。
const SPEECH_THRESHOLD: f32 = 0.3;
/// 过滤总时长不足约 60ms 的候选语音段，抑制短促点击声和提示音。
/// 这与 Handy 的“连续两帧起声”目标相同，但 Silero 时间戳算法带滞回，行为并非逐帧等价。
const MIN_SPEECH_MS: usize = 60;
/// 至少持续 450ms 的非语音才切分语音段，减少短暂停顿导致的误切分。
const MIN_SILENCE_MS: usize = 450;
/// 起止各留约 450ms，避免切掉辅音、轻声开头和句尾。
const SPEECH_PAD_MS: usize = 450;

/// 返回每一段人声（已含首尾留白），按时间排序、互不重叠。
///
/// 空列表明确表示没有检测到人声，调用方应跳过 ASR；`Err` 表示无法可靠判断是否
/// 有人声，为避免生成式模型对静音产生幻觉，调用方也必须停止本次 ASR。
/// 交给模型前再经 `plan_chunks` 拼块：短录音就是首段开头到末段结尾一整块。
pub fn detect_speech_segments(
    samples: &[f32],
    sample_rate: usize,
) -> Result<Vec<Range<usize>>, String> {
    if samples.is_empty() {
        return Ok(Vec::new());
    }

    let mut vad = SileroVad::with_sample_rate(sample_rate)
        .map_err(|e| format!("Failed to create Silero VAD: {e}"))?;
    let window_size_samples = vad.source_window_samples();
    let probabilities = vad
        .forward_audio(samples)
        .map_err(|e| format!("Silero VAD inference failed: {e}"))?;

    let segments = get_timestamps_from_probs_with_config(
        &probabilities,
        samples.len(),
        TimestampConfig {
            sampling_rate: sample_rate,
            threshold: SPEECH_THRESHOLD,
            min_speech_duration_ms: MIN_SPEECH_MS,
            min_silence_duration_ms: MIN_SILENCE_MS,
            speech_pad_ms: SPEECH_PAD_MS,
            window_size_samples,
            ..Default::default()
        },
    );

    let ranges = normalize_segments(
        segments.iter().map(|s| s.start..s.end),
        samples.len(),
    );

    if let (Some(first), Some(last)) = (ranges.first(), ranges.last()) {
        log::debug!(
            "Silero VAD: {:.2}s -> {:.2}s, speech_segments={}",
            samples.len() as f64 / sample_rate as f64,
            (last.end - first.start) as f64 / sample_rate as f64,
            ranges.len(),
        );
    }
    Ok(ranges)
}

/// 截到音频长度以内、丢掉空段、按起点排序，并把重叠或首尾相接的段合并。
/// 加了留白之后相邻两段可能互相覆盖，后面切块要求段与段之间不重叠。
fn normalize_segments(
    segments: impl IntoIterator<Item = Range<usize>>,
    len: usize,
) -> Vec<Range<usize>> {
    let mut ranges: Vec<Range<usize>> = segments
        .into_iter()
        .map(|r| r.start.min(len)..r.end.min(len))
        .filter(|r| r.start < r.end)
        .collect();
    ranges.sort_by_key(|r| r.start);
    let mut merged: Vec<Range<usize>> = Vec::with_capacity(ranges.len());
    for r in ranges {
        match merged.last_mut() {
            Some(last) if r.start <= last.end => last.end = last.end.max(r.end),
            _ => merged.push(r),
        }
    }
    merged
}

/// 把人声段拼成一块块不超过 `max_len` 个采样的识别块，切口落在停顿上。
///
/// 为什么要切：模型一次吃整段长录音，交给显卡的单个任务会随长度变长（Qwen3-ASR
/// 的注意力计算随长度平方增长，实测 1.7B 在集成显卡上 30 s 的录音 0.18 倍时长，
/// 102 s 就到了 0.29 倍）。一个任务超过 Windows 显卡看门狗的 2 秒，显卡就被重置
/// （LiveKernelEvent 141，Vulkan 报 DeviceLost），本次和之后的本地识别全部失败。
/// 2026-10-19 一条 220 秒的录音就是这样把显卡搞挂的。
///
/// 规则：
/// - 相邻人声段贪心地并进同一块，块内的短停顿原样保留（短录音结果与切块之前
///   只裁首尾时一致：总长不超过上限时只有一块，就是首段开头到末段结尾）；
/// - 两块之间的长停顿直接丢掉，那里本来就是静音；
/// - 单独一段话就超过上限（一口气说了很久、中间没有 0.45 s 以上的停顿）时，
///   在这一块的后三分之一里找最安静的 100 ms 切开，找不到更好的位置才硬切。
pub fn plan_chunks(samples: &[f32], segments: &[Range<usize>], max_len: usize) -> Vec<Range<usize>> {
    let segments = normalize_segments(segments.iter().cloned(), samples.len());
    if max_len == 0 {
        return segments;
    }
    let mut chunks: Vec<Range<usize>> = Vec::new();
    let mut current: Option<Range<usize>> = None;
    for seg in segments {
        current = match current {
            Some(cur) if seg.end - cur.start <= max_len => Some(cur.start..seg.end),
            Some(cur) => {
                chunks.push(cur);
                Some(seg)
            }
            None => Some(seg),
        };
    }
    if let Some(cur) = current {
        chunks.push(cur);
    }

    let mut out = Vec::with_capacity(chunks.len());
    for chunk in chunks {
        split_overlong(samples, chunk, max_len, &mut out);
    }
    out
}

/// 一段连续人声超过上限时，在 [上限的 2/3, 上限] 里找最安静的 100 ms 切开。
fn split_overlong(samples: &[f32], chunk: Range<usize>, max_len: usize, out: &mut Vec<Range<usize>>) {
    const FRAME: usize = 1600; // 16 kHz 下 100 ms
    let mut start = chunk.start;
    while chunk.end - start > max_len {
        let search_from = start + max_len * 2 / 3;
        let search_to = start + max_len;
        let mut best_cut = search_to;
        let mut best_level = f32::MAX;
        let mut frame_start = search_from;
        while frame_start + FRAME <= search_to {
            let frame = &samples[frame_start..frame_start + FRAME];
            let level = frame.iter().map(|s| s.abs()).sum::<f32>() / FRAME as f32;
            if level < best_level {
                best_level = level;
                best_cut = frame_start + FRAME / 2;
            }
            frame_start += FRAME;
        }
        out.push(start..best_cut);
        start = best_cut;
    }
    out.push(start..chunk.end);
}

#[cfg(test)]
mod tests {
    use super::*;

    const SR: usize = 16_000;

    fn read_test_speech() -> Vec<f32> {
        let bytes = include_bytes!("../../resources/test_zh.wav");
        bytes[44..]
            .chunks_exact(2)
            .map(|c| i16::from_le_bytes([c[0], c[1]]) as f32 / 32768.0)
            .collect()
    }

    #[test]
    fn pure_silence_is_rejected() {
        let audio = vec![0.0; SR * 3];
        assert!(detect_speech_segments(&audio, SR).unwrap().is_empty());
    }

    #[test]
    fn steady_background_noise_is_rejected() {
        let mut state = 0x1234_5678u32;
        let audio: Vec<f32> = (0..SR * 3)
            .map(|_| {
                state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
                ((state >> 8) as f32 / 16_777_215.0 - 0.5) * 0.02
            })
            .collect();
        assert!(detect_speech_segments(&audio, SR).unwrap().is_empty());
    }

    #[test]
    fn short_transient_noise_is_rejected() {
        let mut audio = vec![0.0; SR];
        // 模拟一次约 32ms 的敲击/提示音；不应满足持续人声条件。
        for (i, sample) in audio[SR / 2..SR / 2 + 512].iter_mut().enumerate() {
            let t = i as f32 / SR as f32;
            *sample = 0.4 * (2.0 * std::f32::consts::PI * 1_500.0 * t).sin();
        }
        assert!(detect_speech_segments(&audio, SR).unwrap().is_empty());
    }

    #[test]
    fn bundled_speech_is_detected() {
        let speech = read_test_speech();
        assert!(
            !detect_speech_segments(&speech, SR).unwrap().is_empty(),
            "真实中文语音不应被 VAD 拒绝",
        );
    }

    #[test]
    fn quiet_speech_is_detected() {
        let speech: Vec<f32> = read_test_speech().into_iter().map(|s| s * 0.2).collect();
        assert!(
            !detect_speech_segments(&speech, SR).unwrap().is_empty(),
            "降低约 14dB 的轻声语音不应被 VAD 拒绝",
        );
    }

    const S: usize = SR; // 一秒的采样数，读起来更直观

    #[test]
    fn short_audio_stays_one_chunk_with_its_pauses() {
        // 总跨度 20 s < 30 s：一块，从首段开头到末段结尾，中间的停顿保留
        let audio = vec![0.1; 25 * S];
        let segments = vec![S..5 * S, 8 * S..12 * S, 15 * S..21 * S];
        assert_eq!(plan_chunks(&audio, &segments, 30 * S), vec![S..21 * S]);
    }

    #[test]
    fn long_audio_is_cut_at_pauses_and_every_chunk_fits() {
        // 220 s：每 10 s 一段话、段间 2 s 停顿
        let audio = vec![0.1; 220 * S];
        let segments: Vec<_> = (0..18).map(|i| (i * 12) * S..(i * 12 + 10) * S).collect();
        let chunks = plan_chunks(&audio, &segments, 30 * S);
        assert!(chunks.len() >= 8, "220 s 应该切成多块，实际 {}", chunks.len());
        for c in &chunks {
            assert!(c.end - c.start <= 30 * S, "块超长：{:?}", c);
            // 切口都落在某段话的边界上，不会把话切成两半
            assert!(segments.iter().any(|s| s.start == c.start));
            assert!(segments.iter().any(|s| s.end == c.end));
        }
        // 所有人声都被覆盖，一段不漏
        for s in &segments {
            assert!(chunks.iter().any(|c| c.start <= s.start && s.end <= c.end));
        }
    }

    #[test]
    fn one_long_breathless_segment_is_split_at_its_quietest_point() {
        // 一口气说 70 s，中间 45~45.1 s 有一处很轻的地方，没到 VAD 认定的停顿
        let mut audio = vec![0.3; 70 * S];
        for s in &mut audio[45 * S..45 * S + 1600] {
            *s = 0.01;
        }
        let chunks = plan_chunks(&audio, &[0..70 * S], 30 * S);
        assert!(chunks.iter().all(|c| c.end - c.start <= 30 * S), "{chunks:?}");
        assert_eq!(chunks.first().unwrap().start, 0);
        assert_eq!(chunks.last().unwrap().end, 70 * S);
        // 首尾相接，没有丢音频
        for pair in chunks.windows(2) {
            assert_eq!(pair[0].end, pair[1].start);
        }
        // 搜索窗是每块的 [20 s, 30 s]。安静点落在搜索窗里时，必须正好切在那 100 ms 的中间
        let mut audio2 = vec![0.3; 50 * S];
        for s in &mut audio2[25 * S..25 * S + 1600] {
            *s = 0.01;
        }
        let chunks2 = plan_chunks(&audio2, &[0..50 * S], 30 * S);
        assert_eq!(chunks2[0].end, 25 * S + 800, "{chunks2:?}");
    }

    #[test]
    fn overlapping_padded_segments_are_merged() {
        let audio = vec![0.1; 10 * S];
        let chunks = plan_chunks(&audio, &[3 * S..6 * S, S..4 * S, 9 * S..12 * S], 30 * S);
        // 截到音频长度、合并重叠，再并成一块
        assert_eq!(chunks, vec![S..10 * S]);
    }

    #[test]
    fn internal_pause_is_preserved() {
        let speech = read_test_speech();
        let half = speech.len() / 2;
        let mut audio = vec![0.0; SR];
        audio.extend_from_slice(&speech[..half]);
        let pause_start = audio.len();
        audio.extend(std::iter::repeat(0.0).take(SR * 2));
        let pause_end = audio.len();
        audio.extend_from_slice(&speech[half..]);
        audio.extend(std::iter::repeat(0.0).take(SR));

        // 走生产路径：分段 → 拼块。几秒的录音只有一块，句中 2 s 的停顿留在块里
        let segments = detect_speech_segments(&audio, SR).unwrap();
        assert!(!segments.is_empty(), "前后两段真实语音应被检测到");
        let chunks = plan_chunks(&audio, &segments, 30 * SR);
        assert_eq!(chunks.len(), 1, "短录音应只有一块：{chunks:?}");
        let span = chunks[0].clone();
        assert!(span.start < pause_start);
        assert!(span.end > pause_end);
        assert!(audio[span.clone()][pause_start - span.start..pause_end - span.start]
            .iter()
            .all(|sample| *sample == 0.0));
    }
}
