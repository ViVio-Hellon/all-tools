//! ブラウザ版とデスクトップ版を同時に動かさない(どちらを後から開いても止まる)
//!
//! Python 側の `portal/instance_guard.py` と**同じ名前・同じ手順**で錠を扱う。
//! 食い違うと止め損ねるので、試験で名前を突き合わせている(`tests/test_shell_contract.py`)。
//! 作りは点検表(vba-inspection-sheet-python-migration)の `instance.rs` と同じ。
//!
//! 【錠は OS の名前付きの錠にする(ファイルにしない)】
//! ラインPCの Python は Microsoft Store 版で、`%LOCALAPPDATA%` に書いたファイルは
//! Python 専用の場所へ振り替えられ、この exe からは見えない。名前付きミューテックスは
//! 振り替えられないので、どちらからも同じ名前で見える。
//!
//! ```text
//! 1. 自分の種類の目印を作る(desktop)
//! 2. 本体の錠を作る
//!    ├─ 作れた                  → 起動してよい(終わるまで握る)
//!    └─ すでにあった
//!       ├─ 相手の目印(browser)がある → ブラウザ版が動いている。止まる
//!       └─ 無い                      → デスクトップ版が動いている(窓を前に出すのは
//!                                       single-instance の仕事)
//! ```

use std::path::Path;

pub const BROWSER: &str = "browser";
pub const DESKTOP: &str = "desktop";

/// 錠の名前の頭。`config/app.json` の `app_id` から作る(Python と同じ規則で崩す)。
pub fn base_name(app_id: &str) -> String {
    let app_id = if app_id.trim().is_empty() { "nlm.all-tools" } else { app_id.trim() };
    app_id
        .chars()
        .map(|c| if c.is_alphanumeric() || "._-".contains(c) { c } else { '_' })
        .collect()
}

pub struct Names {
    pub instance: String,
    pub desktop: String,
    pub browser: String,
}

pub fn names(app_id: &str) -> Names {
    let base = base_name(app_id);
    Names {
        instance: format!("{base}.instance"),
        desktop: format!("{base}.desktop"),
        browser: format!("{base}.browser"),
    }
}

/// `claim_desktop` の結果
pub enum Claim {
    /// 起動してよい。握っている錠(プロセスが終わるまで持っておく)
    Claimed(Vec<sys::Held>),
    /// すでに動いているものがある(その種類)
    Running(&'static str),
}

/// デスクトップ版として動いてよいかを決め、よければ錠を握る。
pub fn claim_desktop(app_id: &str, local: &Path) -> std::io::Result<Claim> {
    let n = names(app_id);
    let ctx = sys::Context::new(local);
    let mut held = Vec::new();
    if let Some(marker) = ctx.create(&n.desktop, false)? {
        held.push(marker); // 1. 自分の目印を先に
    }
    match ctx.create(&n.instance, true)? {
        Some(instance) => {
            held.push(instance);
            Ok(Claim::Claimed(held))
        }
        None => {
            let running = if ctx.exists(&n.browser) { BROWSER } else { DESKTOP };
            drop(held);
            Ok(Claim::Running(running))
        }
    }
}

/// いま動いているほう(試験・診断用)。錠は取らない。
#[allow(dead_code)]
pub fn running(app_id: &str, local: &Path) -> &'static str {
    let n = names(app_id);
    let ctx = sys::Context::new(local);
    if ctx.exists(&n.desktop) {
        DESKTOP
    } else if ctx.exists(&n.instance) || ctx.exists(&n.browser) {
        BROWSER
    } else {
        ""
    }
}

// ------------------------------------------------------------------
// Windows: 名前付きミューテックス(`Local\` = 利用者のログオンごと)
// ------------------------------------------------------------------
#[cfg(windows)]
pub mod sys {
    use std::ffi::c_void;
    use std::path::Path;

    type Handle = *mut c_void;
    const ERROR_ALREADY_EXISTS: u32 = 183;
    const SYNCHRONIZE: u32 = 0x0010_0000;

    #[link(name = "kernel32")]
    extern "system" {
        fn CreateMutexW(attributes: *const c_void, initial_owner: i32, name: *const u16) -> Handle;
        fn OpenMutexW(access: u32, inherit: i32, name: *const u16) -> Handle;
        fn CloseHandle(handle: Handle) -> i32;
        fn GetLastError() -> u32;
    }

    /// 握っている錠。落とすと(プロセスが終わっても)OS が放す
    pub struct Held(Handle);
    unsafe impl Send for Held {}
    unsafe impl Sync for Held {}
    impl Drop for Held {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }

    fn wide(name: &str) -> Vec<u16> {
        format!("Local\\{name}").encode_utf16().chain(std::iter::once(0)).collect()
    }

    pub struct Context;

    impl Context {
        pub fn new(_local: &Path) -> Self {
            Context
        }

        /// 作る。`exclusive` ですでにあったら `None`(目印はあっても握る)
        pub fn create(&self, name: &str, exclusive: bool) -> std::io::Result<Option<Held>> {
            let name = wide(name);
            let (handle, error) = unsafe {
                let handle = CreateMutexW(std::ptr::null(), 0, name.as_ptr());
                (handle, GetLastError())
            };
            if handle.is_null() {
                return Err(std::io::Error::from_raw_os_error(error as i32));
            }
            let held = Held(handle);
            if exclusive && error == ERROR_ALREADY_EXISTS {
                return Ok(None); // `held` を落として閉じる
            }
            Ok(Some(held))
        }

        pub fn exists(&self, name: &str) -> bool {
            let name = wide(name);
            unsafe {
                let handle = OpenMutexW(SYNCHRONIZE, 0, name.as_ptr());
                if handle.is_null() {
                    return false;
                }
                CloseHandle(handle);
                true
            }
        }
    }
}

// ------------------------------------------------------------------
// それ以外: 手元の領域の runtime/ のファイルロック(開発機・試験用)
// ------------------------------------------------------------------
#[cfg(not(windows))]
pub mod sys {
    use std::fs::{File, OpenOptions};
    use std::path::{Path, PathBuf};

    pub struct Held(#[allow(dead_code)] File);

    pub struct Context {
        dir: PathBuf,
    }

    impl Context {
        pub fn new(local: &Path) -> Self {
            Context { dir: local.join("runtime") }
        }

        fn open(&self, name: &str) -> std::io::Result<File> {
            std::fs::create_dir_all(&self.dir)?;
            OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(self.dir.join(format!("{name}.lock")))
        }

        /// 本体は1人だけ(排他)。目印は同じ種類が何人でも握れる(共有)
        pub fn create(&self, name: &str, exclusive: bool) -> std::io::Result<Option<Held>> {
            let file = self.open(name)?;
            let taken = if exclusive { file.try_lock() } else { file.try_lock_shared() };
            Ok(taken.ok().map(|_| Held(file)))
        }

        pub fn exists(&self, name: &str) -> bool {
            match self.open(name) {
                Ok(file) => file.try_lock().is_err(), // 握って確かめ、閉じて放す
                Err(_) => false,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 名前はapp_idから作りpythonと同じ規則で崩す() {
        assert_eq!(base_name("試し.app-id 1"), "試し.app-id_1");
        assert_eq!(base_name(""), "nlm.all-tools");
        let n = names("nlm.all-tools");
        assert_eq!(n.instance, "nlm.all-tools.instance");
        assert_eq!(n.desktop, "nlm.all-tools.desktop");
        assert_eq!(n.browser, "nlm.all-tools.browser");
    }

    #[test]
    fn ブラウザ版が居れば止まり_デスクトップ版同士は種類を言う() {
        let local = std::env::temp_dir().join(format!("alltools_instance_{}", std::process::id()));
        let app = format!("alltools.test.{}", std::process::id());
        let n = names(&app);
        let ctx = sys::Context::new(&local);

        // ブラウザ版の代わり: 目印と本体を握る
        let browser_marker = ctx.create(&n.browser, false).unwrap().unwrap();
        let browser_instance = ctx.create(&n.instance, true).unwrap().unwrap();
        assert!(matches!(claim_desktop(&app, &local).unwrap(), Claim::Running(BROWSER)));
        assert_eq!(running(&app, &local), BROWSER);
        drop(browser_instance);
        drop(browser_marker);

        // 空いたら取れる。2つ目のデスクトップ版は「デスクトップ版が居る」
        let first = claim_desktop(&app, &local).unwrap();
        assert!(matches!(first, Claim::Claimed(_)));
        assert!(matches!(claim_desktop(&app, &local).unwrap(), Claim::Running(DESKTOP)));
        assert_eq!(running(&app, &local), DESKTOP);
        drop(first);
        assert_eq!(running(&app, &local), "");
        let _ = std::fs::remove_dir_all(&local);
    }
}
