//! 終わり方 ── **全ツールに訊いてから、まとめて終える**
//!
//! 単体のデスクトップ版は、× で自分の Python にだけ「終わってよいか」を訊いていた。
//! 統合では1つの窓に4つのツールが動いているので:
//!
//! 0. 各ツールの画面に**打ちかけを置いてもらう**(日報の入力中の行。画面の中にしか無い)
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
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

use crate::bridge::{Bridge, Phase};
use crate::shell::{js_string, Shell};

/// 訊くだけの問い合わせの応答を待つ上限
const ASK_LIMIT: Duration = Duration::from_secs(10);

pub fn request_close(shell: Arc<Shell>, app: AppHandle, quitting: Option<String>) {
    if shell.closing.swap(true, Ordering::SeqCst) {
        // もう終わり方の途中(確認は1つだけ)。本人がもう一度 × ・「終了」を押したなら、
        // 黙って捨てない: 閉じる前の確認を出している画面があれば、そのタブを出して
        // 答えずに終えるかを訊いてもらう(画面が固まって答えないときも閉じられるように)
        if quitting.is_none() {
            shell.tell_shell(CLOSE_AGAIN);
        }
        return;
    }
    thread::spawn(move || {
        if !run(&shell, &app, quitting.as_deref()) {
            shell.closing.store(false, Ordering::SeqCst);
        }
    });
}

/// 終わり方の途中で、もう一度 × ・「終了」が押されたときに大きなタブの画面へ頼むこと
const CLOSE_AGAIN: &str = "window.__shell && window.__shell.closeAgain ? window.__shell.closeAgain() : null";

/// 答えが無かった・読めなかったときの理由。**「終わってよい」とはみなさない**
/// (処理の途中で返事ができないのかもしれない。黙って止めると、途中の処理が消える)
const NO_ANSWER: &str = "返事がありません(処理の途中かもしれません)";

/// 訊いた答え1つを、途中の処理の理由に直す。終わってよいなら None
fn reason_of(reply: &Result<(u16, serde_json::Value), String>) -> Option<String> {
    match reply {
        Ok((status, _)) if (200..300).contains(status) => None,
        Ok((409, body)) => Some(busy_reason(body)),
        _ => Some(NO_ANSWER.to_string()),
    }
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
                reason_of(&reply).map(|reason| (title, reason))
            })
        })
        .collect();
    asks.into_iter().filter_map(|h| h.join().ok().flatten()).collect()
}

/// まとめた確認の「それでも終える」ボタン。**どんな答え(処理中・読めない・返事が無い)でも、
/// 本人はこれで終えられる**(窓を閉じられない状態を作らない)
const QUIT_ANYWAY: &str = "終了する";

/// まとめた確認の文。ツールの名前と理由を並べ、それでも終えるかを訊く
fn busy_text(head: &str, busy: &[(String, String)]) -> String {
    let mut text = head.to_string();
    text.push_str("次のツールで処理が残っています:\n\n");
    for (title, reason) in busy {
        text.push_str(&format!("・{title}: {}\n", reason.replace('\n', " ")));
    }
    text.push_str("\nそれでも日報複合ツールを終了しますか?\n(送れなかった操作は手元に残り、次に開いたときに送ります)");
    text
}

/// 途中の処理があるツールを並べて、まとめて1つ訊く。終えてよければ true
fn confirm_busy(shell: &Arc<Shell>, app: &AppHandle, head: &str, busy: &[(String, String)]) -> bool {
    let mut dialog = app
        .dialog()
        .message(busy_text(head, busy))
        .title(&shell.catalog.name)
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom(QUIT_ANYWAY.into(), "やめる".into()));
    // 窓に付けて出す(確認を読んでいるあいだに、裏の画面で打てないように)
    if let Some(window) = app.get_webview_window("main") {
        dialog = dialog.parent(&window);
    }
    dialog.blocking_show()
}

/// 終えずにやめたとき、そのタブを読み直してよいツール。**止まったと確かめられたものだけ**
/// (`/api/shutdown` が 2xx を返した)。答えが無かった(待ちきれなかった)ツールは、まだ
/// 動いているかもしれない ── 読み直すと画面の打ちかけが消える
fn stopped_ids<'a>(targets: impl IntoIterator<Item = (&'a str, &'a Result<(u16, serde_json::Value), String>)>) -> Vec<String> {
    targets
        .into_iter()
        .filter(|(_, reply)| matches!(reply, Ok((status, _)) if (200..300).contains(status)))
        .map(|(id, _)| id.to_string())
        .collect()
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

/// 終わり方をやめたとき、**止まったツールの Python を終えてから**、そのタブを読み直す
/// (「終了しました / もう一度開く」が出る)。
///
/// 「終了してよい」を言ったツールの Python は、標準入力が閉じるまで要求に答える(看板は
/// 閉じるまで待ち、日報・点検表は 6 秒で自分で終わる)。以前は終えずに読み直していた ──
/// ツールの画面がふつうに出て、すぐ裏の Python が居なくなり、どの操作も「処理が動いて
/// いません」になった(終えたと言ったツールなので「止まりました / もう一度開く」も出ない)。
/// 標準入力を閉じれば、最後の書き戻しを済ませて自分で終わる(待つ上限はツールごと)。
fn end_then_reload(shell: &Arc<Shell>, ids: Vec<String>) {
    let bridges = ids
        .iter()
        .filter_map(|id| Some((shell.catalog.by_id(id)?.quit_wait, shell.bridge(id)?.clone())))
        .collect();
    end_stopped(bridges);
    for id in &ids {
        shell.tell_shell(&format!("window.__shell && window.__shell.reloadTool({})", js_string(id)));
    }
}

/// 止まった(「終了してよい」を言った・終わってもらった)ツールの Python を終える。並べて待つ。
/// 「終了してよい」が届くのを少し待ってから(上限 `quit_wait`)、標準入力を閉じる
fn end_stopped(bridges: Vec<(Duration, Arc<Bridge>)>) {
    let handles: Vec<_> = bridges
        .into_iter()
        .map(|(quit_wait, bridge)| {
            thread::spawn(move || {
                bridge.wait_quit(quit_wait);
                bridge.shutdown(bridge.exit_grace());
            })
        })
        .collect();
    for handle in handles {
        let _ = handle.join();
    }
}

/// 管理者以外停止(入口の Python の `remote_stop`): **確認を出さずに**全ツールに終わってもらい、窓を閉じる。
///
/// 期限・止めてよいかは入口が決めた(各ツールの「残っているもの」を待ち、5分で強制。画面の打ちかけは
/// 入口が大きなタブの画面に頼んで置いてもらった)。ここで訊き直すと、管理者の停止が確認で止まる。
/// 終わり方の途中(確認を出している)でも閉じる。各ツールの「終了してよい」で終わり方が
/// 始まらないよう、先に `closing` を立てる。
pub fn force_close(shell: Arc<Shell>, app: AppHandle) {
    if shell.is_exiting() || shell.remote_stopping.swap(true, Ordering::SeqCst) {
        return;
    }
    shell.closing.store(true, Ordering::SeqCst);
    thread::spawn(move || {
        crate::places::shell_log(&shell.root, "管理者以外停止: 確認なしで全ツールを閉じます");
        let targets: Vec<_> = shell
            .all_bridges()
            .into_iter()
            .filter(|(tool, bridge)| !tool.is_portal() && bridge.phase() == Phase::Started)
            .map(|(tool, bridge)| (tool.quit_wait, bridge))
            .collect();
        let handles: Vec<_> = targets
            .into_iter()
            .map(|(wait, bridge)| {
                thread::spawn(move || {
                    let _ = bridge.call_json("POST", "/api/shutdown", &json!({"force": true}), ASK_LIMIT);
                    bridge.wait_quit(wait);
                })
            })
            .collect();
        for handle in handles {
            let _ = handle.join();
        }
        crate::places::shell_log(&shell.root, "管理者以外停止: 閉じました");
        app.exit(0);
    });
}

/// 画面に打ちかけを置いてもらうのを待つ上限(共有へではなく手元の SQLite へ置くだけなので短い)
const PREPARE_LIMIT: Duration = Duration::from_secs(15);

fn run(shell: &Arc<Shell>, app: &AppHandle, quitting: Option<&str>) -> bool {
    // 0. **画面の打ちかけを置く**(日報の入力中の行は画面の中にしか無い)。
    //    置けずに「閉じない」を選ばれたら、ここでやめる
    if !shell.prepare_screens(PREPARE_LIMIT) {
        crate::places::shell_log(&shell.root, "終了をやめました(画面の打ちかけを置けなかった)");
        end_then_reload(shell, quitting.into_iter().map(str::to_string).collect());
        return false;
    }
    let busy = ask_all(shell, quitting);
    let force = !busy.is_empty();
    if force {
        let head = quitting
            .and_then(|id| shell.catalog.by_id(id))
            .map(|tool| format!("{}は終了しました。\n\n", tool.title))
            .unwrap_or_default();
        if !confirm_busy(shell, app, &head, &busy) {
            // そのタブには「終了しました / もう一度開く」を出す
            end_then_reload(shell, quitting.into_iter().map(str::to_string).collect());
            return false;
        }
        // 確認を読んでいるあいだに打った分がある(確認は窓の外に出ることがある)。
        // **止める直前に、もう一度画面の打ちかけを置いてもらう**
        if !shell.prepare_screens(PREPARE_LIMIT) {
            crate::places::shell_log(&shell.root, "終了をやめました(確認のあとの打ちかけを置けなかった)");
            end_then_reload(shell, quitting.into_iter().map(str::to_string).collect());
            return false;
        }
    }

    // 全ツールに終わってもらう(並べて頼む)
    let targets: Vec<_> = shell
        .all_bridges()
        .into_iter()
        .filter(|(tool, bridge)| Some(tool.id.as_str()) != quitting && bridge.phase() == Phase::Started)
        .collect();
    let asks: Vec<_> = targets
        .iter()
        .map(|(_, bridge)| {
            let bridge = bridge.clone();
            thread::spawn(move || bridge.call_json("POST", "/api/shutdown", &json!({"force": force}), ASK_LIMIT))
        })
        .collect();
    let replies: Vec<_> = asks.into_iter().map(|h| h.join().unwrap_or_else(|_| Err("panic".into()))).collect();

    // 訊いたあとで処理を始めたツール(訊いたときは「終わってよい」だった)。**黙って止めない**
    let late: Vec<_> = targets
        .iter()
        .zip(&replies)
        .filter(|(_, reply)| matches!(reply, Ok((409, _))))
        .map(|((tool, bridge), reply)| (tool.clone(), bridge.clone(), reason_of(reply).unwrap_or_default()))
        .collect();
    if !late.is_empty() {
        let listed: Vec<_> = late.iter().map(|(tool, _, reason)| (tool.title.clone(), reason.clone())).collect();
        if !confirm_busy(shell, app, "ほかのツールは終了しました。\n\n", &listed) {
            // 終えたツールのタブには「終了しました / もう一度開く」を出し、処理中のツールは続ける。
            // 読み直すのは**止まったと確かめられたツールだけ**(答えが無かったツールは動いて
            // いるかもしれない。読み直すと打ちかけが消える)
            let stopped = stopped_ids(targets.iter().zip(&replies).map(|((tool, _), reply)| (tool.id.as_str(), reply)));
            end_then_reload(shell, stopped.into_iter().chain(quitting.map(str::to_string)).collect());
            crate::places::shell_log(&shell.root, "終了をやめました(終える途中で処理を始めたツールがあった)");
            return false;
        }
        for (_, bridge, _) in &late {
            let _ = bridge.call_json("POST", "/api/shutdown", &json!({"force": true}), ASK_LIMIT);
        }
    }

    // 「終了してよい」(送り残しを送り終えた)を待つ。来なくても上限で進む
    let waits: Vec<_> = targets
        .into_iter()
        .map(|(tool, bridge)| {
            let wait = tool.quit_wait;
            thread::spawn(move || {
                bridge.wait_quit(wait);
            })
        })
        .collect();
    for handle in waits {
        let _ = handle.join();
    }
    crate::places::shell_log(
        &shell.root,
        &format!("終了しました(途中の処理: {} / 終える途中で始めた処理: {})", busy.len(), late.len()),
    );
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

    #[test]
    fn やめたときに読み直すのは止まったと確かめたツールだけ() {
        let ok = Ok((200, json!({"stopped": true})));
        let busy = Ok((409, json!({"busy": "x"})));
        let timeout: Result<(u16, serde_json::Value), String> = Err("timeout".into());
        let broken = Ok((500, json!({})));
        let ids = stopped_ids(vec![("kanban", &ok), ("nippou", &busy), ("calendar", &timeout), ("inspection", &broken)]);
        assert_eq!(ids, vec!["kanban".to_string()]);
    }

    #[test]
    fn やめたときは止まったツールのpythonを終えてから読み直す() {
        // 「終了してよい」を言ったあとも、標準入力が閉じるまで答え続ける Python(看板と同じ)
        let (bridge, dir) = crate::bridge::testing::fake("closing_end", "endtool");
        assert!(bridge.ensure_started());
        assert!(bridge.wait_started(Duration::from_secs(30)), "{:?}", bridge.stderr_tail());
        assert_eq!(bridge.call("POST", "/api/shutdown", "", vec![], b"{}").unwrap().status, 200);
        assert!(bridge.wait_quit(Duration::from_secs(10)));
        assert!(bridge.call("GET", "/", "", vec![], b"").is_ok(), "終えなければ、画面はまだ届いてしまう");

        end_stopped(vec![(Duration::from_secs(5), bridge.clone())]);
        assert!(bridge.ended_by_quit(), "読み直すと「終了しました / もう一度開く」が出る");
        assert!(bridge.call("GET", "/", "", vec![], b"").is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn どんな答えでも理由を出してそれでも終えるを選べる() {
        // 未送信あり(409)・読めない(409 の理由・500)・答えない(待ちきれない・つながらない)・おかしな答え
        let answers: Vec<(&str, Result<(u16, serde_json::Value), String>, &str)> = vec![
            ("カレンダー", Ok((409, json!({"running": ["送信待ち 3 件"]}))), "送信待ち 3 件"),
            ("看板", Ok((409, json!({"busy": "共有DBを読めません"}))), "共有DBを読めません"),
            ("日報", Ok((500, json!({}))), NO_ANSWER),
            ("点検表", Err("timeout".into()), NO_ANSWER),
            ("入口", Ok((404, json!(null))), NO_ANSWER),
        ];
        for (title, reply, reason) in answers {
            let got = reason_of(&reply);
            assert_eq!(got.as_deref(), Some(reason), "{title}: 黙って終える・黙って止まる");
            let text = busy_text("", &[(title.to_string(), got.unwrap())]);
            assert!(text.contains(&format!("・{title}: {reason}")), "{text}");
            assert!(text.contains("それでも日報複合ツールを終了しますか?"), "{text}");
        }
        assert_eq!(QUIT_ANYWAY, "終了する");
    }

    #[test]
    fn 終わり方の途中でもう一度押されたら画面に訊いてもらう() {
        // 黙って捨てると、裏のタブで確認を出したままの画面があっても「× が効かない」に見える
        assert!(CLOSE_AGAIN.contains("window.__shell.closeAgain()"));
        let js = include_str!("../../portal/static/js/shell.js");
        assert!(js.contains("closeAgain() {"), "大きなタブの画面に受け口が無い");
    }

    #[test]
    fn 返事が無いツールを終わってよいとはみなさない() {
        assert_eq!(reason_of(&Ok((200, json!({"can_stop": true})))), None);
        assert_eq!(reason_of(&Ok((409, json!({"running": ["印刷"]})))).as_deref(), Some("印刷"));
        // 待ちきれなかった・つながらない・おかしな答え ── どれも訊く
        assert_eq!(reason_of(&Err("timeout".into())).as_deref(), Some(NO_ANSWER));
        assert_eq!(reason_of(&Ok((500, json!({})))).as_deref(), Some(NO_ANSWER));
        assert_eq!(reason_of(&Ok((404, json!({})))).as_deref(), Some(NO_ANSWER));
    }
}
