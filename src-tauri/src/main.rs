//! 統合ツール デスクトップ版の外枠(`統合ツール.exe` / `AllTools.exe`)
//!
//! 日報管理ツール・資材発注看板システム・ライン管理カレンダー・点検表 選択・印刷を、
//! **1つの窓に大きなタブで並べる。中は分けたまま**(ツールごとに別の Python)。
//!
//! 【役割の分け方】(各言語が得意なことをする。docs/統合_事前確認.md)
//!
//! - **Rust(ここ)**: 1つの窓・各ツールの Python の起動と監視(開いたタブだけ起こす・
//!   落ちたらそのタブだけ理由の画面)・宛先ごとの振り分け・画面の部品を直接返す・
//!   台本の差し込み・別窓・保存/選択の標準ダイアログ・既定のブラウザ・
//!   多重起動とブラウザ版との同時起動の防止・まとめた終了の確認
//! - **Python**: 入口(`bridge.py` → `portal/`: 大きなタブ・大設定・タブ表示権限)と
//!   各ツールの業務(`tools/<名前>/bridge.py` 以下。今までどおり)
//! - **JS**: 画面の操作(大きなタブの画面は `portal/static/js/shell.js`)
//!
//! 【ポートを使わない】
//! 画面(WebView)は独自の宛先(`portal://localhost/`・`nippou://localhost/` …、
//! Windows では `http://portal.localhost/` …)を読む。その要求をここで受けて、
//! 部品は自分で返し、それ以外はその宛先のツールの Python の標準入力へ渡す(`relay.rs`)。
//! 127.0.0.1 で待ち受けないので、ポートの取り合い・プロキシ・セキュリティ製品に左右されない。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod bridge;
mod catalog;
mod closing;
mod drops;
mod instance;
mod pages;
mod places;
mod relay;
mod services;
mod shell;
mod statics;

use std::sync::Arc;
use std::thread;

use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_opener::OpenerExt;

use catalog::Catalog;
use shell::{js_string, Shell};

fn main() {
    let root = places::app_root();
    places::set_log_root(&root);
    let demo = std::env::args().any(|a| a == "--demo");
    let catalog = match Catalog::load(&root) {
        Ok(catalog) => catalog,
        Err(reason) => {
            places::shell_log(&root, &format!("一式を読めません: {reason}"));
            show_fatal_and_exit(&format!(
                "統合ツールの一式を読めません。\n\n{reason}\n\n統合ツール.exe は、一式のフォルダ(config\\tools.json と bridge.py がある所)の直下に置いてください。"
            ));
        }
    };
    let shell = Shell::new(root.clone(), catalog, demo);
    let dropped = Arc::new(drops::Dropped::default());

    let mut builder = tauri::Builder::default()
        // 2つ目を開こうとしたら、開いている窓を前に出すだけ(多重起動の防止)。
        // ブラウザ版が「デスクトップ版が動いています」と言うときも、これで前に出る
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        // **dialog より先に。** 画面の confirm をブラウザのもの(答えを待つ)に固定する
        // (dialog は答えを待たない版に置き換え、`if (!confirm(..))` が訊かずに進む)
        .plugin(
            tauri::plugin::Builder::<tauri::Wry>::new("native-dialogs")
                .js_init_script_on_all_frames(include_str!("native_dialogs.js"))
                .build(),
        )
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(shell.clone())
        .manage(dropped.clone());

    // 宛先ごとに受ける(入口と各ツール)。要求ごとに別のスレッドで答える
    // (各ツールの画面は見張り・心拍・操作を同時に出す。重い要求が軽い要求を待たせない)
    for tool in shell.catalog.all().cloned().collect::<Vec<_>>() {
        let shell = shell.clone();
        let scheme = tool.scheme.clone();
        builder = builder.register_asynchronous_uri_scheme_protocol(scheme, move |_ctx, request, responder| {
            let shell = shell.clone();
            let tool = tool.clone();
            thread::spawn(move || responder.respond(relay::handle(&shell, &tool, request)));
        });
    }

    let for_close = shell.clone();
    let for_drop = shell.clone();
    let for_setup = shell.clone();
    let for_exit = shell.clone();
    let app = builder
        .invoke_handler(tauri::generate_handler![
            services::tool_invoke,
            services::tool_invoke_raw,
            services::shell_status,
            services::shell_close,
            services::shell_prepared,
            services::shell_restart_tool,
            drops::shell_dropped_file
        ])
        .on_window_event(move |window, event| match event {
            WindowEvent::CloseRequested { api, .. } if window.label() == "main" => {
                // **画面の「終了」と同じ確認を通る**(全ツールに訊いて、まとめて1つの確認)
                api.prevent_close();
                closing::request_close(for_close.clone(), window.app_handle().clone(), None);
            }
            // ファイルの落下は OS の仕組みで受け、落ちた場所のツールの画面へ渡す(drops.rs)
            WindowEvent::DragDrop(drag) if window.label() == "main" => {
                drops::on_drag(&for_drop, &dropped, drag);
            }
            _ => {}
        })
        .setup(move |app| {
            let shell = for_setup;
            // **ブラウザ版と同時に動かさない**(後から開いたほうが止まる)。
            // デスクトップ版同士なら、窓を前に出すのは single-instance の仕事。
            // それが使えない環境(D-Bus が無い等)でも、ここで2つ目を止める
            match instance::claim_desktop(&shell.catalog.app_id, &places::local_root(&shell.root)) {
                Ok(instance::Claim::Claimed(held)) => {
                    Box::leak(Box::new(held)); // 終わるまで握っておく
                }
                Ok(instance::Claim::Running(instance::BROWSER)) => {
                    places::shell_log(&shell.root, "ブラウザ版が動いているので、デスクトップ版は開きません");
                    refuse_browser_running(app.handle(), &shell.catalog.name);
                    return Ok(());
                }
                Ok(instance::Claim::Running(_)) => {
                    app.handle().exit(0);
                    return Ok(());
                }
                Err(e) => {
                    // 錠そのものが作れない(めったに無い)。止めずに続ける
                    places::shell_log(&shell.root, &format!("同時起動の錠を作れませんでした: {e}"));
                }
            }
            shell.set_app(app.handle().clone());

            // 各ツールの「終了してよい」→ アプリを閉じる道(ほかのツールにも訊く)。
            // 落ちた → **画面は読み直さない**(画面の打ちかけを消さない)。そのタブに知らせを
            // 重ね、読み直すのは本人が「もう一度開く」を押したときだけ。入口が落ちたら、
            // 大きなタブの画面ごと読み直さずに(全タブが消える)、入口の Python だけ起こし直す
            for (tool, bridge) in shell.all_bridges() {
                let id = tool.id.clone();
                let quit_shell = shell.clone();
                let quit_app = app.handle().clone();
                let quit_id = id.clone();
                bridge.set_on_quit(move || {
                    closing::request_close(quit_shell.clone(), quit_app.clone(), Some(quit_id.clone()));
                });
                let lost_shell = shell.clone();
                let portal = tool.is_portal();
                bridge.set_on_lost(move || {
                    if portal {
                        lost_shell.portal_lost();
                    } else {
                        lost_shell.tell_shell(&format!("window.__shell && window.__shell.toolLost({})", js_string(&id)));
                    }
                });
            }
            // 入口の Python はすぐ起こす(ツールはタブが開かれたときに起こす)
            shell.portal_bridge().ensure_started();

            let portal_origin = shell.catalog.portal.origin();
            let url: tauri::Url = format!("{portal_origin}/").parse()?;
            let opener = app.handle().clone();
            let for_new = shell.clone();
            let for_download = shell.clone();
            let for_nav = shell.clone();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
                .title(format!("{} VER{}", shell.catalog.name, shell.catalog.version))
                .inner_size(1440.0, 920.0)
                .min_inner_size(1024.0, 680.0)
                .center()
                // ドラッグ&ドロップ(看板の中身の入れ替え・日報の取り込み)は **Tauri で受ける**。
                // WebView2 に任せると、大きなタブの枠(iframe)の中の画面へ落としても届かなかった
                // (現場の Windows で「効かない」)。受けたものは drops.rs が画面へ渡す
                .on_navigation(move |url| {
                    if url.as_str().starts_with(&portal_origin) {
                        return true;
                    }
                    // Linux(WebKitGTK)では、大きなタブの枠(iframe)の中の移動もここへ来る
                    // (Windows の WebView2 は窓そのものの移動だけ)。各ツールの宛先は通す。
                    // Windows で窓そのものがツールの宛先へ移ると大きなタブが消えるので、通さない
                    if !cfg!(windows) && for_nav.catalog.by_url(url).is_some() {
                        return true;
                    }
                    // アプリの外のページは窓の中で開かない(既定のブラウザへ)
                    if matches!(url.scheme(), "http" | "https") && catalog::scheme_of(url).is_none() {
                        let _ = opener.opener().open_url(url.as_str(), None::<&str>);
                    }
                    false
                })
                .on_new_window(move |url, _features| {
                    services::route_new_window(&for_new, &url, None);
                    tauri::webview::NewWindowResponse::Deny
                })
                .on_download(move |webview, event| services::route_download(&for_download, webview, event))
                .on_document_title_changed(|window, title| {
                    let _ = window.set_title(&title);
                })
                .build()?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("アプリを組み立てられませんでした");

    app.run(move |_app, event| {
        if let RunEvent::Exit = event {
            // 標準入力を閉じて、全ツールの Python に終わってもらう(並べて待つ。
            // 最後の書き戻し・Excel を閉じるのを待つ上限はツールごと)。終わらなければ止める
            for_exit.shutdown_all();
        }
    });
}

/// ブラウザ版が動いているので開かない。理由を出して終わる
fn refuse_browser_running(app: &tauri::AppHandle, name: &str) {
    let handle = app.clone();
    app.dialog()
        .message(
            "ブラウザ版(Start.vbs / start.bat で開いたもの)が動いています。\n\n\
             ブラウザ版とデスクトップ版は同時には使えません。\n\
             ブラウザの画面の「終了」で閉じてから、もう一度開いてください。",
        )
        .title(name)
        .kind(MessageDialogKind::Info)
        .buttons(MessageDialogButtons::Ok)
        .show(move |_| handle.exit(0));
}

/// 窓を出す前に読めないものがあった(一式の置き場所違い)。理由だけ出して終わる
fn show_fatal_and_exit(message: &str) -> ! {
    #[cfg(windows)]
    {
        let text: Vec<u16> = message.encode_utf16().chain(std::iter::once(0)).collect();
        let title: Vec<u16> = "統合ツール".encode_utf16().chain(std::iter::once(0)).collect();
        #[link(name = "user32")]
        extern "system" {
            fn MessageBoxW(hwnd: *mut std::ffi::c_void, text: *const u16, caption: *const u16, kind: u32) -> i32;
        }
        unsafe {
            MessageBoxW(std::ptr::null_mut(), text.as_ptr(), title.as_ptr(), 0x10);
        }
    }
    eprintln!("{message}");
    std::process::exit(1);
}
