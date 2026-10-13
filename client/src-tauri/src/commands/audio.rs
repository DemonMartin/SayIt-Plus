use base64::{engine::general_purpose::STANDARD, Engine};
use std::fs;
use std::path::PathBuf;

fn audio_dir() -> PathBuf {
    let base = dirs::data_local_dir().unwrap_or_else(|| PathBuf::from("."));
    base.join("com.sayit.app").join("audio")
}

/// id 会拼进文件名，带 `..\` 就能写到录音目录外面去。
fn check_record_id(id: &str) -> Result<(), String> {
    if crate::path_guard::is_record_id(id) {
        Ok(())
    } else {
        Err("Invalid recording id".to_string())
    }
}

/// 读、删、判存在只认录音目录里的 .wav。历史记录里的路径会被导入的备份改写，
/// 不能当成可信输入（见 backup.rs 的 rewrite_audio_path）。
fn audio_file_in_dir(file_path: &str) -> Option<PathBuf> {
    let path = PathBuf::from(file_path);
    if !crate::path_guard::has_extension(&path, &["wav"]) {
        return None;
    }
    crate::path_guard::existing_file_in_dir(&path, &audio_dir())
}

#[tauri::command]
pub fn save_audio_file(id: String, wav_base64: String) -> Result<String, String> {
    check_record_id(&id)?;
    let dir = audio_dir();
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;

    let bytes = STANDARD.decode(&wav_base64).map_err(|e| e.to_string())?;
    let path = dir.join(format!("{}.wav", id));
    fs::write(&path, bytes).map_err(|e| e.to_string())?;

    Ok(path.to_string_lossy().to_string())
}

/// 接收 PCM Int16 LE 原始数据（base64），在 Rust 侧编码 WAV header 并写入文件。
/// 避免前端拼 WAV + base64 编码的开销。
#[tauri::command]
pub fn save_pcm_as_wav(id: String, pcm_base64: String, sample_rate: Option<u32>) -> Result<String, String> {
    check_record_id(&id)?;
    let dir = audio_dir();
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;

    let pcm = STANDARD.decode(&pcm_base64).map_err(|e| e.to_string())?;
    let sr = sample_rate.unwrap_or(16000);
    let data_len = pcm.len() as u32;

    // Build 44-byte WAV header + PCM data
    let mut wav = Vec::with_capacity(44 + pcm.len());
    wav.extend_from_slice(b"RIFF");
    wav.extend_from_slice(&(36 + data_len).to_le_bytes());
    wav.extend_from_slice(b"WAVE");
    wav.extend_from_slice(b"fmt ");
    wav.extend_from_slice(&16u32.to_le_bytes());   // chunk size
    wav.extend_from_slice(&1u16.to_le_bytes());    // PCM format
    wav.extend_from_slice(&1u16.to_le_bytes());    // mono
    wav.extend_from_slice(&sr.to_le_bytes());      // sample rate
    wav.extend_from_slice(&(sr * 2).to_le_bytes()); // byte rate
    wav.extend_from_slice(&2u16.to_le_bytes());    // block align
    wav.extend_from_slice(&16u16.to_le_bytes());   // bits per sample
    wav.extend_from_slice(b"data");
    wav.extend_from_slice(&data_len.to_le_bytes());
    wav.extend_from_slice(&pcm);

    let path = dir.join(format!("{}.wav", id));
    fs::write(&path, &wav).map_err(|e| e.to_string())?;

    Ok(path.to_string_lossy().to_string())
}

/// 录音文件还在不在。
///
/// 单独一个命令而不是复用 `read_audio_file`：后者会把整份 WAV 读出来再 base64
/// （五分钟录音约 13MB 的字符串），只为判断"文件存不存在"的话代价太大 ——
/// 历史列表里每条记录都要判一次。
#[tauri::command]
pub fn audio_file_exists(file_path: String) -> bool {
    audio_file_in_dir(&file_path).is_some()
}

/// 目录外的路径按"文件不在"处理，与原来文件缺失时的表现一致，界面显示"录音已不存在"。
#[tauri::command]
pub fn read_audio_file(file_path: String) -> Result<Option<String>, String> {
    let Some(path) = audio_file_in_dir(&file_path) else {
        return Ok(None);
    };
    let bytes = fs::read(&path).map_err(|e| e.to_string())?;
    Ok(Some(STANDARD.encode(&bytes)))
}

/// 目录外的路径静默忽略：删除是清理动作，删不到别人的文件就是正确结果。
#[tauri::command]
pub fn delete_audio_file(file_path: String) -> Result<(), String> {
    if let Some(path) = audio_file_in_dir(&file_path) {
        fs::remove_file(&path).map_err(|e| e.to_string())?;
    }
    Ok(())
}
