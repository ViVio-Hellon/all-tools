/*
  tabs.js — 縦に積む代わりに、面で分ける

  【なぜタブか】
  縦に積んだものは、下にあるほど「無いもの」として扱われます。
  スクロールしないと見えないということは、**見えていないあいだは
  存在を思い出せない**ということで、設定画面のように「どこかに
  あるはず」を探す画面ではとくに効きます。

  設定画面は14枚のカードが1本に積まれていて、「機能を確かめたいのに
  どこにあるか分からない」状態でした。タブなら、**中身を隠しても
  見出しは常に見えています。** 何があるかは一覧できて、いま見て
  いないものも忘れずに済みます。

  【タブが守らなければならないこと】
  1. **問題を隠さない。** 中身に「見つかりません」がある面は、開いて
     いなくても見出しがそう言う。隠したせいで気づけなくなるなら、
     スクロールのほうがまだまし ── 印そのものは**サーバが描きます**
     (`presenters/settings.tab_badges` → `settings.html`)
  2. **その先に何があるかを示す。** 件数や状態を見出しに添える
  3. **色だけで伝えない。** 選ばれている面は下線と太字でも分かる。
     状態は文言でも出す
  4. **キーボードで回れる。** 矢印キーで移動、Home/End で端へ

  【どこまでが画面の仕事か】
  面の**並び・見出し・鍵の要否はサーバが決めた値**(`presenters/
  settings.py` の `TABS`)を写すだけです。「いまどの面を見ているか」は
  業務の事実ではないので画面が持ちます ── 画面ごとに `sessionStorage`
  へ覚え、戻ってきたとき続きから見られるようにします。
*/

const STORE_PREFIX = "tab:";

/** 選んでいる面を覚える。業務の状態ではないので画面側で持つ。 */
function remember(group, key) {
  try {
    sessionStorage.setItem(STORE_PREFIX + group, key);
  } catch (e) {
    // プライベートモード等で使えないことがある。覚えられないだけで
    // 動きは変わらないので、黙って諦める
  }
}

function recall(group) {
  try {
    return sessionStorage.getItem(STORE_PREFIX + group);
  } catch (e) {
    return null;
  }
}

/**
 * 外からの名指し(`?tab=master` 等)。
 *
 * グラフ画面の「目標が設定されていません」のように、**この面を直接
 * 開かせたい**リンクがあります。**前回覚えていた面より優先します**
 * ── 覚えていた面を出しても、リンクが指した先が見えなければ、
 * リンクを踏んだ意味がありません。
 */
function requested() {
  try {
    return new URLSearchParams(location.search).get("tab");
  } catch (e) {
    return null;
  }
}

function barTabs(root) {
  return [...root.querySelectorAll(":scope > .tabs__bar > .tab")];
}

/** 面を1つ選ぶ。`key` が無ければ何もしない(消えた面を覚えていた等)。 */
export function select(root, key) {
  const tabs = barTabs(root);
  const target = tabs.find((t) => t.dataset.key === key);
  if (!target) return false;

  for (const tab of tabs) {
    const on = tab === target;
    tab.setAttribute("aria-selected", String(on));
    // 選ばれていない面はタブ順から外す。Tab キーは**タブ列を1つ**として
    // 扱い、中の移動は矢印キー ── これが tablist の作法
    tab.tabIndex = on ? 0 : -1;
    const id = tab.getAttribute("aria-controls");
    const panel = id ? document.getElementById(id) : null;
    if (panel) panel.hidden = !on;
  }
  remember(root.dataset.tabs, key);
  root.dispatchEvent(new CustomEvent("tab:select", { detail: { key },
                                                     bubbles: true }));
  return true;
}

/** いま選ばれている面のキー。 */
export function current(root) {
  const on = root.querySelector(':scope > .tabs__bar > .tab[aria-selected="true"]');
  return on ? on.dataset.key : "";
}

function move(root, from, step) {
  const tabs = barTabs(root);
  if (!tabs.length) return;
  const at = tabs.indexOf(from);
  const next = tabs[(at + step + tabs.length) % tabs.length];
  select(root, next.dataset.key);
  next.focus();
}

/** 1組の面を動かす。すでに動かしてあるものは触らない。 */
function attach(root) {
  if (!root || root.dataset.tabsReady === "1") return;
  root.dataset.tabsReady = "1";

  // `closest()` に `:scope` は効かない(呼び出した要素自身を指すため、
  // `:scope > …` は決して一致しない)。素直に `.tab` を拾ってから、
  // **この組のものか**を確かめる ── 面の中に面があっても混ざらない
  const own = (node) => {
    const tab = node.closest?.(".tab");
    return tab && tab.parentElement
        && tab.parentElement.parentElement === root ? tab : null;
  };

  root.addEventListener("click", (event) => {
    const tab = own(event.target);
    if (tab) select(root, tab.dataset.key);
  });

  root.addEventListener("keydown", (event) => {
    const tab = own(event.target);
    if (!tab) return;
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (step) { event.preventDefault(); move(root, tab, step); return; }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      const tabs = barTabs(root);
      const target = event.key === "Home" ? tabs[0] : tabs[tabs.length - 1];
      if (!target) return;
      select(root, target.dataset.key);
      target.focus();
    }
  });

  // 外から名指しされた面が最優先、次に前回見ていた面、
  // 無ければ**サーバが既定にした**もの
  const wanted = requested();
  if (wanted && select(root, wanted)) return;
  const saved = recall(root.dataset.tabs);
  if (!saved || !select(root, saved)) {
    const first = root.querySelector(':scope > .tabs__bar > .tab[data-default="1"]')
               || root.querySelector(":scope > .tabs__bar > .tab");
    if (first) select(root, first.dataset.key);
  }
}

/** 画面の中の面を全部動かす。 */
export function attachAll(scope = document) {
  for (const root of scope.querySelectorAll(".tabs")) attach(root);
}
