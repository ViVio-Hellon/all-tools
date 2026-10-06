//! 終わり方 ── **全ツールに訊いてから、まとめて終える**
//!
//! 単体のデスクトップ版は、× で自分の Python にだけ「終わってよいか」を訊いていた。
//! 統合では1つの窓に4つのツールが動いているので:
//!
//! 1. 動いている全ツール(と入口)に**訊くだけ**の問い合わせを送る(`/api/shutdown {"check": true}`)。
//!    この時点ではどれも止めない(1つでも途中の処理があれば、ほかも止めないため)
//! 2. 途中の処理があるツールがあれば、**1つの確認**にまとめて出す(ツールの名前と理由を並べる)
//! 3. 終えてよければ、全ツールに終わってもらう(途中の処理があったときは `force`)
//! 4. 各ツールが送り残しを送り終えて「終了してよい」を知らせてくるのを待つ(ツールごとの上限)
//! 5. 窓を閉じる。標準入力を閉じて、Python に終わってもらう(`Shell::shutdown_all`)
//!
//! あるツールの画面の「終了」で、そのツールの Python が「終了してよい」を知らせてきたときも
//! 同じ道を通る(そのツールはもう終わっているので、ほかのツールにだけ訊く)。やめたときは、
//! そのタブに「終了しました / もう一度開く」が出る。

use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use serde_json::json;
use tauri::AppHandle;
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

use crate::bridge::Phase;
use crate::shell::{js_string, Shell};

/// 訊くだけの問い合わせの応答を待つ上限
const ASK_LIMIT: Duration = Duration::from_secs(10);

pub fn request_close(shell: Arc<Shell>, app: AppHandle, quitting: Option<String>) {
    if shell.closing.swap(true, Ordering::SeqCst) {
        return; // もう終わり方の途中(確認は1つだけ)
    }
    thread::spawn(move || {
        if !run(&shell, &app, quitting.as_deref()) {
            shell.closing.store(false, Ordering::SeqCst);
        }
    });
}

/// 訊いた結果: 途中の処理があったツール(タブの名前, 理由)
fn ask_all(shell: &Arc<Shell>, quitting: Option<&str>) -> Vec<(String, String)> {
    let asks: Vec<_> = shell
        .all_bridges()
        .into_iter()
        .filter(|(tool, bridge)| Some(tool.id.as_str()) != quitting && bridge.phase() == Phase::Started)
        .map(|(tool, bridge)| {
            let title = tool.title.clone();
            thread::spawn(move || {
                let reply = bridge.call_json("POST", "/api/shutdown", &json!({"check": true}), ASK_LIMIT);
                match reply {
                    Ok((409, body)) => Some((title, busy_reason(&body))),
                    _ => None,
                }
            })
        })
        .collect();
    asks.into_iter().filter_map(|h| h.join().ok().flatten()).collect()
}

/// 「途中の処理がある」と断った理由。何をしているか(`running` の一覧・`busy`)を先に使う
/// (`message` は単体の画面向けの「中断して終了しますか?」で、まとめた確認では問いが重なる)
fn busy_reason(body: &serde_json::Value) -> String {
    let running = body
        .get("running")
        .and_then(|v| v.as_array())
        .map(|items| items.iter().filter_map(|x| x.as_str()).collect::<Vec<_>>().join("・"))
        .filter(|text| !text.is_empty());
    running
        .or_else(|| body.get("busy").and_then(|v| v.as_str()).map(str::to_string))
        .or_else(|| body.pointer("/error/message").and_then(|v| v.as_str()).map(str::to_string))
        .or_else(|| body.get("message").and_then(|v| v.as_str()).map(str::to_string))
        .filter(|text| !text.trim().is_empty())
        .unwrap_or_else(|| "実行中の処理があります".to_string())
}

fn run(shell: &Arc<Shell>, app: &AppHandle, quitting: Option<&str>) -> bool {
    let busy = ask_all(shell, quitting);
    let force = !busy.is_empty();
    if force {
        let mut text = String::new();
        if let Some(id) = quitting.and_then(|id| shell.catalog.by_id(id)) {
            text.push_str(&format!("{}は終了しました。\n\n", id.title));
        }
        text.push_str("次のツールで処理が残っています:\n\n");
        for (title, reason) in &busy {
            text.push_str(&format!("・{title}: {}\n", reason.replace('\n', " ")));
        }
        text.push_str("\nそれでも統合ツールを終了しますか?\n(送れなかった操作は手元に残り、次に開いたときに送ります)");
        let yes = app
            .dialog()
            .message(text)
            .title(&shell.catalog.name)
            .kind(MessageDialogKind::Warning)
            .buttons(MessageDialogButtons::OkCancelCustom("終了する".into(), "やめる".into()))
            .blocking_show();
        if !yes {
            if let Some(id) = quitting {
                // そのタブには「終了しました / もう一度開く」を出す
                shell.tell_shell(&format!("window.__shell && window.__shell.reloadTool({})", js_string(id)));
            }
            return false;
        }
    }

    // 全ツールに終わってもらう(並べて頼み、並べて待つ)
    let waits: Vec<_> = shell
        .all_bridges()
        .into_iter()
        .filter(|(tool, bridge)| Some(tool.id.as_str()) != quitting && bridge.phase() == Phase::Started)
        .map(|(tool, bridge)| {
            let wait = tool.quit_wait;
            thread::spawn(move || {
                let _ = bridge.call_json("POST", "/api/shutdown", &json!({"force": force}), ASK_LIMIT);
                // 「終了してよい」(送り残しを送り終えた)を待つ。来なくても上限で進む
                bridge.wait_quit(wait);
            })
        })
        .collect();
    for handle in waits {
        let _ = handle.join();
    }
    crate::places::shell_log(&shell.root, &format!("終了しました(途中の処理: {})", busy.len()));
    app.exit(0);
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 断った理由は何をしているかを先に使う() {
        // 日報・カレンダー・点検表: 一覧と「中断して終了しますか?」
        let body = json!({"reason": "busy", "running": ["取り込み", "印刷"], "message": "中断して終了しますか?"});
        assert_eq!(busy_reason(&body), "取り込み・印刷");
        // 看板: 理由の文
        let body = json!({"busy": "書き戻しの途中です", "error": {"code": "busy", "message": "書き戻しの途中です"}});
        assert_eq!(busy_reason(&body), "書き戻しの途中です");
        assert_eq!(busy_reason(&json!({"error": {"message": "x"}})), "x");
        assert_eq!(busy_reason(&json!({"running": [], "message": "y"})), "y");
        assert_eq!(busy_reason(&json!({})), "実行中の処理があります");
    }
}
