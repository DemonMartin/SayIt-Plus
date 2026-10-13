//! 二进制 IPC 请求体：`[4 字节小端 JSON 长度][JSON 参数][原始字节]`。
//!
//! 音频原来在网页里先转 base64、再塞进 JSON 发过来。网页里那个转码是逐字节拼字符串，
//! 2 分钟录音要约 0.4 秒、5 分钟约 0.7 秒，而且跑在主窗主线程上（2026-10 实测），
//! 本地 / 云 API 模式录完还要转两次（存档一次、识别一次）。改成直接发原始字节后，
//! 这一段几乎为零。参数里可能有中文热词、几 KB 长，放请求头不合适，所以和音频拼在同一个请求体里。

use serde::de::DeserializeOwned;

/// 从 Tauri 的原始请求体里拆出参数和载荷。
pub fn split_framed<T: DeserializeOwned>(request: &tauri::ipc::Request<'_>) -> Result<(T, Vec<u8>), String> {
    let tauri::ipc::InvokeBody::Raw(body) = request.body() else {
        return Err("Expected a binary request body".to_string());
    };
    split_framed_bytes(body)
}

pub fn split_framed_bytes<T: DeserializeOwned>(body: &[u8]) -> Result<(T, Vec<u8>), String> {
    if body.len() < 4 {
        return Err("Binary request body is too short".to_string());
    }
    let json_len = u32::from_le_bytes([body[0], body[1], body[2], body[3]]) as usize;
    let json_end = 4usize
        .checked_add(json_len)
        .filter(|end| *end <= body.len())
        .ok_or_else(|| "Binary request body has an invalid header length".to_string())?;
    let meta: T = serde_json::from_slice(&body[4..json_end])
        .map_err(|e| format!("Invalid request parameters: {e}"))?;
    Ok((meta, body[json_end..].to_vec()))
}

#[cfg(test)]
mod tests {
    use super::split_framed_bytes;
    use serde_json::{json, Value};

    fn frame(meta: &Value, payload: &[u8]) -> Vec<u8> {
        let json = serde_json::to_vec(meta).unwrap();
        let mut out = (json.len() as u32).to_le_bytes().to_vec();
        out.extend_from_slice(&json);
        out.extend_from_slice(payload);
        out
    }

    #[test]
    fn splits_parameters_and_payload() {
        let body = frame(&json!({ "id": "abc", "hotwords": ["中文热词"] }), &[1, 2, 3, 4]);
        let (meta, payload): (Value, Vec<u8>) = split_framed_bytes(&body).unwrap();
        assert_eq!(meta["hotwords"][0], "中文热词");
        assert_eq!(payload, vec![1, 2, 3, 4]);
    }

    #[test]
    fn empty_payload_is_fine() {
        let (_, payload): (Value, Vec<u8>) = split_framed_bytes(&frame(&json!({}), &[])).unwrap();
        assert!(payload.is_empty());
    }

    #[test]
    fn rejects_truncated_or_lying_headers() {
        assert!(split_framed_bytes::<Value>(&[1, 0]).is_err());
        let mut body = frame(&json!({}), &[]);
        body[0] = 200; // 声称 JSON 有 200 字节
        assert!(split_framed_bytes::<Value>(&body).is_err());
    }
}
