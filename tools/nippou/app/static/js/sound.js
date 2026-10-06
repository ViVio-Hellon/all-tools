/*
  sound.js — 音を鳴らす (VBA `音楽を流す`)

  **鳴らすのはここ、決めるのはサーバ。** どの音をいつ鳴らすか、二重に
  鳴らさないか(`SoundGate`)は `nippou/logic/sound.py` が持つ。ここは
  `/api/sound/due` に訊いて、返ってきたものを鳴らすだけ。

  【ブラウザは勝手に音を鳴らせない】
  利用者がそのページを一度も触っていないあいだ、ブラウザは `play()` を
  断ります(自動再生の制限)。これはブラウザ側の事情なので、ここで扱う:

    ・最初のクリック/キー入力で、無音を1回鳴らして「触った」ことにする
    ・それでも断られたら、**画面に文言を出す**

  文言はサーバが一緒に返してくれる。**音は補助で、伝えたいことは文字の
  ほうにある** ── 音が鳴らない端末でも、催促は届く。

  【出来事 (v4.4.0)】
  どの出来事で何を鳴らすかは設定画面で選びます(`logic/sound.SOUNDS`)。
  **鳴らしてよい出来事の一覧はサーバから受け取ります**(`cues` ── 1分ごとの
  見張りの応答に入っている)。一覧に無い出来事は鳴らしに行きません。

  出来事が起きたと知らせてくる道は3つ:
    ・サーバの応答の `sound_cue`(保存した・共有へ保存した)… `api.js` が `sound:cue` を出す
    ・断り・エラーの応答 … `api.js` が `api:failed` を出し、ここが「断られた」
      「エラーが起きた」に振り分ける(決まった音 `sound` があればそちら)
    ・画面のエラー(番号つき)… `errors.js` が `sound:cue` を出す
*/

import { background } from "./api.js";
import { toast } from "./toast.js";

// 出番を訊きに行く間隔(ms)。VBA のタイマーは1分ごとだったので合わせる
const POLL_MS = 60000;

let unlocked = false;
let audio = null;
// 鳴らしてよい出来事(サーバが決める)。**読めるまでは null** ── そのあいだは
// 前と同じく鳴らしに行く(無ければ鳴らないだけ)
let cues = null;

function setCues(list) {
  if (Array.isArray(list)) cues = new Set(list);
}

/** 鳴らしてよい出来事を読み直す(設定を保存した直後など)。 */
export async function loadCues() {
  try {
    setCues((await background.get("/api/sound/cues")).cues);
  } catch (err) { /* 読めなければ前のまま */ }
}

/*
  「触った」ことにする。

  ブラウザは、利用者の操作をきっかけにした `play()` だけを通す。最初の
  操作のときに**無音を一瞬鳴らして**おくと、以後は操作と無関係にも鳴らせる。
*/
function unlock() {
  if (unlocked) return;
  unlocked = true;
  try {
    const el = new Audio();
    // 1サンプルぶんの無音(WAV)。**外部ファイルを読まない** ── 置き場所の
    // 設定に関係なく、この仕掛けだけは必ず動いてほしい
    el.src = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9"
      + "AAACABAAZGF0YQAAAAA=";
    el.volume = 0;
    el.play().catch(() => { /* 断られてもよい。次で分かる */ });
  } catch (err) { /* 音が使えない端末。文言だけで動く */ }
}

function armUnlock() {
  for (const type of ["pointerdown", "keydown"]) {
    document.addEventListener(type, unlock, { once: true, passive: true });
  }
}

/** 鳴らす。鳴らせなければ false(呼び手が文言を出す)。 */
async function play(url) {
  try {
    if (!audio) audio = new Audio();
    audio.src = url;
    await audio.play();
    return true;
  } catch (err) {
    return false;
  }
}

/*
  出番を訊く。

  **鳴らす/鳴らさないの判断はしない。** サーバが `play` を返したときだけ
  鳴らす ── 二重に鳴らさない見張りもサーバ側にあるので、ここで覚えると
  同じことを2か所で決めることになる。
*/
async function tick() {
  let body;
  try {
    body = await background.get("/api/sound/due");
  } catch (err) {
    return;                       // 繋がらないのは `health.js` が知らせる
  }
  setCues(body.cues);
  const due = body.play;
  if (!due) return;

  // **`due.url` をそのまま使わない。** `/sound/<鍵>` はトークンが要る
  // 経路なので、素の道を渡すと 401 で返ってきて**催促の音だけが永久に
  // 鳴りません**(押して鳴らす `playKey` は付けていたので気づけなかった)。
  // 鍵から組み立て直して、トークンを付ける。
  //
  // `sound: false` は**鳴らさないと決めてある**(文言だけ出す催促)
  const ok = due.sound === false ? false : await playKey(due.key);
  // **鳴っても文言は出す。** 音は気づかせるためのもので、内容は文字にある
  toast(due.message, ok || due.sound === false ? "warn" : "error");
  if (!ok) {
    console.info("[sound] 鳴らせませんでした(自動再生の制限か、ファイルが"
      + "ありません)。文言だけ出します:", due.message);
  }
}

/**
 * 名指しで鳴らす。
 *
 * ポーリングを待たずにその場で鳴らしたいとき用 ── 作業時間がマイナスに
 * なったときのように、**打った瞬間に気づいてほしい**もの。
 * 鳴らせなければ false(呼び手が文言を出す)。
 */
export async function playKey(key) {
  if (!key) return false;
  // **鳴らさないと決めてある出来事は鳴らしに行かない**(ファイルも取りに行かない)
  if (cues && !cues.has(key)) return false;
  return play(`/sound/${encodeURIComponent(key)}?t=${
    encodeURIComponent(window.APP.token)}`);
}

/**
 * 出来事が起きた。**鳴らせる出来事なら鳴らす**(文言は呼び手が出している)。
 * 鳴らせなくても何もしない ── 音は補助です。
 */
export function cue(key) {
  if (!key) return;
  playKey(key).catch(() => { /* 鳴らなくてよい */ });
}

/**
 * 設定画面の試聴。**選んであるファイル(保存前でも)を鳴らす**。音声フォルダの
 * 中だけ。押した直後なので、必ず鳴らせる(自動再生の制限にかからない)。
 */
export async function previewFile(name) {
  unlock();
  return play(`/sound/preview?name=${encodeURIComponent(name)}&t=${
    encodeURIComponent(window.APP.token)}`);
}

/*
  断り・エラーを、出来事「断られた」「エラーが起きた」に振り分ける。

  **聞き返しは断りではない**(直の変わり目でどちらへ入れるか・4ページ目を
  出すか)── 選んでもらう前に「断られた」音を鳴らすと、押した人は失敗した
  と思います。**決まった音がある断り**(梱包数・停止記号)はそちらが鳴るので、
  ここでは鳴らしません。
*/
function onFailed({ status, body, eventId }) {
  if (body?.sound) return;
  if (body?.shift_changed?.crossed || body?.needs_confirm) return;
  if (eventId || status >= 500) cue("error");
  else if (status >= 400) cue("refused");
}

export function startSoundWatch() {
  armUnlock();
  window.addEventListener("sound:cue", (event) => cue(event.detail));
  window.addEventListener("api:failed", (event) => onFailed(event.detail || {}));
  tick();
  setInterval(tick, POLL_MS);
}
