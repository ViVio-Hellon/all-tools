"""記録を見る / 印刷 (tkinter版 `ui/print_preview_window.py`)

    GET  /records             画面(記録を見る・呼び出す)
    GET  /print               むかしのURL。`/records` へ転送する
    POST /api/print/load      対象を読み込んで、明細の下見を返す
    GET  /report/nippou       印刷用HTML(**上の「印刷する」か Ctrl+P で刷る**)
                              `page=all` でその直ぜんぶを1つの窓に
    GET  /api/records/backup         控え(LocalBackup)にある直(ライン指定)
    POST /api/records/backup/open    控えの直を手元へ入れて、日報入力に呼び出す

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
from nippou.logging_setup import get_logger, log_button_click
from nippou.logic.shift import SHIFT_NAMES
from nippou.reporting import print_format

from .. import error_body, get_repo, shell

log = get_logger("app.routes.printing")

bp = Blueprint("printing", __name__)

#: 刷る直の選択肢。名前と並びは `logic/shift.SHIFT_NAMES` が持つ
SHIFT_CHOICES: tuple[str, ...] = SHIFT_NAMES


def _aggregate_of(report_date: str, line: str, shift: str) -> dict:
    """紙の頭に載せる、その直の集計。**刷れないよりは集計なしで刷る。**

    残してある値を読むだけですが、まだ集計の無い直(この仕組みより前に
    保存されたぶん)ではその場で作ります。そこで何かあっても、刷るのを
    止める理由にはなりません ── 明細は手元にあるので、記録だけ残して
    集計なしの紙を出します。
    """
    from nippou.services import summary as summary_service

    try:
        found = summary_service.for_date(get_repo(), report_date, line)
        record = next((r for r in found if r.shift == shift), None)
        if record is None:
            return {}
        rows = summary_service.shift_lot_rows(get_repo(), record)
        return {"summary": record, "stops": summary_service.stop_items(rows)}
    except Exception:                     # noqa: BLE001 - 紙は出す
        log.exception("印刷に載せる集計の取得に失敗しました %s/%s/%s",
                      report_date, line, shift)
        return {}


#: `page` にこれを渡すと「その直ぜんぶ」
ALL_PAGES = "all"


def _page_rows() -> list[dict]:
    """一覧のもとになるページ1行ずつ。**未送信を先に、そのあと新しい順。**

        2026年9月15日 L-1 1直 1ページ 1行目 … 梱包数 20 が検入枚数 15 を
        超えています
        直してほしいといいつつないから直せないんですが

    `saved_keys` は**保存の新しい順に200件まで**で、そこから直を20まで
    しか拾っていませんでした。取り込みで同じ時刻の紙が何十枚も入ると、
    古い未送信の直は溢れて一覧に出ません ── 名指しで「直してください」
    と言われたページが、どこにも無い、という形になります。

    未送信は別に引いて先に置きます。**数は知れています**(片付ければ
    消えるものなので)。
    """
    repo = get_repo()
    unsent = sorted(
        ({"report_date": h.report_date, "line": h.line, "shift": h.shift,
          "page": int(h.page), "saved_at": h.saved_at, "synced": False}
         for h in repo.pending_sync_headers()),
        key=lambda r: str(r["saved_at"] or ""), reverse=True)
    return unsent + list(repo.saved_keys())


def _recent_shifts(limit: int = 20) -> list[dict]:
    """保存された直を、**ページではなく直の単位で**並べる。

    `saved_keys` はページごとに1行返すので、3ページある直は3行に見えます。
    見る人・呼び出す人が選びたいのは直なので、ここでまとめてページの一覧を
    添えます。

    並びは**共有へ未送信が先、その中は新しい順**。片付ける順です ──
    そして `limit` で切るのは「済」のほうだけ:**未送信は古くても必ず
    出します**(直せと言われたものが一覧に無いと、直しようがない)。

    **ラインで絞りません。** 「いまのライン以外は出てこない」と、他の
    設備の記録を見たい人が行き止まりになります。

    `synced` は**その直の全ページが共有へ渡ったか**。1ページでも残っていれば
    「未」です ── 「済」と出ているのに一部だけ送られていない、が
    いちばん困ります。
    """
    found: dict[tuple[str, str, str], dict] = {}
    sent_shifts = 0
    for row in _page_rows():
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
        row["problems"] = [
            (f"{f.page}ページ {f.row}行目 " if f.page and f.row else "") + f.message
            for f in found]


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


def _missing(report_date: str, line: str, shift: str) -> Response:
    log.info("印刷用HTML: 該当なし %s/%s/%s", report_date, line, shift)
    return Response(
        "<!doctype html><meta charset='utf-8'>"
        "<p style='font-family:sans-serif;padding:40px'>"
        "該当するデータが見つかりません。</p>",
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
    ctx = work_context.get_context()
    from .entry import build_service, current_calculator
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
    from .entry import current_flow

    # どの行が**いま自分が入力している直**か。**比べ方はサービスに聞く**
    # (`is_current_key`)── ここで自分で比べると、日報入力のページ移動で
    # 通るものと、この画面で押せるものが食い違います。
    #
    # 「押せるか」ではなく「自分の直か」を持つのが要点です。管理者は
    # どの行も押せますが、**昨日の直を「ページを直す」とは呼びません**
    now_shift, now_date = service.current_shift_info(
        datetime.now(), ctx.force_day_shift())
    recent = _recent_shifts()
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
        # **いまの直**。ここから紙・表・グラフへ1押しで行けるように
        current={"report_date": report_date, "line": line, "shift": shift,
                 "pages": saved},
        default={"report_date": report_date, "line": line,
                 "shift": shift, "page": saved[0] if saved else 1},
        # 直近に保存されたものを選びやすくする(「どれを見るか」を
        # 思い出す作業を無くす)。**直の単位で**、ページは添え物
        recent=recent,
        **shell.shell_context("records", ribbon=ctx.ribbon(calc)))


@bp.post("/api/print/load")
def load():
    """対象を読み込んで下見を返す。**刷る前に中身を確かめられるように。**

    `pages` に、その直に**実際にあるページ**を入れて返します ── ページ番号を
    当てずっぽうで打たせないため。歯抜け(ページ1と3だけ)も、そのまま見えます。
    """
    report_date, line, shift, page = _key_from(request.get_json(silent=True) or {})
    if not (report_date and line and shift):
        return jsonify(error_body("empty", "報告日・ライン・直を指定してください")), 400

    saved, loaded, from_backup = _load_any(report_date, line, shift, page)
    if loaded is None:
        return jsonify({
            "found": False, "pages": saved,
            "message": (f"ページ{page} は保存されていません(この直にあるのは "
                        + "・".join(f"{n}ページ" for n in saved) + ")")
            if saved else "該当するデータが見つかりません。"})

    header, details = loaded
    return jsonify({
        "found": True,
        # 手元に無く、控え(LocalBackup)から読んだか
        "from_backup": from_backup,
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
    """その直で後から作ったページの印(ページ → 字)。**紙にも残す**(v4.0.0)。"""
    from nippou.logic import backfill as backfill_rule
    try:
        found = get_repo().backfills(report_date, line, shift)
    except Exception:                             # noqa: BLE001 - 印が出ないだけ
        log.exception("後日作成の印を読めませんでした")
        return {}
    return {key[3]: backfill_rule.stamp_text(info) for key, info in found.items()}


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

    if _wants_all(args):
        pages = _all_pages(report_date, line, shift)
        if not pages:
            return _missing(report_date, line, shift)
        log_button_click("print_report", line=line,
                         extra=f"{report_date}/{shift}/全{len(pages)}ページ")
        return Response(
            print_format.build_shift_html(pages, generated_at=now, stamps=stamps,
                                          **aggregate),
            mimetype="text/html")

    _, loaded, _ = _load_any(report_date, line, shift, page)
    if loaded is None:
        return _missing(report_date, line, shift)

    header, details = loaded
    log_button_click("print_report", line=line, extra=f"{report_date}/{shift}/{page}")
    return Response(
        print_format.build_print_html(header, details, generated_at=now,
                                      stamp=stamps.get(int(page), ""), **aggregate),
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


def _load_any(report_date: str, line: str, shift: str, page: int):
    """(その直のページ, そのページ, 控えから読んだか)。**手元が先、無ければ控え。**"""
    from nippou.services import local_backup

    repo = get_repo()
    saved = repo.saved_pages(report_date, line, shift)
    loaded = repo.load(report_date, line, shift, page)
    if saved or not _may_read_backup(work_context.get_context(), line):
        return saved, loaded, False
    try:
        with local_backup.reader(line) as backup:
            if backup is None:
                return saved, loaded, False
            return (backup.saved_pages(report_date, line, shift),
                    backup.load(report_date, line, shift, page), True)
    except Exception:                             # noqa: BLE001 - 控えが読めないだけ
        log.exception("控えを読めませんでした %s/%s/%s", report_date, line, shift)
        return saved, loaded, False


def _all_pages(report_date: str, line: str, shift: str) -> list:
    """その直の全ページ(手元が先、無ければ控え)。"""
    from nippou.services import local_backup

    repo = get_repo()
    numbers = repo.saved_pages(report_date, line, shift)
    if numbers:
        return [p for p in (repo.load(report_date, line, shift, n) for n in numbers) if p]
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
