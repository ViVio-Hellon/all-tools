//! 画面の部品(`/static/…` の CSS・JS)を、exe が**自分でファイルから返す**。
//!
//! ブラウザ版では Flask(`app/__init__.py`)が返している。デスクトップ版で同じことを
//! Python に頼むと、画面を開くたびに十数本の要求が標準入出力を往復する。ファイルを
//! 読んで返すだけなら Rust のほうが速く、Python の手(8つ)も空く。
//! 控えの決まりは Flask 側(`_apply_cache_policy`)と同じ:
//!
//! - 版が付いている(`?v=…`)… 7日 控える(版が変われば URL が変わる)
//! - 付いていない          … 毎回確かめる
//!
//! 見つからないものは `None` を返し、Python に任せる(返し方を1か所で決めない)。

use std::path::{Component, Path, PathBuf};

use tauri::http::Response;

const MAX_AGE: u32 = 7 * 24 * 60 * 60;

fn content_type(path: &Path) -> &'static str {
    match path.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase().as_str() {
        "css" => "text/css; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "json" => "application/json",
        "html" => "text/html; charset=utf-8",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "ico" => "image/x-icon",
        "woff2" => "font/woff2",
        "woff" => "font/woff",
        "txt" => "text/plain; charset=utf-8",
        _ => "application/octet-stream",
    }
}

/// `/static/<rel>` → アプリの `app/static/<rel>`。外へ出る経路は受けない
pub fn resolve(root: &Path, path: &str) -> Option<PathBuf> {
    let rel = path.strip_prefix("/static/")?;
    if rel.is_empty() || rel.contains('\\') || rel.contains(':') || rel.contains('%') {
        return None;
    }
    let rel = Path::new(rel);
    if !rel.components().all(|c| matches!(c, Component::Normal(_))) {
        return None; // `..`・絶対パスは受けない
    }
    let file = root.join("app").join("static").join(rel);
    file.is_file().then_some(file)
}

pub fn serve(root: &Path, path: &str, query: &str) -> Option<Response<Vec<u8>>> {
    let file = resolve(root, path)?;
    let body = std::fs::read(&file).ok()?;
    let versioned = query.split('&').any(|p| p.starts_with("v=") && p.len() > 2);
    let cache = if versioned {
        format!("public, max-age={MAX_AGE}, immutable")
    } else {
        "no-cache".to_string()
    };
    Response::builder()
        .status(200)
        .header("Content-Type", content_type(&file))
        .header("Cache-Control", cache)
        .header("X-Content-Type-Options", "nosniff")
        .body(body)
        .ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn root() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("isp_static_test_{}", std::process::id()));
        std::fs::create_dir_all(dir.join("app").join("static").join("css")).unwrap();
        std::fs::write(dir.join("app").join("static").join("css").join("a.css"), "body{}").unwrap();
        std::fs::write(dir.join("secret.txt"), "x").unwrap();
        dir
    }

    #[test]
    fn 部品を返し_版付きは控える() {
        let r = root();
        let res = serve(&r, "/static/css/a.css", "v=2.0.0").unwrap();
        assert_eq!(res.body(), b"body{}");
        assert_eq!(res.headers()["Content-Type"], "text/css; charset=utf-8");
        assert!(res.headers()["Cache-Control"].to_str().unwrap().contains("immutable"));
        let res = serve(&r, "/static/css/a.css", "").unwrap();
        assert_eq!(res.headers()["Cache-Control"], "no-cache");
    }

    #[test]
    fn 外へ出る経路と無いものは返さない() {
        let r = root();
        for path in ["/static/../secret.txt", "/static/css/../../secret.txt", "/static/%2e%2e/secret.txt",
                     "/static/C:/Windows/win.ini", "/static/css\\a.css", "/static/", "/static/none.css",
                     "/api/inventory"] {
            assert!(serve(&r, path, "").is_none(), "{path}");
        }
    }
}
