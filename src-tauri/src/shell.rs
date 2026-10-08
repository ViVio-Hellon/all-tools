//! 外枠が持っている状態(全ツールの Python・窓・終わり方)。
//!
//! `relay.rs`(画面の要求)・`services.rs`(画面からの頼まれごと)・`closing.rs`(終わり方)が
//! これを共有する。

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, SystemTime};

use tauri::{AppHandle, Manager};

use crate::bridge::{self, Bridge, Phase, Spec};
use crate::catalog::{Catalog, Tool};

/// 外枠の exe の名前(案内に出す)
pub const EXE_LABEL: &str = "統合ツール.exe(AllTools.exe)";

pub struct Shell {
    pub root: PathBuf,
    pub catalog: Catalog,
    bridges: HashMap<String, Arc<Bridge>>,
    app: OnceLock<AppHandle>,
    /// 終わり方の途中か(× を何度押しても、確認は1つだけ)
    pub closing: AtomicBool,
    /// 帳票などの窓に付ける番号
    windows: AtomicU64,
    /// 埋め込みの台本(`portal/static/js/embed.js`)。書き換わったら読み直す
    shim: Mutex<Option<(SystemTime, String)>>,
    /// 終える前の「打ちかけを置いて」の返事を待っている口([`Shell::prepare_screens`])
    prepared: Mutex<Option<Sender<bool>>>,
}

impl Shell {
    pub fn new(root: PathBuf, catalog: Catalog, demo: bool) -> Arc<Self> {
        let mut bridges = HashMap::new();
        for tool in catalog.all() {
            let mut env = vec![
                ("ALLTOOLS_SHELL".to_string(), "1".to_string()),
                ("ALLTOOLS_TOOL".to_string(), tool.id.clone()),
                ("ALLTOOLS_ROOT".to_string(), root.display().to_string()),
            ];
            if let Some(name) = &tool.demo_env {
                env.push((name.clone(), if demo { "1" } else { "0" }.to_string()));
            }
            let spec = Spec {
                id: tool.id.clone(),
                dir: tool.dir.clone(),
                token_env: tool.token_env(),
                python_envs: vec![tool.python_env(), "ALLTOOLS_PYTHON".to_string()],
                env,
                exit_grace: tool.exit_grace,
                exe_label: EXE_LABEL.to_string(),
            };
            bridges.insert(tool.id.clone(), Bridge::new(spec, bridge::new_token()));
        }
        Arc::new(Self {
            root,
            catalog,
            bridges,
            app: OnceLock::new(),
            closing: AtomicBool::new(false),
            windows: AtomicU64::new(1),
            shim: Mutex::new(None),
            prepared: Mutex::new(None),
        })
    }

    /// 終える前に、**各ツールの画面に打ちかけを置いてもらう**(日報の入力中の行など)。
    ///
    ///     デスクトップ版の窓の×・終了では、日報の打ちかけはまだ保存されません:
    ///     保存するようにしてください
    ///
    /// 画面のデータは Python には無く、画面の中にしか無い。大きなタブの画面に頼み
    /// (`window.__shell.prepareClose()` → 各ツールの枠へ)、返事([`Shell::screens_prepared`])
    /// を待つ。`false`(置けずに「閉じない」を選ばれた)なら終えない。
    /// 返事が来ない(画面が固まっている)ときは上限で進む ── 窓が閉じられなくなるよりよい
    pub fn prepare_screens(&self, limit: Duration) -> bool {
        let (tx, rx) = mpsc::channel();
        if let Ok(mut slot) = self.prepared.lock() {
            *slot = Some(tx);
        }
        self.tell_shell("window.__shell && window.__shell.prepareClose ? window.__shell.prepareClose() : null");
        let answer = rx.recv_timeout(limit).unwrap_or(true);
        if let Ok(mut slot) = self.prepared.lock() {
            *slot = None;
        }
        answer
    }

    /// 大きなタブの画面からの返事(各ツールの画面が打ちかけを置き終えた)
    pub fn screens_prepared(&self, ok: bool) {
        if let Ok(slot) = self.prepared.lock() {
            if let Some(tx) = slot.as_ref() {
                let _ = tx.send(ok);
            }
        }
    }

    pub fn set_app(&self, app: AppHandle) {
        let _ = self.app.set(app);
    }

    pub fn app(&self) -> Option<&AppHandle> {
        self.app.get()
    }

    pub fn bridge(&self, id: &str) -> Option<&Arc<Bridge>> {
        self.bridges.get(id)
    }

    pub fn portal_bridge(&self) -> &Arc<Bridge> {
        &self.bridges[crate::catalog::PORTAL]
    }

    /// 入口と全ツールの Python(入口が先)
    pub fn all_bridges(&self) -> Vec<(&Tool, Arc<Bridge>)> {
        self.catalog.all().filter_map(|t| Some((t, self.bridges.get(&t.id)?.clone()))).collect()
    }

    pub fn next_window_number(&self) -> u64 {
        self.windows.fetch_add(1, Ordering::SeqCst)
    }

    /// 大きなタブの画面へ JS を渡す(そのツールのタブを読み直す・状態を知らせる)
    pub fn tell_shell(&self, js: &str) {
        if let Some(window) = self.app().and_then(|a| a.get_webview_window("main")) {
            let _ = window.eval(js);
        }
    }

    /// そのツールの帳票などの窓を全部閉じる(立て直しのとき。ほかのツールの窓には触らない)
    pub fn close_windows_of(&self, tool: &str) {
        let Some(app) = self.app() else { return };
        let prefix = format!("report-{tool}-");
        for (label, window) in app.webview_windows() {
            if label.starts_with(&prefix) {
                let _ = window.destroy();
            }
        }
    }

    /// そのツールの Python だけを起こし直す(窓は閉じない)。
    ///
    /// 看板のモード切替(`restart_app`)と、「もう一度開く」で使う。前の Python は
    /// 最後の書き戻しを済ませてから終わる(待つ上限はツールごと)。
    pub fn restart_tool(self: &Arc<Self>, tool: &str, reload_frame: bool) -> Result<(), String> {
        let bridge = self.bridge(tool).ok_or_else(|| format!("知らないツールです: {tool}"))?.clone();
        // 先に「起動しています」にしておく(直後に読み直した画面が前の代へ行かないように)
        bridge.mark_restarting();
        self.close_windows_of(tool);
        let me = self.clone();
        let id = tool.to_string();
        thread::spawn(move || {
            bridge.finish_restart();
            if reload_frame {
                me.tell_shell(&format!("window.__shell && window.__shell.reloadTool({})", js_string(&id)));
            }
        });
        Ok(())
    }

    /// 埋め込みの台本を、そのツールの名前と入口の宛先を入れて返す。
    ///
    /// `portal/static/js/embed.js` を読む(exe を作り直さずに直せるように)。
    pub fn shim_for(&self, tool: &Tool) -> Option<String> {
        let path = self.root.join("portal").join("static").join("js").join("embed.js");
        let modified = std::fs::metadata(&path).and_then(|m| m.modified()).ok()?;
        let mut cache = self.shim.lock().unwrap();
        let fresh = matches!(&*cache, Some((stamp, _)) if *stamp == modified);
        if !fresh {
            let text = std::fs::read_to_string(&path).ok()?;
            *cache = Some((modified, text));
        }
        let text = &cache.as_ref()?.1;
        let filled = text
            .replace("\"__ALLTOOLS_TOOL__\"", &js_string(&tool.id))
            .replace("\"__ALLTOOLS_SHELL__\"", &js_string(&self.catalog.portal.origin()));
        // 台本の中の `</script>` で HTML から抜け出させない
        Some(filled.replace("</", "<\\/"))
    }

    /// そのツールの手元の領域のログ(案内用)
    pub fn log_hint(&self, tool: &Tool) -> String {
        crate::places::log_hint(&tool.local_root())
    }

    /// 状態(大きなタブの印に使う)
    pub fn status(&self) -> Vec<serde_json::Value> {
        self.all_bridges()
            .into_iter()
            .map(|(tool, bridge)| {
                let phase = match bridge.phase() {
                    _ if !bridge.launched() => "idle",
                    Phase::Starting => "starting",
                    Phase::Started => "started",
                    Phase::Ended if bridge.ended_by_quit() => "quit",
                    Phase::Ended => "failed",
                };
                serde_json::json!({"id": tool.id, "phase": phase})
            })
            .collect()
    }

    /// 全部の Python を終える(並べて待つ。いちばん長いツールの分だけ掛かる)
    pub fn shutdown_all(&self) {
        let handles: Vec<_> = self
            .all_bridges()
            .into_iter()
            .filter(|(_, b)| b.launched())
            .map(|(_, bridge)| {
                thread::spawn(move || {
                    let grace = bridge.exit_grace();
                    bridge.shutdown(grace);
                })
            })
            .collect();
        for handle in handles {
            let _ = handle.join();
        }
    }
}

/// JS の文字列にする(`"` で囲む。`<` も崩す)
pub fn js_string(text: &str) -> String {
    serde_json::to_string(text).unwrap_or_else(|_| "\"\"".into()).replace('<', "\\u003c")
}
