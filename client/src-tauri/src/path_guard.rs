//! 文件类 IPC 命令的路径校验。
//!
//! 这些命令（读删录音、诊断包、导出、存到下载目录）的路径都来自前端。前端是自己写的，
//! 但它跑在 webview 里：哪天混进一段恶意脚本（依赖被投毒、渲染了不该渲染的内容），
//! 它就能借这些命令读写删本机任意文件。所以 Rust 侧按"这个命令本来只该碰哪里"收口，
//! 不信任前端给的路径。

use std::path::{Path, PathBuf};

/// 只由字母数字、`.`、`_`、`-` 组成的单段文件名，且以指定扩展名结尾。
/// 不允许路径分隔符、`..`、开头的点、Windows 保留字符。
pub fn is_plain_file_name(name: &str, extension: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    !name.is_empty()
        && name.len() <= 128
        && !name.starts_with('.')
        && !name.contains("..")
        && lower.ends_with(&format!(".{}", extension.to_ascii_lowercase()))
        && name.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

/// 录音 id：前端用 `Date.now().toString(36) + 随机 4 位` 生成，只会是字母数字。
/// 放宽到 `_`、`-` 是给以后换 uuid 留余地。
pub fn is_record_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 64 && id.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-'))
}

pub fn has_extension(path: &Path, allowed: &[&str]) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| allowed.iter().any(|a| e.eq_ignore_ascii_case(a)))
}

/// 已存在的文件是否直接位于 dir 里（不含子目录）。两边都 canonicalize，
/// 所以 `..`、符号链接、大小写不同的写法都会落到同一个真实路径上再比。
pub fn existing_file_in_dir(path: &Path, dir: &Path) -> Option<PathBuf> {
    let file = std::fs::canonicalize(path).ok()?;
    let dir = std::fs::canonicalize(dir).ok()?;
    (file.is_file() && file.parent() == Some(dir.as_path())).then_some(file)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plain_file_names() {
        assert!(is_plain_file_name("sayit_20261012_101010.wav", "wav"));
        assert!(is_plain_file_name("mshhzwhhyxxw.WAV", "wav"));
        assert!(!is_plain_file_name("evil.exe", "wav"));
        assert!(!is_plain_file_name("a.wav.exe", "wav"));
        assert!(!is_plain_file_name("..\\..\\Startup\\x.wav", "wav"));
        assert!(!is_plain_file_name("../x.wav", "wav"));
        assert!(!is_plain_file_name("C:x.wav", "wav"));
        assert!(!is_plain_file_name(".wav", "wav"));
        assert!(!is_plain_file_name("a b.wav", "wav"));
    }

    #[test]
    fn record_ids() {
        assert!(is_record_id("mshhzwhhyxxw"));
        assert!(is_record_id("1b9d6bcd-bbfd-4b2d"));
        assert!(!is_record_id(""));
        assert!(!is_record_id("..\\x"));
        assert!(!is_record_id("a/b"));
        assert!(!is_record_id("a.b"));
    }

    #[test]
    fn files_outside_the_directory_are_rejected() {
        let root = std::env::temp_dir().join(format!("sayit-path-guard-{}", uuid::Uuid::new_v4()));
        let inner = root.join("audio");
        std::fs::create_dir_all(&inner).unwrap();
        std::fs::write(inner.join("a.wav"), b"x").unwrap();
        std::fs::write(root.join("b.wav"), b"x").unwrap();

        assert!(existing_file_in_dir(&inner.join("a.wav"), &inner).is_some());
        assert!(existing_file_in_dir(&inner.join("..").join("b.wav"), &inner).is_none());
        assert!(existing_file_in_dir(&root.join("b.wav"), &inner).is_none());
        assert!(existing_file_in_dir(&inner.join("missing.wav"), &inner).is_none());
        assert!(existing_file_in_dir(&inner, &inner).is_none());

        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn extensions() {
        assert!(has_extension(Path::new("x.ZIP"), &["zip"]));
        assert!(!has_extension(Path::new("x.zip.exe"), &["zip"]));
        assert!(!has_extension(Path::new("x"), &["zip"]));
    }
}
