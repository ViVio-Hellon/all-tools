"""同じことを2か所に書いていないか、の見張り。

【なぜテストにするのか】
重複は**書いた日には壊れません。** 壊れるのは、片方だけを直した日です
── バンドの種別を1つ足したのに画面の並びが3つのまま、直が増えたのに
刷る選択肢が4つのまま。どちらも例外は出ず、画面はふつうに開き、
「なぜか選べない」という形で、ずっと後になって出ます。

ここは「出どころが1つであること」そのものを確かめます。名前を写した
瞬間に落ちるので、写す前に気づけます。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.connection import connect
from nippou.db.models import REPORT_FIELDS, PackingReport
from nippou.db.repository import NippouRepository
from nippou.logic import gw_autoselect as autoselect
from nippou.logic.aggregation import SHIFT_ORDER
from nippou.logic.shift import DAY_SHIFT, SHIFT_1, SHIFT_2, SHIFT_3, SHIFT_NAMES
from nippou.presenters.dashboard import SHIFT_STACK_ORDER
from nippou.services import load_factor

from _web import HAS_FLASK, SKIP_REASON

_ROOT = Path(__file__).resolve().parent.parent

#: `app.*` を読む確かめごと。**Flask が無い机でも残りは走らせる**
needs_flask = unittest.skipUnless(HAS_FLASK, SKIP_REASON)


class BandKindTests(unittest.TestCase):
    """バンドの種別は `logic/gw_autoselect` が持つ。"""

    @needs_flask
    def test_画面の並びは自動選択と同じ名前(self) -> None:
        from app.routes.gw import BAND_KINDS

        self.assertEqual(
            BAND_KINDS,
            (autoselect.BAND_NO_SEAL, autoselect.BAND_WITH_SEAL,
             autoselect.BAND_PET))

    @needs_flask
    def test_帯鉄が付くのはシール無とシール有だけ(self) -> None:
        from app.routes.gw import _band_material_name

        self.assertEqual(_band_material_name(autoselect.BAND_NO_SEAL),
                         "帯鉄シール無")
        self.assertEqual(_band_material_name(autoselect.BAND_WITH_SEAL),
                         "帯鉄シール有")
        # PETバンドはそのままの名前でマスタに載っている
        self.assertEqual(_band_material_name(autoselect.BAND_PET), "PETバンド")

    def test_画面側に種別の文字を書き写さない(self) -> None:
        """`app/routes/gw.py` に種別の綴りが直接あってはいけない。

        あると、自動選択側の名前を直したときに画面だけ取り残されます。
        """
        source = (_ROOT / "app/routes/gw.py").read_text(encoding="utf-8")
        # 説明の文は残してよいので、**文字列リテラルとして**あるかを見る
        for name in (autoselect.BAND_NO_SEAL, autoselect.BAND_WITH_SEAL,
                     autoselect.BAND_PET):
            self.assertNotIn(f'"{name}"', source)
            self.assertNotIn(f"'{name}'", source)


class ShiftNameTests(unittest.TestCase):
    """直の名前と並びは `logic/shift.SHIFT_NAMES` が持つ。"""

    def test_紙に出る順(self) -> None:
        self.assertEqual(SHIFT_NAMES, (SHIFT_1, SHIFT_2, SHIFT_3, DAY_SHIFT))

    @needs_flask
    def test_刷る選択肢も積み上げの順も同じ4つ(self) -> None:
        """**同じ物であることは見ません**(`assertIs` にしない)。

        見たいのは「中身が揃っていること」で、同じ入れものかどうかでは
        ありません ── 同じ名前を別に書き写しても中身が揃っていれば
        画面は正しく、食い違ったときだけ困ります。ここが落ちるのは
        **どちらか片方だけ直したとき**です。
        """
        from app.routes.printing import SHIFT_CHOICES

        self.assertEqual(SHIFT_CHOICES, SHIFT_NAMES)
        self.assertEqual(SHIFT_STACK_ORDER, SHIFT_NAMES)

    def test_集計の並べ替えも同じ4つ(self) -> None:
        self.assertEqual(tuple(SHIFT_ORDER), SHIFT_NAMES)
        self.assertEqual(list(SHIFT_ORDER.values()),
                         list(range(len(SHIFT_NAMES))))

    def test_負荷係数の並びはわざと違う(self) -> None:
        """こちらは**係数を引き継ぐ順**なので、揃えてはいけない。

        揃えると 1直 → 日勤 の引き継ぎが切れます(`services/load_factor`)。
        """
        self.assertEqual(set(load_factor.SHIFT_ORDER), set(SHIFT_NAMES))
        self.assertNotEqual(tuple(load_factor.SHIFT_ORDER), SHIFT_NAMES)


class ReportFieldTests(unittest.TestCase):
    """集計の列は `db/models.REPORT_FIELDS` が持つ。

    保存の SQL は**この並びから組み立てます。** 手で `?` を並べていた
    ころは、値を1つ足すと列名・`?`・値の3か所を揃って直す必要があり、
    ずれても例外にならず「1つ手前の値が入る」という形で出ました。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "t.sqlite3")
        self.repo = NippouRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_どの欄も往復して値が変わらない(self) -> None:
        # **全部に別々の値**を入れる。同じ値だと、ずれても気づけない
        report = PackingReport(work_date="2026年9月16日", line_name="L-1",
                               shift="1直")
        for i, field in enumerate(REPORT_FIELDS, start=1):
            current = getattr(report, field)
            setattr(report, field,
                    f"作業者{i}" if isinstance(current, str) else i)

        self.repo.save_packing_report(report, [])
        back = self.repo.load_packing_report("2026年9月16日", "L-1", "1直")

        self.assertIsNotNone(back)
        for i, field in enumerate(REPORT_FIELDS, start=1):
            with self.subTest(field=field):
                got = getattr(back, field)
                want = f"作業者{i}" if isinstance(got, str) else i
                self.assertEqual(got, want)

    def test_モデルに無い欄を並びに入れない(self) -> None:
        for field in REPORT_FIELDS:
            self.assertTrue(hasattr(PackingReport(work_date="", line_name="",
                                                  shift=""), field), field)

    def test_表の列をひとつ残らず覆う(self) -> None:
        """`packing_report` の**値の列は全部**この並びに入っている。

        入れ忘れた列は、例外ではなく「いつも既定値」という形で出ます
        ── 落ちないので、気づくのは集計を見比べた日です。
        """
        columns = {r[1] for r in
                   self.conn.execute("PRAGMA table_info(packing_report)")}
        # 鍵と台帳の列は値ではないので、並びには入らない
        # (指紋の2つも台帳です ── 中身から作るもので、中身ではない)
        bookkeeping = {"id", "work_date", "line_name", "shift",
                       "dirty", "synced_at", "created_at", "updated_at",
                       "content_hash", "synced_hash"}
        self.assertEqual(set(REPORT_FIELDS), columns - bookkeeping)


if __name__ == "__main__":
    unittest.main()
