//! ライン管理カレンダー デスクトップ版の外枠
//!
//! 【役割の分け方】(各言語が得意なことをする)
//!
//! - **Rust(ここ)**: 窓・Python の起動と監視・多重起動の防止・終了の確認・
//!   印刷の窓・ファイルの保存先を訊く窓・Python が書けない記録(起動できない理由)
//! - **Python(`bridge.py` 以下)**: 登録の規則・同期・マスタ管理・画面の組み立て
//! - **JS(`app/static/js`)**: 画面の操作
//!
//! 【ポートを使わない】
//! 画面(WebView)は独自の宛先 `app://localhost/`(Windows では
//! `http://app.localhost/`)を読む。その要求をここで受けて、Python の標準入力へ
//! 渡す(`bridge.rs`)。ブラウザ版のように 127.0.0.1 で待ち受けないので、
//! ポートの取り合い・プロキシ・セキュリティ製品に左右されない。
//!
//! 作りは梱包資材総合ツール(python-web-tools)の外枠と同じ。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod bridge;
mod pages;
mod places;

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use tauri::http::{Request, Response};
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

use bridge::{Bridge, Phase};

/// 画面の宛先の名前。Python 側の `app_config.BRIDGE_HOSTS` と揃える
const SCHEME: &str = "app";
const TITLE: &str = "ライン管理カレンダー";

/// 「終了してよい」(Python が未送信を送り終えた知らせ)を待つ上限。
/// Python は送り切るのに最大 8 秒使う(`sync_service.EXIT_SEND_SEC`)
const QUIT_WAIT: Duration = Duration::from_secs(15);

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

/// Python へ渡す Host。**画面の宛先(Origin)と同じ名前にする。**
///
/// Python は「更新の要求は、画面と同じ宛先から来たか」を Origin と Host を
/// 比べて確かめる(別のページから操作させない守り)。Windows の WebView2 は
/// 画面を `http://app.localhost/` で開くが、外枠が受け取る要求の URI は
/// `app://localhost/…` になる。URI から Host を取ると `localhost` になり、
/// Origin(`app.localhost`)と合わず、**すべての操作が断られた**
/// (GitHub Actions の Windows で実際に起きた。Linux では両方 `localhost` で気づけない)
fn page_host() -> String {
    origin().split("://").nth(1).unwrap_or("app.localhost").to_string()
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
            html(
                503,
                pages::failure(bridge.failure().as_ref(), &bridge.python(), &bridge.stderr_tail(), &places::log_hint(bridge.root())),
            )
        } else {
            json_error(503, "python_down", reason)
        }
    };
    if bridge.phase() != Phase::Started {
        return down("Python の処理が動いていません。アプリを開き直してください。");
    }

    let host = page_host();
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
            // (梱包資材総合ツールで「Redirecting...」のまま止まった)。
            // 移り先へ自分で移るページに替える
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
// 画面から呼ぶもの(`app/static/js/desktop.js` と印刷のページ)
// ------------------------------------------------------------------
static WINDOWS: AtomicU64 = AtomicU64::new(1);

/// 印刷などを別の窓で開く。`url` はアプリの中の経路(`/print?…` など)だけ受ける。
#[tauri::command]
async fn open_window(app: AppHandle, url: String, title: Option<String>) -> Result<(), String> {
    if !url.starts_with('/') || url.starts_with("//") {
        return Err("アプリの中の経路だけ開けます".into());
    }
    let label = format!("print-{}", WINDOWS.fetch_add(1, Ordering::SeqCst));
    WebviewWindowBuilder::new(&app, label, WebviewUrl::External(app_url(&url)))
        .title(title.unwrap_or_else(|| TITLE.to_string()))
        .inner_size(1180.0, 900.0)
        .disable_drag_drop_handler()
        .on_navigation(|url| is_app_url(url))
        .build()
        .map(|_| ())
        .map_err(|e| format!("窓を開けませんでした: {e}"))
}

/// その窓を閉じる(印刷の「閉じる」)。いちばん大きい窓なら、ふだんの閉じ方(確認つき)を通る
#[tauri::command]
fn close_window(window: WebviewWindow) {
    let _ = window.close();
}

/// 文字をファイルに保存する(なぜなぜ分析の材料など)。**保存先は OS の窓で訊く。**
///
/// WebView のダウンロードは OS によって動きが違う(何も起きないことがある)ので、
/// デスクトップ版では外枠が書く。Windows のメモ帳で化けないよう、BOM と CRLF を付ける。
/// 保存したら `true`、やめたら `false`。
#[tauri::command]
async fn save_text_file(app: AppHandle, file_name: String, text: String) -> Result<bool, String> {
    let name: String = file_name.chars().filter(|c| !"\\/:*?\"<>|".contains(*c)).collect();
    let picked = app
        .dialog()
        .file()
        .set_title("保存先を選んでください")
        .set_file_name(if name.is_empty() { "記録.txt" } else { &name })
        .add_filter("テキスト", &["txt"])
        .blocking_save_file();
    let Some(picked) = picked else { return Ok(false) };
    let path = picked.into_path().map_err(|e| format!("保存先を読めませんでした: {e}"))?;
    let body = format!("\u{feff}{}", text.replace("\r\n", "\n").replace('\n', "\r\n"));
    std::fs::write(&path, body).map_err(|e| format!("保存できませんでした({}): {e}", path.display()))?;
    Ok(true)
}

// ------------------------------------------------------------------
// 終わり方
// ------------------------------------------------------------------
/// いちばん大きい窓の × を押した。**画面の「終了」と同じ確認を通る**
/// (同期の途中なら訊く)。通ったら、Python が**未送信を送り終えて**
/// 「終了してよい」を知らせてくるのを待って終える(`bridge.py` の `stop`)。
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
                    .message(format!("{message}\n(送れていない入力は次に起動したときに送られます)"))
                    .title(TITLE)
                    .kind(MessageDialogKind::Warning)
                    .buttons(MessageDialogButtons::OkCancelCustom("中断して終了".into(), "やめる".into()))
                    .blocking_show();
                if yes {
                    let _ = bridge.call("POST", "/api/shutdown", "", json, br#"{"force": true}"#);
                    exit_after(app, QUIT_WAIT);
                }
            }
            // 200: Python が未送信を送り終えて「終わってよい」を知らせてくる。来なくても待って終える
            Ok(reply) if reply.status == 200 => exit_after(app, QUIT_WAIT),
            // それ以外(Python が居ない等): そのまま終える
            _ => exit_after(app, Duration::from_millis(200)),
        }
    });
}

fn exit_after(app: AppHandle, wait: Duration) {
    thread::spawn(move || {
        thread::sleep(wait);
        app.exit(0);
    });
}

fn main() {
    let root = places::app_root();
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
        .register_asynchronous_uri_scheme_protocol(SCHEME, move |_ctx, request, responder| {
            let bridge = for_protocol.clone();
            // 要求ごとに別のスレッドで答える(画面は見張り・心拍・操作を同時に出す)
            thread::spawn(move || responder.respond(handle(&bridge, request)));
        })
        .invoke_handler(tauri::generate_handler![open_window, close_window, save_text_file])
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
            match places::take_instance_lock(for_setup.root()) {
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

            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(app_url("/")))
                .title(TITLE)
                .inner_size(1440.0, 900.0)
                .min_inner_size(1024.0, 680.0)
                .center()
                .disable_drag_drop_handler()
                // アプリの外のページは窓の中で開かない
                .on_navigation(|url| is_app_url(url))
                .build()?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("アプリを組み立てられませんでした");

    app.run(move |_app, event| {
        if let RunEvent::Exit = event {
            // 標準入力を閉じて Python に終わってもらう(閉じられた Python も
            // 未送信を送れるだけ送る)。終わらなければ止める
            for_exit.shutdown(Duration::from_secs(8));
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 画面の宛先はosで決まる() {
        let url = app_url("/print?year=2026&month=9&t=x");
        assert!(is_app_url(&url));
        assert!(url.as_str().ends_with("/print?year=2026&month=9&t=x"));
        assert!(!is_app_url(&"http://127.0.0.1:8730/".parse().unwrap()));
        assert!(!is_app_url(&"https://example.com/".parse().unwrap()));
    }

    #[test]
    fn hostは画面の宛先と同じ名前() {
        // Origin が `http://app.localhost` なら Host も `app.localhost`(Python の守りが比べる)
        let origin = origin();
        assert!(origin.ends_with(&format!("://{}", page_host())), "{origin}");
        if cfg!(windows) {
            assert_eq!(page_host(), "app.localhost");
        } else {
            assert_eq!(page_host(), "localhost");
        }
    }
}
