"""外枠(Rust/Tauri、`src-tauri/`)と、入口・各ツール(Python・JS)との取り決め

言語をまたぐ取り決めは、片方だけ直すと**黙って**食い違う(既定の値で動いてしまう・
頼みごとが届かない)。ここでソースを突き合わせて見つける。
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

import tests  # noqa: F401  (一時フォルダへ向ける)

from portal import app_config, catalog as catalog_mod, instance_guard

ROOT = Path(__file__).resolve().parent.parent
RUST = ROOT / "src-tauri" / "src"


def rust(name: str) -> str:
    return (RUST / name).read_text(encoding="utf-8")


class CatalogTests(unittest.TestCase):
    """`config/tools.json` は外枠と入口が同じものを読む。"""

    def setUp(self) -> None:
        self.raw = json.loads((ROOT / "config" / "tools.json").read_text(encoding="utf-8"))
        self.catalog = catalog_mod.load()

    def test_各ツールの置き場所に入口がある(self) -> None:
        for tool in self.catalog.tools:
            with self.subTest(tool=tool.id):
                for name in ("bridge.py", "start_app.py", "config/app.json", "app/static"):
                    self.assertTrue((tool.dir / name).exists(), f"{tool.dir / name}")

    def test_宛先と名前は英小文字で重ならない(self) -> None:
        entries = [self.raw["portal"], *self.raw["tools"]]
        schemes = [e.get("scheme") or e["id"] for e in entries]
        ids = [e["id"] for e in entries]
        for value in schemes + ids:
            self.assertRegex(value, r"^[a-z0-9]+$")
        self.assertEqual(len(set(schemes)), len(schemes))
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(self.raw["portal"]["id"], "portal")
        self.assertIn('pub const PORTAL: &str = "portal";', rust("catalog.rs"))

    def test_環境変数の頭はツールが読む名前と同じ(self) -> None:
        """外枠は <頭>_TOKEN を渡す。ツールの bridge.py がその名前を読んでいること。"""
        for tool in self.catalog.tools:
            with self.subTest(tool=tool.id):
                bridge = (tool.dir / "bridge.py").read_text(encoding="utf-8")
                self.assertIn(tool.token_env, bridge)
        self.assertIn('format!("{}_TOKEN", self.env_prefix)', rust("catalog.rs"))
        self.assertIn('format!("{}_PYTHON", self.env_prefix)', rust("catalog.rs"))
        self.assertIn("ALLTOOLS_TOKEN", (ROOT / "bridge.py").read_text(encoding="utf-8"))

    def test_手元の領域の名前はツールの設定と同じ(self) -> None:
        for tool in self.catalog.tools:
            with self.subTest(tool=tool.id):
                self.assertEqual(tool.local_dir_name, tool.app_config().get("local_dir_name"))


class OriginTests(unittest.TestCase):
    def test_宛先の頭の作り方はPythonとRustで同じ(self) -> None:
        catalog_rs = rust("catalog.rs")
        self.assertIn('format!("http://{scheme}.localhost")', catalog_rs)
        self.assertIn('format!("{scheme}://localhost")', catalog_rs)
        expected = "http://kanban.localhost" if sys.platform == "win32" else "kanban://localhost"
        self.assertEqual(app_config.origin_of("kanban"), expected)

    def test_ツールへ渡すHostは各ツールが受け付ける名前(self) -> None:
        relay = rust("relay.rs")
        self.assertIn('pub const TOOL_HOST: &str = "app.localhost";', relay)
        self.assertIn('pub const TOOL_ORIGIN: &str = "http://app.localhost";', relay)
        self.assertIn("app.localhost", app_config.BRIDGE_HOSTS)


class ServicesTests(unittest.TestCase):
    """ツールの画面が外枠に頼むこと(`window.__TAURI__.core.invoke`)は、外枠が全部受ける。"""

    def test_ツールの画面が頼む名前は外枠が受け持つ(self) -> None:
        services = rust("services.rs")
        handled = set(re.findall(r'^\s*"([a-z_]+)" =>', services, re.M))
        asked: dict[str, set[str]] = {}
        for js in ROOT.glob("tools/*/app/static/js/**/*.js"):
            text = js.read_text(encoding="utf-8")
            for name in re.findall(r"invoke\(\s*['\"]([a-z_]+)['\"]", text):
                asked.setdefault(name, set()).add(str(js.relative_to(ROOT)))
        self.assertTrue(asked, "ツールの desktop.js が見つからない")
        missing = {name: sorted(where) for name, where in asked.items() if name not in handled}
        self.assertEqual(missing, {}, "外枠(services.rs)が受けない頼みごと")

    def test_入口の画面が呼ぶ命令は外枠に登録されている(self) -> None:
        main = rust("main.rs")
        for js in ("shell.js", "settings.js"):
            text = (ROOT / "portal" / "static" / "js" / js).read_text(encoding="utf-8")
            for name in re.findall(r"invoke\(\s*['\"]([a-z_]+)['\"]", text):
                with self.subTest(js=js, name=name):
                    self.assertRegex(main, rf"(services|drops)::{name}\b")
        # 埋め込みの台本は、ツールの画面の頼みごとを tool_invoke / tool_invoke_raw で取り次ぐ
        embed = (ROOT / "portal" / "static" / "js" / "embed.js").read_text(encoding="utf-8")
        handled = set(re.findall(r'^\s*"([a-z_]+)" =>', rust("services.rs"), re.M))
        for name in re.findall(r"invoke\(\s*['\"]([a-z_]+)['\"]", embed):
            with self.subTest(js="embed.js", name=name):
                self.assertTrue(name in handled or re.search(rf"services::{name}\b", main), name)

    def test_埋め込みの台本の差し込み口(self) -> None:
        embed = (ROOT / "portal" / "static" / "js" / "embed.js").read_text(encoding="utf-8")
        shell_rs = rust("shell.rs")
        for placeholder in ('"__ALLTOOLS_TOOL__"', '"__ALLTOOLS_SHELL__"'):
            self.assertIn(placeholder, embed)
            self.assertIn(placeholder.replace('"', '\\"'), shell_rs)


class VersionTests(unittest.TestCase):
    def test_版は3か所で揃っている(self) -> None:
        """config/app.json・Cargo.toml・tauri.conf.json。食い違うと exe の版がずれる。"""
        app = json.loads((ROOT / "config" / "app.json").read_text(encoding="utf-8"))["version"]
        cargo = (ROOT / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
        conf = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
        self.assertEqual(re.search(r'^version = "([^"]+)"', cargo, re.M).group(1), app)
        self.assertEqual(conf["version"], app)

    def test_exeの名前(self) -> None:
        cargo = (ROOT / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
        self.assertRegex(cargo, r'(?m)^name = "AllTools"$')
        self.assertIn("AllTools.exe", instance_guard.DESKTOP_EXES)
        self.assertIn("統合ツール.exe", instance_guard.DESKTOP_EXES)


class InstanceNameTests(unittest.TestCase):
    """ブラウザ版(Python)とデスクトップ版(Rust)が同じ名前の錠を見る。"""

    def test_錠の名前の作り方はPythonとRustで揃っている(self) -> None:
        source = rust("instance.rs")
        for suffix in ("instance", "desktop", "browser"):
            self.assertIn(f'format!("{{base}}.{suffix}")', source)
            self.assertTrue(instance_guard.names()[suffix if suffix == "instance" else suffix]
                            .endswith(f".{suffix}"))
        self.assertIn('format!("Local\\\\{name}")', source)
        self.assertIn('"._-"', source)
        self.assertEqual(instance_guard.base_name(), "nlm.all-tools")
        self.assertIn('"nlm.all-tools"', source)



class DialogTests(unittest.TestCase):
    """画面の confirm は**ブラウザのもの(答えを待つ)**のまま。

    tauri-plugin-dialog は window.confirm を答えを待たない版(Promise を返す)に置き換える。
    `if (!confirm("消しますか?")) return;` が訊かずに進む(Windows では各ツールの画面にも入る)。
    外枠の native_dialogs.js が dialog より先に走って、ブラウザのものを固定する。
    """

    def test_ブラウザの確認ダイアログを_dialogより先に固定する(self) -> None:
        main = rust("main.rs")
        guard = main.index('include_str!("native_dialogs.js")')
        self.assertIn("js_init_script_on_all_frames", main[main.rindex(".plugin(", 0, guard):guard + 40])
        self.assertLess(guard, main.index("tauri_plugin_dialog::init()"))
        script = (ROOT / "src-tauri" / "src" / "native_dialogs.js").read_text(encoding="utf-8")
        for name in ("alert", "confirm", "prompt"):
            self.assertIn(f'"{name}"', script)
        self.assertIn("writable: false", script)

    def test_起動確認がどの画面の確認ダイアログも見る(self) -> None:
        self.assertIn("確認ダイアログ", (ROOT / "portal" / "static" / "js" / "embed.js").read_text(encoding="utf-8"))
        self.assertIn("dialogsKind()", (ROOT / "portal" / "static" / "js" / "shell.js").read_text(encoding="utf-8"))
        self.assertIn("確認ダイアログ", (ROOT / "scripts" / "desktop_smoke.py").read_text(encoding="utf-8"))



class ConsoleEncodingTests(unittest.TestCase):
    """Windows のコマンドの日本語の出力(Shift-JIS)で、ツールが起動できなくならない。

    tasklist はそのプロセスが無いとき「情報: 指定された条件に一致するタスクは…」と返す。
    外枠が Python を UTF-8 モード(`-X utf8`)で起こしていたため、これを UTF-8 で読もうとして
    落ち、日報が起動できなかった(前の起動の印が残っていたとき)。
    """

    FILES = ("portal/browser_tools.py", "tools/nippou/launch_guard.py", "tools/kanban/launch_guard.py",
             "tools/calendar/launch_guard.py", "tools/inspection/launch_guard.py")

    def test_ツールのPythonをUTF8モードで起こさない(self) -> None:
        bridge = rust("bridge.rs")
        self.assertNotIn('.arg("utf8")', bridge)
        self.assertIn('.env_remove("PYTHONUTF8")', bridge)

    def test_tasklistとwmicはコンソールの文字コードで_読めない字は置き換えて読む(self) -> None:
        for name in self.FILES:
            source = (ROOT / name).read_text(encoding="utf-8")
            calls = re.findall(r"subprocess\.run\((?:[^()]|\([^()]*\))*\)", source, re.S)
            calls = [c for c in calls if "tasklist" in c or "wmic" in c]
            with self.subTest(file=name):
                self.assertTrue(calls)
                for call in calls:
                    self.assertIn("encoding=CONSOLE_ENCODING", call)
                    self.assertIn('errors="replace"', call)
                self.assertIn('CONSOLE_ENCODING = "oem" if os.name == "nt" else None', source)
                self.assertNotRegex(source, r"\bout\.stdout\b(?! or)")



class UploadTests(unittest.TestCase):
    """ファイルを送る(FormData)は、embed.js が中身を読んでから1つのバイト列にして送る。

    WebView2 は外枠の宛先へ送る FormData のファイル(ディスクのファイルを指す部分)を
    外枠へ渡さず、看板の「Access の最新で表の中身を入れ替える」が「ファイルが小さすぎます」
    になっていた(ブラウザ版では起きない)。
    """

    def test_ファイル入りのFormDataを組み立て直して送る(self) -> None:
        embed = (ROOT / "portal" / "static" / "js" / "embed.js").read_text(encoding="utf-8")
        self.assertIn("window.fetch = function", embed)
        self.assertIn("arrayBuffer()", embed)
        self.assertIn("multipart/form-data; boundary=", embed)


class DropTests(unittest.TestCase):
    """ファイルのドラッグ&ドロップは外枠が OS の仕組みで受け、落ちた場所のツールの画面へ渡す。

    WebView2 に任せると、大きなタブの枠(iframe)の中の画面へ落としても届かなかった
    (現場の Windows で「ドラッグ&ドロップが効かない」)。
    """

    def test_大きなタブの窓は落下をTauriで受ける(self) -> None:
        main = (ROOT / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
        window = main[main.index('WebviewWindowBuilder::new(app, "main"'):]
        window = window[:window.index(".build()?")]
        self.assertNotIn(".disable_drag_drop_handler()", window, "大きなタブの窓で落下を受けていない")
        self.assertIn("WindowEvent::DragDrop", main)
        self.assertIn("drops::shell_dropped_file", main)

    def test_画面へ渡すのは位置と名前だけで中身は落とされた枠の頼みにだけ(self) -> None:
        drops = (ROOT / "src-tauri" / "src" / "drops.rs").read_text(encoding="utf-8")
        self.assertIn("window.__shell.fileDrag", drops)
        shell = (ROOT / "portal" / "static" / "js" / "shell.js").read_text(encoding="utf-8")
        self.assertIn("fileDrag(info)", shell)
        self.assertIn("drag.drop.id !== id || drag.drop.seq !== data.seq", shell)
        self.assertIn("devicePixelRatio", shell)
        embed = (ROOT / "portal" / "static" / "js" / "embed.js").read_text(encoding="utf-8")
        for needle in ('d.type === "alltools:drag"', 'fire("drop"', "over.defaultPrevented",
                       "alltools:dropped-file", "alltools:drop-refused"):
            self.assertIn(needle, embed)


class ThemeDefaultTests(unittest.TestCase):
    """選んでいなければ、どのツールもライト(以前はツールごとに 自動 / ダーク とバラバラだった)。"""

    def text(self, *parts: str) -> str:
        return ROOT.joinpath(*parts).read_text(encoding="utf-8")

    def test_入口と説明書はライト(self) -> None:
        self.assertIn('data-theme="light"', self.text("portal", "templates", "shell.html").split("<head>")[0])
        for page in (ROOT / "portal" / "static" / "manual").glob("*.html"):
            with self.subTest(page=page.name):
                self.assertIn('<html lang="ja" data-theme="light">', page.read_text(encoding="utf-8"))

    def test_日報はライト(self) -> None:
        base = self.text("tools", "nippou", "app", "templates", "base.html")
        self.assertIn('<html lang="ja" data-skin="neon" data-theme="light">', base)
        self.assertIn('return saved() || DEFAULT;', self.text("tools", "nippou", "app", "static", "js", "theme.js"))
        self.assertIn("const DEFAULT = 'light'", self.text("tools", "nippou", "app", "static", "vc", "coil", "coil-theme.js"))

    def test_看板はライト(self) -> None:
        self.assertIn("localStorage.getItem('kanban.theme') || 'light'",
                      self.text("tools", "kanban", "app", "templates", "base.html"))
        self.assertIn("const DEFAULT = 'light';", self.text("tools", "kanban", "app", "static", "js", "theme.js"))

    def test_カレンダーと点検表はライト(self) -> None:
        self.assertIn("value = get(KEY_THEME, THEME_LIGHT)",
                      self.text("tools", "calendar", "calendar_app", "settings.py"))
        self.assertIn("return False if value is None else bool(value)",
                      self.text("tools", "inspection", "app", "services", "settings_service.py"))


class LocationViewTests(unittest.TestCase):
    """大設定の「共有の DB の置き場所」: 反映しているか、打ちかけかを見分けられる。"""

    def test_反映済みと打ちかけで見え方が変わる(self) -> None:
        js = (ROOT / "portal" / "static" / "js" / "settings.js").read_text(encoding="utf-8")
        css = (ROOT / "portal" / "static" / "css" / "shell.css").read_text(encoding="utf-8")
        self.assertIn("function paintLocState", js)
        self.assertIn("まだ反映していません", js)
        self.assertIn("反映しています", js)
        self.assertIn(".pathform input.is-edited", css)
        self.assertIn('id="loc-state"', (ROOT / "portal" / "templates" / "settings.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


class ManualAndEndTests(unittest.TestCase):
    """説明書の Esc・F1 と、終了したあとの問い合わせ(通しの点検で見つかったもの)。"""

    def setUp(self) -> None:
        self.shell = (ROOT / "portal" / "static" / "js" / "shell.js").read_text(encoding="utf-8")

    def test_説明書の中でも_Esc_F1_で閉じる(self) -> None:
        """説明書をクリックするとキーは説明書のページへ行き、外枠に届かなかった。"""
        self.assertIn('manualFrame.addEventListener("load"', self.shell)
        self.assertIn("doc.addEventListener(\"keydown\"", self.shell)

    def test_説明書を開いたままタブを替えてもツールへ入らない(self) -> None:
        self.assertIn("if (!manualBox.hidden) return;", self.shell)

    def test_終了したら定期の問い合わせを止める(self) -> None:
        self.assertNotIn("setInterval(followTabs", self.shell)
        self.assertNotIn("setInterval(pollStatus", self.shell)
        self.assertNotIn("setInterval(beat", self.shell)
        ended = self.shell[self.shell.index("function ended()"):]
        self.assertIn("timers.forEach(clearInterval)", ended[:200])


class PrepareCloseTests(unittest.TestCase):
    """窓の × ・「終了」の前に、各ツールの画面へ打ちかけを置いてもらう。"""

    def test_外枠は訊く前に画面へ頼んで返事を待つ(self) -> None:
        closing = rust("closing.rs")
        run = closing[closing.index("fn run("):]
        self.assertLess(run.index("prepare_screens"), run.index("ask_all"))
        shell_rs = rust("shell.rs")
        self.assertIn("window.__shell.prepareClose()", shell_rs)
        self.assertIn("wait_prepared(&rx, limit, PREPARE_WAITING_LIMIT)", shell_rs)
        self.assertIn("services::shell_prepared", rust("main.rs"))

    def test_大きなタブの画面が各枠へ頼む(self) -> None:
        shell = (ROOT / "portal" / "static" / "js" / "shell.js").read_text(encoding="utf-8")
        self.assertIn("async prepareClose()", shell)
        self.assertIn('invoke("shell_prepared", { ok })', shell)
        self.assertIn('type: "alltools:before-close"', shell)
        # ブラウザ版の「終了」も同じ頼みを通る
        quit_ = shell[shell.index('getElementById("quit")'):]
        self.assertLess(quit_.index("prepareFrames()"), quit_.index('"/api/shutdown"'))
        self.assertIn('"/api/shutdown", { screens_ready: true }', quit_, "置いたあとは入口に頼み直させない")

    def test_外からの停止もブラウザ版の画面に頼む(self) -> None:
        """ランチャー・stop.bat の停止でも、開いている画面に打ちかけを置いてもらう(docs/ランチャー連携.md)。"""
        shell = (ROOT / "portal" / "static" / "js" / "shell.js").read_text(encoding="utf-8")
        watch = shell[shell.index("async function watchCloseAsk()"):]
        watch = watch[:watch.index("\n}\n")]
        self.assertIn("/api/close-ask?page=", watch)
        # 確認の窓は画面を止めるので、先に「置いています」を返してから置く
        self.assertLess(watch.index('state: "working"'), watch.index("prepareFrames()"))
        self.assertIn('state: ok ? "ok" : "refused"', watch)
        self.assertIn("every(watchCloseAsk, 1000)", shell)
        # 閉じた画面には頼まない(閉じ際の合図)
        self.assertIn("leaving: true", shell)
        self.assertIn('window.addEventListener("pagehide"', shell)


def js(name: str) -> str:
    return (ROOT / "portal" / "static" / "js" / name).read_text(encoding="utf-8")


def between(text: str, start: str, end: str) -> str:
    at = text.index(start)
    return text[at:text.index(end, at + len(start))]


class CloseWaitTests(unittest.TestCase):
    """「受けた」のあとは短い上限で見切らない。

        打った行が消えることがありました

    日報は打ちかけを置けなかったとき「続けますか?」と訊く。本人が読んでいるあいだに、
    画面は 12 秒・外枠は 15 秒で見切って閉じていた。
    """

    def test_画面は受けたあと短い上限で閉じない(self) -> None:
        shell = js("shell.js")
        ask = between(shell, "function askFrameToSave(", "\n}\n")
        self.assertNotIn("DONE_MS", shell)
        after_ack = ask[ask.index('"alltools:before-close-ack"'):]
        self.assertIn("ACKED_MS", after_ack)
        self.assertIn("finish(false)", after_ack, "受けたあと上限が来たら閉じない")
        self.assertRegex(shell, r"const ACKED_MS = 10 \* 60000;")

    def test_画面は先に待っていますを外枠へ返す(self) -> None:
        shell = js("shell.js")
        prep = between(shell, "async prepareClose()", "\n  },")
        self.assertLess(prep.index('invoke("shell_prepared", { ok: true, waiting: true })'),
                        prep.index("prepareFrames()"))
        services = rust("services.rs")
        self.assertIn("waiting: Option<bool>", services)
        self.assertIn("Prepared::Waiting", services)

    def test_外枠は待っていますのあと上限で閉じない(self) -> None:
        shell_rs = rust("shell.rs")
        wait = between(shell_rs, "pub fn wait_prepared(", "\n}\n")
        self.assertIn("return !acked", wait)
        self.assertNotIn("unwrap_or(true)", shell_rs)
        self.assertIn("Duration::from_secs(30 * 60)", shell_rs)

    def test_確認のあとでもう一度打ちかけを置いてから止める(self) -> None:
        closing = rust("closing.rs")
        run = closing[closing.index("fn run("):]
        first = run.index("confirm_busy(shell, app, &head, &busy)")
        again = run.index("shell.prepare_screens(PREPARE_LIMIT)", first)
        self.assertLess(again, run.index('"/api/shutdown", &json!({"force": force})'))
        self.assertIn("dialog.parent(&window)", closing)

    def test_やめたときに読み直すのは止まったと確かめたツールだけ(self) -> None:
        closing = rust("closing.rs")
        self.assertIn("let stopped = stopped_ids(", closing)
        self.assertNotIn("if !matches!(reply, Ok((409, _)))", closing)


class BrowserOriginTests(unittest.TestCase):
    """ブラウザ版の「終了」で、各ツールに打ちかけを置く頼みが届く。

    入口はブラウザ版のツールに宛先を渡さない(`origin: ""`)。以前は宛先が空のままで、
    頼みを1つも送らず、返事も捨てていた。
    """

    def test_枠の宛先を覚えてどこでも使う(self) -> None:
        shell = js("shell.js")
        origin = between(shell, "function originOf(", "\n}\n")
        self.assertIn("entry.origin", origin)
        self.assertIn("new URL(url, location.href).origin", shell)
        self.assertIn("entry.origin = frameOrigin(id, entry.url);", shell)
        self.assertIn("event.origin !== originOf(id)", shell)
        self.assertIn("postMessage(message, originOf(id))", shell)
        self.assertIn('postMessage({ type: "alltools:before-close", seq }, originOf(id))', shell)


class LostToolTests(unittest.TestCase):
    """処理が止まった・終わったツールの画面を、黙って読み直さない・消さない。"""

    def test_ツールが落ちても枠は読み直さず知らせを重ねる(self) -> None:
        shell = js("shell.js")
        lost = between(shell, "  toolLost(id) {", "\n  },")
        self.assertNotIn("reloadTool(id)", lost)
        self.assertIn('postToFrame(id, { type: "alltools:python-lost" })', lost)
        self.assertIn("showLost(", lost)
        self.assertIn('invoke("shell_restart_tool", { tool: id })', lost)
        self.assertIn("confirmDiscard(id)", lost)
        self.assertIn('"alltools:python-lost"', js("embed.js"))

    def test_入口が落ちても窓ごと読み直さない(self) -> None:
        main = rust("main.rs")
        self.assertNotIn('tell_shell("location.reload()")', main)
        self.assertIn("lost_shell.portal_lost()", main)
        shell_rs = rust("shell.rs")
        self.assertIn("pub fn portal_lost(self: &Arc<Self>)", shell_rs)
        restart = between(shell_rs, "pub fn restart_portal(", "\n    }\n")
        self.assertIn("finish_restart()", restart)
        self.assertNotIn("location.reload", restart)
        self.assertIn("if self.exiting.load(Ordering::SeqCst)", shell_rs, "終えるときに起こし直さない")
        shell = js("shell.js")
        for name in ("portalLost()", "portalBack()", "portalDown(text)"):
            self.assertIn(name, shell)
        self.assertIn('id="portal-state"', (ROOT / "portal" / "templates" / "settings.html").read_text(encoding="utf-8"))
        self.assertIn("Some(t) if t.is_portal() =>", rust("services.rs"))

    def test_ブラウザ版は印が消えるまで終わったとしない_枠は消さない(self) -> None:
        shell = js("shell.js")
        watch = between(shell, "async function watchBrowserTools()", "\n}\n")
        self.assertIn("item.ended === false", watch)
        self.assertNotIn("showNote(", watch)
        self.assertIn("showLost(", watch)
        self.assertNotIn(".remove()", watch)


class ReloadTests(unittest.TestCase):
    """F5 / Ctrl+R は、打ちかけを置いてもらってから、その画面だけ読み直す。"""

    def test_帯のF5は打ちかけを置いてから(self) -> None:
        shell = js("shell.js")
        keys = between(shell, 'document.addEventListener("keydown"', "\n});\n")
        self.assertIn("userReload(current)", keys)
        self.assertNotIn("reloadTool(current)", keys)
        reload_ = between(shell, "async function userReload(", "\n}\n")
        self.assertLess(reload_.index("askFrameToSave(id, win)"), reload_.index("reloadTool(id)"))
        self.assertIn('data.action === "reload"', shell)

    def test_ツールの画面の中のF5は大きなタブに頼む(self) -> None:
        embed = js("embed.js")
        keys = between(embed, "  // ---- キー(このツールの画面にいるとき) ----", "\n  });\n")
        self.assertNotIn("location.reload()", keys)
        self.assertIn("askReload()", keys)
        ask = between(embed, "function askReload()", "\n  }\n")
        self.assertIn('tellShell({ action: "reload" })', ask)
        self.assertIn("location.reload()", ask, "大きなタブの外(単体)では今までどおり")


class FallbackCloseTests(unittest.TestCase):
    """自分で「閉じる前」を受けないツールの画面は、embed.js が受け皿になる。"""

    def test_受けるツールには任せ_受けないツールでは打ちかけを訊く(self) -> None:
        embed = js("embed.js")
        before = between(embed, "function beforeClose(d)", "\n  }\n")
        self.assertLess(before.index("window.__alltoolsHandlesClose"), before.index('"alltools:before-close-ack"'))
        self.assertIn("保存していない入力があります。閉じますか?", before)
        self.assertIn('reply("alltools:before-close-done", { ok: ok })', before)
        self.assertIn("el.defaultValue", embed)
        self.assertIn("dialog[open]", embed)


class SettingsFormTests(unittest.TestCase):
    """大設定の打ちかけ(置き場所・行の窓)を消さない・2重に書かない。"""

    def setUp(self) -> None:
        self.js = js("settings.js")

    def test_置き場所の打ちかけは描き直しで消さない(self) -> None:
        source = between(self.js, "function paintSource(", "\n}\n")
        self.assertIn("if (reset || !typed) input.value = saved;", source)
        self.assertIn("s.set_folder ?? s.folder", source)
        self.assertIn("{ location: true }", self.js)

    def test_反映していない置き場所があれば配布設定を書き出さない(self) -> None:
        export = between(self.js, '$("dist-export").addEventListener', "\n  });\n")
        self.assertLess(export.index("locEdited()"), export.index('write("/api/distribution/export"'))

    def test_保存するの2度押しで2行足さない(self) -> None:
        save = between(self.js, "async function saveRow(", "\n}\n")
        self.assertLess(save.index("if (rowBusy) return;"), save.index("await write("))
        self.assertIn('$("row-save").disabled = true;', save)

    def test_断られても打った値を残し_行のいまに差し替える(self) -> None:
        refused = between(self.js, "function rowRefused(", "\n}\n")
        self.assertIn("editing = fresh;", refused)
        self.assertIn('$("row-unlock").hidden = false;', refused)
        self.assertNotIn('$("row-dialog").close()', refused)

    def test_古い答えで描き直さない(self) -> None:
        self.assertIn("if (seq < shown)", self.js)

    def test_やめる_Escは打った値があれば訊く(self) -> None:
        close = between(self.js, "function closeRow()", "\n}\n")
        self.assertIn("rowEdited()", close)
        self.assertIn('$("row-dialog").addEventListener("cancel"', self.js)

    def test_閉じる前に大設定の打ちかけも見る(self) -> None:
        shell = js("shell.js")
        prepare = between(shell, "async function prepareFrames()", "\n}\n")
        self.assertLess(prepare.index("settingsView.unsaved()"), prepare.index("askFrameToSave("))
        self.assertIn("export function unsaved()", self.js)
