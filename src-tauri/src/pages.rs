//! 外枠が自分で出す画面。**そのツールの Python がまだ居ない / 居なくなった**ときだけ使う。
//! ふだんの画面(各ツールの待機画面も含む)は Python が返す。
//!
//! どれもツールのタブ(iframe)の中に出る。「もう一度開く」は、そのツールの Python
//! だけを起こし直す(`/__alltools__/restart`。ほかのタブには触らない)。

use crate::bridge::Failure;

/// 外枠が受け持つ経路の頭(ツールの経路と重ならない名前)
pub const SHELL_PREFIX: &str = "/__alltools__/";
pub const RESTART_PATH: &str = "/__alltools__/restart";

pub fn escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

const STYLE: &str = r#"
 :root{--bg:#f3f6f8;--fg:#17232d;--box:#fff;--line:#dde5ea;--bad:#982326;--bad-bg:#fdf0f0;--muted:#5b6d79;
       --accent:#0b6d86;--code:rgba(0,0,0,.05);color-scheme:light}
 @media (prefers-color-scheme: dark){:root{--bg:#101820;--fg:#e2eaef;--box:#182129;--line:#28343d;--bad:#e49093;
       --bad-bg:#331e21;--muted:#95a7b2;--accent:#5cc2d4;--code:rgba(255,255,255,.07);color-scheme:dark}}
 body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
      font-family:system-ui,"Segoe UI","Yu Gothic UI","Meiryo UI",sans-serif;line-height:1.7}
 .box{width:min(680px,calc(100vw - 48px));background:var(--box);border:1px solid var(--line);border-radius:10px;
      padding:28px 32px;box-shadow:0 5px 18px rgb(21 56 77/5%)}
 h1{margin:0 0 12px;font-size:19px}
 h1.ng{color:var(--bad)}
 .hint{margin-top:16px;padding:14px;background:var(--bad-bg);border-left:4px solid var(--bad);white-space:pre-wrap}
 dt{color:var(--muted);font-size:13px;margin-top:14px}
 code,pre{font-family:ui-monospace,Consolas,monospace;font-size:12px;background:var(--code);
          padding:2px 5px;border-radius:2px;word-break:break-all}
 pre{padding:10px;max-height:16em;overflow:auto;white-space:pre-wrap}
 .act{display:inline-block;margin-top:18px;padding:9px 20px;border-radius:10px;background:var(--accent);
      color:#fff;font-weight:700;text-decoration:none}
 .spin{width:26px;height:26px;border-radius:50%;border:3px solid var(--line);border-top-color:var(--accent);
       animation:r 1s linear infinite;display:inline-block;vertical-align:middle;margin-right:10px}
 @keyframes r{to{transform:rotate(1turn)}}
 @media (prefers-reduced-motion: reduce){.spin{animation:none}}
"#;

fn page(title: &str, head_extra: &str, body: &str) -> String {
    format!(
        r#"<!doctype html><html lang="ja"><head><meta charset="utf-8">{head_extra}<title>{title}</title>
<style>{STYLE}</style></head><body><main class="box">{body}</main></body></html>"#,
        title = escape(title)
    )
}

/// Python が「受け付け始めた」と言う前の、ほんの一瞬に出す画面。
/// 1秒ごとに読み直し、そのツールの待機画面(段と進み具合が出る)へ移る。
pub fn starting(name: &str) -> String {
    page(
        name,
        r#"<meta http-equiv="refresh" content="1">"#,
        &format!(
            r#"<h1><span class="spin" aria-hidden="true"></span>{}を起動しています…</h1><p>しばらくお待ちください。</p>"#,
            escape(name)
        ),
    )
}

/// 移り先へ移るだけのページ(画面の 302 の代わり)。
/// 移り先はアプリの中の経路だけにする(外へは移らない)。
pub fn moving_to(target: &str) -> String {
    let inside = target.starts_with('/') && !target.starts_with("//");
    let target = if inside { target } else { "/" };
    // `</script>` で抜け出させない(JSON は `<` をそのまま書くので、自分で崩す)
    let quoted = serde_json::to_string(target)
        .unwrap_or_else(|_| "\"/\"".into())
        .replace('<', "\\u003c");
    format!(
        r#"<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="0;url={url}"><title>移動しています</title></head>
<body><script>location.replace({quoted});</script></body></html>"#,
        url = escape(target)
    )
}

/// 起動できなかった / 途中で止まった。**次に何をすればよいか**まで出す。
pub fn failure(name: &str, failure: Option<&Failure>, python: &str, stderr: &[String], log_hint: &str) -> String {
    let (message, hint, log_dir) = match failure {
        Some(f) => (f.message.clone(), f.hint.clone(), f.log_dir.clone()),
        None => (
            "Python の処理が止まりました".to_string(),
            "「もう一度開く」を押してください。続けて起きるときは、下の「最後の出力」と\
             ログを担当に送ってください。"
                .to_string(),
            String::new(),
        ),
    };
    let log_dir = if log_dir.is_empty() { log_hint.to_string() } else { log_dir };
    let tail = stderr.iter().rev().take(25).rev().cloned().collect::<Vec<_>>().join("\n");
    page(
        &format!("{name} — 起動できませんでした"),
        "",
        &format!(
            r#"<h1 class="ng">{name}を開けませんでした</h1>
<p>{message}</p>
{hint}
<dt>ログの場所</dt><p><code>{log_dir}</code></p>
<dt>使った Python</dt><p><code>{python}</code></p>
{tail}
<p>ほかのタブはそのまま使えます。</p>
<a class="act" href="{RESTART_PATH}">もう一度開く</a>"#,
            name = escape(name),
            message = escape(&message),
            hint = if hint.is_empty() { String::new() } else { format!(r#"<div class="hint">{}</div>"#, escape(&hint)) },
            log_dir = escape(if log_dir.is_empty() { "(分かりません)" } else { &log_dir }),
            python = escape(if python.is_empty() { "(見つかっていません)" } else { python }),
            tail = if tail.is_empty() {
                String::new()
            } else {
                format!("<dt>最後の出力</dt><pre>{}</pre>", escape(&tail))
            },
        ),
    )
}

/// そのツールの「終了」で終わった(ほかのタブは動いている)。
pub fn ended(name: &str) -> String {
    page(
        name,
        "",
        &format!(
            r#"<h1>{name}は終了しました</h1>
<p>このタブのツールだけが終わっています。ほかのタブはそのまま使えます。</p>
<a class="act" href="{RESTART_PATH}">もう一度開く</a>"#,
            name = escape(name)
        ),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 理由と次の一手と出力が出て_タグは無害化される() {
        let f = Failure { message: "<b>だめ</b>".into(), hint: "pip で入れる".into(), log_dir: "C:\\logs".into() };
        let html = failure("看板", Some(&f), "python.exe", &["Traceback <x>".into()], "");
        assert!(html.contains("&lt;b&gt;だめ&lt;/b&gt;"));
        assert!(html.contains("pip で入れる"));
        assert!(html.contains("C:\\logs"));
        assert!(html.contains("Traceback &lt;x&gt;"));
        assert!(html.contains("python.exe"));
        assert!(html.contains(RESTART_PATH), "そのツールだけ起こし直せる");
    }

    #[test]
    fn 移り先はアプリの中だけ() {
        assert!(moving_to("/lot?x=1").contains(r#"location.replace("/lot?x=1")"#));
        assert!(moving_to("https://evil.example/").contains(r#"location.replace("/")"#));
        assert!(moving_to("//evil.example/").contains(r#"location.replace("/")"#));
        let tricky = moving_to("/a\"</script><script>alert(1)</script>");
        assert_eq!(tricky.matches("</script>").count(), 1, "{tricky}");
    }

    #[test]
    fn 理由が無ければ止まったと言う() {
        let html = failure("日報", None, "", &[], "D:\\ログ");
        assert!(html.contains("止まりました"));
        assert!(html.contains("D:\\ログ"));
        assert!(html.contains("見つかっていません"));
        assert!(starting("日報").contains("日報を起動しています"));
        assert!(ended("点検表").contains("点検表は終了しました"));
    }
}
