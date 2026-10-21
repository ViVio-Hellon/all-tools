"""記録を見る / 印刷 (tkinter版 `ui/print_preview_window.py`)

    GET  /records             画面(記録を見る・呼び出す)
    GET  /print               むかしのURL。`/records` へ転送する
    POST /api/print/load      対象を読み込んで、明細の下見を返す
    GET  /report/nippou       印刷用HTML(**上の「印刷する」か Ctrl+P で刷る**)
                              `page=all` でその直ぜんぶを1つの窓に
    GET  /api/records/backup         控え(LocalBackup)にある直(ライン指定)
    POST /api/records/backup/open    控えの直を手元へ入れて、日報入力に呼び出す
    POST /api/records/lot-search     ロットを探す(期間・ロット№の頭の数桁)
    POST /api/records/lot-search/csv 同じ表を出力先のフォルダへCSVに書く

【ロットを探す(v4.33.0)】
期間とロット№(頭の数桁だけでも)から、保存した日報の行を探します。当たった行から
「紙を見る」「下見」「呼び出す」(管理者)へは、この画面の既存の道をそのまま使います。
探し方・出どころ・上限・ラインの決め方は `presenters/lot_search.py`。探す範囲は
**共有の日報データ(全 PC が送った行)+ この端末のまだ送っていない行**で、共有を
読めなければ手元と控え(`services/shared_reports`)。ラインの欄は全員に出し、
「全ライン」も選べます(見るだけ。呼び出すのは今の決まりのまま)。

【共有にしか無い直の紙・下見】
ロットを探すは共有から行を拾うので、**この端末にも控えにも無い直**が当たります
(ほかのラインの PC が送った直・VBA の直)。紙と下見は、手元 → 控え(読めるときだけ)
→ 共有の日報データ の順に探して出します。共有は読むだけです。

【保存した直はラインを選んで見る(v4.35.3)】

    記録を見る:保存した直:Administrator であれば全ラインが見れるが全ライン表示している見づらい。
    ほかのPCの日報のようにライン選択させてください

「保存した直」は手元の作業用DBの直を並べます。管理者の PC は控えから入れた直・取り込んだ
直で**ほかのラインのページも持っている**ので、全部が1つの一覧に混ざっていました。
Administrator(アクセス権限の表)と管理者モードの PC には「ほかのPCの日報」と同じ形の
ラインの欄を出し、**選んだラインの直だけ**を出します(混ぜない・「全ライン」は無い ──
ほかのPCの日報の欄に無いので)。既定はこの端末のライン、選んだラインは `?line=` で受けて
サーバが覚えます(`WorkContext.records_line`。画面を移って戻っても選び直させない)。
ほかの PC には欄を出さず、この端末のラインの直だけです(名指しされても)。

ラインで絞るのは `_page_rows` の1か所だけ(未送信も済も、20直の数え方もそこから)。
ほかのラインの行の「呼び出す」は、ロットを探すと同じく**控えから入れて開きます**
(`/api/records/backup/open`。手元のほうが新しいページはそのまま)。

【手元に無ければ控えを見る(v4.12.0)】
下見と紙は、手元に無い直なら控え(`services/local_backup`)から出します。
見られる控えは**この端末のライン**のぶん。アクセス権限の表で Administrator の
PC(と管理者モード)は**どのラインの控えも**見られ、「呼び出して直す」で直せます
── 管理者が現地へ行かずに直すため。直したあとの「共有へ保存」は、
その日報のラインの表へ送られます(送り先はページのラインで決まる)。

【紙は任意。ただし「いつでも見られる」ことは要る】
刷るのは**作業者が欲しいときだけ**。保存とは別のボタンで、押さなければ
1枚も出ません。直の終わりの確定(`services/shift_close.py`)も紙を作らず、
刷ったからといって確定したことにもなりません ── 途中で1枚刷っただけで
催促が止まると、そのあとに打った行が確かめられないまま直が終わります。

**出すのをやめたのは「自動で出すこと」だけです。** 紙そのものは
保存済みの明細から**その場で組み立てる**ので、保存してある直なら
いつでも・何度でも同じものが出ます ── 貯めたファイルを開いているの
ではないので、消えることも古くなることもありません。

【なぜHTMLを別窓で出すのか】
PDF生成のライブラリを足さずに「任意のタイミングで印刷」を実現するため。
ブラウザの印刷機能がそのまま使えるので、プレビューで確かめてから刷れる。
レイアウトは `reporting/print_format.py` が持つ(tkinter版と同じもの)。

【紙の頭に集計が載る】
VBA は印刷フォーマットと集計フォーマットが別のシートで、別々に出て
いました。ここでは残してある集計(`services/summary.py`)を明細の上に
はさみます ── 刷った紙だけを見る人に、その直がどうだったかが伝わる
ように。
"""
from __future__ import annotations

from datetime import datetime

from flask import (Blueprint, Response, jsonify, redirect, render_template,
                   request)

from nippou import constants, work_context
from nippou.config import SETTINGS
from nippou.logging_setup import get_logger, log_button_click
from nippou.logic.shift import SHIFT_NAMES
from nippou.reporting import csv_export, print_format

from .. import error_body, get_repo, shell

log = get_logger("app.routes.printing")

bp = Blueprint("printing", __name__)

#: 刷る直の選択肢。名前と並びは `logic/shift.SHIFT_NAMES` が持つ
SHIFT_CHOICES: tuple[str, ...] = SHIFT_NAMES


def _aggregate_of(report_date: str, line: str, shift: str) -> dict:
    """紙の頭に載せる、その直の集計(`services/paper_files.aggregate_of` の1か所)。"""
    from nippou.services import paper_files

    return paper_files.aggregate_of(get_repo(), report_date, line, shift)


#: `page` にこれを渡すと「その直ぜんぶ」
ALL_PAGES = "all"


def save_paper(report_date: str, line: str, shift: str, pages=None, *,
               generated_at: str = "") -> dict:
    """その直の紙(印刷用HTML)を出力先へ残す。**残す道は `services/paper_files` の1本**(v4.39.2)。

    ここは画面の道ぶんだけ ── `pages` を省くと、紙を見るときと同じ順(手元 → 控え → 共有の
    日報データ。`_all_pages`)で全ページを探してから渡します。
    """
    from nippou.services import paper_files

    if pages is None:
        try:
            pages, _why = _all_pages(report_date, line, shift)
        except Exception as exc:                  # noqa: BLE001 - 印刷は止めない
            log.exception("印刷用HTMLにするページを読めませんでした %s/%s/%s", report_date, line, shift)
            return {"files": [], "errors": [str(exc)], "dir": ""}
    return paper_files.save_shift(get_repo(), report_date, line, shift, pages,
                                  generated_at=generated_at)


def saved_note(result: dict) -> str:
    """`save_paper` の結果を一言に(`services/paper_files.saved_note`)。"""
    from nippou.services import paper_files

    return paper_files.saved_note(result)


def _page_rows(line: str) -> list[dict]:
    """一覧のもとになる**そのラインの**ページ1行ずつ。**未送信を先に、そのあと新しい順。**

        2026年9月15日 L-1 1直 1ページ 1行目 … 梱包数 20 が検入枚数 15 を
        超えています
        直してほしいといいつつないから直せないんですが

    `saved_keys` は**保存の新しい順に200件まで**で、そこから直を20まで
    しか拾っていませんでした。取り込みで同じ時刻の紙が何十枚も入ると、
    古い未送信の直は溢れて一覧に出ません ── 名指しで「直してください」
    と言われたページが、どこにも無い、という形になります。

    未送信は別に引いて先に置きます。**数は知れています**(片付ければ
    消えるものなので)。

    **ラインで絞るのはここだけ**(v4.35.3)。済の200件もそのラインの中で数えるので、
    ほかのラインの保存が多くても、選んだラインの直が溢れません。
    """
    repo = get_repo()
    unsent = sorted(
        ({"report_date": h.report_date, "line": h.line, "shift": h.shift,
          "page": int(h.page), "saved_at": h.saved_at, "synced": False}
         for h in repo.pending_sync_headers() if h.line == line),
        key=lambda r: str(r["saved_at"] or ""), reverse=True)
    return unsent + list(repo.saved_keys(line))


def _recent_shifts(line: str, limit: int = 20) -> list[dict]:
    """そのラインの保存された直を、**ページではなく直の単位で**並べる。

    `saved_keys` はページごとに1行返すので、3ページある直は3行に見えます。
    見る人・呼び出す人が選びたいのは直なので、ここでまとめてページの一覧を
    添えます。

    並びは**共有へ未送信が先、その中は新しい順**。片付ける順です ──
    そして `limit` で切るのは「済」のほうだけ:**未送信は古くても必ず
    出します**(直せと言われたものが一覧に無いと、直しようがない)。

    **1度に1ライン**(v4.35.3)。前は絞らずに全ラインを混ぜていましたが、管理者の
    PC では見づらい一覧になりました。ほかのラインは、Administrator・管理者モードなら
    ラインの欄で選んで出します(`_saved_line_view`)。ほかの PC はこの端末のラインだけ
    ── ほかの設備の記録を見るのは「ロットを探す」(ラインを選べる・見るだけ)です。

    `synced` は**その直の全ページが共有へ渡ったか**。1ページでも残っていれば
    「未」です ── 「済」と出ているのに一部だけ送られていない、が
    いちばん困ります。
    """
    found: dict[tuple[str, str, str], dict] = {}
    sent_shifts = 0
    for row in _page_rows(line):
        key = (row["report_date"], row["line"], row["shift"])
        got = found.get(key)
        if got is None:
            if row["synced"] and sent_shifts >= limit:
                continue                          # 済はここまで(読みすぎない)
            sent_shifts += bool(row["synced"])
            got = found[key] = {
                "report_date": key[0], "line": key[1], "shift": key[2],
                "pages": [], "saved_at": "", "saved_at_raw": "",
                "synced": True}
        page = int(row["page"])
        if page not in got["pages"]:              # 2つの出どころが重なる
            got["pages"].append(page)
        raw = str(row["saved_at"] or "")
        if raw > got["saved_at_raw"]:             # その直でいちばん新しい保存
            got["saved_at_raw"] = raw
            got["saved_at"] = _when(raw)
        got["synced"] = got["synced"] and bool(row["synced"])
    rows = list(found.values())
    for row in rows:
        row["pages"].sort()
    rows.sort(key=lambda r: r["saved_at_raw"], reverse=True)
    rows.sort(key=lambda r: r["synced"])          # 安定 ── 未送信が先
    return rows


def _mark_problems(rows: list[dict], *, admin: bool) -> None:
    """一覧の各直に、**入力画面と同じ確認**の結果を添える(赤くする・並べ替えるため)。

        入力画面にあるエラーのあるデータは 保存した直 でも赤くしてくれないと探すのが大変です

    確かめ方は日報入力の「この直をチェック」と同じ(`services/shift_check.run`)。
    停止の記号の一覧は1回だけ読みます(直ごとに読むと、共有のマスタを何十回も開く)。
    """
    from nippou.logic.shift import parse_business_date
    from nippou.services import shift_check

    repo = get_repo()
    codes = shift_check.known_stop_codes()
    for row in rows:
        day = parse_business_date(row["report_date"])
        row["date_key"] = day.isoformat() if day else str(row["report_date"])
        try:
            report = shift_check.run(repo, row["report_date"], row["line"], row["shift"],
                                     admin=admin, codes=codes)
            found = list(report.findings)
        except Exception:                         # noqa: BLE001 - 一覧は出す
            log.exception("一覧の確認に失敗しました: %s %s %s",
                          row["report_date"], row["line"], row["shift"])
            found = []
        # **いま打っている直は、直が終わってから決まるもの(最終の時刻・休憩)を
        # 赤にしない。** 入力画面は「直の終わりまでに」と灰色で出すのに、一覧だけ
        # 1行目を打った時点で赤くなっていた
        later = [f for f in found if f.at_shift_end] if row.get("own") else []
        found = [f for f in found if f not in later]
        text = lambda f: ((f"{f.page}ページ {f.row}行目 " if f.page and f.row else "")
                          + f.message)
        row["problems"] = [text(f) for f in found]
        row["later"] = [text(f) for f in later]


def _when(saved_at: object) -> str:
    """保存日時を、目で追える形に。

    DBには `2026-09-11T12:36:23` の形で入っています。表の1列として
    並べるには長すぎるうえ、`T` が読みづらい ── **どの直を選ぶか**の
    判断に要るのは「いつごろ」までなので、月日と時分に落とします。
    読めない値はそのまま出します(勝手に消さない)。
    """
    text = str(saved_at or "")
    if len(text) >= 16 and text[4] == "-" and text[10] in ("T", " "):
        return f"{text[5:10]} {text[11:16]}"
    return text


def _key_from(payload: dict) -> tuple[str, str, str, int]:
    try:
        page = int(payload.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    return (str(payload.get("report_date", "")).strip(),
            str(payload.get("line", "")).strip(),
            str(payload.get("shift", "")).strip(),
            max(1, page))


def _wants_all(payload: dict) -> bool:
    return str(payload.get("page", "")).strip().lower() == ALL_PAGES


def _missing_text(why: str = "") -> str:
    """見つからないときの一言。共有を読めなかったなら、そのわけも(押して何も分からない、にしない)。"""
    text = "該当するデータが見つかりません。"
    if why:
        text += f"({why}、共有からは読めませんでした)"
    return text


def _missing(report_date: str, line: str, shift: str, why: str = "") -> Response:
    from html import escape

    log.info("印刷用HTML: 該当なし %s/%s/%s", report_date, line, shift)
    return Response(
        "<!doctype html><meta charset='utf-8'>"
        "<p style='font-family:sans-serif;padding:40px'>"
        f"{escape(_missing_text(why))}</p>",
        mimetype="text/html", status=404)


@bp.get("/print")
def legacy_print():
    """むかしの `/print`。**記録の画面へ送ります。**

    リンクやお気に入りを壊さないために残してあります ── 404 にすると、
    押した人には「壊れた」としか見えません。
    """
    return redirect("/records")


@bp.get("/records")
def index():
    """記録 ── **保存したものを、見る・呼び出す。**

    ここに集めたのは、VBA でも本ツールでも散らばっていた4つのうちの
    2つです:

        見る    紙(印刷用HTML)・表(集計管理)・グラフ(集計・グラフ)
        呼び出す 過去の直を日報入力に開き直す(管理者モード)

    「呼び出す」は**設定・管理者の中**にありました。日々使うものが、
    めったに触らない設定と同居していると探せません。
    """
    from nippou.presenters import lot_search
    from nippou.presenters import settings as settings_view

    ctx = work_context.get_context()
    from ..current import build_service, current_calculator
    calc = current_calculator()
    service = build_service(ctx, calc)
    report_date, line, shift = ctx.current_key(calc)

    # **呼出中は、それを言う**(`NippouDB_BlockIfRecallMode` 相当)。
    #
    # VBA は印刷ボタン自体を止めていた。こちらは止めない ── 過去データを
    # わざと刷る使い方(`NippouDB_ReprintPast`)があり、この画面は刷る対象を
    # 目に見える形で選ばせるので、止めると正当な操作まで塞ぐことになる。
    #
    # ただし**初期値が呼出中のキーになる**のは危ない。押した人は「今日の
    # ぶん」を刷ったつもりで、先週の2直が出る。だから初期値をそのまま
    # 出したうえで、何を開いているかを画面に書く
    recall_note = ""
    if ctx.recall.active:
        recall_note = (f"{service.recall_info()} を開いています。"
                       "この画面の初期値も、その過去データです。")

    saved = get_repo().saved_pages(report_date, line, shift)
    from ..current import current_flow

    # どの行が**いま自分が入力している直**か。**比べ方はサービスに聞く**
    # (`is_current_key`)── ここで自分で比べると、日報入力のページ移動で
    # 通るものと、この画面で押せるものが食い違います。
    #
    # 「押せるか」ではなく「自分の直か」を持つのが要点です。管理者は
    # どの行も押せますが、**昨日の直を「ページを直す」とは呼びません**
    now_shift, now_date = service.current_shift_info(
        datetime.now(), ctx.force_day_shift())
    # どのラインの直を出すか(v4.35.3)。欄は Administrator・管理者モードだけ
    saved_line = _saved_line_view(ctx, request.args.get("line"))
    recent = _recent_shifts(saved_line["line"])
    # **後から作った直には印**(v4.0.0「後日作ったことがわかる」)
    from nippou.logic import backfill as backfill_rule
    try:
        made_later = get_repo().backfills()
    except Exception:                             # noqa: BLE001 - 印が出ないだけ
        log.exception("後日作成の印を読めませんでした")
        made_later = {}
    for row in recent:
        row["own"] = service.is_current_key(
            row["report_date"], row["line"], row["shift"],
            ctx.line, now_date, now_shift)
        stamps = [f"ページ{key[3]}: {backfill_rule.stamp_text(info)}"
                  for key, info in sorted(made_later.items())
                  if key[:3] == (row["report_date"], row["line"], row["shift"])]
        row["backfill"] = " / ".join(stamps)
        # 「呼び出す」の行き先。**この端末のラインは手元から、ほかのラインは控えから
        # 入れて開く**(ロットを探すと同じ決まり。`presenters/lot_search`)
        row["recall_path"] = (lot_search.RECALL_LOCAL if row["line"] == ctx.terminal_line
                              else lot_search.RECALL_BACKUP)
    _mark_problems(recent, admin=ctx.admin)

    return render_template(
        "records.html",
        # 直ひと回りの動線。**3つの画面で同じ帯**(`presenters/flow.py`)
        flow=current_flow(report_date, line, shift,
                          recall=ctx.recall.active).as_dict(),
        # 並びは定義の表の順、見せる字は正規の呼び名(v4.12.5)
        lines=_line_codes(),
        shifts=SHIFT_CHOICES,
        recall_note=recall_note,
        recall=ctx.recall,
        # 呼び出せるか。管理者モードか、アクセス権限の表で Administrator(v4.12.0)
        admin=ctx.editor,
        backup=_backup_view(ctx),
        # ロットを探す(v4.33.0)。既定の期間とラインの欄はここで決める
        # (`presenters/lot_search`。ラインの欄は全員に、「全ライン」も)
        lot=lot_search.screen(ctx.business_date(calc), ctx.line),
        # 押す前に「どこへ出るか」を出す(集計管理と同じ1行)
        out_dir=settings_view.output_dir_view(),
        # **いまの直**。ここから紙・表・グラフへ1押しで行けるように
        current={"report_date": report_date, "line": line, "shift": shift,
                 "pages": saved},
        default={"report_date": report_date, "line": line,
                 "shift": shift, "page": saved[0] if saved else 1},
        # 直近に保存されたものを選びやすくする(「どれを見るか」を
        # 思い出す作業を無くす)。**直の単位で**、ページは添え物
        recent=recent,
        # 「保存した直」のラインの欄(v4.35.3。Administrator・管理者モードだけ)
        saved_line=saved_line,
        **shell.shell_context("records", ribbon=_ribbon(ctx, calc)))


@bp.post("/api/print/load")
def load():
    """対象を読み込んで下見を返す。**刷る前に中身を確かめられるように。**

    `pages` に、その直に**実際にあるページ**を入れて返します ── ページ番号を
    当てずっぽうで打たせないため。歯抜け(ページ1と3だけ)も、そのまま見えます。
    """
    report_date, line, shift, page = _key_from(request.get_json(silent=True) or {})
    if not (report_date and line and shift):
        return jsonify(error_body("empty", "報告日・ライン・直を指定してください")), 400

    saved, loaded, where, why = _load_any(report_date, line, shift, page)
    if loaded is None:
        return jsonify({
            "found": False, "pages": saved,
            "message": (f"ページ{page} は保存されていません(この直にあるのは "
                        + "・".join(f"{n}ページ" for n in saved) + ")")
            if saved else _missing_text(why)})

    header, details = loaded
    return jsonify({
        "found": True,
        # 手元に無く、控え(LocalBackup)から読んだか / 共有の日報データから読んだか
        "from_backup": where == FROM_BACKUP,
        "from_shared": where == FROM_SHARED,
        "pages": saved,
        "worker": header.worker or "(未入力)",
        "count": header.count,
        "weight_kg": header.weight_kg,
        "rows": [
            {"row_no": d.row_no, "lot": d.lot, "zai": d.zai,
             "siz": d.siz, "mai": d.mai, "wei": d.wei}
            for d in sorted(details, key=lambda r: r.row_no) if d.lot
        ],
        "message": "",
    })


def _backfill_stamps(report_date: str, line: str, shift: str) -> dict[int, str]:
    """その直で後から作ったページの印(`services/paper_files.backfill_stamps` の1か所)。"""
    from nippou.services import paper_files

    return paper_files.backfill_stamps(get_repo(), report_date, line, shift)


@bp.get("/report/nippou")
def report():
    """印刷用HTMLそのもの。別窓で開いて、上の「印刷する」(または Ctrl+P)で刷る。

    「印刷する」の帯は v4.6.0 から(それまでは Ctrl+P を知らないと刷れなかった)。
    帯は紙に出ません(`reporting/paper.toolbar_html`)。

    **保存してある直なら、いつでも何度でも同じものが出ます。** 貯めた
    ファイルを開いているのではなく、そのつど明細から組み立てるので。

    `page=all` はその直ぜんぶ。ページのあいだで改ページするので、1回の
    Ctrl+P でページの数だけ紙が出ます ── 3ページある直のために窓を3つ開いて
    3回刷る、では取りこぼします。

    `/report/` はトークンが要る経路(`app/__init__.py`)。業務データが
    そのまま載るので、画面のHTMLと同じ扱いにはしない。
    """
    args = request.args.to_dict()
    report_date, line, shift, page = _key_from(args)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    aggregate = _aggregate_of(report_date, line, shift)
    stamps = _backfill_stamps(report_date, line, shift)
    # 日報入力の「印刷」から開いたときは、出力先へも残す(`save=1`。v4.39.0)。
    # 記録を見るの「紙を見る」・紙の札は見るだけなので残さない
    keep = str(args.get("save", "")).strip() == "1"

    if _wants_all(args):
        pages, why = _all_pages(report_date, line, shift)
        if not pages:
            return _missing(report_date, line, shift, why)
        log_button_click("print_report", line=line,
                         extra=f"{report_date}/{shift}/全{len(pages)}ページ")
        note = saved_note(save_paper(report_date, line, shift, pages, generated_at=now)) if keep else ""
        return Response(
            print_format.build_shift_html(pages, generated_at=now, stamps=stamps,
                                          note=note, **aggregate),
            mimetype="text/html")

    _, loaded, _, why = _load_any(report_date, line, shift, page)
    if loaded is None:
        return _missing(report_date, line, shift, why)

    header, details = loaded
    log_button_click("print_report", line=line, extra=f"{report_date}/{shift}/{page}")
    note = saved_note(save_paper(report_date, line, shift, [loaded], generated_at=now)) if keep else ""
    return Response(
        print_format.build_print_html(header, details, generated_at=now,
                                      stamp=stamps.get(int(page), ""), note=note, **aggregate),
        mimetype="text/html")


# ----------------------------------------------------------------------
# 控え(LocalBackup)── 手元に無ければ控えを見る / Administrator は全ライン (v4.12.0)
# ----------------------------------------------------------------------
def _line_codes() -> list[str]:
    from nippou.logic import line_names
    return line_names.codes()


def _line_label(line: str) -> str:
    from nippou.logic import line_names
    return line_names.label(line)


def backup_unreadable_message(line: str, exc: BaseException) -> str:
    """控えを開けなかったときの文。**どのファイルを・どうすればよいか**まで(v4.18.0)。

        AIM の控えを読めませんでした: invalid uri authority: nlmfangyshrd
        控えを読めませんでした: AIM:
        よくわかりません

    SQLite の言葉(`invalid uri authority`)だけでは何も分かりません。ファイルの
    場所と、確かめる設定の名前を先に書き、元の言葉は最後に添えます。
    """
    from nippou import config
    from nippou.presenters.settings import PROTECTED_LABELS
    from nippou.services import local_backup

    setting = PROTECTED_LABELS.get(config.KEY_BACKUP_DIR, "日報入力データの控えの置き場所")
    return (f"{_line_label(line)} の控えのファイルを開けませんでした"
            f"({local_backup.path_for(line)})。"
            f"参照設定の「{setting}」の場所に届くか(共有フォルダに入れるか)、"
            f"そのファイルがほかで開かれたままになっていないかを確かめてください。"
            f"(詳しく: {exc})")


def _may_read_backup(ctx, line: str) -> bool:
    """そのラインの控えを読んでよいか。**この端末のライン**は誰でも、他は管理者だけ。"""
    return bool(line) and (ctx.editor or line == ctx.terminal_line)


#: 紙・下見をどこから読んだか(`_load_any`)
FROM_LOCAL = "local"
FROM_BACKUP = "backup"
FROM_SHARED = "shared"


def _backup_pages(report_date: str, line: str, shift: str) -> list:
    """控えにあるその直の全ページ。**読めるときだけ**(`_may_read_backup`)。無ければ空。"""
    from nippou.services import local_backup

    if not _may_read_backup(work_context.get_context(), line):
        return []
    try:
        with local_backup.reader(line) as backup:
            if backup is None:
                return []
            return [p for p in (backup.load(report_date, line, shift, n)
                                for n in backup.saved_pages(report_date, line, shift)) if p]
    except Exception:                             # noqa: BLE001 - 控えが読めないだけ
        log.exception("控えを読めませんでした %s/%s/%s", report_date, line, shift)
        return []


def _shared_pages(report_date: str, line: str, shift: str) -> tuple[list, str]:
    """(共有の日報データにあるその直の全ページ, 読めなかったわけ)。**読むだけ。**

    ロットを探すで共有から拾った直(この端末にも控えにも無い)の紙と下見のため。
    ほかのラインでも誰でも読めます(見るだけ。直すのは「呼び出す」の決まりのまま)。
    """
    from nippou.services import shared_reports

    found = shared_reports.shift_pages(report_date, line, shift)
    return found.pages, ("" if found.ok else found.reason())


def _all_pages(report_date: str, line: str, shift: str) -> tuple[list, str]:
    """(その直の全ページ, 共有を読めなかったわけ)。**手元 → 控え → 共有の日報データ。**"""
    repo = get_repo()
    numbers = repo.saved_pages(report_date, line, shift)
    if numbers:
        return [p for p in (repo.load(report_date, line, shift, n) for n in numbers) if p], ""
    pages = _backup_pages(report_date, line, shift)
    if pages:
        return pages, ""
    return _shared_pages(report_date, line, shift)


def _load_any(report_date: str, line: str, shift: str, page: int):
    """(その直のページ, そのページ, どこから読んだか, 共有を読めなかったわけ)。

    **手元 → 控え(読めるときだけ)→ 共有の日報データ**の順。直の単位で決めます
    (手元にその直が1ページでもあれば手元だけ ── これまでどおり)。
    """
    repo = get_repo()
    saved = repo.saved_pages(report_date, line, shift)
    if saved:
        return saved, repo.load(report_date, line, shift, page), FROM_LOCAL, ""
    pages, where, why = _backup_pages(report_date, line, shift), FROM_BACKUP, ""
    if not pages:
        (pages, why), where = _shared_pages(report_date, line, shift), FROM_SHARED
    if not pages:
        return [], None, "", why
    loaded = next((p for p in pages if int(p[0].page) == int(page)), None)
    return [int(h.page) for h, _ in pages], loaded, where, why


def _backup_view(ctx) -> dict:
    """「ほかのPCの日報」の札。**管理者(Administrator・管理者モード)だけに出す。**"""
    from nippou.services import access_rights, local_backup

    if not ctx.editor:
        return {"shown": False}
    try:
        lines = local_backup.lines_available()
        reachable = local_backup.reachable()
    except Exception:                             # noqa: BLE001 - 札に理由を出す
        log.exception("控えの一覧を読めませんでした")
        lines, reachable = [], False
    first = ctx.terminal_line if ctx.terminal_line in lines else (lines[0] if lines else "")
    return {"shown": True, "lines": lines, "line": first, "reachable": reachable,
            "root": str(local_backup.root()),
            "administrator": access_rights.is_administrator()}


def _saved_line_view(ctx, asked: object) -> dict:
    """「保存した直」の**ラインの欄**(v4.35.3)。出すラインもここで決める。

        記録を見る:保存した直:Administrator であれば全ラインが見れるが全ライン表示している見づらい。
        ほかのPCの日報のようにライン選択させてください

    欄を出すのは「ほかのPCの日報」と同じく **Administrator・管理者モード**(`ctx.editor`)。
    ほかの PC は欄を出さず、何を名指しされても**この端末のライン**です。

    選べるのは**手元に保存のあるライン**とこの端末のライン(ラインの並びで)。「全ライン」は
    ありません(ほかのPCの日報の欄に無いので。混ぜない)。名指しが無い・選べないラインなら、
    前に選んだライン(`ctx.records_line`)、それも無ければこの端末のライン。

    **共有へ未送信の直があるラインは、名前に数を添えます。** 一覧は1ラインずつになったので、
    ほかのラインの未送信は欄を開かないと見えません ── 「未送信は古くても必ず出す」を、
    欄の字で守ります(`unsent`)。
    """
    own = ctx.terminal_line
    if not ctx.editor:
        return {"shown": False, "line": own, "own": own, "lines": [], "unsent": []}
    repo = get_repo()
    try:
        have = set(repo.saved_lines())
        pending: dict[str, set] = {}
        for h in repo.pending_sync_headers():
            pending.setdefault(h.line, set()).add((h.report_date, h.shift))
    except Exception:                             # noqa: BLE001 - この端末のラインだけ
        log.exception("保存のあるラインを読めませんでした")
        have, pending = set(), {}
    have.add(own)
    lines = [ln for ln in _line_codes() if ln in have]
    lines += sorted(have - set(lines))            # 定義の表から外れたライン(そのまま出す)
    text = str(asked or "").strip()
    if text in lines:
        ctx.records_line = text                   # 選んだラインを覚える
    line = ctx.records_line if ctx.records_line in lines else own
    return {
        "shown": True, "line": line, "own": own,
        "lines": [{"code": ln, "label": _line_label(ln)
                   + (f"(未送信 {len(pending[ln])}直)" if pending.get(ln) else "")}
                  for ln in lines],
        # ほかのラインの未送信(いま出していないラインだけ。「ラインを選ぶと出ます」に使う)
        "unsent": [f"{_line_label(ln)} {len(pending[ln])}直"
                   for ln in lines if ln != line and pending.get(ln)],
    }


@bp.get("/api/records/backup")
def backup_list():
    """そのラインの控えにある直(新しい順)。"""
    from nippou.services import local_backup

    ctx = work_context.get_context()
    line = str(request.args.get("line", "")).strip()
    if not _may_read_backup(ctx, line):
        return jsonify(error_body(
            "not_admin", "ほかのラインの控えを見るには、アクセス権限の表で Administrator の"
                         "PCか、管理者モードが要ります")), 403
    try:
        shifts = local_backup.list_shifts(line)
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log.exception("%s の控えを開けませんでした: %s", line, local_backup.path_for(line))
        return jsonify(error_body("backup_unreadable", backup_unreadable_message(line, exc))), 500
    repo = get_repo()
    for s in shifts:
        # 手元にもあるか(あれば「呼び出して直す」は手元のほうが新しいページを残す)
        s["in_local"] = bool(repo.saved_pages(s["report_date"], s["line"], s["shift"]))
    return jsonify({"line": line, "shifts": shifts, "path": str(local_backup.path_for(line)),
                    "reachable": local_backup.reachable()})


@bp.post("/api/records/backup/open")
def backup_open():
    """控えの直を**手元へ入れて**、日報入力に呼び出す(直せる状態にする)。

    手元のほうが新しいページ(このPCで直して、まだ控えに届いていないもの)は
    触りません。呼び出しの関門は「呼び出す」と同じ(`settings.recall`)。
    """
    from nippou.services import local_backup

    from .settings import recall

    ctx = work_context.get_context()
    payload = request.get_json(silent=True) or {}
    report_date = str(payload.get("report_date", "")).strip()
    line = str(payload.get("line", "")).strip()
    shift = str(payload.get("shift", "")).strip()
    if not (report_date and line and shift):
        return jsonify(error_body("empty", "呼び出す直を選んでください")), 400
    if not ctx.editor:
        return jsonify(error_body(
            "not_admin", "ほかのPCの日報を直すには、アクセス権限の表で Administrator の"
                         "PCか、管理者モードが要ります")), 403
    try:
        result = local_backup.import_shift(get_repo(), report_date, line, shift)
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log.exception("控えから手元へ入れられませんでした %s/%s/%s", report_date, line, shift)
        return jsonify(error_body("backup_import_failed",
                                  f"控えから入れられませんでした: {exc}")), 500
    if result.error:
        return jsonify(error_body("backup_missing", result.error)), 404
    log_button_click("backup_open", line=line,
                     extra=f"{report_date}/{shift} 入れた{result.pulled}ページ")
    # 呼び出しそのものは「呼び出す」と同じ道(関門を2つ書かない)
    return recall()


# ----------------------------------------------------------------------
# ロットを探す (v4.33.0)
# ----------------------------------------------------------------------
def _lot_search(payload: dict):
    """(結果, 断り)。**どちらか1つだけ。**

    ラインは誰でも選べます(定義のラインか「全ライン」。見るだけ ── `presenters/lot_search`)。
    共有・控えが読めなくても断りにはせず、手元で探した結果にわけを添えて返します。
    """
    from nippou.presenters import lot_search

    ctx = work_context.get_context()
    try:
        line = lot_search.pick_line(payload.get("line"), ctx.line)
        query = lot_search.LotQuery.of(payload, line)
    except lot_search.BadRequest as exc:
        return None, (jsonify(error_body("bad_condition", str(exc), field=exc.field)),
                      exc.status)
    found = lot_search.search(get_repo(), query, own_line=ctx.line, editor=ctx.editor)
    return found, None


@bp.post("/api/records/lot-search")
def lot_search_view():
    """ロットを探す。**表もボタンの行き先もサーバが決めて返す**(画面は写すだけ)。"""
    payload = request.get_json(silent=True) or {}
    found, err = _lot_search(payload)
    if err:
        return err
    log_button_click("lot_search", line=found.query.line_label,
                     extra=f"{found.query.lot} {found.query.start}~{found.query.end} "
                           f"{found.total}行 共有:{found.shared}")
    return jsonify(found.as_dict())


@bp.post("/api/records/lot-search/csv")
def lot_search_csv():
    """探した表を、**画面の表そのまま**CSVにする(BOM付きUTF-8)。

    ダウンロードにはしません。集計管理の「4本ともCSVに出す」と同じく、
    出力先のフォルダ(設定・参照設定の「集計CSV・印刷用HTMLの出力パス」)へ
    書いて、書いた場所を返します。
    """
    from nippou.presenters import lot_search

    payload = request.get_json(silent=True) or {}
    found, err = _lot_search(payload)
    if err:
        return err
    if not found.total:
        return jsonify({"file": "", "message": lot_search.empty_message()})
    query = found.query
    out = csv_export.lot_search_output_path(
        SETTINGS.report_output_dir, query.lot, query.start.isoformat(),
        query.end.isoformat(), query.line_label)
    try:
        count = csv_export.write_table(found.table, out)
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log.exception("ロットを探すのCSVを書けませんでした")
        return jsonify(error_body("write_failed", f"出力に失敗しました: {exc}")), 500
    log_button_click("lot_search_csv", line=query.line_label,
                     extra=f"{query.lot} {query.start}~{query.end} {count}行")
    message = f"{found.table.title} {count}行\n{out}"
    if found.notice:
        message += f"\n{found.notice}"
    if found.too_many:
        message += (f"\n(当たった {found.total}行のうち、画面と同じ先の {count}行です。"
                    "ロット№の桁を増やすか、期間を絞ってください)")
    return jsonify({"file": str(out), "message": message})


def _ribbon(ctx, calc):
    """帯(いま書いているページで。`entry.ribbon_now`)。"""
    from ..current import ribbon_now

    return ribbon_now(ctx, calc)
