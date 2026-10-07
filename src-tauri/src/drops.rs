//! ファイルのドラッグ&ドロップ(看板の「Access の最新で表の中身を入れ替える」・日報の取り込み)。
//!
//! **窓への落下は OS の仕組み(Tauri)で受け、落ちた場所のツールの画面へ外枠が渡す。**
//! 以前は WebView2 に任せていた(画面の HTML がそのまま受ける)が、Windows の現場で
//! 「ドラッグ&ドロップが効かない」(大きなタブの枠 = iframe の中の画面へ落としても届かない)。
//!
//! 1. ここ(窓の出来事)… 落ちたファイルの場所を覚え、位置と名前・大きさだけを大きなタブの画面へ
//! 2. 大きなタブの画面(`shell.js`)… その位置にあるツールの枠へ渡す
//! 3. 台本(`embed.js`)… その位置の要素に dragenter / dragover / drop を起こす。
//!    受ける要素(落とす枠)の上で離したときだけ、中身を頼む
//! 4. [`shell_dropped_file`] … **いま落とされたファイルだけ**を読んで返す(ほかの場所は読ませない)

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::UNIX_EPOCH;

use serde_json::{json, Value};
use tauri::{DragDropEvent, State};

use crate::shell::Shell;

/// 渡す大きさの上限(看板の受け取りと同じ。Access の上限 2GB より手前)
pub const LIMIT_BYTES: u64 = 1024 * 1024 * 1024;

/// いちばん新しく落とされたファイル(フォルダは入れない)。[`shell_dropped_file`] はここだけ読む
#[derive(Default)]
pub struct Dropped(Mutex<Vec<PathBuf>>);

impl Dropped {
    fn set(&self, files: Vec<PathBuf>) {
        if let Ok(mut held) = self.0.lock() {
            *held = files;
        }
    }

    fn get(&self, index: usize) -> Option<PathBuf> {
        self.0.lock().ok().and_then(|held| held.get(index).cloned())
    }
}

/// 落とされたもののうちファイルだけ(フォルダは渡さない)
fn files_only(paths: &[PathBuf]) -> Vec<PathBuf> {
    paths.iter().filter(|p| p.is_file()).cloned().collect()
}

/// 画面へ渡すのは名前・大きさ・更新日時だけ(場所は渡さない)
pub fn describe(path: &Path) -> Value {
    let meta = std::fs::metadata(path).ok();
    let modified = meta
        .as_ref()
        .and_then(|m| m.modified().ok())
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0);
    json!({
        "name": path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default(),
        "size": meta.map(|m| m.len()).unwrap_or(0),
        "modified": modified,
    })
}

/// 大きなタブの画面へ渡す1つの知らせ(`window.__shell.fileDrag(…)` の引数)
pub fn payload(event: &DragDropEvent, dropped: &Dropped) -> Option<Value> {
    // Windows の位置は物理の画素(画面の CSS の大きさとは表示倍率だけ違う)。画面で割り戻す
    let physical = cfg!(windows);
    let at = |kind: &str, x: f64, y: f64| json!({ "kind": kind, "x": x, "y": y, "physical": physical });
    match event {
        DragDropEvent::Enter { paths, position } => {
            let mut body = at("enter", position.x, position.y);
            body["files"] = Value::Array(files_only(paths).iter().map(|p| describe(p)).collect());
            body["folders"] = json!(paths.iter().filter(|p| p.is_dir()).count());
            Some(body)
        }
        DragDropEvent::Over { position } => Some(at("over", position.x, position.y)),
        DragDropEvent::Drop { paths, position } => {
            let files = files_only(paths);
            let mut body = at("drop", position.x, position.y);
            body["files"] = Value::Array(files.iter().map(|p| describe(p)).collect());
            body["folders"] = json!(paths.iter().filter(|p| p.is_dir()).count());
            dropped.set(files);
            Some(body)
        }
        DragDropEvent::Leave => Some(json!({ "kind": "leave" })),
        _ => None,
    }
}

/// 窓の出来事(`WindowEvent::DragDrop`)を受けて、大きなタブの画面へ渡す
pub fn on_drag(shell: &Arc<Shell>, dropped: &Dropped, event: &DragDropEvent) {
    if let Some(body) = payload(event, dropped) {
        shell.tell_shell(&format!("window.__shell && window.__shell.fileDrag({body})"));
    }
}

/// いま落とされたファイルの中身(`index` 番目)。**落とされたファイルのほかは読まない。**
pub fn read_dropped(dropped: &Dropped, index: usize) -> Result<Vec<u8>, String> {
    let path = dropped
        .get(index)
        .ok_or("落としたファイルが見当たりません。もう一度落としてください")?;
    let meta = std::fs::metadata(&path).map_err(|e| format!("ファイルを開けません: {}({e})", path.display()))?;
    if meta.len() > LIMIT_BYTES {
        return Err(format!("ファイルが大きすぎます(1GB まで): {}", path.display()));
    }
    std::fs::read(&path).map_err(|e| format!("ファイルを読めません: {}({e})", path.display()))
}

/// 大きなタブの画面が、落とされた枠(ツールの画面)の頼みで呼ぶ。中身はそのままのバイト列で返す。
/// 共有フォルダのファイルは読むのに時間が掛かるので、窓を止めないよう別の糸で読む
#[tauri::command]
pub async fn shell_dropped_file(
    dropped: State<'_, Arc<Dropped>>,
    index: usize,
) -> Result<tauri::ipc::Response, String> {
    let dropped = dropped.inner().clone();
    let bytes = tauri::async_runtime::spawn_blocking(move || read_dropped(&dropped, index))
        .await
        .map_err(|e| format!("ファイルを読めません({e})"))??;
    Ok(tauri::ipc::Response::new(bytes))
}

#[cfg(test)]
mod tests {
    use super::*;
    use tauri::PhysicalPosition;

    fn temp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("alltools-drops-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn 落としたファイルだけを読ませる() {
        let dir = temp_dir("only");
        let file = dir.join("看板マスタ.accdb");
        std::fs::write(&file, b"abc").unwrap();
        let other = dir.join("ほかのファイル.txt");
        std::fs::write(&other, b"secret").unwrap();
        let sub = dir.join("フォルダ");
        std::fs::create_dir_all(&sub).unwrap();

        let dropped = Dropped::default();
        assert!(read_dropped(&dropped, 0).is_err(), "落とす前から読めた");
        let event = DragDropEvent::Drop {
            paths: vec![sub.clone(), file.clone()],
            position: PhysicalPosition::new(150.0, 30.0),
        };
        let body = payload(&event, &dropped).unwrap();
        assert_eq!(body["kind"], "drop");
        assert_eq!(body["x"], 150.0);
        assert_eq!(body["folders"], 1);
        let files = body["files"].as_array().unwrap();
        assert_eq!(files.len(), 1, "フォルダまで渡した");
        assert_eq!(files[0]["name"], "看板マスタ.accdb");
        assert_eq!(files[0]["size"], 3);
        assert!(!body.to_string().contains(&dir.display().to_string()), "場所まで画面へ渡した");

        assert_eq!(read_dropped(&dropped, 0).unwrap(), b"abc");
        assert!(read_dropped(&dropped, 1).is_err(), "落としていないものを読めた");

        // 次に落とせば、前のファイルはもう読めない
        let event = DragDropEvent::Drop { paths: vec![other.clone()], position: PhysicalPosition::new(0.0, 0.0) };
        payload(&event, &dropped).unwrap();
        assert_eq!(read_dropped(&dropped, 0).unwrap(), b"secret");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn 上を通るあいだは位置だけ() {
        let dropped = Dropped::default();
        let over = payload(&DragDropEvent::Over { position: PhysicalPosition::new(1.0, 2.0) }, &dropped).unwrap();
        assert_eq!(over["kind"], "over");
        assert!(over.get("files").is_none());
        assert_eq!(payload(&DragDropEvent::Leave, &dropped).unwrap()["kind"], "leave");
    }
}
