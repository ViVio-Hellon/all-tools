//! 待ちの締め切り ── **PC が眠っていたあいだを、待った時間に数えない**
//!
//! 外枠はあちこちで「何秒まで待つ」をしている(Python の起動待ち・受け付けの開始・
//! 「終了してよい」の待ち・終わり方の待ち・要求の応答待ち・閉じる前の返事)。以前は
//! どれも `Instant` の差で測っていた。`Instant` はスリープのあいだも進むことがある
//! (Linux はプロセスを止めていても進む。Windows も眠っているあいだ進むなら同じ)ので:
//!
//! - 起動の途中で蓋を閉じると、起きて 0.5 秒以内に「受け付けを始めた」が来なければ
//!   Python を止めて「Python が起動を終えません」を出していた
//! - × で閉じている途中に蓋を閉じると、起きた直後に Python を孫ごと止めていた
//!   (看板の最後の書き戻しが切れる)
//! - 要求の途中で眠ると、起きた直後に「応答がありません」(503・失敗の画面)
//!
//! そこで締め切りは**待ち1回ごとの経過を足していって**測る。1回の待ちは短く
//! (`SLICE`)、1回で `JUMP` より大きく空いたら眠っていたとみなし、その1回は
//! 待ち1回ぶん(`SLICE`)だけ数える(日報の `awake_clock` と同じ考え)。
//! 起きているあいだはこれまでどおり締め切りで切れる。

use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::sync::{Arc, Condvar, MutexGuard};
use std::time::{Duration, Instant};

/// 1回の待ちの上限。これより長くは続けて待たない(待ちのあいだに眠っても気づけるように)
pub const SLICE: Duration = Duration::from_millis(500);

/// 待ち1回でこれより空いたら、そのあいだ PC ごと眠っていた(スリープ・休止)とみなす。
/// 1回は `SLICE` までなので、混んでいて少し遅れた程度では眠ったことにしない
pub const JUMP: Duration = Duration::from_secs(5);

/// 時計。試験は止まっていた時間を差し込める時計を渡す
pub trait Clock: Send + Sync {
    fn now(&self) -> Instant;
}

/// ふつうの時計(`Instant::now()`)
pub struct SystemClock;

impl Clock for SystemClock {
    fn now(&self) -> Instant {
        Instant::now()
    }
}

pub type SharedClock = Arc<dyn Clock>;

pub fn system_clock() -> SharedClock {
    Arc::new(SystemClock)
}

/// 起きていた時間だけで数える締め切り
pub struct Deadline {
    clock: SharedClock,
    limit: Duration,
    spent: Duration,
    last: Instant,
}

impl Deadline {
    pub fn new(limit: Duration, clock: SharedClock) -> Self {
        let last = clock.now();
        Self { clock, limit, spent: Duration::ZERO, last }
    }

    /// ふつうの時計で
    pub fn after(limit: Duration) -> Self {
        Self::new(limit, system_clock())
    }

    /// 前に見たときからの経過を足す。**大きく空いていたら、待ち1回ぶんだけ**
    fn count(&mut self) {
        let now = self.clock.now();
        let step = now.saturating_duration_since(self.last);
        self.last = now;
        self.spent += if step > JUMP { SLICE } else { step };
    }

    /// 締め切りを過ぎたか
    pub fn passed(&mut self) -> bool {
        self.count();
        self.spent >= self.limit
    }

    /// 残り(起きていた時間で)
    pub fn left(&mut self) -> Duration {
        self.count();
        self.limit.saturating_sub(self.spent)
    }

    /// 次に待つ長さ(残りと `SLICE` の短いほう)。締め切りを過ぎていれば `None`
    pub fn slice(&mut self) -> Option<Duration> {
        let left = self.left();
        if left.is_zero() {
            None
        } else {
            Some(left.min(SLICE))
        }
    }

    /// 数え直す(締め切りを `limit` 先へ置き直す)
    pub fn restart(&mut self, limit: Duration) {
        self.limit = limit;
        self.spent = Duration::ZERO;
        self.last = self.clock.now();
    }
}

/// `keep_waiting` が真のあいだ `changed` を待つ。締め切りまでに偽になれば `true`
pub fn wait_while<'a, T>(
    changed: &Condvar,
    mut guard: MutexGuard<'a, T>,
    deadline: &mut Deadline,
    mut keep_waiting: impl FnMut(&T) -> bool,
) -> (MutexGuard<'a, T>, bool) {
    loop {
        if !keep_waiting(&guard) {
            return (guard, true);
        }
        let Some(step) = deadline.slice() else {
            return (guard, false);
        };
        guard = changed.wait_timeout(guard, step).unwrap().0;
    }
}

/// 締め切りまで受け取りを待つ(`recv_timeout` の1回の長い待ちを、短い待ちの繰り返しに分ける)
pub fn recv_until<T>(rx: &Receiver<T>, deadline: &mut Deadline) -> Result<T, RecvTimeoutError> {
    loop {
        let Some(step) = deadline.slice() else {
            return Err(RecvTimeoutError::Timeout);
        };
        match rx.recv_timeout(step) {
            Ok(value) => return Ok(value),
            Err(RecvTimeoutError::Timeout) => continue,
            Err(RecvTimeoutError::Disconnected) => return Err(RecvTimeoutError::Disconnected),
        }
    }
}

/// 試験用の時計(ほかの組み立ての試験からも使う)
#[cfg(test)]
pub(crate) mod testing {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    /// ふつうに進み、`sleep` で**眠っていたことにできる**時計。何度見られたかも数える
    /// (待っている側が眠りに気づいたかを、時間ではなく状態で待つため)
    #[derive(Default)]
    pub struct FakeClock {
        extra: Mutex<Duration>,
        calls: AtomicUsize,
    }

    impl FakeClock {
        pub fn new() -> Arc<Self> {
            Arc::new(Self::default())
        }

        /// PC が `d` だけ眠っていたことにする(時計だけが一気に進む)
        pub fn sleep(&self, d: Duration) {
            *self.extra.lock().unwrap() += d;
        }

        pub fn calls(&self) -> usize {
            self.calls.load(Ordering::SeqCst)
        }

        /// 待っている側が、いまから `n` 回 時計を見るまで待つ(上限は実時間で 30 秒)
        pub fn wait_looked(&self, n: usize) {
            let target = self.calls() + n;
            let give_up = Instant::now() + Duration::from_secs(30);
            while self.calls() < target {
                assert!(Instant::now() < give_up, "待っている側が時計を見に来ません");
                std::thread::sleep(Duration::from_millis(5));
            }
        }
    }

    impl Clock for FakeClock {
        fn now(&self) -> Instant {
            self.calls.fetch_add(1, Ordering::SeqCst);
            Instant::now() + *self.extra.lock().unwrap()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::testing::FakeClock;
    use super::*;
    use std::sync::mpsc;
    use std::sync::Mutex;
    use std::thread;

    const HOUR: Duration = Duration::from_secs(3600);

    #[test]
    fn 大きく飛んだら数えない() {
        let clock = FakeClock::new();
        let mut deadline = Deadline::new(Duration::from_secs(10), clock.clone());
        clock.sleep(HOUR);
        assert!(!deadline.passed(), "眠っていた1時間を待った時間に数えた");
        assert!(deadline.left() >= Duration::from_secs(10) - SLICE - Duration::from_millis(100));
        // 何度眠っても同じ
        clock.sleep(HOUR);
        clock.sleep(HOUR);
        assert!(!deadline.passed());
    }

    #[test]
    fn ふつうは締め切りで切れる() {
        let lock = Mutex::new(());
        let changed = Condvar::new();
        let mut deadline = Deadline::after(Duration::from_millis(300));
        let started = Instant::now();
        let (_guard, ok) = wait_while(&changed, lock.lock().unwrap(), &mut deadline, |_| true);
        assert!(!ok);
        let took = started.elapsed();
        assert!(took >= Duration::from_millis(290), "締め切りより早く切れた: {took:?}");
        assert!(took < Duration::from_secs(5), "締め切りで切れない: {took:?}");
    }

    #[test]
    fn 少し遅れた程度では眠ったことにしない() {
        let clock = FakeClock::new();
        let mut deadline = Deadline::new(Duration::from_secs(3), clock.clone());
        clock.sleep(Duration::from_secs(4)); // 混んでいて 4 秒 回ってこなかった
        assert!(deadline.passed(), "眠っていないのに締め切りを延ばした");
    }

    #[test]
    fn 待っているあいだに眠っても_起きてから届けば受け取る() {
        let clock = FakeClock::new();
        let lock = Mutex::new(());
        let changed = Condvar::new();
        let mut deadline = Deadline::new(Duration::from_secs(3), clock.clone());
        let mut looked = 0;
        let (_guard, ok) = wait_while(&changed, lock.lock().unwrap(), &mut deadline, |_| {
            looked += 1;
            if looked == 1 {
                clock.sleep(HOUR); // 待ち始めたところで蓋を閉じた
                return true;
            }
            false // 起きた直後に届いた
        });
        assert!(ok, "起きた瞬間に締め切りを過ぎたことにした");
    }

    #[test]
    fn 応答待ちも眠った分を数えない() {
        let clock = FakeClock::new();
        let (tx, rx) = mpsc::channel();
        let mut deadline = Deadline::new(Duration::from_secs(3), clock.clone());
        clock.sleep(HOUR);
        let sender = thread::spawn(move || {
            thread::sleep(Duration::from_millis(100));
            let _ = tx.send(7);
        });
        assert_eq!(recv_until(&rx, &mut deadline), Ok(7), "起きた直後に「応答がありません」にした");
        sender.join().unwrap();
        // 送り手が居なくなったらすぐ返る
        let (tx, rx) = mpsc::channel::<u8>();
        drop(tx);
        assert_eq!(recv_until(&rx, &mut Deadline::after(Duration::from_secs(30))), Err(RecvTimeoutError::Disconnected));
    }

    #[test]
    fn 応答待ちもふつうは締め切りで切れる() {
        let (_tx, rx) = mpsc::channel::<u8>();
        let started = Instant::now();
        assert_eq!(recv_until(&rx, &mut Deadline::after(Duration::from_millis(300))), Err(RecvTimeoutError::Timeout));
        assert!(started.elapsed() < Duration::from_secs(5));
    }
}
