//! 画面からの要求1件に答える(宛先ごと)。
//!
//! 1. 部品(`/static/…`、日報の `/sv/<版>/…`)は exe が自分でファイルから返す
//! 2. それ以外は、その宛先のツールの Python へ標準入出力で渡す
//!    (まだ起こしていなければ、ここで起こす ── **開いたタブのツールだけ**動く)
//! 3. ツールへ渡す前に、Host と Origin を**単体のデスクトップ版のときと同じ形**に直す
//!    (各ツールの Python は Host を `app.localhost` / `localhost` に限って確かめる。
//!    カレンダーは Origin と Host の一致も見る)
//! 4. ツールの画面(HTML)には、窓まわりの頼みごとを大きなタブ経由で外枠へ渡す
//!    台本(`embed.js`)を差し込む。`X-Frame-Options` は外す(大きなタブの枠に出すため。
//!    独自の宛先はアプリの外からは読めないので、はめ込みの心配が無い)

use std::sync::Arc;
use std::time::Duration;

use tauri::http::{Request, Response};

use crate::bridge::Phase;
use crate::catalog::Tool;
use crate::pages;
use crate::shell::Shell;
use crate::statics;

/// ツールの Python へ渡すときの Host(単体のデスクトップ版の窓と同じ名前)
pub const TOOL_HOST: &str = "app.localhost";
/// その Origin
pub const TOOL_ORIGIN: &str = "http://app.localhost";

pub fn html(status: u16, body: String) -> Response<Vec<u8>> {
    Response::builder()
        .status(status)
        .header("Content-Type", "text/html; charset=utf-8")
        .header("Cache-Control", "no-store")
        .body(body.into_bytes())
        .unwrap()
}

pub fn json_error(status: u16, code: &str, message: &str) -> Response<Vec<u8>> {
    let body = serde_json::json!({"error": {"code": code, "message": message}});
    Response::builder()
        .status(status)
        .header("Content-Type", "application/json")
        .header("Cache-Control", "no-store")
        .body(serde_json::to_vec(&body).unwrap())
        .unwrap()
}

/// 画面(ページ)を求めているか。API・部品・音は違う(待たせ方と、失敗の返し方が変わる)
pub fn wants_page(method: &str, path: &str) -> bool {
    method == "GET"
        && !path.starts_with("/api/")
        && !path.starts_with("/static/")
        && !path.starts_with("/sv/")
        && !path.starts_with("/sound/")
        && !path.starts_with(pages::SHELL_PREFIX)
}

/// 日報の `/sv/<版>/<道>` → `/static/<道>`(版は道の一部。中身が変われば URL が変わる)
fn versioned_static(path: &str) -> Option<String> {
    let rest = path.strip_prefix("/sv/")?;
    let (_, file) = rest.split_once('/')?;
    (!file.is_empty()).then(|| format!("/static/{file}"))
}

pub fn handle(shell: &Arc<Shell>, tool: &Tool, request: Request<Vec<u8>>) -> Response<Vec<u8>> {
    let uri = request.uri().clone();
    let path = uri.path().to_string();
    let query = uri.query().unwrap_or("").to_string();
    let method = request.method().as_str().to_string();
    let Some(bridge) = shell.bridge(&tool.id).cloned() else {
        return json_error(404, "no_tool", "そのツールはありません");
    };

    // --- 外枠が受け持つ経路(そのツールだけ起こし直す) ---
    if path == pages::RESTART_PATH {
        if bridge.phase() == Phase::Ended || !bridge.launched() {
            let _ = shell.restart_tool(&tool.id, false);
        }
        return html(200, pages::moving_to("/"));
    }

    // --- 部品は自分で返す(Python を待たない) ---
    if method == "GET" {
        if path.starts_with("/static/") {
            if let Some(reply) = statics::serve(&tool.static_dir(), &path, &query) {
                return reply;
            }
        } else if let Some(file) = versioned_static(&path) {
            // 版が道に入っているので長く控えてよい(日報の Flask と同じ)
            if let Some(reply) = statics::serve(&tool.static_dir(), &file, "v=stamped") {
                return reply;
            }
        }
    }

    let page = wants_page(&method, &path);

    // --- まだ起こしていなければ起こす(開いたタブのツールだけ) ---
    if !bridge.launched() {
        bridge.ensure_started();
    }
    if bridge.phase() == Phase::Starting {
        // 画面なら少しだけ待ち、まだなら「起動しています」を出して読み直してもらう。
        // 画面以外(API・部品)は始まるまで待つ
        let limit = if page { Duration::from_millis(400) } else { Duration::from_secs(120) };
        if !bridge.wait_started(limit) && bridge.phase() == Phase::Starting {
            return html(200, pages::starting(&tool.name));
        }
    }
    if bridge.phase() != Phase::Started {
        if !page && path != "/" {
            return json_error(503, "python_down", "このツールの処理が動いていません。タブの「もう一度開く」を押してください。");
        }
        if bridge.ended_by_quit() {
            return html(200, pages::ended(&tool.name));
        }
        return html(
            503,
            pages::failure(&tool.name, bridge.failure().as_ref(), &bridge.python(), &bridge.stderr_tail(), &shell.log_hint(tool)),
        );
    }

    // --- Host・Origin を単体のデスクトップ版と同じ形に ---
    let own_origin = tool.origin();
    let navigating = request
        .headers()
        .get("sec-fetch-mode")
        .and_then(|v| v.to_str().ok())
        .map(|v| v.eq_ignore_ascii_case("navigate"))
        .unwrap_or(false);
    let mut headers: Vec<(String, String)> = Vec::new();
    for (name, value) in request.headers() {
        let Ok(value) = value.to_str() else { continue };
        let key = name.as_str();
        if key.eq_ignore_ascii_case("host") {
            continue;
        }
        if !tool.is_portal() && key.eq_ignore_ascii_case("origin") {
            // 自分の宛先からの要求だけ、単体のときの宛先に読み替える(ほかの宛先はそのまま。ツールが断る)
            let value = if value == own_origin { TOOL_ORIGIN.to_string() } else { value.to_string() };
            headers.push((key.to_string(), value));
            continue;
        }
        if !tool.is_portal() && key.eq_ignore_ascii_case("referer") {
            if let Some(rest) = value.strip_prefix(&own_origin) {
                headers.push((key.to_string(), format!("{TOOL_ORIGIN}{rest}")));
            }
            continue;
        }
        if key.eq_ignore_ascii_case("sec-fetch-site") && navigating && value != "same-origin" {
            // 大きなタブの枠への最初の読み込みは「別のサイトから」になる。単体の窓では
            // 「利用者が開いた(none)」だったので、それに合わせる(ツールの守りはそのまま)
            headers.push((key.to_string(), "none".to_string()));
            continue;
        }
        headers.push((key.to_string(), value.to_string()));
    }
    headers.push(("Host".into(), TOOL_HOST.into()));

    match bridge.call(&method, &path, &query, headers, request.body()) {
        Ok(reply) if page && (300..400).contains(&reply.status) => {
            // **画面の移り先(302 など)は、WebView が独自の宛先ではたどらない**
            // (「Redirecting...」のまま止まる)。移り先へ自分で移るページに替える
            let target = reply.header("location").unwrap_or("/").to_string();
            let target = target
                .strip_prefix(TOOL_ORIGIN)
                .or_else(|| target.strip_prefix(&own_origin))
                .map(|rest| if rest.is_empty() { "/".to_string() } else { rest.to_string() })
                .unwrap_or(target);
            html(200, pages::moving_to(&target))
        }
        Ok(reply) => {
            let is_html = reply
                .header("content-type")
                .map(|v| v.to_ascii_lowercase().starts_with("text/html"))
                .unwrap_or(false);
            let mut builder = Response::builder().status(reply.status);
            for (k, v) in &reply.headers {
                // 長さと転送の方法は WebView が自分で決める
                if k.eq_ignore_ascii_case("content-length") || k.eq_ignore_ascii_case("transfer-encoding") {
                    continue;
                }
                if !tool.is_portal() {
                    // 大きなタブの枠に出す(はめ込みの守りは外す。上の説明)
                    if k.eq_ignore_ascii_case("x-frame-options") {
                        continue;
                    }
                    if k.eq_ignore_ascii_case("content-security-policy") && v.to_ascii_lowercase().contains("frame-ancestors") {
                        continue;
                    }
                }
                builder = builder.header(k.as_str(), v.as_str());
            }
            let body = if is_html && !tool.is_portal() && method == "GET" {
                inject(&reply.body, shell.shim_for(tool).as_deref())
            } else {
                reply.body
            };
            builder.body(body).unwrap_or_else(|_| json_error(500, "bad_reply", "応答を組み立てられませんでした"))
        }
        Err(reason) => {
            if page || path == "/" {
                html(
                    503,
                    pages::failure(&tool.name, bridge.failure().as_ref(), &bridge.python(), &bridge.stderr_tail(), &shell.log_hint(tool)),
                )
            } else {
                json_error(503, "python_down", &reason)
            }
        }
    }
}

/// 画面(HTML)の先頭に台本を差し込む。`<head>` の直後(無ければ `<html>` の直後、それも無ければ頭)。
/// **ツールのどの台本よりも先に**走らせるため(各ツールの `desktop.js` は読み込んだ時点で
/// `window.__TAURI__` を見る)。
pub fn inject(body: &[u8], shim: Option<&str>) -> Vec<u8> {
    let Some(shim) = shim else { return body.to_vec() };
    let tag = format!("<script>{shim}</script>");
    let lower: Vec<u8> = body.iter().take(4096).map(|b| b.to_ascii_lowercase()).collect();
    // `<head` のあとが `>` か空白のもの(`<header>` と取り違えない)
    let after = |needle: &[u8]| -> Option<usize> {
        let n = needle.len();
        let start = (0..lower.len().saturating_sub(n)).find(|&i| {
            &lower[i..i + n] == needle && matches!(lower.get(i + n), Some(b'>' | b' ' | b'\t' | b'\n' | b'\r'))
        })?;
        let close = lower[start..].iter().position(|&b| b == b'>')?;
        Some(start + close + 1)
    };
    let at = after(b"<head").or_else(|| after(b"<html")).unwrap_or(0);
    let mut out = Vec::with_capacity(body.len() + tag.len());
    out.extend_from_slice(&body[..at]);
    out.extend_from_slice(tag.as_bytes());
    out.extend_from_slice(&body[at..]);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 画面かどうか() {
        assert!(wants_page("GET", "/"));
        assert!(wants_page("GET", "/board"));
        assert!(!wants_page("POST", "/board"));
        for path in ["/api/x", "/static/a.js", "/sv/4.22.0-ab/js/app.js", "/sound/x", "/__alltools__/restart"] {
            assert!(!wants_page("GET", path), "{path}");
        }
    }

    #[test]
    fn 版付きの部品の道() {
        assert_eq!(versioned_static("/sv/4.22.0-abcd/js/app.js").as_deref(), Some("/static/js/app.js"));
        assert_eq!(versioned_static("/sv/x/").as_deref(), None);
        assert_eq!(versioned_static("/static/a.js"), None);
    }

    #[test]
    fn 台本はheadの直後に入る() {
        let page = b"<!doctype html>\n<HTML lang=\"ja\"><Head data-x=\"1\"><meta charset=\"utf-8\"><script src=\"/a.js\"></script>";
        let out = String::from_utf8(inject(page, Some("window.X=1"))).unwrap();
        let shim = out.find("<script>window.X=1</script>").unwrap();
        assert!(shim > out.find("<Head data-x=\"1\">").unwrap());
        assert!(shim < out.find("<meta").unwrap(), "ツールの台本より先");
        // head が無ければ html の直後、それも無ければ頭
        let out = String::from_utf8(inject(b"<html><body>x", Some("Y"))).unwrap();
        assert!(out.starts_with("<html><script>Y</script>"));
        let out = String::from_utf8(inject(b"<p>x", Some("Z"))).unwrap();
        assert!(out.starts_with("<script>Z</script><p>"));
        // 台本が無ければそのまま
        assert_eq!(inject(b"<p>x", None), b"<p>x".to_vec());
    }
}
