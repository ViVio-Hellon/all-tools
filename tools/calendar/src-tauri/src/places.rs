//! 置き場所の決め方。**Python 側(`calendar_app/app_config.py`)と同じ決め方**にする
//! ── ずれると、外枠が案内するログの場所と、Python が書いている場所が食い違う。

use std::io::Write;
use std::path::{Path, PathBuf};

/// アプリ一式のフォルダ(`bridge.py` がある所)。
///
/// 配るときは exe をフォルダの直下に置く。開発中は `src-tauri/target/...` から
/// 動くので、上へたどって探す。`CALENDAR_ROOT` で指定もできる。
pub fn app_root() -> PathBuf {
    if let Ok(root) = std::env::var("CALENDAR_ROOT") {
        if !root.trim().is_empty() {
            return PathBuf::from(root);
        }
    }
    let exe = std::env::current_exe().unwrap_or_default();
    let mut dir = exe.parent().map(Path::to_path_buf).unwrap_or_default();
    let start = dir.clone();
    for _ in 0..5 {
        if dir.join("bridge.py").is_file() {
            return dir;
        }
        match dir.parent() {
            Some(parent) => dir = parent.to_path_buf(),
            None => break,
        }
    }
    start
}

/// この端末のローカル領域(Python の `app_config.local_root()` と同じ決め方)
pub fn local_root(root: &Path) -> PathBuf {
    if let Ok(dir) = std::env::var("CALENDAR_LOCAL_DIR") {
        if !dir.trim().is_empty() {
            return PathBuf::from(dir.trim());
        }
    }
    let name = std::fs::read_to_string(root.join("config").join("app.json"))
        .ok()
        .and_then(|text| serde_json::from_str::<serde_json::Value>(&text).ok())
        .and_then(|conf| conf.get("local_dir_name").and_then(|v| v.as_str()).map(str::to_string))
        .unwrap_or_else(|| "LineCalendar".into());
    if let Ok(base) = std::env::var("LOCALAPPDATA") {
        return PathBuf::from(base).join(name);
    }
    if let Ok(base) = std::env::var("XDG_DATA_HOME") {
        return PathBuf::from(base).join(name);
    }
    PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".local").join("share").join(name)
}

/// 起動できないときに案内するログの場所(Python が答えられないとき用)
pub fn log_hint(root: &Path) -> String {
    local_root(root).join("logs").display().to_string()
}

/// 外枠の記録(`<ローカル領域>\logs\desktop_shell.log`)。
///
/// **Python が書けないことだけを書く**: Python が見つからない・起動できない・
/// 知らせずに落ちた(最後の出力)。後から「なぜ起動しなかったか」を追えるように
/// する(Python のログには、そもそも Python が動かなかったことは残らない)。
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

/// **2つ目を起動させない**ための錠(OS のファイルロック)。取れたら握ったまま返す。
///
/// 多重起動の防止は `tauri-plugin-single-instance` がするが、その仕組みは OS ごとに
/// 違い(Linux は D-Bus)、使えない環境では黙って素通しになる(梱包資材総合ツールの
/// 試験の環境で実際に2つ起動した)。同じ DB に2つの Python が書きに行かないよう、
/// ここでも止める。錠はプロセスが終われば OS が外すので、落ちても残らない。
pub fn take_instance_lock(root: &Path) -> Option<std::fs::File> {
    let dir = local_root(root).join("runtime");
    let _ = std::fs::create_dir_all(&dir);
    let file = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(dir.join("desktop.lock"))
        .ok()?;
    match file.try_lock() {
        Ok(()) => Some(file),
        Err(_) => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ログの場所と錠と外枠の記録() {
        // 環境変数を触るので1つの試験にまとめる(試験は並んで走る)
        let dir = std::env::temp_dir().join(format!("lc_desktop_test_{}", std::process::id()));
        std::fs::create_dir_all(dir.join("config")).unwrap();
        std::fs::write(dir.join("config").join("app.json"), r#"{"local_dir_name": "暦テスト"}"#).unwrap();
        std::env::remove_var("CALENDAR_LOCAL_DIR");
        assert!(log_hint(&dir).contains("暦テスト"));

        std::env::set_var("CALENDAR_LOCAL_DIR", dir.join("local"));
        let first = take_instance_lock(&dir);
        assert!(first.is_some(), "1つ目は錠を取れる");
        assert!(take_instance_lock(&dir).is_none(), "2つ目は取れない");
        drop(first);
        assert!(take_instance_lock(&dir).is_some(), "1つ目が終われば取れる");

        shell_log(&dir, "Python が見つかりません");
        let text = std::fs::read_to_string(dir.join("local").join("logs").join("desktop_shell.log")).unwrap();
        assert!(text.contains("Python が見つかりません"));
        std::env::remove_var("CALENDAR_LOCAL_DIR");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn 置き場所は環境変数で指定できる() {
        std::env::set_var("CALENDAR_ROOT", "/opt/calendar");
        assert_eq!(app_root(), PathBuf::from("/opt/calendar"));
        std::env::remove_var("CALENDAR_ROOT");
    }
}
