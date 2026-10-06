//! 点検表 選択・印刷 デスクトップ版の外枠
//!
//! 【役割の分け方】(各言語が得意なことをする。docs/デスクトップ版.md)
//!
//! - **Rust(ここ)**: 窓・Python の起動と監視・多重起動とブラウザ版との同時起動の防止・
//!   終了の確認・フォルダーの選択窓・画面の部品(CSS/JS)を返す・Python が居ないときの理由の画面
//! - **Python(`bridge.py` 以下)**: 一覧・設定・ログ・Excel の段取り・印刷の進み具合
//!   (ブラウザ版と同じものを1つだけ持つ)
//! - **VBScript(`app/vbscript/excel_worker.vbs`)**: Excel の操作(今までどおり)
//! - **JS(`app/static/js`)**: 画面の操作
//!
//! 【ポートを使わない】
//! 画面(WebView)は独自の宛先 `app://localhost/`(Windows では
//! `http://app.localhost/`)を読む。その要求をここで受けて、部品(`/static/`)は
//! 自分で返し、それ以外は Python の標準入力へ渡す(`bridge.rs`)。127.0.0.1 で
//! 待ち受けないので、ポートの取り合い・プロキシ・セキュリティ製品に左右されない。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod bridge;
mod instance;
mod pages;
mod statics;

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use tauri::http::{Request, Response};
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_opener::OpenerExt;

use bridge::{Bridge, Phase};

/// 画面の宛先の名前。Python 側の `app_config.BRIDGE_HOSTS` と揃える
const SCHEME: &str = "app";
const TITLE: &str = "点検表 選択・印刷";

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
/// 動くので、上へたどって探す。`INSPECTION_ROOT` で指定もできる。
fn app_root() -> PathBuf {
    if let Ok(root) = std::env::var("INSPECTION_ROOT") {
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

/// 起動できないときに案内するログの場所(Python が答えられないとき用)。
/// Store 版の Python では実際の場所が `…\Packages\PythonSoftwareFoundation.Python…\LocalCache\Local\…`
/// になるので、そう添える(Python が答えられるときは Python が正しい場所を返す)。
fn log_hint(root: &Path) -> String {
    let local = instance::local_root(root).join("logs");
    if cfg!(windows) {
        format!(
            "{}(Microsoft Store 版の Python では %LOCALAPPDATA%\\Packages\\PythonSoftwareFoundation.Python…\\LocalCache\\Local\\ の下)",
            local.display()
        )
    } else {
        local.display().to_string()
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

/// 画面からの要求1件に答える。部品は自分で、それ以外は Python へ。
fn handle(bridge: &Bridge, request: Request<Vec<u8>>) -> Response<Vec<u8>> {
    let uri = request.uri().clone();
    let path = uri.path().to_string();
    let query = uri.query().unwrap_or("").to_string();

    // 画面の部品(CSS/JS)。Python を待たずに返せる
    if request.method() == "GET" && path.starts_with("/static/") {
        if let Some(reply) = statics::serve(bridge.root(), &path, &query) {
            return reply;
        }
    }

    let wants_page = request.method() == "GET" && !path.starts_with("/api/") && !path.starts_with("/static/");
    if bridge.phase() == Phase::Starting {
        // 画面なら少しだけ待ち、まだなら「起動しています」を出して読み直してもらう。
        // 画面以外(API)は始まるまで待つ
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

    match bridge.call(request.method().as_str(), &path, &query, headers, request.body()) {
        Ok(reply) if wants_page && (300..400).contains(&reply.status) => {
            // **画面の移り先(302 など)は、WebView が独自の宛先ではたどらない**
            // (python-web-tools で「Redirecting...」のまま止まった)。自分で移るページに替える
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
/// Windows の「フォルダーの選択」窓を出す(点検表フォルダ・ログの保存先)。
/// 選ばなければ `None`。共有フォルダ(`\\サーバ\…`)もそのまま選べる。
#[tauri::command]
async fn pick_folder(app: AppHandle, current: Option<String>, title: Option<String>) -> Result<Option<String>, String> {
    let mut dialog = app.dialog().file().set_title(title.unwrap_or_else(|| "フォルダーを選ぶ".into()));
    if let Some(dir) = current.filter(|d| !d.trim().is_empty()) {
        let dir = PathBuf::from(dir);
        if dir.is_dir() {
            dialog = dialog.set_directory(dir);
        }
    }
    // 窓を出しているあいだ待つので、窓の仕事をするスレッドでは呼ばない(async コマンド)
    let picked = dialog.blocking_pick_folder();
    Ok(picked.and_then(|p| p.into_path().ok()).map(|p| p.display().to_string()))
}

/// アプリの外のページを既定のブラウザで開く(窓の中では開かない)
#[tauri::command]
fn open_external(app: AppHandle, url: String) -> Result<(), String> {
    if !(url.starts_with("http://") || url.starts_with("https://")) {
        return Err("http(s) のアドレスだけ開けます".into());
    }
    app.opener().open_url(url, None::<&str>).map_err(|e| e.to_string())
}

// ------------------------------------------------------------------
// 終わり方
// ------------------------------------------------------------------
/// 窓の × を押した。**画面の「終了」と同じ確認を通る**(印刷中なら訊く)。
fn confirm_close(app: AppHandle, bridge: Arc<Bridge>) {
    thread::spawn(move || {
        let json = vec![("Content-Type".to_string(), "application/json".to_string())];
        let ask = bridge.call("POST", "/api/shutdown", "", json.clone(), b"{}");
        match ask {
            Ok(reply) if reply.status == 409 => {
                let message = serde_json::from_slice::<serde_json::Value>(&reply.body)
                    .ok()
                    .and_then(|v| v.get("message").and_then(|m| m.as_str()).map(str::to_string))
                    .unwrap_or_else(|| "実行中の処理があります。終了しますか?".into());
                let yes = app
                    .dialog()
                    .message(message)
                    .title(TITLE)
                    .kind(MessageDialogKind::Warning)
                    .buttons(MessageDialogButtons::OkCancelCustom("中断して終了".into(), "やめる".into()))
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

/// ブラウザ版が動いているので開かない。理由を出して終わる
fn refuse_browser_running(app: &AppHandle) {
    let handle = app.clone();
    app.dialog()
        .message(
            "ブラウザ版(Start.vbs / start.bat で開いたもの)が動いています。\n\n\
             ブラウザ版とデスクトップ版は同時には使えません。\n\
             ブラウザの画面の「終了」で閉じてから、もう一度開いてください。",
        )
        .title(TITLE)
        .kind(MessageDialogKind::Info)
        .buttons(MessageDialogButtons::Ok)
        .show(move |_| handle.exit(0));
}

fn main() {
    let root = app_root();
    let demo = std::env::args().any(|a| a == "--demo");
    let bridge = Bridge::new(root, bridge::new_token(), demo);

    let for_protocol = bridge.clone();
    let for_close = bridge.clone();
    let for_setup = bridge.clone();
    let for_exit = bridge.clone();

    let app = tauri::Builder::default()
        // 2つ目を開こうとしたら、開いている窓を前に出すだけ(多重起動の防止)。
        // ブラウザ版が「デスクトップ版が動いています」と言うときも、これで前に出る
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .register_asynchronous_uri_scheme_protocol(SCHEME, move |_ctx, request, responder| {
            let bridge = for_protocol.clone();
            // 要求ごとに別のスレッドで答える(画面は見張り・心拍・操作を同時に出す)
            thread::spawn(move || responder.respond(handle(&bridge, request)));
        })
        .invoke_handler(tauri::generate_handler![pick_folder, open_external])
        .on_window_event(move |window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    api.prevent_close();
                    confirm_close(window.app_handle().clone(), for_close.clone());
                }
            }
        })
        .setup(move |app| {
            // **ブラウザ版と同時に動かさない**(後から開いたほうが止まる)。
            // デスクトップ版同士なら、窓を前に出すのは single-instance の仕事。
            // それが使えない環境(D-Bus が無い等)でも、ここで2つ目を止める
            match instance::claim_desktop(for_setup.root()) {
                Ok(instance::Claim::Claimed(held)) => {
                    Box::leak(Box::new(held)); // 終わるまで握っておく
                }
                Ok(instance::Claim::Running(instance::BROWSER)) => {
                    refuse_browser_running(app.handle());
                    return Ok(());
                }
                Ok(instance::Claim::Running(_)) => {
                    app.handle().exit(0);
                    return Ok(());
                }
                Err(e) => {
                    // 錠そのものが作れない(めったに無い)。止めずに続ける
                    eprintln!("同時起動の錠を作れませんでした: {e}");
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

            let external = app.handle().clone();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(app_url("/")))
                .title(TITLE)
                .inner_size(1400.0, 860.0)
                .min_inner_size(1024.0, 640.0)
                .center()
                // 画面(HTML)のドラッグ&ドロップをブラウザ版と同じに保つ
                .disable_drag_drop_handler()
                .on_navigation(move |url| {
                    if is_app_url(url) {
                        return true;
                    }
                    // アプリの外のページは窓の中で開かない(既定のブラウザへ)
                    if url.scheme() == "http" || url.scheme() == "https" {
                        let _ = external.opener().open_url(url.as_str(), None::<&str>);
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
            // 標準入力を閉じて Python に終わってもらう。終わらなければ止める
            // (Python は終わるときに Excel を閉じる。その時間を見て長めに待つ)
            for_exit.shutdown(Duration::from_secs(20));
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 画面の宛先はosで決まる() {
        let url = app_url("/?t=x");
        assert!(is_app_url(&url));
        assert!(url.as_str().ends_with("/?t=x"));
        assert!(!is_app_url(&"http://127.0.0.1:8733/".parse().unwrap()));
    }

    #[test]
    fn ログの場所はconfigのフォルダ名から() {
        let dir = std::env::temp_dir().join(format!("isp_desktop_test_{}", std::process::id()));
        std::fs::create_dir_all(dir.join("config")).unwrap();
        std::fs::write(dir.join("config").join("app.json"), r#"{"local_dir_name": "点検テスト"}"#).unwrap();
        // 試験は並んで走り、環境変数は instance の試験も触るので、値に頼らない形で確かめる
        let hint = log_hint(&dir);
        assert!(hint.contains("logs"), "{hint}");
    }
}
