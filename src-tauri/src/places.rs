//! 置き場所の決め方。**Python 側(`portal/app_config.py`)と同じ決め方**にする
//! ── ずれると、外枠が案内するログの場所と、Python が書いている場所が食い違う。

use std::io::Write;
use std::path::{Path, PathBuf};

/// 日報複合ツール一式のフォルダ(`config/tools.json` と `bridge.py` がある所)。
///
/// 配るときは exe をフォルダの直下に置く。開発中は `src-tauri/target/...` から
/// 動くので、上へたどって探す。`ALLTOOLS_ROOT` で指定もできる。
pub fn app_root() -> PathBuf {
    if let Ok(root) = std::env::var("ALLTOOLS_ROOT") {
        if !root.trim().is_empty() {
            return PathBuf::from(root.trim());
        }
    }
    let exe = std::env::current_exe().unwrap_or_default();
    let mut dir = exe.parent().map(Path::to_path_buf).unwrap_or_default();
    let start = dir.clone();
    for _ in 0..6 {
        if is_app_root(&dir) {
            return dir;
        }
        match dir.parent() {
            Some(parent) => dir = parent.to_path_buf(),
            None => break,
        }
    }
    start
}

/// 一式のフォルダか(各ツールの `bridge.py` と取り違えないよう、`config/tools.json` も見る)
pub fn is_app_root(dir: &Path) -> bool {
    dir.join("bridge.py").is_file() && dir.join("config").join("tools.json").is_file()
}

fn read_json(path: &Path) -> Option<serde_json::Value> {
    let text = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(text.trim_start_matches('\u{feff}')).ok()
}

/// フォルダ `root` の `config/app.json` の `local_dir_name`(ツールごとの手元の領域の名前)
pub fn local_dir_name(root: &Path, fallback: &str) -> String {
    read_json(&root.join("config").join("app.json"))
        .and_then(|conf| conf.get("local_dir_name").and_then(|v| v.as_str()).map(str::to_string))
        .filter(|name| !name.trim().is_empty())
        .unwrap_or_else(|| fallback.to_string())
}

/// 手元の領域(`%LOCALAPPDATA%\<名前>`)。`env` が指定されていればそちら。
/// Python の `app_config.local_root()` と同じ決め方。
pub fn local_root_for(env: &str, name: &str) -> PathBuf {
    if let Ok(dir) = std::env::var(env) {
        if !dir.trim().is_empty() {
            return PathBuf::from(dir.trim());
        }
    }
    if let Ok(base) = std::env::var("LOCALAPPDATA") {
        if !base.trim().is_empty() {
            return PathBuf::from(base).join(name);
        }
    }
    if let Ok(base) = std::env::var("XDG_DATA_HOME") {
        if !base.trim().is_empty() {
            return PathBuf::from(base).join(name);
        }
    }
    PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".local").join("share").join(name)
}

/// 日報複合ツールの手元の領域
pub fn local_root(root: &Path) -> PathBuf {
    local_root_for("ALLTOOLS_LOCAL_DIR", &local_dir_name(root, "AllTools"))
}

/// 起動できないときに案内するログの場所(Python が答えられないとき用)。
/// Store 版の Python では実際の場所が `…\Packages\PythonSoftwareFoundation.Python…\LocalCache\Local\…`
/// になるので、そう添える(Python が答えられるときは Python が正しい場所を返す)。
pub fn log_hint(local: &Path) -> String {
    let logs = local.join("logs");
    if cfg!(windows) {
        format!(
            "{}(Microsoft Store 版の Python では %LOCALAPPDATA%\\Packages\\PythonSoftwareFoundation.Python…\\LocalCache\\Local\\ の下)",
            logs.display()
        )
    } else {
        logs.display().to_string()
    }
}

/// 外枠の記録の置き場所(起動のときに1度だけ決める)
static LOG_ROOT: std::sync::OnceLock<PathBuf> = std::sync::OnceLock::new();

/// 外枠の記録の置き場所を決める(`main` の最初に呼ぶ)
pub fn set_log_root(root: &Path) {
    let _ = LOG_ROOT.set(root.to_path_buf());
}

/// ツールの名前を付けて外枠の記録に書く(`bridge.rs` から)
pub fn shell_log_for(spec: &crate::bridge::Spec, text: &str) {
    if let Some(root) = LOG_ROOT.get() {
        shell_log(root, &format!("[{}] {text}", spec.id));
    }
}

/// 外枠の記録(`<日報複合ツールの手元の領域>\logs\desktop_shell.log`)。
///
/// **Python が書けないことだけを書く**: Python が見つからない・起動できない・
/// 知らせずに落ちた(最後の出力)・同時起動を止めた。後から「なぜ起動しなかったか」を
/// 追えるようにする(Python のログには、そもそも Python が動かなかったことは残らない)。
/// 書けなくても何もしない ── 記録のためにアプリを止めない。
pub fn shell_log(root: &Path, text: &str) {
    let dir = local_root(root).join("logs");
    let _ = std::fs::create_dir_all(&dir);
    let path = dir.join("desktop_shell.log");
    // 大きくなりすぎたら作り直す(落ちるたびに数十行なので、ふつうは届かない)
    if std::fs::metadata(&path).map(|m| m.len() > 1_000_000).unwrap_or(false) {
        let _ = std::fs::remove_file(&path);
    }
    if let Ok(mut file) = std::fs::OpenOptions::new().create(true).append(true).open(&path) {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let _ = writeln!(file, "[{stamp}] pid={} {text}", std::process::id());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 一式のフォルダは二つの目印で見分ける() {
        let dir = std::env::temp_dir().join(format!("alltools_places_{}", std::process::id()));
        std::fs::create_dir_all(dir.join("config")).unwrap();
        std::fs::write(dir.join("bridge.py"), "").unwrap();
        assert!(!is_app_root(&dir), "bridge.py だけではツールのフォルダかもしれない");
        std::fs::write(dir.join("config").join("tools.json"), "{}").unwrap();
        assert!(is_app_root(&dir));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn 手元の領域の名前はconfigから_無ければ既定() {
        let dir = std::env::temp_dir().join(format!("alltools_places_name_{}", std::process::id()));
        std::fs::create_dir_all(dir.join("config")).unwrap();
        assert_eq!(local_dir_name(&dir, "AllTools"), "AllTools");
        std::fs::write(dir.join("config").join("app.json"), "\u{feff}{\"local_dir_name\": \"統合テスト\"}").unwrap();
        assert_eq!(local_dir_name(&dir, "AllTools"), "統合テスト");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn 環境変数があればその場所() {
        std::env::set_var("ALLTOOLS_PLACES_TEST_DIR", "/opt/手元");
        assert_eq!(local_root_for("ALLTOOLS_PLACES_TEST_DIR", "x"), PathBuf::from("/opt/手元"));
        std::env::remove_var("ALLTOOLS_PLACES_TEST_DIR");
    }
}
