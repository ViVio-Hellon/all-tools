//! 画面からの頼みごと(各ツールの `desktop.js` が頼むもの)と、大きなタブの画面の口。
//!
//! 各ツールの `desktop.js` は、単体のデスクトップ版の外枠に頼んでいたのと**同じ名前**で頼む。
//! 統合の外枠は、それを**どのツールからの頼みか**を添えて受ける:
//!
//! - 大きなタブの中(iframe)から … 台本(`embed.js`)→ 大きなタブの画面(送り元を確かめる)
//!   → `tool_invoke`(`tool` にツールの名前)
//! - 別窓(帳票・印刷)から … 台本 → `tool_invoke`(窓の宛先からツールを決める)
//!
//! | 頼みごと | 頼むツール | すること |
//! |---|---|---|
//! | `open_window` | 看板・カレンダー・日報 | 帳票・印刷・早見表を別の窓で(そのツールの宛先の中だけ) |
//! | `close_window` | カレンダー・python-web-tools 流の帳票 | その別窓を閉じる |
//! | `open_external` | 看板・点検表・日報 | アプリの外のページを既定のブラウザで |
//! | `save_file`(本文) | 看板 | CSV・なぜなぜシートを「名前を付けて保存」 |
//! | `save_text_file` | カレンダー | なぜなぜ材料を BOM・CRLF 付きで保存 |
//! | `save_url` | (予備) | ツールの経路の中身を外枠が取りに行って保存 |
//! | `pick_path` | 看板・日報 | OS の標準のフォルダ/ファイル選択 |
//! | `pick_folder` | 点検表 | OS の標準のフォルダ選択 |
//! | `restart_app` | 看板 | **そのツールの Python だけ**を立て直す(モード切替) |

use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Value};
use tauri::ipc::InvokeBody;
use tauri::{AppHandle, Manager, State, Webview, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;

use crate::catalog::Tool;
use crate::shell::{Prepared, Shell};

/// 頼んできたツール。大きなタブの窓(`main`)からなら `tool` の名前(大きなタブの画面が
/// 送り元を確かめてある)、別窓からなら窓の宛先で決める(名乗りは信じない)。
fn caller(shell: &Shell, webview: &Webview, tool: Option<&str>) -> Result<Tool, String> {
    let found = if webview.label() == "main" {
        tool.and_then(|id| shell.catalog.by_id(id))
    } else {
        webview.url().ok().and_then(|url| shell.catalog.by_url(&url))
    };
    match found {
        Some(t) if !t.is_portal() => Ok(t.clone()),
        _ => Err("どのツールからの頼みか分かりません".into()),
    }
}

fn arg_str(args: &Value, key: &str) -> Option<String> {
    args.get(key).and_then(Value::as_str).map(str::to_string).filter(|s| !s.trim().is_empty())
}

fn parent_window(app: &AppHandle, webview: &Webview) -> Option<tauri::WebviewWindow> {
    app.get_webview_window(webview.window().label())
}

#[tauri::command]
pub async fn tool_invoke(
    app: AppHandle,
    webview: Webview,
    shell: State<'_, Arc<Shell>>,
    tool: Option<String>,
    cmd: String,
    args: Option<Value>,
) -> Result<Value, String> {
    let shell = shell.inner().clone();
    let tool = caller(&shell, &webview, tool.as_deref())?;
    let args = args.unwrap_or(Value::Null);
    match cmd.as_str() {
        "open_window" => {
            let url = arg_str(&args, "url").ok_or("開く経路がありません")?;
            open_window(
                &app,
                &shell,
                &tool,
                &url,
                arg_str(&args, "title"),
                arg_str(&args, "label"),
                args.get("width").and_then(Value::as_f64),
                args.get("height").and_then(Value::as_f64),
            )?;
            Ok(Value::Null)
        }
        "close_window" => {
            // 別窓(帳票・印刷)だけ閉じる。大きなタブの窓は閉じない(× と「終了」の仕事)
            if webview.window().label() != "main" {
                let _ = webview.window().close();
            }
            Ok(Value::Null)
        }
        "open_external" => {
            let url = arg_str(&args, "url").ok_or("開くアドレスがありません")?;
            open_external(&app, &url)?;
            Ok(Value::Null)
        }
        "pick_folder" => {
            let start = arg_str(&args, "current");
            let title = arg_str(&args, "title").unwrap_or_else(|| "フォルダーを選ぶ".into());
            pick(&app, &webview, "folder", start, Some(title), vec![]).await.map(|p| json!(p))
        }
        "pick_path" => {
            let kind = arg_str(&args, "kind").unwrap_or_else(|| "folder".into());
            let extensions = args
                .get("extensions")
                .and_then(Value::as_array)
                .map(|list| list.iter().filter_map(|v| v.as_str().map(str::to_string)).collect())
                .unwrap_or_default();
            pick(&app, &webview, &kind, arg_str(&args, "start"), arg_str(&args, "title"), extensions)
                .await
                .map(|p| json!(p))
        }
        "save_text_file" => {
            let name = arg_str(&args, "fileName").unwrap_or_else(|| "記録.txt".into());
            let text = args.get("text").and_then(Value::as_str).unwrap_or("").to_string();
            save_text_file(&app, &webview, &name, &text).await.map(|ok| json!(ok))
        }
        "save_url" => {
            let url = arg_str(&args, "url").ok_or("保存する経路がありません")?;
            save_url(&app, &shell, &webview, &tool, &url, arg_str(&args, "name")).await.map(|p| json!(p))
        }
        "restart_app" => {
            shell.restart_tool(&tool.id, true)?;
            Ok(Value::Null)
        }
        other => Err(format!("この頼みごとは日報複合ツールでは使えません: {other}")),
    }
}

/// 本文(生のバイト列)つきの頼みごと。いまは看板の `save_file` だけ。
#[tauri::command]
pub async fn tool_invoke_raw(
    app: AppHandle,
    webview: Webview,
    shell: State<'_, Arc<Shell>>,
    request: tauri::ipc::Request<'_>,
) -> Result<Value, String> {
    let shell = shell.inner().clone();
    let header = |name: &str| header_text(&request, name);
    let tool = caller(&shell, &webview, Some(&header("x-alltools-tool")))?;
    let cmd = header("x-alltools-cmd");
    let InvokeBody::Raw(data) = request.body() else {
        return Err("保存する中身がありません".into());
    };
    match cmd.as_str() {
        "save_file" => {
            let name = safe_file_name(&header("x-file-name"));
            let start = header("x-save-dir");
            save_bytes(&app, &webview, &name, &start, data.clone()).await.map(|p| json!(p))
        }
        other => Err(format!("この頼みごとは日報複合ツールでは使えません: {other}({})", tool.title)),
    }
}

// ------------------------------------------------------------------
// 大きなタブの画面から
// ------------------------------------------------------------------
/// 各ツールの Python の状態(タブの印)
#[tauri::command]
pub fn shell_status(shell: State<'_, Arc<Shell>>) -> Vec<Value> {
    shell.status()
}

/// 「終了」(大きなタブの帯)。窓の × と同じ道
#[tauri::command]
pub fn shell_close(app: AppHandle, shell: State<'_, Arc<Shell>>) {
    crate::closing::request_close(shell.inner().clone(), app, None);
}

/// 終える前の「打ちかけを置いて」の返事(大きなタブの画面の `prepareClose` から)。
/// `waiting: true` は「待っています」(本人に訊いている・ツールの画面の返事を待っている)。
/// これを受けたら、外枠は短い上限で見切らない([`crate::shell::wait_prepared`])
#[tauri::command]
pub fn shell_prepared(shell: State<'_, Arc<Shell>>, ok: bool, waiting: Option<bool>) {
    let answer = if waiting.unwrap_or(false) { Prepared::Waiting } else { Prepared::Done(ok) };
    shell.screens_prepared(answer);
}

/// そのツールの Python を起こし直す(大設定・止まった知らせの「もう一度開く」)。
/// 入口(`portal`)は画面を読み直さずに Python だけ起こし直す
#[tauri::command]
pub fn shell_restart_tool(shell: State<'_, Arc<Shell>>, tool: String) -> Result<(), String> {
    let shell = shell.inner().clone();
    match shell.catalog.by_id(&tool) {
        Some(t) if t.is_portal() => {
            shell.restart_portal();
            Ok(())
        }
        Some(_) => shell.restart_tool(&tool, true),
        _ => Err("そのツールはありません".into()),
    }
}

// ------------------------------------------------------------------
// 別窓
// ------------------------------------------------------------------
/// 帳票などを別の窓で開く。`url` は**そのツールの宛先の中の経路**(`/report/…`)だけ受ける。
/// `label` があれば、同じ名前の窓を使い回す(日報の VC 早見表)。
#[allow(clippy::too_many_arguments)]
pub fn open_window(
    app: &AppHandle,
    shell: &Arc<Shell>,
    tool: &Tool,
    url: &str,
    title: Option<String>,
    label: Option<String>,
    width: Option<f64>,
    height: Option<f64>,
) -> Result<(), String> {
    if !url.starts_with('/') || url.starts_with("//") {
        return Err("アプリの中の経路だけ開けます".into());
    }
    let target: tauri::Url = format!("{}{}", tool.origin(), url).parse().map_err(|e| format!("経路が読めません: {e}"))?;
    let window_label = match &label {
        Some(name) => {
            let clean: String = name.chars().filter(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_').collect();
            format!("report-{}-{}", tool.id, if clean.is_empty() { "named".into() } else { clean })
        }
        None => format!("report-{}-{}", tool.id, shell.next_window_number()),
    };
    if let Some(existing) = app.get_webview_window(&window_label) {
        let _ = existing.unminimize();
        let _ = existing.show();
        let _ = existing.set_focus();
        return Ok(());
    }
    let own_origin = tool.origin();
    let opener = app.clone();
    let for_new = shell.clone();
    let for_download = shell.clone();
    let tool_for_new = tool.clone();
    WebviewWindowBuilder::new(app, window_label, WebviewUrl::External(target))
        .title(title.unwrap_or_else(|| tool.name.clone()))
        .inner_size(width.unwrap_or(1180.0).clamp(400.0, 4000.0), height.unwrap_or(900.0).clamp(300.0, 3000.0))
        .disable_drag_drop_handler()
        .on_navigation(move |url| {
            if url.as_str().starts_with(&own_origin) {
                return true;
            }
            if matches!(url.scheme(), "http" | "https") && crate::catalog::scheme_of(url).is_none() {
                let _ = opener.opener().open_url(url.as_str(), None::<&str>);
            }
            false
        })
        .on_new_window(move |url, _features| {
            route_new_window(&for_new, &url, Some(&tool_for_new));
            tauri::webview::NewWindowResponse::Deny
        })
        .on_download(move |webview, event| route_download(&for_download, webview, event))
        .build()
        .map(|_| ())
        .map_err(|e| format!("窓を開けませんでした: {e}"))
}

/// 新しい窓を開こうとした(`window.open`・`target="_blank"`)。アプリの中ならそのツールの別窓、
/// 外なら既定のブラウザ。WebView に任せない(独自の宛先を持たない窓になってしまう)
pub fn route_new_window(shell: &Arc<Shell>, url: &tauri::Url, from: Option<&Tool>) {
    let Some(app) = shell.app() else { return };
    match shell.catalog.by_url(url) {
        Some(tool) if !tool.is_portal() => {
            // 名乗った宛先と頼んだ窓のツールが違うときは開かない(ツールは自分の宛先だけ開ける)
            if from.is_some_and(|f| f.id != tool.id) {
                return;
            }
            let path = match url.query() {
                Some(q) => format!("{}?{q}", url.path()),
                None => url.path().to_string(),
            };
            let tool = tool.clone();
            let _ = open_window(app, shell, &tool, &path, None, None, None, None);
        }
        Some(_) => {}
        None => {
            if matches!(url.scheme(), "http" | "https") {
                let _ = app.opener().open_url(url.as_str(), None::<&str>);
            }
        }
    }
}

/// WebView の「ダウンロード」を、外枠の保存ダイアログに置き換える(各ツールはふだん
/// 自分で保存を頼むが、取りこぼしがあってもファイルを失わないための受け皿)
pub fn route_download(shell: &Arc<Shell>, webview: Webview, event: tauri::webview::DownloadEvent<'_>) -> bool {
    if let tauri::webview::DownloadEvent::Requested { url, .. } = event {
        if let Some(tool) = shell.catalog.by_url(&url).filter(|t| !t.is_portal()).cloned() {
            let path = match url.query() {
                Some(q) => format!("{}?{q}", url.path()),
                None => url.path().to_string(),
            };
            let shell = shell.clone();
            if let Some(app) = shell.app().cloned() {
                tauri::async_runtime::spawn(async move {
                    let _ = save_url(&app, &shell, &webview, &tool, &path, None).await;
                });
            }
        }
    }
    false
}

/// アプリの外のページを既定のブラウザで開く
pub fn open_external(app: &AppHandle, url: &str) -> Result<(), String> {
    if !(url.starts_with("http://") || url.starts_with("https://")) {
        return Err("http(s) のアドレスだけ開けます".into());
    }
    if let Ok(parsed) = url.parse::<tauri::Url>() {
        if crate::catalog::scheme_of(&parsed).is_some() {
            return Err("アプリの中の宛先は既定のブラウザでは開けません".into());
        }
    }
    app.opener().open_url(url, None::<&str>).map_err(|e| e.to_string())
}

// ------------------------------------------------------------------
// 選ぶ・保存する(OS の標準のダイアログ)
// ------------------------------------------------------------------
/// OS の標準のダイアログでフォルダ/ファイルを選ぶ(共有フォルダも辿れる)。選んだパス
async fn pick(
    app: &AppHandle,
    webview: &Webview,
    kind: &str,
    start: Option<String>,
    title: Option<String>,
    extensions: Vec<String>,
) -> Result<Option<String>, String> {
    let app = app.clone();
    let parent = parent_window(&app, webview);
    let kind = kind.to_string();
    tauri::async_runtime::spawn_blocking(move || {
        let mut dialog = app.dialog().file();
        if let Some(window) = &parent {
            dialog = dialog.set_parent(window);
        }
        if let Some(title) = title {
            dialog = dialog.set_title(title);
        }
        let start = start.unwrap_or_default();
        let start = Path::new(start.trim());
        if start.is_dir() {
            dialog = dialog.set_directory(start);
        } else if let Some(parent) = start.parent().filter(|p| !p.as_os_str().is_empty() && p.is_dir()) {
            dialog = dialog.set_directory(parent);
        }
        let picked = if kind == "folder" {
            dialog.blocking_pick_folder()
        } else {
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

/// 中身を「名前を付けて保存」で保存する。保存した場所(やめたら `None`)
async fn save_bytes(app: &AppHandle, webview: &Webview, name: &str, start: &str, data: Vec<u8>) -> Result<Option<String>, String> {
    let app = app.clone();
    let parent = parent_window(&app, webview);
    let name = name.to_string();
    let start = start.to_string();
    tauri::async_runtime::spawn_blocking(move || {
        let mut dialog = app.dialog().file().set_title("名前を付けて保存").set_file_name(&name);
        if let Some(window) = &parent {
            dialog = dialog.set_parent(window);
        }
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

/// 文字をファイルに保存する(カレンダーのなぜなぜ材料)。Windows のメモ帳で化けないよう、BOM と CRLF を付ける
async fn save_text_file(app: &AppHandle, webview: &Webview, file_name: &str, text: &str) -> Result<bool, String> {
    let name: String = file_name.chars().filter(|c| !"\\/:*?\"<>|".contains(*c)).collect();
    let app = app.clone();
    let parent = parent_window(&app, webview);
    let body = format!("\u{feff}{}", text.replace("\r\n", "\n").replace('\n', "\r\n"));
    tauri::async_runtime::spawn_blocking(move || {
        let mut dialog = app
            .dialog()
            .file()
            .set_title("保存先を選んでください")
            .set_file_name(if name.is_empty() { "記録.txt" } else { &name })
            .add_filter("テキスト", &["txt"]);
        if let Some(window) = &parent {
            dialog = dialog.set_parent(window);
        }
        let Some(picked) = dialog.blocking_save_file() else { return Ok(false) };
        let path = picked.into_path().map_err(|e| format!("保存先を読めませんでした: {e}"))?;
        std::fs::write(&path, body).map_err(|e| format!("保存できませんでした({}): {e}", path.display()))?;
        Ok(true)
    })
    .await
    .map_err(|e| e.to_string())?
}

/// ツールの経路の中身を**外枠が自分で取りに行き**、「名前を付けて保存」で保存する
/// (画面を通さずに、Python → Rust → ファイル)。
async fn save_url(app: &AppHandle, shell: &Arc<Shell>, webview: &Webview, tool: &Tool, url: &str, name: Option<String>) -> Result<Option<String>, String> {
    let (path, query) = url.split_once('?').unwrap_or((url, ""));
    if !path.starts_with('/') || path.starts_with("//") {
        return Err("アプリの中の経路だけ保存できます".into());
    }
    let bridge = shell.bridge(&tool.id).ok_or("そのツールはありません")?.clone();
    let (path, query) = (path.to_string(), query.to_string());
    let reply = tauri::async_runtime::spawn_blocking(move || {
        bridge.call_with(
            "GET",
            &path,
            &query,
            vec![("Host".into(), crate::relay::TOOL_HOST.into()), ("Sec-Fetch-Site".into(), "same-origin".into())],
            b"",
            Duration::from_secs(300),
        )
    })
    .await
    .map_err(|e| e.to_string())??;
    if reply.status != 200 {
        return Err(format!("保存する中身を取れませんでした({})", reply.status));
    }
    let fallback = url.split('?').next().unwrap_or("").rsplit('/').next().unwrap_or("download").to_string();
    let file = name
        .or_else(|| reply.header("content-disposition").and_then(file_name_of))
        .unwrap_or(fallback);
    save_bytes(app, webview, &safe_file_name(&file), "", reply.body).await
}

/// `Content-Disposition` からファイル名を拾う(`filename*=UTF-8''…` を優先)
pub fn file_name_of(disposition: &str) -> Option<String> {
    let lower = disposition.to_ascii_lowercase();
    if let Some(at) = lower.find("filename*=utf-8''") {
        let raw = &disposition[at + "filename*=utf-8''".len()..];
        let raw = raw.split(';').next().unwrap_or("").trim();
        return Some(percent_decode(raw));
    }
    let at = lower.find("filename=")?;
    let raw = disposition[at + "filename=".len()..].split(';').next().unwrap_or("").trim();
    Some(raw.trim_matches('"').to_string()).filter(|s| !s.is_empty())
}

/// `%E7%9C%8B` のような書き方を戻す(ファイル名は見出しで来るので ASCII にして送られる)
pub fn percent_decode(text: &str) -> String {
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
pub fn safe_file_name(name: &str) -> String {
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ファイル名は戻して_使えない文字は除く() {
        assert_eq!(percent_decode("%E7%9C%8B%E6%9D%BF_L1.csv"), "看板_L1.csv");
        assert_eq!(percent_decode("a%2"), "a%2");
        assert_eq!(percent_decode("100%"), "100%");
        assert_eq!(safe_file_name("..\\..\\evil/なぜなぜ:1.txt"), "_.._evil_なぜなぜ_1.txt");
        assert_eq!(safe_file_name(" .. "), "download");
    }

    #[test]
    fn 添付のファイル名を拾う() {
        assert_eq!(file_name_of("attachment; filename*=UTF-8''%E9%9B%86%E8%A8%88.csv").as_deref(), Some("集計.csv"));
        assert_eq!(file_name_of("attachment; filename=\"a.csv\"").as_deref(), Some("a.csv"));
        assert_eq!(file_name_of("inline"), None);
    }
}
