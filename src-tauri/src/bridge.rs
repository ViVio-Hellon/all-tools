//! Python(各ツールの `bridge.py`)を子として起動し、標準入出力で要求を渡す。
//!
//! やりとりの形は各ツールの `bridge.py` の説明と同じ(全ツール共通):
//!
//! ```text
//! 要求  {"id": 7, "method": "POST", "path": "/api/x", "query": "", "headers": [[k, v]], "len": 12}\n<本文>
//! 応答  {"id": 7, "status": 200, "headers": [[k, v]], "len": 345}\n<本文>
//! 知らせ {"event": "started" | "quit" | "fatal", ...}\n
//! ```
//!
//! **ポートは使わない。** 画面(WebView)からの要求は、Tauri の独自の宛先で受けて、
//! ここから Python へ渡す。ツールごとに別の Python(別のプロセス・別の import の空間)
//! なので、`app` や `server` のような同じ名前のモジュールを持つツールが並んでも
//! ぶつからない。1つが落ちても、ほかのツールは動き続ける。
//!
//! 作りは資材発注看板システムの `bridge.rs`(立て直し・代の数え方)を、ツールの数だけ
//! 持てるようにしたもの。

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

/// Python が1行目(started / fatal)を言うまで待つ上限。読み込みが詰まって
/// 黙ったままのとき、「起動しています」を出し続けないため
const FIRST_WORD_LIMIT: Duration = Duration::from_secs(180);

pub struct Reply {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Reply {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(k, _)| k.eq_ignore_ascii_case(name)).map(|(_, v)| v.as_str())
    }
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
    /// 終わった(終了してよいと言われた・落ちた・起動できなかった)
    Ended,
}

/// 子の Python に渡すもの(ツールごと)
#[derive(Clone, Debug)]
pub struct Spec {
    /// ツールの名前(記録用)
    pub id: String,
    /// 一式のフォルダ(`bridge.py` がある所)。Python はここを作業フォルダにして動く
    pub dir: PathBuf,
    /// 合言葉を渡す環境変数(`KANBAN_TOKEN` など)
    pub token_env: String,
    /// 使う Python を指定する環境変数(先にあるものが優先)
    pub python_envs: Vec<String>,
    /// ほかに渡す環境変数
    pub env: Vec<(String, String)>,
    /// 終わるとき・立て直すとき、Python が自分で終わるのを待つ上限
    pub exit_grace: Duration,
    /// exe の名前(見つからないときの案内に出す)
    pub exe_label: String,
}

struct State {
    phase: Phase,
    failure: Option<Failure>,
    /// どの Python で動いているか(画面の診断用)
    python: String,
    /// 何代目の Python か。**立て直したら増える。** 前の代の知らせ(終わった・
    /// 終了してよい)を今の代のものと取り違えないために使う
    generation: u64,
    /// この代の Python を起こし始めたか(開いたタブのツールだけ起こす)
    launched: bool,
    /// 「終了してよい」を言ってから終わったか(落ちたのと区別する)
    said_quit: bool,
}

type Pending = Arc<Mutex<HashMap<u64, mpsc::Sender<Reply>>>>;
type Hook = Mutex<Option<Box<dyn Fn() + Send + Sync>>>;

/// いちど動いた Python を、次のツールでは最初に試す(Store 版の代役を何度も試さない)
static PREFERRED: Mutex<Option<(String, Vec<String>)>> = Mutex::new(None);

pub struct Bridge {
    spec: Spec,
    token: String,
    state: Mutex<State>,
    changed: Condvar,
    stdin: Mutex<Option<ChildStdin>>,
    child: Mutex<Option<Child>>,
    pending: Pending,
    next_id: AtomicU64,
    stderr: Arc<Mutex<VecDeque<String>>>,
    on_quit: Hook,
    on_lost: Hook,
}

impl Bridge {
    pub fn new(spec: Spec, token: String) -> Arc<Self> {
        Arc::new(Self {
            spec,
            token,
            state: Mutex::new(State {
                phase: Phase::Starting,
                failure: None,
                python: String::new(),
                generation: 0,
                launched: false,
                said_quit: false,
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

    pub fn exit_grace(&self) -> Duration {
        self.spec.exit_grace
    }

    /// 「終了してよい」と言われたときに呼ぶもの
    pub fn set_on_quit(&self, f: impl Fn() + Send + Sync + 'static) {
        *self.on_quit.lock().unwrap() = Some(Box::new(f));
    }

    /// 受け付けていた Python が、言われもせずに居なくなったときに呼ぶもの
    /// (そのツールの画面を読み直して、理由の画面を出す)
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

    /// 起こし始めたか(まだならこのツールの Python は居ない)
    pub fn launched(&self) -> bool {
        self.state.lock().unwrap().launched
    }

    /// 「終了してよい」を言って終わったか(落ちた・起動できなかったのではない)
    pub fn ended_by_quit(&self) -> bool {
        let state = self.state.lock().unwrap();
        state.phase == Phase::Ended && state.said_quit && state.failure.is_none()
    }

    /// まだなら、別のスレッドで起こす(**開いたタブのツールだけ**起こす)。
    /// 起こし始めたのがこの呼び出しなら `true`
    pub fn ensure_started(self: &Arc<Self>) -> bool {
        {
            let mut state = self.state.lock().unwrap();
            if state.launched {
                return false;
            }
            state.launched = true;
        }
        let me = self.clone();
        let _ = thread::Builder::new().name(format!("start-{}", self.spec.id)).spawn(move || me.start());
        true
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

    /// 終わるまで待つ(終了の知らせを待つとき)。終わっていれば `true`
    #[allow(dead_code)]
    pub fn wait_ended(&self, limit: Duration) -> bool {
        let deadline = Instant::now() + limit;
        let mut state = self.state.lock().unwrap();
        while state.phase != Phase::Ended {
            let now = Instant::now();
            if now >= deadline {
                return false;
            }
            state = self.changed.wait_timeout(state, deadline - now).unwrap().0;
        }
        true
    }

    /// 「終了してよい」が届くか、終わるまで待つ(終わり方の途中で)。届いていれば `true`
    pub fn wait_quit(&self, limit: Duration) -> bool {
        let deadline = Instant::now() + limit;
        let mut state = self.state.lock().unwrap();
        while !state.said_quit && state.phase != Phase::Ended {
            let now = Instant::now();
            if now >= deadline {
                return false;
            }
            state = self.changed.wait_timeout(state, deadline - now).unwrap().0;
        }
        true
    }

    fn set_phase(&self, phase: Phase, failure: Option<Failure>) {
        let mut state = self.state.lock().unwrap();
        // 一度失敗の理由が入ったら、あとの「終わった」で消さない
        if failure.is_some() {
            state.failure = failure;
        }
        state.phase = phase;
        self.changed.notify_all();
    }

    fn fail(&self, message: String, hint: String) {
        crate::places::shell_log_for(&self.spec, &format!("{message} / {}", hint.replace('\n', " ")));
        self.set_phase(Phase::Ended, Some(Failure { message, hint, log_dir: String::new() }));
    }

    /// Python を探して起動する。**見つかるまで候補を順に試す。**
    ///
    /// Windows には「Python が入っていないのに `python.exe` がある」状態がある
    /// (Microsoft Store へ案内するだけの代役)。それは何も話さずにすぐ終わるので、
    /// 1行も返さずに終わった候補は「使えない」として次へ進む。
    pub fn start(self: &Arc<Self>) {
        {
            let mut state = self.state.lock().unwrap();
            state.launched = true;
            state.said_quit = false;
        }
        let script = self.spec.dir.join("bridge.py");
        if !script.is_file() {
            self.fail(
                format!("bridge.py が見つかりません: {}", script.display()),
                format!(
                    "{} は、日報複合ツール一式が入ったフォルダ(config/tools.json がある所)の直下に置いてください\
                     (ショートカットを作るのは大丈夫です)。",
                    self.spec.exe_label
                ),
            );
            return;
        }
        let mut tried = Vec::new();
        for (program, args) in self.python_candidates() {
            tried.push(program.clone());
            if self.spawn(&program, &args, &script).is_err() {
                continue; // 見つからない
            }
            // 1行目(started / fatal)か、何も言わずに終わるのを待つ
            let deadline = Instant::now() + FIRST_WORD_LIMIT;
            let mut state = self.state.lock().unwrap();
            while state.phase == Phase::Starting && Instant::now() < deadline {
                state = self.changed.wait_timeout(state, Duration::from_millis(500)).unwrap().0;
            }
            if state.phase == Phase::Starting {
                drop(state);
                self.kill_child();
                self.fail(
                    "Python が起動を終えません".into(),
                    format!("{} 秒待っても「受け付けを始めた」が届きませんでした。もう一度開いてください。", FIRST_WORD_LIMIT.as_secs()),
                );
                return;
            }
            let spoke = state.phase == Phase::Started || state.failure.is_some();
            drop(state);
            if spoke {
                if self.phase() == Phase::Started {
                    *PREFERRED.lock().unwrap() = Some((program.clone(), args.clone()));
                } else if let Some(f) = self.failure() {
                    crate::places::shell_log_for(&self.spec, &format!("起動できない: {} / {}", f.message, f.hint.replace('\n', " ")));
                }
                return;
            }
            // 何も言わずに終わった。次の候補へ
            let mut state = self.state.lock().unwrap();
            state.phase = Phase::Starting;
        }
        self.fail(
            "Python が見つかりません(または起動できません)".into(),
            format!(
                "https://www.python.org/downloads/ から Python 3.9 以上を入れてください。\
                 インストーラの最初の画面で「Add python.exe to PATH」に必ずチェックを入れます。\n\
                 入れたあと、コマンドプロンプトでこのフォルダへ移り、次を1度だけ実行してください:\n\
                 python -m pip install -r requirements.txt\n\n試したもの: {}",
                tried.join(" / ")
            ),
        );
    }

    /// 試す Python の順番。指定(`<ツール>_PYTHON` → `ALLTOOLS_PYTHON`)があればそれだけ。
    fn python_candidates(&self) -> Vec<(String, Vec<String>)> {
        for name in &self.spec.python_envs {
            if let Ok(path) = std::env::var(name) {
                if !path.trim().is_empty() {
                    return vec![(path.trim().to_string(), vec![])];
                }
            }
        }
        let mut list = default_pythons();
        if let Some(found) = PREFERRED.lock().unwrap().clone() {
            list.retain(|c| *c != found);
            list.insert(0, found);
        }
        list
    }

    fn spawn(self: &Arc<Self>, program: &str, args: &[String], script: &Path) -> std::io::Result<()> {
        let mut command = Command::new(program);
        // **`-X utf8`(UTF-8 モード)にしない。** 各ツールは単体のとき Windows の既定の
        // 文字コードで動いていた。UTF-8 モードにすると、tasklist などの日本語の出力
        // (Shift-JIS)を UTF-8 で読もうとして落ち、ツールが起動できなかった。
        // やりとり(標準入出力)だけを UTF-8 にする(PYTHONIOENCODING)
        command
            .args(args)
            .arg(script)
            .current_dir(&self.spec.dir)
            .env(&self.spec.token_env, &self.token)
            .env("PYTHONIOENCODING", "utf-8")
            .env_remove("PYTHONUTF8")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        for (k, v) in &self.spec.env {
            command.env(k, v);
        }
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
        self.state.lock().unwrap().python = std::iter::once(program.to_string()).chain(args.iter().cloned()).collect::<Vec<_>>().join(" ");

        // 標準エラー: 末尾だけ覚える(Python のログもここに出る)
        let tail = self.stderr.clone();
        thread::Builder::new()
            .name(format!("stderr-{}", self.spec.id))
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
            .name(format!("stdout-{}", self.spec.id))
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
            let tail = self.stderr_tail();
            let last: Vec<_> = tail.iter().rev().take(8).rev().cloned().collect();
            crate::places::shell_log_for(&self.spec, &format!("知らせずに終わった。最後の出力: {}", last.join(" | ")));
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
                {
                    let mut state = self.state.lock().unwrap();
                    state.said_quit = true;
                    self.changed.notify_all();
                }
                if let Some(f) = self.on_quit.lock().unwrap().as_ref() {
                    f();
                }
            }
            _ => {}
        }
    }

    /// 要求を1件渡して、応答を待つ。
    pub fn call(&self, method: &str, path: &str, query: &str, headers: Vec<(String, String)>, body: &[u8]) -> Result<Reply, String> {
        self.call_with(method, path, query, headers, body, REPLY_TIMEOUT)
    }

    /// 要求を1件渡して、応答を待つ(待つ上限を指定する)。
    pub fn call_with(
        &self,
        method: &str,
        path: &str,
        query: &str,
        headers: Vec<(String, String)>,
        body: &[u8],
        limit: Duration,
    ) -> Result<Reply, String> {
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
        receiver.recv_timeout(limit).map_err(|_| {
            self.pending.lock().unwrap().remove(&id);
            "Python から応答がありません".to_string()
        })
    }

    /// JSON を送って JSON で受け取る(外枠が自分で Python に訊くとき)。Origin は付けない
    pub fn call_json(&self, method: &str, path: &str, body: &Value, limit: Duration) -> Result<(u16, Value), String> {
        let data = serde_json::to_vec(body).map_err(|e| e.to_string())?;
        let reply = self.call_with(
            method,
            path,
            "",
            vec![
                ("Host".into(), "app.localhost".into()),
                ("Content-Type".into(), "application/json".into()),
                ("Accept".into(), "application/json".into()),
            ],
            &data,
            limit,
        )?;
        let value = serde_json::from_slice(&reply.body).unwrap_or(Value::Null);
        Ok((reply.status, value))
    }

    fn is_current(&self, generation: u64) -> bool {
        self.state.lock().unwrap().generation == generation
    }

    /// **Python だけを立て直す**(窓は閉じない)。看板のモード切替・「もう一度開く」で使う。
    ///
    /// 前半(`mark_restarting`)と後半(`finish_restart`)に分けてある。
    /// 前半(すぐ終わる): 「起動しています」にして、前の代の知らせを無視し始める。
    /// これを先に済ませれば、直後に読み直した画面は前の代へ行かない
    pub fn mark_restarting(&self) {
        let mut state = self.state.lock().unwrap();
        state.generation += 1;
        state.phase = Phase::Starting;
        state.failure = None;
        state.said_quit = false;
        state.launched = true;
        self.changed.notify_all();
    }

    /// 立て直しの後半(時間がかかる): 前の代が書き戻しを済ませて終わるのを待ち、次を起こす
    pub fn finish_restart(self: &Arc<Self>) {
        self.close_and_wait(self.spec.exit_grace);
        self.pending.lock().unwrap().clear();
        self.stderr.lock().unwrap().clear();
        self.start();
    }

    /// 終える。標準入力を閉じて Python に終わってもらい、終わらなければ止める。
    pub fn shutdown(&self, grace: Duration) {
        self.close_and_wait(grace);
        let mut state = self.state.lock().unwrap();
        if state.phase != Phase::Ended && state.launched {
            state.phase = Phase::Ended;
            self.changed.notify_all();
        }
    }

    fn close_and_wait(&self, grace: Duration) {
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

    fn kill_child(&self) {
        self.stdin.lock().unwrap().take();
        if let Some(mut child) = self.child.lock().unwrap().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn default_pythons() -> Vec<(String, Vec<String>)> {
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
    for i in 0..4u32 {
        let mut h = RandomState::new().build_hasher();
        h.write_u128(Instant::now().elapsed().as_nanos());
        h.write_u128(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0));
        h.write_u32(std::process::id());
        h.write_u32(i);
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
if os.environ.get("FAKE_FATAL"):
    out.write(json.dumps({"event": "fatal", "message": "ブラウザ版が動いています", "hint": "閉じてから"}).encode() + b"\n"); out.flush()
    sys.exit(1)
out.write(b'{"event": "started"}\n'); out.flush()
inp = sys.stdin.buffer
while True:
    line = inp.readline()
    if not line:
        break
    head = json.loads(line)
    n = head.get("len", 0)
    body = inp.read(n) if n else b""
    if head["path"] == "/api/shutdown":
        out.write(json.dumps({"id": head["id"], "status": 200, "headers": [], "len": 2}).encode() + b"\n{}")
        out.write(b'{"event": "quit"}\n'); out.flush()
        continue
    token = dict((k.lower(), v) for k, v in head["headers"]).get("x-tool-token", "")
    data = json.dumps({"pid": os.getpid(), "path": head["path"], "body": body.decode(),
                       "token": token, "shell": os.environ.get("ALLTOOLS_SHELL", "")}).encode()
    out.write(json.dumps({"id": head["id"], "status": 200, "headers": [["X-Ok", "1"]],
                          "len": len(data)}).encode() + b"\n" + data)
    out.flush()
"#;

    fn spec(dir: &Path, id: &str) -> Spec {
        let python = if cfg!(windows) { "python.exe" } else { "python3" };
        std::env::set_var(format!("{}_PYTHON", id.to_uppercase()), python);
        Spec {
            id: id.into(),
            dir: dir.to_path_buf(),
            token_env: format!("{}_TOKEN", id.to_uppercase()),
            python_envs: vec![format!("{}_PYTHON", id.to_uppercase())],
            env: vec![("ALLTOOLS_SHELL".into(), "1".into())],
            exit_grace: Duration::from_secs(5),
            exe_label: "AllTools.exe".into(),
        }
    }

    fn pid_of(reply: &Reply) -> u64 {
        let v: Value = serde_json::from_slice(&reply.body).unwrap();
        v["pid"].as_u64().unwrap()
    }

    #[test]
    fn 起こして渡して_立て直しても前の代の終わりを取り違えない() {
        let dir = std::env::temp_dir().join(format!("alltools_bridge_test_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("bridge.py"), FAKE_BRIDGE).unwrap();

        let bridge = Bridge::new(spec(&dir, "faketool"), "合言葉".into());
        assert!(!bridge.launched(), "開くまでは起こさない");
        let lost = Arc::new(AtomicBool::new(false));
        let flag = lost.clone();
        bridge.set_on_lost(move || flag.store(true, Ordering::SeqCst));
        assert!(bridge.ensure_started());
        assert!(!bridge.ensure_started(), "2度は起こさない");
        assert!(bridge.wait_started(Duration::from_secs(30)), "{:?}", bridge.stderr_tail());

        let first = bridge.call("POST", "/api/mode", "", vec![], "{\"mode\":\"warehouse\"}".as_bytes()).unwrap();
        assert_eq!(first.status, 200);
        assert_eq!(first.header("x-ok"), Some("1"));
        let v: Value = serde_json::from_slice(&first.body).unwrap();
        assert_eq!(v["path"], "/api/mode");
        assert_eq!(v["body"], "{\"mode\":\"warehouse\"}");
        assert_eq!(v["token"], "合言葉", "外枠が合言葉を付ける");
        assert_eq!(v["shell"], "1", "日報複合ツールの中で動いていることを渡す");

        bridge.mark_restarting();
        bridge.finish_restart();
        assert_eq!(bridge.phase(), Phase::Started);
        let second = bridge.call("GET", "/", "", vec![], b"").unwrap();
        assert_ne!(pid_of(&first), pid_of(&second), "新しい Python が答える");
        thread::sleep(Duration::from_millis(300));
        assert!(!lost.load(Ordering::SeqCst), "立て直しで終わった前の代を「落ちた」と読まない");

        // 「終了してよい」を言って終わったのは、落ちたのとは別
        let quit = Arc::new(AtomicBool::new(false));
        let flag = quit.clone();
        bridge.set_on_quit(move || flag.store(true, Ordering::SeqCst));
        let reply = bridge.call("POST", "/api/shutdown", "", vec![], b"{}").unwrap();
        assert_eq!(reply.status, 200);
        bridge.shutdown(Duration::from_secs(5));
        assert!(bridge.wait_ended(Duration::from_secs(5)));
        assert!(quit.load(Ordering::SeqCst));
        assert!(bridge.ended_by_quit());
        assert!(!lost.load(Ordering::SeqCst));
        assert!(bridge.call("GET", "/", "", vec![], b"").is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn 起動できない理由はpythonが言ったとおりに出す() {
        let dir = std::env::temp_dir().join(format!("alltools_bridge_fatal_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("bridge.py"), FAKE_BRIDGE).unwrap();
        let mut s = spec(&dir, "fataltool");
        s.env.push(("FAKE_FATAL".into(), "1".into()));
        let bridge = Bridge::new(s, "x".into());
        bridge.start();
        assert_eq!(bridge.phase(), Phase::Ended);
        let failure = bridge.failure().expect("理由がある");
        assert_eq!(failure.message, "ブラウザ版が動いています");
        assert!(!bridge.ended_by_quit());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn bridge_pyが無ければ置き場所を案内する() {
        let dir = std::env::temp_dir().join(format!("alltools_bridge_none_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let bridge = Bridge::new(spec(&dir, "nonetool"), "x".into());
        bridge.start();
        let failure = bridge.failure().unwrap();
        assert!(failure.message.contains("bridge.py"));
        assert!(failure.hint.contains("AllTools.exe"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
