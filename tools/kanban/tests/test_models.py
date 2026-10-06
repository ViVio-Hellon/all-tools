"""状態遷移のテスト(VBA のボタンクリック処理と同じ挙動か)。"""

from __future__ import annotations

import datetime as dt
import unittest

from kanban import config
from kanban.domain import models
from kanban.domain.models import KanbanItem, OrderMissingError

NOW = dt.datetime(2026, 7, 14, 9, 5, 0)


def make_item(**kwargs) -> KanbanItem:
    base = dict(line="LVC", mgmt_no="1", material="外装紙", size="2200 × 50M : 3本")
    base.update(kwargs)
    return KanbanItem(**base)


class OrderButtonTest(unittest.TestCase):
    def test_not_ordered_to_ordered(self):
        item = make_item(want="", unwant="〇")
        changes = models.order_button_changes(item, now=NOW)
        self.assertEqual(changes["want"], config.MARK_ON)
        self.assertEqual(changes["unwant"], config.MARK_OFF)
        self.assertEqual(changes["ordered_at"], "2026/07/14 09:05:00")
        # 発注し直したら発送状態はクリアされる
        self.assertEqual(changes["shipped"], config.MARK_OFF)

    def test_ordered_to_not_ordered_keeps_shipped(self):
        item = make_item(want="〇", unwant="", ordered_at="2026/07/14 09:05:00")
        changes = models.order_button_changes(item, now=NOW)
        self.assertEqual(changes["want"], config.MARK_OFF)
        self.assertEqual(changes["unwant"], config.MARK_ON)
        self.assertEqual(changes["ordered_at"], "")
        # 「発送は維持」= 発送列には一切触れない(キー自体を含めない)。
        # 空文字を明示的に書き戻すと、ローカルの発送状態が古い場合に
        # 他端末(倉庫)が Access へ書き戻した「発送済み」を消してしまう。
        self.assertNotIn("shipped", changes)

    def test_ordered_to_not_ordered_does_not_touch_shipped_even_if_stale(self):
        """ローカルの発送状態が古くても、この遷移は発送列を書き換えない。

        現場のローカル SQLite が「未発送」のまま(倉庫の発送済みをまだ
        取り込んでいない)状態でこの操作をしても、発送列を巻き込まないこと
        を明示的に確認する(実際の Access 上は発送済みかもしれない想定)。
        """
        item = make_item(want="〇", unwant="", shipped="")
        changes = models.order_button_changes(item, now=NOW)
        self.assertNotIn("shipped", changes)
        self.assertNotIn("confirmed_at", changes)

    def test_ordered_and_shipped_requires_confirmation(self):
        item = make_item(want="〇", shipped="〇")
        # 確認していない場合は何も変えない(VBA の [いいえ] と同じ)
        self.assertEqual(models.order_button_changes(item, now=NOW), {})

    def test_ordered_and_shipped_confirmed_clears_both(self):
        item = make_item(want="〇", shipped="〇", ordered_at="2026/07/14 09:05:00")
        changes = models.order_button_changes(item, treat_as_delivered=True, now=NOW)
        self.assertEqual(changes["want"], config.MARK_OFF)
        self.assertEqual(changes["unwant"], config.MARK_ON)
        self.assertEqual(changes["shipped"], config.MARK_OFF)
        self.assertEqual(changes["ordered_at"], "")

    def test_ordered_and_held_is_locked(self):
        # 注文中(倉庫対応中)の間は、倉庫が解除するまで現場は一切操作不可
        item = make_item(want="〇", hold="〇")
        self.assertEqual(models.order_button_changes(item, now=NOW), {})


class ShipButtonTest(unittest.TestCase):
    def test_cannot_ship_without_order(self):
        item = make_item(want="", unwant="〇")
        with self.assertRaises(OrderMissingError):
            models.ship_button_changes(item, now=NOW)

    def test_ship_sets_confirm_time(self):
        item = make_item(want="〇")
        changes = models.ship_button_changes(item, now=NOW)
        self.assertEqual(changes["shipped"], config.MARK_ON)
        self.assertEqual(changes["confirmed_at"], "2026/07/14 09:05:00")

    def test_unship_clears_confirm_time(self):
        item = make_item(want="〇", shipped="〇", confirmed_at="2026/07/14 09:05:00")
        changes = models.ship_button_changes(item, now=NOW)
        self.assertEqual(changes["shipped"], config.MARK_OFF)
        self.assertEqual(changes["confirmed_at"], "")

    def test_unship_allowed_even_without_order(self):
        # 発送解除は発注が無くても可能(VBA も currentShip='〇' なら通す)
        item = make_item(want="", shipped="〇")
        changes = models.ship_button_changes(item, now=NOW)
        self.assertEqual(changes["shipped"], config.MARK_OFF)

    def test_ship_while_held_succeeds_and_clears_hold(self):
        # 以前はエラーで発送を拒んでいたが、現在は発送と同時に注文中を
        # 自動解除する(VBA の needClearHold と同じ)
        item = make_item(want="〇", hold="〇", hold_at="2026/07/14 09:00:00")
        changes = models.ship_button_changes(item, now=NOW)
        self.assertEqual(changes["shipped"], config.MARK_ON)
        self.assertEqual(changes["confirmed_at"], "2026/07/14 09:05:00")
        self.assertEqual(changes["hold"], config.MARK_OFF)
        self.assertEqual(changes["hold_at"], "")

    def test_missing_order_takes_priority_over_hold(self):
        # VBA も「注文がありません」を先にチェックしていた
        item = make_item(want="", unwant="〇", hold="〇")
        with self.assertRaises(OrderMissingError):
            models.ship_button_changes(item, now=NOW)

    def test_unship_allowed_even_while_held(self):
        # 発送解除の方向は保留の影響を受けない(VBA も currentShip='〇' の
        # ときだけ保留チェックをしていた)
        item = make_item(want="〇", shipped="〇", hold="〇")
        changes = models.ship_button_changes(item, now=NOW)
        self.assertEqual(changes["shipped"], config.MARK_OFF)


class HoldButtonTest(unittest.TestCase):
    def test_hold_records_time_and_forces_ship_off(self):
        item = make_item(want="〇", shipped="〇", confirmed_at="2026/07/14 09:05:00")
        changes = models.hold_button_changes(item, now=NOW)
        self.assertEqual(changes["hold"], config.MARK_ON)
        self.assertEqual(changes["hold_at"], "2026/07/14 09:05:00")
        self.assertEqual(changes["shipped"], config.MARK_OFF)
        # 欲には一切触れない(現場の赤はそのまま)
        self.assertNotIn("want", changes)
        self.assertNotIn("unwant", changes)
        # VBA の ExecuteSQLHold も倉庫確認日時は更新しない
        self.assertNotIn("confirmed_at", changes)

    def test_hold_does_not_touch_shipped_related_columns_when_not_shipped(self):
        item = make_item(want="〇", shipped="")
        changes = models.hold_button_changes(item, now=NOW)
        self.assertEqual(changes["shipped"], config.MARK_OFF)

    def test_unhold_clears_hold_and_hold_at_only(self):
        item = make_item(want="〇", hold="〇", hold_at="2026/07/14 09:00:00", shipped="")
        changes = models.hold_button_changes(item)
        self.assertEqual(changes, {"hold": config.MARK_OFF, "hold_at": ""})
        self.assertNotIn("shipped", changes)

    def test_a_row_with_no_order_cannot_be_held(self):
        """**発注の無い行は注文中にできない。**

        「注文中」は *現場から来た発注を倉庫が仕入れ先へ注文した* という
        意味なので、発注が無い行に付けても指すものがありません。発送ボタンが
        同じ理由で断るのと揃えます(倉庫モードで赤が付いていないのに黄を
        押せてしまっていた)。
        """
        item = make_item(want="", unwant="〇")
        with self.assertRaises(OrderMissingError):
            models.hold_button_changes(item, now=NOW)

    def test_a_held_row_can_always_be_released(self):
        """**外す道は、どんな状態でも残す。**

        発注が先に取り消されても、倉庫は注文中を下ろせなければなりません。
        """
        item = make_item(want="", unwant="〇", hold="〇", hold_at="2026/07/14 09:00:00")
        changes = models.hold_button_changes(item)
        self.assertEqual(changes["hold"], config.MARK_OFF)


class BatchChangesTest(unittest.TestCase):
    def test_reset_clears_everything(self):
        changes = models.batch_reset_changes()
        self.assertEqual(changes["want"], config.MARK_OFF)
        self.assertEqual(changes["unwant"], config.MARK_ON)
        self.assertEqual(changes["shipped"], config.MARK_OFF)
        self.assertEqual(changes["ordered_at"], "")
        self.assertEqual(changes["confirmed_at"], "")

    def test_batch_ship(self):
        changes = models.batch_ship_changes(now=NOW)
        self.assertEqual(changes["shipped"], config.MARK_ON)
        self.assertEqual(changes["confirmed_at"], "2026/07/14 09:05:00")

    def test_batch_ship_releases_hold_like_the_single_button(self):
        """**まとめて発送しても、注文中は解除される。**

        VBA の ``ExecuteSQLBatchShip`` は保留列を触らなかったので、一括発送を
        通すと**緑が付いているのに黄も点いたまま**の行ができていました ──
        倉庫から見れば「発送したのにまだ注文中」で、どちらが本当か読めません。
        1 行ずつ押しても一括で押しても同じ結果にします。
        """
        item = make_item(want="〇", hold="〇", hold_at="2026/07/14 09:00:00")
        changes = models.batch_ship_changes(item, now=NOW)
        self.assertEqual(changes["shipped"], config.MARK_ON)
        self.assertEqual(changes["hold"], config.MARK_OFF)
        self.assertEqual(changes["hold_at"], "")

        # 発送ボタン 1 つぶんと同じ結果であること(ここがずれると意味がない)
        single = models.ship_button_changes(item, now=NOW)
        self.assertEqual(changes, single)

    def test_batch_ship_leaves_hold_alone_when_it_is_not_set(self):
        item = make_item(want="〇")
        changes = models.batch_ship_changes(item, now=NOW)
        self.assertNotIn("hold", changes)


class PredicateTest(unittest.TestCase):
    def test_flags(self):
        item = make_item(want="〇", shipped="〇", permanent="×")
        self.assertTrue(item.is_ordered)
        self.assertTrue(item.is_shipped)
        self.assertTrue(item.is_non_permanent)
        self.assertTrue(item.is_delivered_candidate)
        self.assertFalse(item.needs_shipping)

    def test_needs_shipping(self):
        self.assertTrue(make_item(want="〇").needs_shipping)
        self.assertFalse(make_item(want="").needs_shipping)

    def test_marks_are_trimmed(self):
        self.assertTrue(make_item(want=" 〇 ").is_ordered)

    def test_is_held(self):
        self.assertTrue(make_item(hold="〇").is_held)
        self.assertFalse(make_item(hold="").is_held)


class CancelledWhileShippingTest(unittest.TestCase):
    """赤なし・緑あり(発送処理中に注文が取り消された)。"""

    def orphan(self):
        return make_item(want="", unwant=config.MARK_ON, shipped=config.MARK_ON)

    def test_detected(self):
        self.assertTrue(self.orphan().is_cancelled_while_shipping)
        self.assertFalse(make_item(want=config.MARK_ON, shipped=config.MARK_ON).is_cancelled_while_shipping)
        self.assertFalse(make_item(want="", shipped="").is_cancelled_while_shipping)

    def test_ordering_again_is_refused(self):
        with self.assertRaises(models.CancelledWhileShippingError) as cm:
            models.order_button_changes(self.orphan())
        self.assertIn("発送処理中に注文が取り消されました", str(cm.exception))

    def test_acknowledging_clears_only_the_green(self):
        self.assertEqual(models.acknowledge_cancelled_shipping_changes(self.orphan()),
                         {"shipped": "", "confirmed_at": ""})
        self.assertEqual(models.acknowledge_cancelled_shipping_changes(make_item()), {})


class TimeFormatTest(unittest.TestCase):
    def test_display_format_matches_vba(self):
        # VBA の FormatKanbanTime は "m/d h:nn"
        self.assertEqual(
            models.format_display_time("2026/07/14 09:05:00"), "7/14 9:05"
        )

    def test_display_passthrough_on_unparsable(self):
        self.assertEqual(models.format_display_time("不明"), "不明")
        self.assertEqual(models.format_display_time(""), "")

    def test_db_string_is_zero_padded(self):
        self.assertEqual(models.now_string(NOW), "2026/07/14 09:05:00")


if __name__ == "__main__":
    unittest.main()
