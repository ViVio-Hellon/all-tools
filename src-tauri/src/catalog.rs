//! 何を載せるか(`config/tools.json`)。**Python 側(`portal/catalog.py`)と同じファイルを読む。**
//!
//! 1つの窓に載せるのは「入口(大きなタブの画面と大設定)」と各ツール。どれも
//! 自分の Python を持ち、自分の宛先(`<scheme>://localhost` / Windows は
//! `http://<scheme>.localhost`)で画面を出す。宛先を分けるのは、各ツールの画面が
//! `/api/…` `/static/…` のような**根元からの経路**で書かれているため
//! (1つの宛先の下に並べると、経路がぶつかる)。

use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::Deserialize;

/// 入口(大きなタブの画面・大設定)の名前。ツールの名前と重ならないこと
pub const PORTAL: &str = "portal";

#[derive(Clone, Debug)]
pub struct Tool {
    /// 名前(英字)。宛先・記録・画面とのやりとりで使う
    pub id: String,
    /// 大きなタブに出す短い名前
    pub title: String,
    /// 正式な名前(窓の題名・ダイアログ)
    pub name: String,
    /// 一式のフォルダ(`bridge.py` がある所)
    pub dir: PathBuf,
    /// 宛先の名前(`<scheme>://localhost`)
    pub scheme: String,
    /// 環境変数の頭(`<頭>_TOKEN` `<頭>_PYTHON` `<頭>_LOCAL_DIR`)
    pub env_prefix: String,
    /// 手元の領域の既定の名前(`config/app.json` の `local_dir_name` が優先)
    pub local_dir_name: String,
    /// 終わるとき、Python が自分で終わるのを待つ上限(最後の書き戻し・Excel を閉じる等)
    pub exit_grace: Duration,
    /// 「終わってよい」と答えたあと、Python が送り残しを送り終えて
    /// 「終了してよい」を知らせてくるまで待つ上限
    pub quit_wait: Duration,
    /// 模擬モード(`--demo`)を渡す環境変数(あるツールだけ)
    pub demo_env: Option<String>,
}

impl Tool {
    pub fn is_portal(&self) -> bool {
        self.id == PORTAL
    }

    pub fn token_env(&self) -> String {
        format!("{}_TOKEN", self.env_prefix)
    }

    pub fn python_env(&self) -> String {
        format!("{}_PYTHON", self.env_prefix)
    }

    /// 手元の領域(ログの場所の案内用)
    pub fn local_root(&self) -> PathBuf {
        let name = crate::places::local_dir_name(&self.dir, &self.local_dir_name);
        crate::places::local_root_for(&format!("{}_LOCAL_DIR", self.env_prefix), &name)
    }

    /// 部品(CSS・JS)の置き場所。ツールは `app/static`、入口は `portal/static`
    pub fn static_dir(&self) -> PathBuf {
        if self.is_portal() {
            self.dir.join("portal").join("static")
        } else {
            self.dir.join("app").join("static")
        }
    }

    /// この宛先の頭(`http://kanban.localhost` / `kanban://localhost`)
    pub fn origin(&self) -> String {
        origin(&self.scheme)
    }
}

#[derive(Debug)]
pub struct Catalog {
    pub app_id: String,
    pub name: String,
    pub version: String,
    pub portal: Tool,
    pub tools: Vec<Tool>,
}

#[derive(Deserialize)]
struct RawEntry {
    id: String,
    #[serde(default)]
    title: String,
    #[serde(default)]
    name: String,
    #[serde(default)]
    dir: String,
    #[serde(default)]
    scheme: String,
    #[serde(default)]
    env_prefix: String,
    #[serde(default)]
    local_dir_name: String,
    #[serde(default)]
    exit_grace_sec: Option<u64>,
    #[serde(default)]
    quit_wait_sec: Option<u64>,
    #[serde(default)]
    demo_env: Option<String>,
}

#[derive(Deserialize)]
struct RawCatalog {
    portal: RawEntry,
    tools: Vec<RawEntry>,
}

fn is_name(text: &str) -> bool {
    !text.is_empty() && text.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
}

fn build(root: &Path, raw: RawEntry) -> Result<Tool, String> {
    let scheme = if raw.scheme.is_empty() { raw.id.clone() } else { raw.scheme.clone() };
    if !is_name(&raw.id) || !is_name(&scheme) {
        return Err(format!("名前は英小文字と数字だけにしてください: {} / {}", raw.id, scheme));
    }
    let dir = if raw.dir.is_empty() || raw.dir == "." { root.to_path_buf() } else { root.join(&raw.dir) };
    let env_prefix = if raw.env_prefix.is_empty() { raw.id.to_uppercase() } else { raw.env_prefix.clone() };
    Ok(Tool {
        title: if raw.title.is_empty() { raw.id.clone() } else { raw.title.clone() },
        name: if raw.name.is_empty() { raw.title.clone() } else { raw.name.clone() },
        id: raw.id,
        dir,
        scheme,
        local_dir_name: if raw.local_dir_name.is_empty() { env_prefix.clone() } else { raw.local_dir_name },
        env_prefix,
        exit_grace: Duration::from_secs(raw.exit_grace_sec.unwrap_or(10).clamp(1, 120)),
        quit_wait: Duration::from_secs(raw.quit_wait_sec.unwrap_or(3).clamp(1, 60)),
        demo_env: raw.demo_env.filter(|e| !e.trim().is_empty()),
    })
}

impl Catalog {
    pub fn load(root: &Path) -> Result<Catalog, String> {
        let path = root.join("config").join("tools.json");
        let text = std::fs::read_to_string(&path).map_err(|e| format!("{} を読めません: {e}", path.display()))?;
        let raw: RawCatalog = serde_json::from_str(text.trim_start_matches('\u{feff}'))
            .map_err(|e| format!("{} の書き方が違います: {e}", path.display()))?;
        let app: serde_json::Value = std::fs::read_to_string(root.join("config").join("app.json"))
            .ok()
            .and_then(|t| serde_json::from_str(t.trim_start_matches('\u{feff}')).ok())
            .unwrap_or_default();
        let text_of = |key: &str, fallback: &str| {
            app.get(key).and_then(|v| v.as_str()).filter(|v| !v.is_empty()).unwrap_or(fallback).to_string()
        };
        let mut portal = build(root, raw.portal)?;
        portal.id = PORTAL.into();
        let mut tools = Vec::new();
        for entry in raw.tools {
            let tool = build(root, entry)?;
            if tool.id == PORTAL || tool.scheme == portal.scheme {
                return Err(format!("ツールの名前「{}」は入口と同じです", tool.id));
            }
            if tools.iter().any(|t: &Tool| t.id == tool.id || t.scheme == tool.scheme) {
                return Err(format!("ツールの名前「{}」が重なっています", tool.id));
            }
            tools.push(tool);
        }
        Ok(Catalog {
            app_id: text_of("app_id", "nlm.all-tools"),
            name: text_of("display_name", "日報複合ツール"),
            version: text_of("version", "0.0.0"),
            portal,
            tools,
        })
    }

    /// 入口と全ツール(入口が先)
    pub fn all(&self) -> impl Iterator<Item = &Tool> {
        std::iter::once(&self.portal).chain(self.tools.iter())
    }

    pub fn by_id(&self, id: &str) -> Option<&Tool> {
        self.all().find(|t| t.id == id)
    }

    pub fn by_scheme(&self, scheme: &str) -> Option<&Tool> {
        self.all().find(|t| t.scheme == scheme)
    }

    /// 画面の宛先からツールを引く(`http://kanban.localhost/…` / `kanban://localhost/…`)
    pub fn by_url(&self, url: &tauri::Url) -> Option<&Tool> {
        self.by_scheme(&scheme_of(url)?)
    }
}

/// 宛先の頭。Windows(WebView2)は `http://<名前>.localhost`、ほかは `<名前>://localhost`
pub fn origin(scheme: &str) -> String {
    if cfg!(windows) {
        format!("http://{scheme}.localhost")
    } else {
        format!("{scheme}://localhost")
    }
}

/// 宛先から名前を取り出す(`origin` の逆)。アプリの外の宛先なら `None`
pub fn scheme_of(url: &tauri::Url) -> Option<String> {
    if cfg!(windows) {
        if url.scheme() != "http" {
            return None;
        }
        url.host_str()?.strip_suffix(".localhost").filter(|s| is_name(s)).map(str::to_string)
    } else {
        (url.host_str() == Some("localhost") && is_name(url.scheme())).then(|| url.scheme().to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("alltools_catalog_{}", std::process::id()));
        std::fs::create_dir_all(dir.join("config")).unwrap();
        std::fs::write(
            dir.join("config").join("tools.json"),
            r#"{"portal": {"id": "portal", "title": "大設定", "env_prefix": "ALLTOOLS"},
                "tools": [
                  {"id": "kanban", "title": "看板", "name": "資材発注看板システム", "dir": "tools/kanban",
                   "env_prefix": "KANBAN", "exit_grace_sec": 30},
                  {"id": "inspection", "title": "点検表", "dir": "tools/inspection", "demo_env": "INSPECTION_DEMO"}
                ]}"#,
        )
        .unwrap();
        std::fs::write(dir.join("config").join("app.json"), r#"{"display_name": "日報複合ツール", "version": "1.2.3"}"#).unwrap();
        dir
    }

    #[test]
    fn 読み込み_既定を埋める() {
        let root = sample();
        let c = Catalog::load(&root).unwrap();
        assert_eq!(c.name, "日報複合ツール");
        assert_eq!(c.version, "1.2.3");
        assert_eq!(c.portal.dir, root);
        assert_eq!(c.portal.token_env(), "ALLTOOLS_TOKEN");
        let kanban = c.by_id("kanban").unwrap();
        assert_eq!(kanban.dir, root.join("tools/kanban"));
        assert_eq!(kanban.scheme, "kanban");
        assert_eq!(kanban.exit_grace, Duration::from_secs(30));
        assert_eq!(kanban.python_env(), "KANBAN_PYTHON");
        assert_eq!(kanban.static_dir(), root.join("tools/kanban/app/static"));
        let inspection = c.by_id("inspection").unwrap();
        assert_eq!(inspection.env_prefix, "INSPECTION");
        assert_eq!(inspection.name, "点検表");
        assert_eq!(inspection.demo_env.as_deref(), Some("INSPECTION_DEMO"));
        assert_eq!(c.all().count(), 3);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn 宛先と名前は行き来できる() {
        for name in ["portal", "kanban", "nippou"] {
            let url: tauri::Url = format!("{}/api/x?y=1", origin(name)).parse().unwrap();
            assert_eq!(scheme_of(&url).as_deref(), Some(name));
        }
        assert_eq!(scheme_of(&"https://example.com/".parse().unwrap()), None);
        assert_eq!(scheme_of(&"http://127.0.0.1:8741/".parse().unwrap()), None);
        if cfg!(windows) {
            assert_eq!(scheme_of(&"http://evil.example.localhost.com/".parse().unwrap()), None);
        }
    }
}
