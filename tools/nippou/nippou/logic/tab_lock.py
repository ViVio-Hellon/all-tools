"""打てるタブは1つだけ ── 同じ日報を2枚のタブで開かせない

【何が起きていたか】
このアプリはブラウザで動くので、**タブは何枚でも開けます。** 2枚開くと
どちらにも打ててしまい、保存すると後から押したほうが勝ちます。

    タブA: 1〜6行目を打って保存      → DBには6行
    タブB: (Aを開く前の画面のまま)
           7行目だけ打って保存       → DBは**Bが持っていた形**で上書き
                                      → Aの6行が消える

消えたことは誰にも見えません。タブBの画面は正常で、タブAも自分が保存した
ときのままに見えています。気づくのは翌日「打ったはずの直が無い」という
形です ── **アプリを2つ起動したときとまったく同じ壊れ方**で、こちらの
ほうがずっと起きやすい(タブは指1本で増えます)。

【どう決めるか】
「いま打てるのは1枚」とだけ決めます。開くこと自体は止めません ── 見る
だけなら2枚目に意味があります(前の直を見ながら打つ、など)。

    1枚目を開く          → 打てる(editor)
    2枚目を開く          → 見るだけ(viewer)。打ちたければ押して奪う
    1枚目を閉じる/落ちる → 2枚目が次の心拍で打てるようになる

**奪うのは押したときだけ。** 勝手に移すと、打っている最中に後ろのタブへ
権利が移ることがあります。

【裏に回ったタブは、権利を持ったまま】(v3.79.0)
ブラウザは**見えていないタブのタイマーを間引きます**(Chrome は隠れて5分
たつと1分に1回、Edge のスリープ中のタブやメモリセーバーは止める)。5秒
ごとの心拍が1分に1回になると、15秒で「閉じられた」とみなされ、

    タブA(打っていた)を裏に回して、タブBでグラフを見る
      → Aの心拍が間引かれて15秒で外れる
      → Bの心拍が先に届き、**押してもいないのに**Bへ権利が移る
      → Aに戻ると「この日報は、別のタブで開いています」

になっていました。**奪うのは押したときだけ**、という決まりを、ブラウザの
都合で破っていたことになります。

そこで、画面は**裏に回る瞬間に知らせます**(`hide`)。裏に回ったタブは、
心拍が来なくても `HIDDEN_KEEP_SEC` のあいだは居るものとみなし、権利も
動かしません。閉じたときは `release` がその場で届きます。

もう1つ、**裏のタブは空いた権利を拾いません。** 打てる側が閉じたあと、
次に心拍を送ったタブが引き継ぎますが、それが見えていないタブだと、
見えているほうのタブが「見るだけ」になります。拾うのは見えているタブ
だけです(誰も持っていなければ、書き込みはどのタブからでも通ります)。

【引き継いだタブの画面は古い】(v3.91.0)
権利が移るとき、移った先のタブの表は**そのタブを開いた時点の中身**です。

    タブB を開く(1〜3行目)          ← B は見るだけ
    タブA で4行目を打って保存
    タブA を閉じる                   → B が次の心拍で打てる側になる
    B で5行目を打つ → 自動保存       → B の表(1〜3行目+5行目)で上書き
                                       → **A の4行目が消える**

画面はそのとき読み直しますが(`tab_lock.js`)、読み直す前に飛んだ要求や、
戻る/進むで出てきた古い画面は画面だけでは止められません。そこで
**最後に書いたタブ**を覚え、「自分の画面を描いたあとに、ほかのタブが
書いている」タブからの書き込みは断ります(`stale`)。画面は断られたら
読み直します。描いた時刻は画面が `claim` で渡します(`loaded`)。

【なぜ時刻を渡してもらうのか】
ここは「いま何秒か」を自分では見ません。呼ぶ側が渡します ── そうしないと
「20秒経ったら」の試験が本当に20秒待つことになります。渡す時刻は
**スリープを数えない時計**(`nippou/awake_clock.py`)です。Windows では
スリープ中も時計が進むので、フタを開けた瞬間に全部のタブが「15秒
音沙汰なし」で外れていました。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

#: 画面が心拍を送る間隔(秒)。画面側もこの値を使う
HEARTBEAT_SEC = 5.0

#: これだけ音沙汰が無ければ、そのタブは閉じられたものとみなす。
#: 心拍の3回ぶん ── 1回や2回の取りこぼし(重い保存の最中など)で
#: 権利が動くと、打っている人の足元が崩れる
LOST_AFTER_SEC = HEARTBEAT_SEC * 3

#: 裏に回ったタブを、心拍が無くても居るものとみなす長さ(秒)。
#: **ブラウザが止めたタブからは何も来ない**ので、短く見切ると権利が勝手に
#: 動く。自動終了の見張り(`idle_exit.HIDDEN_KEEP_SEC`)と同じ7日
HIDDEN_KEEP_SEC = 7 * 24 * 3600.0

#: 覚えておく「画面を描いた時刻」の数。閉じ際の合図が届かなかった
#: タブのぶんが溜まらないよう、これを超えたら古いものから捨てる
MAX_STAMPS = 200

#: 役割。画面はこの文字で分岐する
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"


@dataclass
class Tab:
    """開いているタブ1枚。"""

    token: str
    opened_at: float
    seen_at: float
    #: 裏に回っている(見えていない)。ブラウザが心拍を間引く・止める
    hidden: bool = False

    def alive(self, now: float) -> bool:
        limit = HIDDEN_KEEP_SEC if self.hidden else LOST_AFTER_SEC
        return (now - self.seen_at) <= limit


@dataclass
class Verdict:
    """そのタブがいま何をしてよいか。"""

    role: str = ROLE_EDITOR
    #: ほかに開いているタブの数(自分は数えない)
    others: int = 0
    #: 打てるタブが自分でないとき、そちらが開かれてからの秒数
    editor_age: float = 0.0
    #: 打てるタブが自分でないとき、そちらが裏に回っているか
    editor_hidden: bool = False
    #: このタブの画面は、ほかのタブの書き込みより古い(`TabDesk.stale`)。
    #: 打てる側でこれが立っていたら、画面は読み直す
    stale: bool = False

    @property
    def may_edit(self) -> bool:
        return self.role == ROLE_EDITOR

    @property
    def note(self) -> str:
        """見るだけの画面に添える一言。**打てるタブが裏に回っているとき**だけ。

        裏に回ったタブは権利を持ったままなので、閉じずに裏へ置いてきた人は
        「どこにも開いていないのに見るだけ」に見えます。どこに居るかを言う。
        """
        if self.may_edit or not self.editor_hidden:
            return ""
        return ("打てるタブは、いま裏に回っています(別のタブ・最小化した窓)。"
                "そちらへ戻れば続きを打てます。閉じてしまった・見つからない"
                "ときは、下のボタンでこのタブへ移してください")

    def as_dict(self) -> dict:
        return {"role": self.role, "may_edit": self.may_edit,
                "others": self.others, "editor_age": round(self.editor_age, 1),
                "editor_hidden": self.editor_hidden, "note": self.note,
                "stale": self.stale,
                "heartbeat_sec": HEARTBEAT_SEC}


@dataclass
class TabDesk:
    """開いているタブを覚えておいて、打てる1枚を決める。

    **プロセスの中だけで覚えます。** アプリを立て直せば忘れますが、
    立て直せば開いていたタブはどのみち繋がらないので、それでよい。
    """

    tabs: dict[str, Tab] = field(default_factory=dict)
    editor: str = ""
    #: タブごとの「画面を描いた時刻」。**`tabs` とは別に持つ** ── 裏で
    #: 開いたタブは `tabs` に入らない(`_touch`)が、描いた時刻は要る
    loaded: dict[str, float] = field(default_factory=dict)
    #: 最後に書いたタブと時刻。**プロセスの中だけ**(立て直せば忘れる ──
    #: 立て直したあとは誰の画面も同じDBから描かれている)
    last_write: tuple[str, float] = ("", 0.0)

    # -- 中で使うもの ---------------------------------------------
    def _sweep(self, now: float) -> None:
        """音沙汰の無いタブを落とす。**打てる側も落とす。**"""
        for token in [t for t, tab in self.tabs.items() if not tab.alive(now)]:
            del self.tabs[token]
        if self.editor and self.editor not in self.tabs:
            self.editor = ""

    def _verdict(self, token: str, now: float) -> Verdict:
        if self.editor == token:
            return Verdict(role=ROLE_EDITOR, others=max(0, len(self.tabs) - 1),
                           stale=self.stale(token, now=now))
        holder = self.tabs.get(self.editor)
        age = (now - holder.opened_at) if holder else 0.0
        return Verdict(role=ROLE_VIEWER, others=max(0, len(self.tabs) - 1),
                       editor_age=age,
                       editor_hidden=bool(holder and holder.hidden))

    def _touch(self, token: str, now: float, hidden: bool) -> None:
        tab = self.tabs.get(token)
        if tab is None:
            if hidden:
                # **裏からの知らないタブは覚えない。** 閉じたタブの送りかけの
                # 心拍が、明け渡し(`release`)のあとに着くことがある ──
                # 覚えると、閉じたタブが「裏に回ったタブ」として7日残る。
                # 本当に開いているなら、表に出たときの心拍で名乗ります
                return
            self.tabs[token] = Tab(token=token, opened_at=now, seen_at=now)
        else:
            tab.seen_at = now
            tab.hidden = hidden

    # -- 外から呼ぶもの -------------------------------------------
    def _stamp(self, token: str, now: float, loaded: Optional[float]) -> None:
        """画面を描いた時刻を覚える。**渡されなければ、いま。**"""
        if loaded is None or loaded > now:
            # 立て直す前のプロセスの時計で描かれた画面は、今の時計と
            # 比べられない。いまとみなす(立て直したあとの書き込みは無い)
            loaded = now
        self.loaded[token] = loaded
        # 閉じたタブの時刻を溜めない。古いものから捨てる
        while len(self.loaded) > MAX_STAMPS:
            del self.loaded[next(iter(self.loaded))]

    def claim(self, token: str, *, now: float, hidden: bool = False,
              loaded: Optional[float] = None) -> Verdict:
        """タブが開いた。**空いていれば打てる、居れば見るだけ。**

        裏で開いたタブ(`hidden=True`)は空いていても拾いません。見えている
        タブのほうが先に来るとは限らないので、拾わせると見えているほうが
        「見るだけ」になります。表に出たときの心拍で拾います。
        """
        self._sweep(now)
        self._touch(token, now, hidden)
        self._stamp(token, now, loaded)
        if not self.editor and not hidden:
            self.editor = token
        return self._verdict(token, now)

    def ping(self, token: str, *, now: float, hidden: bool = False) -> Verdict:
        """心拍。**知らないタブなら、その場で開いたものとして扱う。**

        アプリを立て直したあとの画面がここへ来ます。断ると、画面は
        理由の分からないまま打てなくなります。

        `hidden=True` は裏に回ったタブの心拍(ブラウザが間引いたもの)。
        居ることは覚え直しますが、空いた権利は拾いません。
        """
        self._sweep(now)
        if token not in self.tabs:
            # 描いた時刻は**覚えているほうを使う**(裏で開いた・15秒黙って
            # 落ちたタブ)。いまで上書きすると、古い画面が新しく見える
            return self.claim(token, now=now, hidden=hidden,
                              loaded=self.loaded.get(token))
        self._touch(token, now, hidden)
        if not self.editor and not hidden:
            # 打てる側が落ちた。**次に心拍を送った(見えている)タブが引き継ぐ**
            self.editor = token
        return self._verdict(token, now)

    def hide(self, token: str, *, now: float) -> None:
        """タブが裏に回った(`sendBeacon`)。**権利は持ったまま。**

        この先ブラウザは心拍を間引く・止めるので、`HIDDEN_KEEP_SEC` の
        あいだは音沙汰が無くても居るものとみなします。

        知らないタブは覚えません ── アプリを立て直したあとに裏の合図だけ
        来たものを「開いた」扱いにする理由がありません(表に出れば心拍で
        名乗ります)。
        """
        self._sweep(now)
        tab = self.tabs.get(token)
        if tab is not None:
            tab.seen_at = now
            tab.hidden = True

    def take(self, token: str, *, now: float) -> Verdict:
        """「このタブで入力する」を押した。**押したときだけ動かす。**"""
        self._sweep(now)
        self._touch(token, now, False)
        self.editor = token
        return self._verdict(token, now)

    def release(self, token: str, *, now: float) -> None:
        """タブが閉じた。**待たずに次へ渡す。**

        落ちた場合は心拍が途切れるまで分かりませんが、ふつうに閉じた
        ときはその場で分かります ── 20秒黙って待たせない。
        """
        self.tabs.pop(token, None)
        self.loaded.pop(token, None)
        if self.editor == token:
            self.editor = ""
        self._sweep(now)

    def note_write(self, token: str, *, now: float) -> None:
        """書き込みが通った。**誰が書いたか**を覚える(`stale` が見る)。"""
        self.last_write = (token, now)

    def stale(self, token: str, *, now: float) -> bool:
        """このタブの画面は、ほかのタブの書き込みより古いか。

        古い画面のまま書かせると、ほかのタブが書いた行を**古い表で
        上書きして消します**(冒頭「引き継いだタブの画面は古い」)。

        名札の無い要求・描いた時刻を知らないタブは見ません ── 表を
        持っていない(記録やグラフの画面から来た)か、アプリの外からの
        要求で、比べる相手がありません。
        """
        writer, at = self.last_write
        if not token or not writer or writer == token:
            return False
        drawn = self.loaded.get(token)
        return drawn is not None and at > drawn

    def may_edit(self, token: str, *, now: float) -> bool:
        """この token は書いてよいか。**書き込みの口が最後に見る。**

        画面を無効にするだけでは足りません ── 無効にしたのは見た目で、
        要求そのものは止まっていないからです(戻る/進む、開きっぱなしの
        古いタブ、二重送信)。
        """
        self._sweep(now)
        if not self.editor:
            # 誰も居ない。**断らない** ── 心拍より先に保存が飛ぶことが
            # あり(打ち終わって即保存)、そこで断ると打った分が消える
            return True
        return self.editor == token


_DESK: Optional[TabDesk] = None


def get_desk() -> TabDesk:
    """このプロセスの1つ。"""
    global _DESK
    if _DESK is None:
        _DESK = TabDesk()
    return _DESK


def reset_desk() -> None:
    """試験用に忘れる。"""
    global _DESK
    _DESK = None
