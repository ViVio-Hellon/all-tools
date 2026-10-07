"""集計・グラフ (tkinter版 `ui/graph_window.py` + `ui/graph_canvas.py`)

    GET  /graph                 画面(ダッシュボード)
    POST /api/graph/dashboard   期間を指定して並べ直す
    POST /api/graph/history     期間の推移だけ(直終わりの確認が使う)
    POST /api/graph/csv         集計用CSVを書き出す(3本。ライン/集計/年月/日 へ)

【45度線(目標値)】
ライン毎の目標(1日あたりの枚数)を読んで、赤い線を重ねる
(VBA `目標値抜き取り` + `グラフ挿入` の「★目標値追加」)。
累積枚数には 目標×日数 の45度線、日別枚数には同じ値の横ばい線。
出す・出さないは画面のチェック(VBA `Targetline`)。目標の出どころは
**設定画面で決めたCSV**(`services/targets.py`)。

【主表示は並べたほう】
VBA は `graphF` のタブで**1枚ずつ**切り替えていた。Web版は数字・棒・
ドーナツ・表を1画面に並べ、切り替え(1つずつ大きく見る)はその下に残す。
タイルの中身と並びは `presenters/dashboard.py` が決める。

【描画は SVG】
tkinter版は Canvas に自分で線を引いていた。Web版は**サーバがビューモデル
(ラベルと値の並び)を返し、JSがSVGの座標へ写す**。座標計算はJSだが、
何を出すかは `presenters/dashboard.py` が決める。

【数字は残した集計から】
描くもとは、直ごとに残してある集計(`services/summary.py`)。明細を
毎回数え直さないので、**紙に出した数字とグラフの数字が必ず一致する**。
"""
from __future__ import annotations

from datetime import date, datetime

from flask import Blueprint, jsonify, render_template, request

from nippou import work_context
from nippou.config import SETTINGS
from nippou.logging_setup import get_logger, log_button_click
from nippou.presenters import dashboard as dash
from nippou.presenters import settings as settings_view
from nippou.reporting import csv_export
from nippou.services import summary
from nippou.services.nippou_service import format_business_date

from .. import error_body, get_repo, shell

log = get_logger("app.routes.graph")

bp = Blueprint("graph", __name__)


def _line_label(line: str) -> str:
    """画面に出すライン名(正規の呼び名。v4.12.5)。"""
    from nippou.logic import line_names
    return line_names.label(line)


def _history(start: date, end: date, line: str) -> dict:
    """期間の日別推移。**空でも「無い」と言える形で返す。**

    枚数の推移には目標を重ねます ── **直の終わりに見るのはここ**で、
    「今日は何枚だったか」だけでなく「目標に届いたか」が同じ絵で
    読めないと、確認させる意味が薄い。VBA と同じ横ばいの線です
    (`logic/line_target.flat_line`)。
    """
    from nippou.logic import line_target

    rows = summary.period_rows(get_repo(), start, end, line)
    labels = [r.report_date for r in rows]
    target = line_target.flat_line(_target_of(line), len(rows))
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "line": line,
        "labels": labels,
        "series": [
            {"key": "weight", "title": "日別重量推移", "unit": "t", "kind": "line",
             "values": [round(r.weight_ton, 3) for r in rows]},
            {"key": "count", "title": "日別枚数推移", "unit": "枚", "kind": "line",
             "values": [round(r.sheet_count, 1) for r in rows],
             "target": [round(v, 1) for v in target]},
            {"key": "rate", "title": "日別稼働率推移", "unit": "%", "kind": "line",
             "values": [round(r.operating_rate_pct, 1) for r in rows]},
        ],
    }


def _today_shifts(report_date: str, line: str) -> dict:
    """その日の直別集計(集計CSVと同じ中身)。"""
    rows = summary.day_rows(get_repo(), report_date, line)
    # 停止は**項目まで降ろす**(VBA `Stop_Agg` → `停止グラフ挿入`)。
    # 「突発が120分」では手を打てず、ﾌｫｰｸ待ちなのか機械なのかで話が違う
    items = summary.day_stop_items(get_repo(), report_date, line)
    return {
        "report_date": report_date,
        "labels": [r.shift for r in rows],
        "series": [
            {"key": "weight", "title": "直別重量", "unit": "t", "kind": "bar",
             "values": [round(r.weight_ton, 3) for r in rows]},
            {"key": "stop", "title": "直別停止時間", "unit": "分", "kind": "bar",
             "values": [round(r.total_stop_minutes, 1) for r in rows]},
        ],
        "table": [
            {"shift": r.shift,
             "count": round(r.sheet_count, 1),
             "weight_ton": round(r.weight_ton, 2),
             "work_minutes": round(r.work_minutes, 1),
             "loss": round(r.management_loss_minutes, 1),
             "sudden": round(r.unplanned_stop_minutes, 1),
             "handling": round(r.handling_stop_minutes, 1),
             "rate": round(r.operating_rate_pct, 1),
             "productivity": round(r.productivity_t_per_h, 2)}
            for r in rows
        ],
        "stops": [
            {"code": stop.stop_code, "label": stop.stop_reason,
             "text": stop.text, "kind": stop.stop_kind,
             "count": times, "minutes": round(stop.stop_minutes, 1)}
            for stop, times in items
        ],
    }


def _dashboard(start: date, end: date, report_date: str, line: str,
               *, with_target: bool = True, mode: str = dash.MODE_DAY):
    """1画面ぶんのタイル。**DBを読むのは presenter の中だけ。**

    `with_target` は画面の「目標線」のチェック(VBA `Targetline`)。
    外すと45度線を重ねません ── 実績だけを見たい場面があるので、
    VBA と同じく**出す・出さないを人が決められる**ようにしています。
    """
    return dash.build(
        get_repo(), report_date=report_date, line=line, start=start, end=end,
        # 共有へまだ出していないページ。0でも出す(押し忘れか、無いのか)
        pending=len(get_repo().pending_sync_headers()),
        target=_target_of(line) if with_target else None,
        # その日を見るのか、期間を見るのか。**押された側をそのまま渡す**
        mode=mode)


def _target_of(line: str):
    """そのラインの目標(1日あたりの枚数)。**読めなければ None。**

    出どころは設定画面で決めたCSV(`services/targets.py`)。読めなくても
    グラフは出します ── 目標線が引かれないだけ。
    """
    from nippou.services import targets

    try:
        return targets.of(line)
    except Exception:                     # noqa: BLE001 - グラフは出す
        log.exception("目標値を読めませんでした line=%s", line)
        return None


@bp.get("/graph")
def index():
    ctx = work_context.get_context()
    from .entry import current_calculator
    calc = current_calculator()

    # VBAの集計シートが常に「月初〜出力タイミングまで」を描いていたのに
    # 合わせ、既定は月初〜当日にする
    end = ctx.business_date(calc)
    start = end.replace(day=1)
    report_date = format_business_date(end)

    return render_template(
        "graph.html",
        dashboard=_dashboard(start, end, report_date, ctx.line).as_dict(),
        # 押す前に「どこへ出るか」を出す(設定・参照パスで変えられる)
        out_dir=settings_view.output_dir_view(),
        **shell.shell_context("graph", ribbon=_ribbon(ctx, calc)))


@bp.post("/api/graph/dashboard")
def dashboard_reload():
    """期間を変えて並べ直す。**数字も表も作り直して返す。**

    表の組み立てを JS 側に持たせると、同じ表の作り方が Jinja と2か所に
    分かれます。作るのはいつもサーバ側の1か所です。
    """
    payload = request.get_json(silent=True) or {}
    try:
        start = date.fromisoformat(str(payload.get("start", "")))
        end = date.fromisoformat(str(payload.get("end", "")))
    except ValueError:
        return jsonify(error_body(
            "bad_date", "日付は yyyy-mm-dd の形式で入れてください")), 400
    if start > end:
        return jsonify(error_body(
            "bad_range", "開始日は終了日以前にしてください", field="start")), 422

    ctx = work_context.get_context()
    from .entry import current_calculator
    report_date = format_business_date(ctx.business_date(current_calculator()))
    # 目標線は画面のチェック(VBA `Targetline`)。**既定は出す**
    with_target = bool(payload.get("with_target", True))
    # その日 / 期間。**知らない字は「その日」に倒します**(画面が古くても動く)
    mode = (dash.MODE_PERIOD if str(payload.get("mode", "")) == dash.MODE_PERIOD
            else dash.MODE_DAY)
    log_button_click("graph_dashboard", line=ctx.line,
                     extra=f"{start}~{end} {mode}")
    return jsonify(_dashboard(start, end, report_date, ctx.line,
                              with_target=with_target, mode=mode).as_dict())


@bp.post("/api/graph/history")
def history():
    """期間を変えて取り直す。"""
    payload = request.get_json(silent=True) or {}
    try:
        start = date.fromisoformat(str(payload.get("start", "")))
        end = date.fromisoformat(str(payload.get("end", "")))
    except ValueError:
        return jsonify(error_body(
            "bad_date", "日付は yyyy-mm-dd の形式で入れてください")), 400
    if start > end:
        return jsonify(error_body(
            "bad_range", "開始日は終了日以前にしてください", field="start")), 422

    ctx = work_context.get_context()
    log_button_click("graph_history", line=ctx.line, extra=f"{start}~{end}")
    return jsonify(_history(start, end, ctx.line))


@bp.post("/api/graph/csv")
def export_csv():
    r"""集計用CSVを書き出す。**3本出ます。**

    VBA の集計シートには別々のものが2つ載っていました ── 上の帯が
    直ごとの合計(`Aggre_Calcul`)、下の表がその日の明細行ぜんぶ
    (`Agg_OutPut`)。1本にはまとまらないので、そのまま2本にします。

        集計_<日付>_<ライン>.csv      直ごとの合計
        集計明細_<日付>_<ライン>.csv  明細行(**紙に載らない
                                      用途コード/納入先などもここ**)
        停止内訳_<日付>_<ライン>.csv  **直ごと・分類ごと・記号ごとの停止**
        計算内容_<日付>_<ライン>.csv  **稼働率や生産性の途中式**

    後ろの2本は VBA に無かったものです。停止は集計CSVに3分類の合計しか
    出ておらず、「段取り・突発が120分」から先へ降りられませんでした。
    計算内容は、稼働率90.0%と出ていてもその数を作った式がどこにも
    出ていなかったからです。

    出し先は **`<設定した出力先>\<ライン>\集計\年月\日\`** です
    (`csv_export.dated_dir`)── 同じフォルダに出し続けると1か月で120本を
    超えて、先月の分を探せなくなります。ラインで分けてあるのは、出力先に
    共有のフォルダを指せるからです(全ラインが同じ所へ書くと混ざります)。

    【**画面に出している期間ぶんを出します**】
    以前はこのボタンが画面の開始日・終了日を見ておらず、押すと今日の
    ぶんだけが出ていました。期間を決めてグラフを見ているのに、その
    期間のCSVが出せないのは筋が通りません ── 過去のぶん(Excelから
    取り込んだ日や、集計の仕組みが入る前に保存した日)を出す手立ても
    月まるごと(`month_export`)しかありませんでした。

    **中身のある日だけ**書きます(`csv_export.write_period_set`)。
    操業していない日まで空のCSVを並べると、フォルダを開いたときに
    「出し忘れ」と「休み」が見分けられません。
    """
    payload = request.get_json(silent=True) or {}
    ctx = work_context.get_context()
    from .entry import current_calculator
    calc = current_calculator()

    # 1日だけ名指しされたら、その日(保存のあとの自動書き出しと同じ道)。
    # 期間が来たらその期間。どちらも無ければ今日
    raw = str(payload.get("report_date", "")).strip()
    today = raw or format_business_date(ctx.business_date(calc))
    try:
        start, end, named = _csv_range(payload, today)
    except ValueError:
        return jsonify(error_body(
            "bad_date", "日付は yyyy-mm-dd の形式で入れてください")), 400
    if start > end:
        return jsonify(error_body(
            "bad_range", "開始日は終了日以前にしてください", field="start")), 422

    # **1つ目だけ。** 2つ目の出力先へは「共有へ保存」で送れたときに出します
    # (`services/second_output`)
    try:
        written = csv_export.write_period_set(
            get_repo(), start, end, ctx.line, SETTINGS.report_output_dir,
            always=named)
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log.exception("集計CSVの書き出しに失敗しました")
        return jsonify(error_body("write_failed", f"出力に失敗しました: {exc}")), 500

    # **過ぎた日のぶんも見て回ります。** 書くたびに確かめるのはその日の
    # フォルダだけなので、先月の3日から `CSVの読み方.txt` が消されても
    # 気づけません ── 押したときが、まとめて直せる唯一の機会です
    swept = _sweep_legends()

    period = f"{start}〜{end}" if start != end else str(start)
    log_button_click("graph_csv", line=ctx.line, extra=period)
    return jsonify({**_csv_payload(written, period, start, end),
                    "line": ctx.line,
                    "legend_sweep": swept.as_dict(),
                    "message": _csv_message(written, period, ctx.line, swept)})


def _csv_range(payload: dict, today: str):
    """出す期間を決める。`(開始, 終了, 名指しされた1日)`。

    **画面が期間を送ってくればそれに従います。** 送ってこない(または
    1日を名指しした)ときは、その1日だけ ── そのときは中身が無くても
    書きます(`write_period_set` の `always`)。
    """
    from nippou.logic.shift import parse_business_date

    raw_start = str(payload.get("start", "")).strip()
    raw_end = str(payload.get("end", "")).strip()
    if raw_start and raw_end:
        start, end = date.fromisoformat(raw_start), date.fromisoformat(raw_end)
        # 同じ日を両端に入れたら「その日を出せ」と読む(空でも書く)
        return start, end, format_business_date(start) if start == end else ""
    one = parse_business_date(today) or date.today()
    return one, one, today


def _csv_payload(written, period: str, start: date, end: date) -> dict:
    """書いた結果。**日ごとの内訳も返す**(何日ぶん出たのかが読めるように)。"""
    days = [{
        "report_date": work_date,
        "dir": str(one.folder),
        "counts": one.counts(),
        "legend_reissued": one.legend_reissued,
    } for work_date, one in written.days]
    first = written.days[0][1] if written.days else None
    body = {
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": days,
        "day_count": len(days),
        "file_count": written.files,
        "failed": [[work_date, why] for work_date, why in written.failed],
        # **開くのはファイルではなくフォルダ。** 1日ぶんなら3本が入っている
        "dir": str(first.folder) if first else "",
        "legend_path": str(first.legend) if first else "",
        # 年月のフォルダの式の説明(`計算内容.csv`)
        "guide_path": str(first.guide) if first and first.guide else "",
        "legend_reissued": any(one.legend_reissued for _d, one in written.days),
    }
    if len(written.days) == 1 and first is not None:
        # 1日ぶんのときは**その4本を名指しで**返します ── 出したものを
        # そのまま開きたい場面(押して確かめる)がここだからです。
        # 何日ぶんも出したときは `days` のほうを読みます
        for key, (path, count) in first.files.items():
            flat = "" if key == "aggregate" else f"{key}_"
            body[f"{flat}path"] = str(path)
            body[f"{flat}count"] = count
    return body


def _csv_message(written, period: str, line: str, swept) -> str:
    if not written.days:
        # **どのラインを見たのかまで言う。** 出るのはこの端末のラインぶん
        # だけなので、別のラインで取り込んだぶんはここに出ません ──
        # ラインを言わないと「取り込んだのに出ない」の理由が読めません
        text = (f"{period} の {_line_label(line)} には出せるものがありませんでした"
                f"(その期間に {_line_label(line)} で保存された日報がありません。"
                "別のラインで取り込んだぶんは、この端末のラインを"
                "変えてから出してください)")
    elif len(written.days) == 1:
        work_date, one = written.days[0]
        counts = one.counts()
        text = (f"{one.folder} に3本出しました\n"
                f"直ごとの集計 {counts['aggregate']}件 / "
                f"明細 {counts['detail']}行 / "
                f"停止内訳 {counts['stop']}行")
        if one.placed:
            text += ("\n年月のフォルダに "
                     + "・".join(p.name for p in one.placed) + " を置きました")
    else:
        # 根は**設定した出力先から言います**。書いたフォルダから段を数えて
        # 遡ると、`<出力先>/ライン/集計/年月/日` の段数を変えたときに
        # ここだけ静かに別の場所を指します(以前それで1段ずれていました)
        text = (f"{period} の {len(written.days)}日ぶん "
                f"{written.files}本を出しました\n"
                f"{SETTINGS.report_output_dir} の"
                f"{line}/集計/年月/日 のフォルダに入っています")
    if written.failed:
        text += f"\n出せなかった日が {len(written.failed)}日あります"
    if swept.message:
        text += f"\n{swept.message}"
    return text


def _sweep_legends():
    """出力先ぜんぶの `CSVの読み方.txt` を見て回る。**落ちても止めない。**

    CSVは書けているので、説明が配り直せなかったことで出力そのものを
    失敗にはしません。
    """
    from nippou.reporting import csv_legend

    # 2つ目の出力先も見て回る(同じ4本と説明を出しているので)
    checked, reissued, failed = 0, [], []
    for base in SETTINGS.summary_csv_dirs:
        try:
            one = csv_legend.sweep(base)
        except Exception:                         # noqa: BLE001 - CSVは書けている
            log.exception("CSVの読み方の確認に失敗しました: %s", base)
            continue
        checked += one.checked
        reissued += list(one.reissued)
        failed += list(one.failed)
    swept = csv_legend.Sweep(checked=checked, reissued=tuple(reissued),
                             failed=tuple(failed))
    if swept.reissued:
        log.info("CSVの読み方を %d か所に出し直しました", len(swept.reissued))
    for folder, why in swept.failed:
        log.warning("CSVの読み方を出し直せませんでした: %s (%s)", folder, why)
    return swept


# ------------------------------------------------------------------
# 直の終わりの確認 (VBA `graphF` を最大化して出していたもの)
# ------------------------------------------------------------------
def _review_state(ctx):
    """いま確認画面を出すべきか。**判断は `logic/shift_review.py`。**

    機会は直に2度まで:

        1. 終わりが近づいたとき(既定15分前)
        2. **終わったのに、まだ共有へ渡していないとき**(1度だけ)

    **どの直について出しているかは、この答えが持ちます**(`report_date`
    / `shift`)── 締めくくりの帯も、閉じたときに覚える相手も、ここから
    取ります。別々に決めると、**覚える相手がずれて出続けます**。
    """
    from nippou.logic import shift_review

    from .entry import build_service, current_calculator

    calc = current_calculator()
    service = build_service(ctx, calc)
    now = datetime.now()
    shift, report_date = service.current_shift_info(now, ctx.force_day_shift())

    state = shift_review.evaluate(
        now, calc, report_date, shift,
        force_day_shift=ctx.force_day_shift(),
        warn_minutes=SETTINGS.print_warning_minutes,
        recall_mode=ctx.recall.active,
        # 空のグラフを全画面で出しても確かめようがない
        has_data=bool(get_repo().list_headers_for_date(report_date, line=ctx.line)))
    if state.due:
        return state

    # いまの直で出す用が無いなら、**終わったまま残っている直**を見る
    ended = _ended_unsynced(ctx.line, (report_date, shift))
    if ended is not None:
        found = shift_review.evaluate_ended(
            ended[0], ended[1], unsynced=True, recall_mode=ctx.recall.active)
        if found.due:
            return found
    return state


def _ended_unsynced(line: str, current: tuple[str, str]) -> tuple[str, str] | None:
    """**終わったのに共有へ渡していない直**を1つ。無ければ None。

    時計では測りません ── 共有へ未送信のまま「いまの直ではなくなった」
    ものを拾います。日をまたぐ3直や日勤で数え直すと、別の答えが出ます。
    古いものから返すので、溜まっていても順に片付きます。
    """
    try:
        found = sorted({(h.report_date, h.shift)
                        for h in get_repo().pending_sync_headers()
                        if h.line == line and (h.report_date, h.shift) != current})
    except Exception:                             # noqa: BLE001 - 確認は出す
        log.exception("終わった直を数えられませんでした line=%s", line)
        return None
    return found[0] if found else None


@bp.get("/api/graph/review")
def review_state():
    """いま確認画面を出すべきか。**判断はサーバ**(`logic/shift_review.py`)。

    機会は直に2度まで:

        1. 終わりが近づいたとき(既定15分前)
        2. **終わったのに、まだ共有へ渡していないとき**(1度だけ)

    2つ目は「あとで」で閉じたまま直が終わった場合のための呼び戻しです。
    """
    return jsonify(_review_state(work_context.get_context()).as_dict())


@bp.get("/graph/review")
def review_page():
    """直の終わりに出す、画面いっぱいの確認。

    **ブラウザは操作なしに本当の全画面にできない**ので、既定は覆い
    (`position: fixed` の 100dvw×100dvh)。本当の全画面にするボタンも
    置いてあり、そちらは押した操作が起点なので通る。
    """
    ctx = work_context.get_context()
    from .entry import current_calculator
    calc = current_calculator()

    end = ctx.business_date(calc)
    report_date = format_business_date(end)
    # **締めくくりはサーバが描いておく。** 応答を1つ待つあいだ空の帯が
    # 見える、をなくします(v3.59.0 の入力画面と同じ作り)
    target_date, target_shift = _closing_target(ctx)
    return render_template(
        "graph_review.html",
        sound_cue=_daily_result_cue(target_date, target_shift),
        today=_today_shifts(report_date, ctx.line),
        history=_history(end.replace(day=1), end, ctx.line),
        report_date=report_date,
        line=ctx.line,
        closing=_closing(ctx, target_date, target_shift),
        **shell.shell_context("graph", ribbon=_ribbon(ctx, calc)))


def _daily_result_cue(report_date: str, shift: str) -> str:
    """「本日の梱包結果」を鳴らすか(締めくくる直ごとに1回)。

    VBA は直の終わりに実績のグラフを最大化で出し、`本日の梱包結果なのだ.wav`
    を鳴らしていました。Web版は鍵も設定の欄もあったのに、**鳴らすきっかけが
    どこにも繋がっていませんでした**(v4.4.0 で繋いだ)。

    **同じ直では1回だけ**(`logic/sound.SoundGate`) ── 確認の画面は
    「あとで」で閉じても直の終わりにもう一度出るので、そのたびに鳴らさない。
    鳴らせない(鳴らさないと決めてある・ファイルが無い)ときは覚えも使いません。
    """
    from nippou.logic import sound
    from nippou.services import sound_files

    if not sound_files.playable(sound.KEY_DAILY_RESULT):
        return ""
    if not sound.gate().take(f"{sound.KEY_DAILY_RESULT}:{report_date}:{shift}"):
        return ""
    return sound.KEY_DAILY_RESULT


def _closing_target(ctx) -> tuple[str, str]:
    """締めくくる相手 ── (報告日, 直)。

    **確認を出した相手と同じにします**(`_review_state`)。別々に決めると、
    画面は2直の話をしているのに覚えるのは1直、という食い違いが起きて
    **閉じても閉じても出続けます**。

    出す用が無いとき(人が自分で `/graph/review` を開いたとき)は、
    いまの直に打ってあればそれ、無ければ終わったのに共有へ渡していない
    直を拾います ── 直が変わった直後に開いた画面で「前の直を片付ける」
    ためです。
    """
    from .entry import build_service, current_calculator

    state = _review_state(ctx)
    if state.due:
        return state.report_date, state.shift

    service = build_service(ctx, current_calculator())
    shift, report_date = service.current_shift_info(
        datetime.now(), ctx.force_day_shift())
    try:
        if get_repo().saved_pages(report_date, ctx.line, shift):
            return report_date, shift
    except Exception:                             # noqa: BLE001 - 画面は出す
        log.exception("締めくくる相手を決められませんでした")
        return report_date, shift
    ended = _ended_unsynced(ctx.line, (report_date, shift))
    return ended if ended is not None else (report_date, shift)


def _closing(ctx, report_date: str, shift: str, *, just_pushed: bool = False):
    """締めくくりの成り行き。**組み立てるのは `logic/shift_closing`。**

    ここは数を集めるだけです ── 保存してあるページ、7項目の指摘、
    共有へ未送信の数。**数えられなくても画面は出します**(出さないと
    締めくくる場所が無くなります)。
    """
    from nippou.logic import shift_closing
    from nippou.services import shift_check

    repo = get_repo()
    try:
        pages = len(repo.saved_pages(report_date, ctx.line, shift))
    except Exception:                             # noqa: BLE001
        log.exception("保存ページを数えられませんでした")
        pages = 0

    findings: list = []
    counted = True
    try:
        report = shift_check.run(repo, report_date, ctx.line, shift,
                                 admin=ctx.editor)
        findings = [f.as_dict() for f in report.findings]
    except Exception:                             # noqa: BLE001
        log.exception("締めくくりのチェックでエラー %s/%s", report_date, shift)
        counted = False

    try:
        unsynced = sum(1 for h in repo.pending_sync_headers()
                       if (h.report_date, h.line, h.shift)
                       == (report_date, ctx.line, shift))
    except Exception:                             # noqa: BLE001
        log.exception("共有へ未送信を数えられませんでした")
        unsynced = 0

    try:
        from .entry import current_calculator
        shown = format_business_date(ctx.business_date(current_calculator()))
    except Exception:                             # noqa: BLE001 - 日を添えないだけ
        shown = ""
    return shift_closing.build(
        report_date=report_date, line=ctx.line, shift=shift, pages=pages,
        unsynced=unsynced, findings=findings, counted=counted,
        just_pushed=just_pushed, shown_date=shown)


@bp.get("/api/graph/review/closing")
def review_closing():
    """締めくくりの成り行きを返す。**共有へ保存したあとに描き直す口。**"""
    ctx = work_context.get_context()
    report_date, shift = _closing_target(ctx)
    just = request.args.get("pushed") == "1"
    return jsonify(_closing(ctx, report_date, shift, just_pushed=just).as_dict())


@bp.post("/api/graph/review/ack")
def review_ack():
    """確認しました。**その機会ではもう出さない。**

    覚えるのはサーバ(プロセス)。画面に覚えさせると、タブを開き直す
    たびにまた出る。

    `stage` は出した機会(`warn` / `end`)。**片方を閉じても、もう片方は
    残ります** ── 15分前に「あとで」で閉じた人を、直が終わったときに
    もう一度だけ呼び戻すためです。
    """
    from nippou.logic import shift_review

    payload = request.get_json(silent=True) or {}
    stage = (payload.get("stage") or shift_review.STAGE_WARN).strip()
    if stage not in (shift_review.STAGE_WARN, shift_review.STAGE_END):
        stage = shift_review.STAGE_WARN

    ctx = work_context.get_context()
    report_date, shift = _closing_target(ctx)
    shift_review.gate().mark(report_date, shift, stage)
    log_button_click("shift_review_ack", line=ctx.line,
                     extra=f"{report_date}/{shift}/{stage}")
    return jsonify({"acknowledged": True, "stage": stage,
                    "message": f"{shift} の実績を確認しました", "next": "/"})


def _ribbon(ctx, calc):
    """帯(いま書いているページで。`entry.ribbon_now`)。"""
    from .entry import ribbon_now

    return ribbon_now(ctx, calc)
