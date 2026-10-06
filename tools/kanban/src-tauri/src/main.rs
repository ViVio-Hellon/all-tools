//! 資材発注看板システム デスクトップ版の外枠
//!
//! 【役割の分け方】(各言語が得意なことをする)
//!
//! - **Rust(ここ)**: 窓・Python の起動と監視・多重起動の防止・終了の確認・
//!   帳票の窓・保存/選択の標準ダイアログ・モード切替での立て直し・
//!   社内サイトを既定のブラウザで開く
//! - **Python(`bridge.py` 以下)**: 業務の処理(取り込み・書き戻し・集計・権限・記録)と画面の組み立て
//! - **JS(`app/static/js`)**: 画面の操作(窓まわりだけ `desktop.js` がここへ頼む)
//!
//! 【ポートを使わない】
//! 画面(WebView)は独自の宛先 `app://localhost/`(Windows では
//! `http://app.localhost/`)を読む。その要求をここで受けて、Python の標準入力へ
//! 渡す(`bridge.rs`)。ブラウザ版のように 127.0.0.1 で待ち受けないので、
//! ポートの取り合い・プロキシ・セキュリティ製品に左右されない。
//! モード(現場/倉庫/倉庫参照)もポートに結びつかないので、走ったまま
//! Python だけを立て直して切り替えられる。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod bridge;
mod pages;

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use tauri::http::{Request, Response};
use tauri::ipc::InvokeBody;
use tauri::{AppHandle, Manager, RunEvent, State, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_opener::OpenerExt;

use bridge::{Bridge, Phase};

/// 画面の宛先の名前。Python 側の `app_config.BRIDGE_HOSTS` と揃える
const SCHEME: &str = "app";
const TITLE: &str = "資材発注看板システム";

/// 画面の宛先の頭。Windows(WebView2)は `http://<名前>.localhost`、ほかは `<名前>://localhost`
fn origin() -> String {
    if cfg!(windows) {
        format!("http://{SCHEME}.localhost")
    } else {
        format!("{SCHEME}://localhost")
    }
}

fn app_url(path: &str) -> tauri::Url {
    format!("{}{}", origin(), path).parse().expect("app url")
}

fn is_app_url(url: &tauri::Url) -> bool {
    url.as_str().starts_with(&origin())
}

/// アプリ一式のフォルダ(`bridge.py` がある所)。
///
/// 配るときは exe をフォルダの直下に置く。開発中は `src-tauri/target/...` から
/// 動くので、上へたどって探す。`KANBAN_ROOT` で指定もできる。
fn app_root() -> PathBuf {
    if let Ok(root) = std::env::var("KANBAN_ROOT") {
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

/// 起動できないときに案内するログの場所(Python が答えられないとき用)
fn log_hint(root: &Path) -> String {
    local_root(root).join("logs").display().to_string()
}

/// この端末のローカル領域(Python の `app_config.local_root()` と同じ決め方)
fn local_root(root: &Path) -> PathBuf {
    if let Ok(dir) = std::env::var("KANBAN_LOCAL_DIR") {
        if !dir.trim().is_empty() {
            return PathBuf::from(dir.trim());
        }
    }
    let name = std::fs::read_to_string(root.join("config").join("app.json"))
        .ok()
        .and_then(|text| serde_json::from_str::<serde_json::Value>(&text).ok())
        .and_then(|conf| conf.get("local_dir_name").and_then(|v| v.as_str()).map(str::to_string))
        .unwrap_or_else(|| "KanbanSystem".into());
    if let Ok(base) = std::env::var("LOCALAPPDATA") {
        return PathBuf::from(base).join(name);
    }
    if let Ok(base) = std::env::var("XDG_DATA_HOME") {
        return PathBuf::from(base).join(name);
    }
    PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".local").join("share").join(name)
}

/// **2つ目を起動させない**ための錠(OS のファイルロック)。取れたら握ったまま返す。
///
/// 多重起動の防止は `tauri-plugin-single-instance` がするが、その仕組みは OS ごとに
/// 違い(Linux は D-Bus)、使えない環境では黙って素通しになる。同じ手元の SQLite と
/// 共有DBに2つの Python が書きに行かないよう、ここでも止める。
/// **ブラウザ版(`start_app.py`)もこの錠を見て**、握られていれば起動しない
/// (`launch_guard.desktop_running`)。錠はプロセスが終われば OS が外すので、落ちても残らない。
fn take_instance_lock(root: &Path) -> Option<std::fs::File> {
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

fn html(status: u16, body: String) -> Response<Vec<u8>> {
    Response::builder()
        .status(status)
        .header("Content-Type", "text/html; charset=utf-8")
        .header("Cache-Control", "no-store")
        .body(body.into_bytes())
        .unwrap()
}

fn json_error(status: u16, code: &str, message: &str) -> Response<Vec<u8>> {
    let body = serde_json::json!({"error": {"code": code, "message": message}});
    Response::builder()
        .status(status)
        .header("Content-Type", "application/json")
        .body(serde_json::to_vec(&body).unwrap())
        .unwrap()
}

/// 画面からの要求1件を Python へ渡す。
fn handle(bridge: &Bridge, request: Request<Vec<u8>>) -> Response<Vec<u8>> {
    let uri = request.uri().clone();
    let path = uri.path().to_string();
    let wants_page = request.method() == "GET" && !path.starts_with("/api/") && !path.starts_with("/static/");

    if bridge.phase() == Phase::Starting {
        // 画面なら少しだけ待ち、まだなら「起動しています」を出して読み直してもらう。
        // 画面以外(静的ファイル・API)は始まるまで待つ
        let limit = if wants_page { Duration::from_millis(400) } else { Duration::from_secs(120) };
        if !bridge.wait_started(limit) && bridge.phase() == Phase::Starting {
            return html(200, pages::starting());
        }
    }
    let down = |reason: &str| {
        if wants_page || path == "/" {
            html(503, pages::failure(bridge.failure().as_ref(), &bridge.python(), &bridge.stderr_tail(), &log_hint(bridge.root())))
        } else {
            json_error(503, "python_down", reason)
        }
    };
    if bridge.phase() != Phase::Started {
        return down("Python の処理が動いていません。アプリを開き直してください。");
    }

    let host = uri.host().unwrap_or("app.localhost").to_string();
    let mut headers: Vec<(String, String)> = request
        .headers()
        .iter()
        .filter_map(|(k, v)| Some((k.as_str().to_string(), v.to_str().ok()?.to_string())))
        .filter(|(k, _)| !k.eq_ignore_ascii_case("host"))
        .collect();
    headers.push(("Host".into(), host));

    match bridge.call(request.method().as_str(), &path, uri.query().unwrap_or(""), headers, request.body()) {
        Ok(reply) if wants_page && (300..400).contains(&reply.status) => {
            // **画面の移り先(302 など)は、WebView が独自の宛先ではたどらない**
            // (「Redirecting...」のまま止まる)。移り先へ自分で移るページに替える
            let target = reply
                .headers
                .iter()
                .find(|(k, _)| k.eq_ignore_ascii_case("location"))
                .map(|(_, v)| v.clone())
                .unwrap_or_else(|| "/".into());
            html(200, pages::moving_to(&target))
        }
        Ok(reply) => {
            let mut builder = Response::builder().status(reply.status);
            for (k, v) in &reply.headers {
                // 長さと転送の方法は WebView が自分で決める
                if k.eq_ignore_ascii_case("content-length") || k.eq_ignore_ascii_case("transfer-encoding") {
                    continue;
                }
                builder = builder.header(k.as_str(), v.as_str());
            }
            builder.body(reply.body).unwrap_or_else(|_| json_error(500, "bad_reply", "応答を組み立てられませんでした"))
        }
        Err(reason) => down(&reason),
    }
}

// ------------------------------------------------------------------
// 画面から呼ぶもの(`app/static/js/desktop.js`)
// ------------------------------------------------------------------
static WINDOWS: AtomicU64 = AtomicU64::new(1);

/// 帳票などを別の窓で開く。`url` はアプリの中の経路(`/report/site?...` など)だけ受ける。
#[tauri::command]
async fn open_window(app: AppHandle, url: String, title: Option<String>) -> Result<(), String> {
    if !url.starts_with('/') || url.starts_with("//") {
        return Err("アプリの中の経路だけ開けます".into());
    }
    let label = format!("report-{}", WINDOWS.fetch_add(1, Ordering::SeqCst));
    WebviewWindowBuilder::new(&app, label, WebviewUrl::External(app_url(&url)))
        .title(title.filter(|t| !t.trim().is_empty()).unwrap_or_else(|| TITLE.to_string()))
        .inner_size(1100.0, 900.0)
        .disable_drag_drop_handler()
        .build()
        .map(|_| ())
        .map_err(|e| format!("窓を開けませんでした: {e}"))
}

/// その窓を閉じる。いちばん大きい窓なら、ふだんの閉じ方(確認つき)を通る
#[tauri::command]
fn close_window(window: WebviewWindow) {
    let _ = window.close();
}

/// 社内サイトなど、アプリの外のページを既定のブラウザで開く
#[tauri::command]
fn open_external(app: AppHandle, url: String) -> Result<(), String> {
    if !(url.starts_with("http://") || url.starts_with("https://")) {
        return Err("http(s) のアドレスだけ開けます".into());
    }
    app.opener().open_url(url, None::<&str>).map_err(|e| e.to_string())
}

/// `%E7%9C%8B` のような書き方を戻す(ファイル名は見出しで来るので ASCII にして送られる)
fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).ok();
            if let Some(v) = hex.and_then(|h| u8::from_str_radix(h, 16).ok()) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// 保存するファイルの名前として使えない文字を除く(フォルダを指させない)
fn safe_file_name(name: &str) -> String {
    let cleaned: String = name
        .chars()
        .map(|c| if matches!(c, '/' | '\\' | ':' | '*' | '?' | '"' | '<' | '>' | '|') || c.is_control() { '_' } else { c })
        .collect();
    let cleaned = cleaned.trim().trim_matches('.').to_string();
    if cleaned.is_empty() {
        "download".into()
    } else {
        cleaned
    }
}

fn header_text(request: &tauri::ipc::Request<'_>, name: &str) -> String {
    request
        .headers()
        .get(name)
        .and_then(|v| v.to_str().ok())
        .map(percent_decode)
        .unwrap_or_default()
}

/// **「名前を付けて保存」で保存する**(CSV・なぜなぜシート・記録の一覧)。
///
/// ブラウザ版はブラウザのダウンロードに任せていた。窓の中では置き場所を選べないので、
/// OS の保存ダイアログを出して、ここで書き込む。中身は本文(生のバイト列)で受け取る。
/// 保存した場所を返す(やめたら `None`)。
#[tauri::command]
async fn save_file(window: WebviewWindow, request: tauri::ipc::Request<'_>) -> Result<Option<String>, String> {
    let InvokeBody::Raw(data) = request.body() else {
        return Err("保存する中身がありません".into());
    };
    let data = data.clone();
    let name = safe_file_name(&header_text(&request, "x-file-name"));
    let start = header_text(&request, "x-save-dir");
    let app = window.app_handle().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut dialog = app.dialog().file().set_parent(&window).set_title("名前を付けて保存").set_file_name(&name);
        if !start.is_empty() && Path::new(&start).is_dir() {
            dialog = dialog.set_directory(&start);
        }
        if let Some(ext) = Path::new(&name).extension().and_then(|e| e.to_str()) {
            dialog = dialog.add_filter(ext.to_uppercase(), &[ext]);
        }
        let Some(picked) = dialog.blocking_save_file() else {
            return Ok(None);
        };
        let path = picked.into_path().map_err(|e| format!("保存先が分かりません: {e}"))?;
        std::fs::write(&path, &data).map_err(|e| format!("保存できませんでした({}): {e}", path.display()))?;
        Ok(Some(path.display().to_string()))
    })
    .await
    .map_err(|e| e.to_string())?
}

/// **OS の標準のダイアログでフォルダ・ファイルを選ぶ**(設定の「参照...」)。
///
/// ブラウザ版は、ブラウザが手元のパスを返さないので Python がフォルダを一覧して
/// 画面に出していた。窓の中では OS のダイアログが使える(ネットワークのフォルダも
/// 辿れる)。選んだパスを返すだけで、保存はしない(画面の「変更」で保存する)。
#[tauri::command]
async fn pick_path(
    window: WebviewWindow,
    kind: String,
    start: Option<String>,
    title: Option<String>,
    extensions: Option<Vec<String>>,
) -> Result<Option<String>, String> {
    let app = window.app_handle().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut dialog = app.dialog().file().set_parent(&window);
        if let Some(title) = title.filter(|t| !t.trim().is_empty()) {
            dialog = dialog.set_title(title);
        }
        let start = start.unwrap_or_default();
        let start = Path::new(start.trim());
        if start.is_dir() {
            dialog = dialog.set_directory(start);
        } else if let Some(parent) = start.parent().filter(|p| p.is_dir()) {
            dialog = dialog.set_directory(parent);
        }
        let picked = if kind == "folder" {
            dialog.blocking_pick_folder()
        } else {
            let extensions = extensions.unwrap_or_default();
            if !extensions.is_empty() {
                let list: Vec<&str> = extensions.iter().map(String::as_str).collect();
                dialog = dialog.add_filter("対象のファイル", &list);
            }
            dialog.blocking_pick_file()
        };
        match picked {
            None => Ok(None),
            Some(path) => path.into_path().map(|p| Some(p.display().to_string())).map_err(|e| e.to_string()),
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

/// **Python だけを立て直す**(モードを変えたあと)。窓は閉じない。
///
/// 前の Python は最後の書き戻しを済ませてから終わる。そのあいだ画面には
/// 「起動しています」が出て、新しい Python の待機画面 → 業務画面へ移る。
#[tauri::command]
fn restart_app(app: AppHandle, bridge: State<'_, Arc<Bridge>>) {
    let bridge = bridge.inner().clone();
    // 先に「起動しています」にしておく(直後に読み直した画面が前の代へ行かないように)
    bridge.mark_restarting();
    // 帳票の窓は前のモードのもの。閉じる
    for (label, window) in app.webview_windows() {
        if label != "main" {
            let _ = window.destroy();
        }
    }
    thread::spawn(move || {
        bridge.finish_restart();
        if let Some(window) = app.get_webview_window("main") {
            let _ = window.eval("location.replace('/')");
        }
    });
}

// ------------------------------------------------------------------
// 終わり方
// ------------------------------------------------------------------
/// いちばん大きい窓の × を押した。**画面の「終了」と同じ確認を通る**
/// (共有DBへ未反映の操作があれば訊く)。
fn confirm_close(app: AppHandle, bridge: Arc<Bridge>) {
    thread::spawn(move || {
        let json = vec![("Content-Type".to_string(), "application/json".to_string())];
        let ask = bridge.call("POST", "/api/shutdown", "", json.clone(), b"{}");
        match ask {
            Ok(reply) if reply.status == 409 => {
                let body = serde_json::from_slice::<serde_json::Value>(&reply.body).ok();
                let reason = body
                    .as_ref()
                    .and_then(|v| v.get("busy").or_else(|| v.get("message")).and_then(|m| m.as_str()).map(str::to_string))
                    .unwrap_or_else(|| "実行中の処理があります".into());
                let yes = app
                    .dialog()
                    .message(format!("{reason}\n\nそれでも終了しますか?\n(送れなかった操作は手元に残り、次に開いたときに送ります)"))
                    .title(TITLE)
                    .kind(MessageDialogKind::Warning)
                    .buttons(MessageDialogButtons::OkCancelCustom("終了する".into(), "やめる".into()))
                    .blocking_show();
                if yes {
                    let _ = bridge.call("POST", "/api/shutdown", "", json, br#"{"force": true}"#);
                    exit_soon(app);
                }
            }
            // 200: Python が「終わってよい」を知らせてくる。来なくても少し待って終える
            // それ以外(Python が居ない等): そのまま終える
            _ => exit_soon(app),
        }
    });
}

fn exit_soon(app: AppHandle) {
    thread::spawn(move || {
        thread::sleep(Duration::from_millis(1500));
        app.exit(0);
    });
}

fn main() {
    let root = app_root();
    let bridge = Bridge::new(root, bridge::new_token());

    let for_protocol = bridge.clone();
    let for_close = bridge.clone();
    let for_setup = bridge.clone();
    let for_exit = bridge.clone();

    let app = tauri::Builder::default()
        // 2つ目を開こうとしたら、開いている窓を前に出すだけ(多重起動の防止)
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(bridge.clone())
        .register_asynchronous_uri_scheme_protocol(SCHEME, move |_ctx, request, responder| {
            let bridge = for_protocol.clone();
            // 要求ごとに別のスレッドで答える(画面は見張り・心拍・操作を同時に出す)
            thread::spawn(move || responder.respond(handle(&bridge, request)));
        })
        .invoke_handler(tauri::generate_handler![
            open_window,
            close_window,
            open_external,
            save_file,
            pick_path,
            restart_app
        ])
        .on_window_event(move |window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    api.prevent_close();
                    confirm_close(window.app_handle().clone(), for_close.clone());
                }
            }
        })
        .setup(move |app| {
            // 先に動いているものがあれば、何も起こさずに終わる(窓を前に出すのは
            // single-instance の仕事。使えない環境でも、ここで2つ目を止める)
            match take_instance_lock(for_setup.root()) {
                Some(lock) => {
                    // 終わるまで握っておく
                    Box::leak(Box::new(lock));
                }
                None => {
                    app.handle().exit(0);
                    return Ok(());
                }
            }
            let handle = app.handle().clone();
            for_setup.set_on_quit(move || handle.exit(0));
            let handle = app.handle().clone();
            for_setup.set_on_lost(move || {
                // 理由の画面を出す(読み直すと `handle` が失敗の画面を返す)
                if let Some(window) = handle.get_webview_window("main") {
                    let _ = window.eval("location.reload()");
                }
            });
            // Python は別スレッドで起こす。窓はすぐに出す(「起動しています」)
            let starter = for_setup.clone();
            thread::spawn(move || starter.start());

            let opener = app.handle().clone();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(app_url("/")))
                .title(TITLE)
                .inner_size(1440.0, 900.0)
                .min_inner_size(1024.0, 680.0)
                .center()
                // 「中身を入れ替える」のドラッグ&ドロップを画面(HTML)で受けるため、
                // Tauri がファイルの落下を横取りしないようにする
                .disable_drag_drop_handler()
                .on_navigation(move |url| {
                    if is_app_url(url) {
                        return true;
                    }
                    // アプリの外のページは窓の中で開かない(既定のブラウザへ)
                    if matches!(url.scheme(), "http" | "https") {
                        let _ = opener.opener().open_url(url.as_str(), None::<&str>);
                    }
                    false
                })
                .build()?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("アプリを組み立てられませんでした");

    app.run(move |_app, event| {
        if let RunEvent::Exit = event {
            // 標準入力を閉じて Python に終わってもらう(最後の書き戻しを待つ)。終わらなければ止める
            for_exit.shutdown(bridge::EXIT_GRACE);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 画面の宛先はosで決まる() {
        let url = app_url("/report/site?line=L1&t=x");
        assert!(is_app_url(&url));
        assert!(url.as_str().ends_with("/report/site?line=L1&t=x"));
        assert!(!is_app_url(&"http://127.0.0.1:8741/".parse().unwrap()));
    }

    #[test]
    fn ファイル名は戻して_使えない文字は除く() {
        assert_eq!(percent_decode("%E7%9C%8B%E6%9D%BF_L1.csv"), "看板_L1.csv");
        assert_eq!(percent_decode("a%2"), "a%2");
        assert_eq!(percent_decode("100%"), "100%");
        assert_eq!(safe_file_name("..\\..\\evil/なぜなぜ:1.txt"), "_.._evil_なぜなぜ_1.txt");
        assert_eq!(safe_file_name(" .. "), "download");
    }

    #[test]
    fn ログの場所と錠() {
        // 環境変数を触るので1つの試験にまとめる(試験は並んで走る)
        let dir = std::env::temp_dir().join(format!("kanban_desktop_test_{}", std::process::id()));
        std::fs::create_dir_all(dir.join("config")).unwrap();
        std::fs::write(dir.join("config").join("app.json"), r#"{"local_dir_name": "看板テスト"}"#).unwrap();
        std::env::remove_var("KANBAN_LOCAL_DIR");
        assert!(log_hint(&dir).contains("看板テスト"));

        std::env::set_var("KANBAN_LOCAL_DIR", dir.join("local"));
        let first = take_instance_lock(&dir);
        assert!(first.is_some(), "1つ目は錠を取れる");
        assert!(take_instance_lock(&dir).is_none(), "2つ目は取れない");
        drop(first);
        assert!(take_instance_lock(&dir).is_some(), "1つ目が終われば取れる");
        std::env::remove_var("KANBAN_LOCAL_DIR");
    }
}
