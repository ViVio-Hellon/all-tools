//! Python(`bridge.py`)を子として起動し、標準入出力で要求を渡す。
//!
//! やりとりの形は `bridge.py` の説明と同じ:
//!
//! ```text
//! 要求  {"id": 7, "method": "POST", "path": "/api/x", "query": "", "headers": [[k, v]], "len": 12}\n<本文>
//! 応答  {"id": 7, "status": 200, "headers": [[k, v]], "len": 345}\n<本文>
//! 知らせ {"event": "started" | "quit" | "fatal", ...}\n
//! ```
//!
//! **ポートは使わない。** 画面(WebView)からの要求は、Tauri の独自の宛先
//! (`app://`)で受けて、ここから Python へ渡す。

use std::collections::{HashMap, VecDeque};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

/// 標準エラーの末尾を何行覚えておくか(起動に失敗したとき画面に出す)
const STDERR_LINES: usize = 60;

/// 1件の応答を待つ上限。取り込みなどの長い処理は背景で走るので、
/// 1件がこれほど掛かることは無い。止まったまま待たせきりにしないための上限
const REPLY_TIMEOUT: Duration = Duration::from_secs(600);

/// 終わるとき・立て直すとき、Python が自分で終わるのを待つ上限。
/// Python は終わる前に**最後の書き戻し**をする(最大 20 秒)。それより長く待つ
pub const EXIT_GRACE: Duration = Duration::from_secs(30);

pub struct Reply {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

/// 起動できなかった理由。画面にそのまま出す
#[derive(Clone, Debug)]
pub struct Failure {
    pub message: String,
    pub hint: String,
    pub log_dir: String,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Phase {
    /// まだ Python を起動していない / 起動待ち
    Starting,
    /// 要求を受け付けている
    Started,
    /// 終わった(終了してよいと言われた・落ちた)
    Ended,
}

struct State {
    phase: Phase,
    failure: Option<Failure>,
    /// どの Python で動いているか(画面の診断用)
    python: String,
    /// 何代目の Python か。**立て直したら増える。** 前の代の知らせ(終わった・
    /// 終了してよい)を今の代のものと取り違えないために使う
    generation: u64,
}

type Pending = Arc<Mutex<HashMap<u64, mpsc::Sender<Reply>>>>;

pub struct Bridge {
    root: PathBuf,
    token: String,
    state: Mutex<State>,
    changed: Condvar,
    stdin: Mutex<Option<ChildStdin>>,
    child: Mutex<Option<Child>>,
    pending: Pending,
    next_id: AtomicU64,
    stderr: Arc<Mutex<VecDeque<String>>>,
    on_quit: Mutex<Option<Box<dyn Fn() + Send + Sync>>>,
    on_lost: Mutex<Option<Box<dyn Fn() + Send + Sync>>>,
}

impl Bridge {
    pub fn new(root: PathBuf, token: String) -> Arc<Self> {
        Arc::new(Self {
            root,
            token,
            state: Mutex::new(State {
                phase: Phase::Starting,
                failure: None,
                python: String::new(),
                generation: 0,
            }),
            changed: Condvar::new(),
            stdin: Mutex::new(None),
            child: Mutex::new(None),
            pending: Arc::new(Mutex::new(HashMap::new())),
            next_id: AtomicU64::new(1),
            stderr: Arc::new(Mutex::new(VecDeque::new())),
            on_quit: Mutex::new(None),
            on_lost: Mutex::new(None),
        })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    /// 「終了してよい」と言われたときに呼ぶもの(窓を閉じてアプリを終える)
    pub fn set_on_quit(&self, f: impl Fn() + Send + Sync + 'static) {
        *self.on_quit.lock().unwrap() = Some(Box::new(f));
    }

    /// 受け付けていた Python が、言われもせずに居なくなったときに呼ぶもの
    /// (窓を読み直して、理由の画面を出す)
    pub fn set_on_lost(&self, f: impl Fn() + Send + Sync + 'static) {
        *self.on_lost.lock().unwrap() = Some(Box::new(f));
    }

    pub fn phase(&self) -> Phase {
        self.state.lock().unwrap().phase.clone()
    }

    pub fn failure(&self) -> Option<Failure> {
        self.state.lock().unwrap().failure.clone()
    }

    pub fn python(&self) -> String {
        self.state.lock().unwrap().python.clone()
    }

    pub fn stderr_tail(&self) -> Vec<String> {
        self.stderr.lock().unwrap().iter().cloned().collect()
    }

    /// 受け付けが始まるか、終わるまで待つ。始まっていれば `true`
    pub fn wait_started(&self, limit: Duration) -> bool {
        let deadline = Instant::now() + limit;
        let mut state = self.state.lock().unwrap();
        while state.phase == Phase::Starting {
            let now = Instant::now();
            if now >= deadline {
                return false;
            }
            state = self.changed.wait_timeout(state, deadline - now).unwrap().0;
        }
        state.phase == Phase::Started
    }

    fn set_phase(&self, phase: Phase, failure: Option<Failure>) {
        let mut state = self.state.lock().unwrap();
        // 一度失敗の理由が入ったら、あとの「終わった」で消さない
        if failure.is_some() || state.failure.is_none() {
            if failure.is_some() {
                state.failure = failure;
            }
        }
        state.phase = phase;
        self.changed.notify_all();
    }

    /// Python を探して起動する。**見つかるまで候補を順に試す。**
    ///
    /// Windows には「Python が入っていないのに `python.exe` がある」状態がある
    /// (Microsoft Store へ案内するだけの代役)。それは何も話さずにすぐ終わるので、
    /// 1行も返さずに終わった候補は「使えない」として次へ進む。
    pub fn start(self: &Arc<Self>) {
        let script = self.root.join("bridge.py");
        if !script.is_file() {
            self.set_phase(
                Phase::Ended,
                Some(Failure {
                    message: format!("bridge.py が見つかりません: {}", script.display()),
                    hint: "KanbanSystem.exe は、アプリ一式が入ったフォルダ(bridge.py がある所)に置いてください\
                           (ショートカットを作るのは大丈夫です)。"
                        .into(),
                    log_dir: String::new(),
                }),
            );
            return;
        }
        let mut tried = Vec::new();
        for (program, args) in python_candidates() {
            tried.push(program.clone());
            match self.spawn(&program, &args, &script) {
                Ok(()) => {}
                Err(_) => continue, // 見つからない
            }
            // 1行目(started / fatal)か、何も言わずに終わるのを待つ
            let mut state = self.state.lock().unwrap();
            while state.phase == Phase::Starting {
                state = self.changed.wait(state).unwrap();
            }
            let spoke = state.phase == Phase::Started || state.failure.is_some();
            drop(state);
            if spoke {
                return;
            }
            // 何も言わずに終わった。次の候補へ
            let mut state = self.state.lock().unwrap();
            state.phase = Phase::Starting;
        }
        self.set_phase(
            Phase::Ended,
            Some(Failure {
                message: "Python が見つかりません(または起動できません)".into(),
                hint: format!(
                    "https://www.python.org/downloads/ から Python 3.9 以上を入れてください。\
                     インストーラの最初の画面で「Add python.exe to PATH」に必ずチェックを入れます。\n\
                     入れたあと、コマンドプロンプトでこのフォルダへ移り、次を1度だけ実行してください:\n\
                     python -m pip install -r requirements.txt\n\n試したもの: {}",
                    tried.join(" / ")
                ),
                log_dir: String::new(),
            }),
        );
    }

    fn spawn(self: &Arc<Self>, program: &str, args: &[String], script: &Path) -> std::io::Result<()> {
        let mut command = Command::new(program);
        command
            .args(args)
            .arg("-X")
            .arg("utf8")
            .arg(script)
            .current_dir(&self.root)
            .env("KANBAN_TOKEN", &self.token)
            .env("PYTHONIOENCODING", "utf-8")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            // コンソールの窓を出さない
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NO_WINDOW);
        }
        let mut child = command.spawn()?;
        let stdout = child.stdout.take().expect("stdout");
        let stderr = child.stderr.take().expect("stderr");
        *self.stdin.lock().unwrap() = child.stdin.take();
        *self.child.lock().unwrap() = Some(child);
        self.state.lock().unwrap().python = program.to_string();

        // 標準エラー: 末尾だけ覚える(Python のログもここに出る)
        let tail = self.stderr.clone();
        thread::Builder::new()
            .name("python-stderr".into())
            .spawn(move || {
                for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                    let mut tail = tail.lock().unwrap();
                    if tail.len() >= STDERR_LINES {
                        tail.pop_front();
                    }
                    tail.push_back(line);
                }
            })?;

        // 標準出力: 応答と知らせを読む
        let me = self.clone();
        let generation = self.state.lock().unwrap().generation;
        thread::Builder::new()
            .name("python-stdout".into())
            .spawn(move || me.read_loop(stdout, generation))?;
        Ok(())
    }

    fn read_loop(self: Arc<Self>, stdout: impl Read, generation: u64) {
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        let mut was_started = false;
        let mut said_quit = false;
        loop {
            line.clear();
            match reader.read_line(&mut line) {
                Ok(0) | Err(_) => break,
                Ok(_) => {}
            }
            let head: Value = match serde_json::from_str(line.trim_end()) {
                Ok(v) => v,
                Err(_) => continue, // 形の崩れた行は飛ばす(本来は来ない)
            };
            let len = head.get("len").and_then(Value::as_u64).unwrap_or(0) as usize;
            let mut body = vec![0u8; len];
            if len > 0 && reader.read_exact(&mut body).is_err() {
                break;
            }
            if let Some(event) = head.get("event").and_then(Value::as_str) {
                was_started |= event == "started";
                said_quit |= event == "quit";
                if self.is_current(generation) {
                    self.on_event(event, &head);
                }
                continue;
            }
            let id = head.get("id").and_then(Value::as_u64).unwrap_or(0);
            let reply = Reply {
                status: head.get("status").and_then(Value::as_u64).unwrap_or(500) as u16,
                headers: head
                    .get("headers")
                    .and_then(Value::as_array)
                    .map(|items| {
                        items
                            .iter()
                            .filter_map(|pair| {
                                let pair = pair.as_array()?;
                                Some((pair.first()?.as_str()?.to_string(), pair.get(1)?.as_str()?.to_string()))
                            })
                            .collect()
                    })
                    .unwrap_or_default(),
                body,
            };
            if let Some(sender) = self.pending.lock().unwrap().remove(&id) {
                let _ = sender.send(reply);
            }
        }
        // 立て直しで終わった前の代なら、何もしない(今の代はもう動き始めている)
        if !self.is_current(generation) {
            return;
        }
        // 終わった。待っている要求には「答えられない」を返す(送り手を捨てる)
        self.pending.lock().unwrap().clear();
        self.set_phase(Phase::Ended, None);
        if was_started && !said_quit {
            if let Some(f) = self.on_lost.lock().unwrap().as_ref() {
                f();
            }
        }
    }

    fn on_event(&self, event: &str, head: &Value) {
        match event {
            "started" => self.set_phase(Phase::Started, None),
            "fatal" => {
                let text = |key: &str| head.get(key).and_then(Value::as_str).unwrap_or("").to_string();
                self.set_phase(
                    Phase::Ended,
                    Some(Failure { message: text("message"), hint: text("hint"), log_dir: text("log_dir") }),
                );
            }
            "quit" => {
                if let Some(f) = self.on_quit.lock().unwrap().as_ref() {
                    f();
                }
            }
            _ => {}
        }
    }

    /// 要求を1件渡して、応答を待つ。
    pub fn call(&self, method: &str, path: &str, query: &str, headers: Vec<(String, String)>, body: &[u8]) -> Result<Reply, String> {
        if self.phase() != Phase::Started {
            return Err("Python が動いていません".into());
        }
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let mut headers = headers;
        headers.retain(|(k, _)| !k.eq_ignore_ascii_case("x-tool-token"));
        headers.push(("X-Tool-Token".into(), self.token.clone()));
        let head = json!({
            "id": id,
            "method": method,
            "path": path,
            "query": query,
            "headers": headers.iter().map(|(k, v)| vec![k.clone(), v.clone()]).collect::<Vec<_>>(),
            "len": body.len(),
        });
        let (sender, receiver) = mpsc::channel();
        self.pending.lock().unwrap().insert(id, sender);
        {
            let mut stdin = self.stdin.lock().unwrap();
            let Some(pipe) = stdin.as_mut() else {
                self.pending.lock().unwrap().remove(&id);
                return Err("Python への通り道が閉じています".into());
            };
            let mut frame = serde_json::to_vec(&head).map_err(|e| e.to_string())?;
            frame.push(b'\n');
            frame.extend_from_slice(body);
            if let Err(e) = pipe.write_all(&frame).and_then(|_| pipe.flush()) {
                self.pending.lock().unwrap().remove(&id);
                return Err(format!("Python へ渡せませんでした: {e}"));
            }
        }
        receiver.recv_timeout(REPLY_TIMEOUT).map_err(|_| {
            self.pending.lock().unwrap().remove(&id);
            "Python から応答がありません".to_string()
        })
    }

    fn is_current(&self, generation: u64) -> bool {
        self.state.lock().unwrap().generation == generation
    }

    /// **Python だけを立て直す**(窓は閉じない)。モードを変えたときに使う。
    ///
    /// ブラウザ版ではモードがポートに結びついていたので「開き直す」しかなかった。
    /// ここではポートを使わないので、今の Python に終わってもらい(最後の書き戻しは
    /// 済ませる)、新しい Python を起こすだけでよい。新しい Python は手元の SQLite に
    /// 保存されたモードで開く。立て直しのあいだ、画面には「起動しています」が出る。
    ///
    /// 前半(`mark_restarting`)と後半(`finish_restart`)に分けてある。画面から頼まれたら
    /// 前半はその場で、後半は別のスレッドで行う(`main.rs` の `restart_app`)。
    ///
    /// 立て直しの前半(すぐ終わる): 「起動しています」にして、前の代の知らせを無視し始める。
    /// これを先に済ませれば、直後に読み直した画面は前の代へ行かない
    pub fn mark_restarting(&self) {
        let mut state = self.state.lock().unwrap();
        state.generation += 1;
        state.phase = Phase::Starting;
        state.failure = None;
        self.changed.notify_all();
    }

    /// 立て直しの後半(時間がかかる): 前の代が書き戻しを済ませて終わるのを待ち、次を起こす
    pub fn finish_restart(self: &Arc<Self>) {
        self.shutdown(EXIT_GRACE);
        self.pending.lock().unwrap().clear();
        self.stderr.lock().unwrap().clear();
        self.start();
    }

    /// 終える。標準入力を閉じて Python に終わってもらい、終わらなければ止める。
    pub fn shutdown(&self, grace: Duration) {
        self.stdin.lock().unwrap().take(); // 閉じる = 「もう要求は来ない」
        let mut child = self.child.lock().unwrap();
        if let Some(child) = child.as_mut() {
            let deadline = Instant::now() + grace;
            while Instant::now() < deadline {
                if let Ok(Some(_)) = child.try_wait() {
                    break;
                }
                thread::sleep(Duration::from_millis(50));
            }
            let _ = child.kill();
            let _ = child.wait();
        }
        child.take();
    }
}

/// 試す Python の順番。`KANBAN_PYTHON` があればそれだけを使う。
fn python_candidates() -> Vec<(String, Vec<String>)> {
    if let Ok(path) = std::env::var("KANBAN_PYTHON") {
        if !path.trim().is_empty() {
            return vec![(path, vec![])];
        }
    }
    if cfg!(windows) {
        // python.exe → py ランチャ(PATH に入れ忘れても py は入っていることが多い)
        vec![("python.exe".into(), vec![]), ("py.exe".into(), vec!["-3".into()])]
    } else {
        vec![("python3".into(), vec![]), ("python".into(), vec![])]
    }
}

/// 起動ごとの合言葉。ほかのプロセスから Python へ要求は届かないが
/// (標準入出力は親子だけのもの)、アプリ側の確認はそのまま生かす。
pub fn new_token() -> String {
    use std::collections::hash_map::RandomState;
    use std::hash::{BuildHasher, Hasher};
    let mut text = String::new();
    for _ in 0..4 {
        let mut h = RandomState::new().build_hasher();
        h.write_u128(Instant::now().elapsed().as_nanos());
        h.write_u32(std::process::id());
        text.push_str(&format!("{:016x}", h.finish()));
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicBool;

    #[test]
    fn 合言葉は毎回違い十分に長い() {
        let a = new_token();
        let b = new_token();
        assert_eq!(a.len(), 64);
        assert_ne!(a, b);
    }

    /// 外枠から見た Python の代わり(`bridge.py` と同じやりとりをする小さな台本)
    const FAKE_BRIDGE: &str = r#"
import json, os, sys
out = sys.stdout.buffer
out.write(b'{"event": "started"}\n'); out.flush()
inp = sys.stdin.buffer
while True:
    line = inp.readline()
    if not line:
        break
    head = json.loads(line)
    n = head.get("len", 0)
    body = inp.read(n) if n else b""
    token = dict((k.lower(), v) for k, v in head["headers"]).get("x-tool-token", "")
    data = json.dumps({"pid": os.getpid(), "path": head["path"], "body": body.decode(),
                       "token": token}).encode()
    out.write(json.dumps({"id": head["id"], "status": 200, "headers": [["X-Ok", "1"]],
                          "len": len(data)}).encode() + b"\n" + data)
    out.flush()
"#;

    fn pid_of(reply: &Reply) -> u64 {
        let v: Value = serde_json::from_slice(&reply.body).unwrap();
        v["pid"].as_u64().unwrap()
    }

    #[test]
    fn 起こして渡して_立て直しても前の代の終わりを取り違えない() {
        // 環境変数を触るので1つの試験にまとめる(試験は並んで走る)
        std::env::set_var("KANBAN_PYTHON", "/opt/py/bin/python3");
        assert_eq!(python_candidates(), vec![("/opt/py/bin/python3".to_string(), vec![])]);

        let python = if cfg!(windows) { "python.exe" } else { "python3" };
        std::env::set_var("KANBAN_PYTHON", python);
        let dir = std::env::temp_dir().join(format!("kanban_bridge_test_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("bridge.py"), FAKE_BRIDGE).unwrap();

        let bridge = Bridge::new(dir.clone(), "合言葉".into());
        let lost = Arc::new(AtomicBool::new(false));
        let flag = lost.clone();
        bridge.set_on_lost(move || flag.store(true, Ordering::SeqCst));
        bridge.start();
        assert_eq!(bridge.phase(), Phase::Started, "{:?}", bridge.stderr_tail());

        let first = bridge.call("POST", "/api/mode", "", vec![], "{\"mode\":\"warehouse\"}".as_bytes()).unwrap();
        assert_eq!(first.status, 200);
        assert!(first.headers.contains(&("X-Ok".to_string(), "1".to_string())));
        let v: Value = serde_json::from_slice(&first.body).unwrap();
        assert_eq!(v["path"], "/api/mode");
        assert_eq!(v["body"], "{\"mode\":\"warehouse\"}");
        assert_eq!(v["token"], "合言葉", "外枠が合言葉を付ける");

        bridge.mark_restarting();
        bridge.finish_restart();
        assert_eq!(bridge.phase(), Phase::Started);
        let second = bridge.call("GET", "/", "", vec![], b"").unwrap();
        assert_ne!(pid_of(&first), pid_of(&second), "新しい Python が答える");
        thread::sleep(Duration::from_millis(300));
        assert!(!lost.load(Ordering::SeqCst), "立て直しで終わった前の代を「落ちた」と読まない");

        bridge.shutdown(Duration::from_secs(5));
        thread::sleep(Duration::from_millis(300));
        assert_eq!(bridge.phase(), Phase::Ended);
        assert!(bridge.call("GET", "/", "", vec![], b"").is_err());
        std::env::remove_var("KANBAN_PYTHON");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
