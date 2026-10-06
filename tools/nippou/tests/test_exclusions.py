import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic.exclusions import apply_exclusion


class FakeBoolStore:
    def __init__(self, **initial: bool) -> None:
        self._values = dict(initial)

    def get(self, name: str) -> bool:
        return self._values.get(name, False)

    def set(self, name: str, value: bool) -> None:
        self._values[name] = value


class HdChExclusionTests(unittest.TestCase):
    def test_hdch1_clears_related_hdch_and_checkboxes(self) -> None:
        store = FakeBoolStore(
            HdCh1=True, HdCh2=True, HdCh3=True, HdCh5=True,
            CheckBox8=True, CheckBox11=True, CheckBox75=True, CheckBox9=True,
        )
        apply_exclusion("HdCh1", store.get, store.set)

        # HdCh1's exclusion list: HdCh2, HdCh3, HdCh4, HdCh7
        self.assertFalse(store.get("HdCh2"))
        self.assertFalse(store.get("HdCh3"))
        # HdCh5 not in HdCh1's exclusion list -> untouched.
        self.assertTrue(store.get("HdCh5"))
        # HdCh1's checkbox exclusion list: CheckBox8, CheckBox11, CheckBox75, CheckBox78
        self.assertFalse(store.get("CheckBox8"))
        self.assertFalse(store.get("CheckBox11"))
        self.assertFalse(store.get("CheckBox75"))
        # CheckBox9 not in that list -> untouched.
        self.assertTrue(store.get("CheckBox9"))
        self.assertTrue(store.get("HdCh1"))  # trigger itself is untouched

    def test_noop_when_trigger_is_false(self) -> None:
        store = FakeBoolStore(HdCh1=False, HdCh2=True)
        apply_exclusion("HdCh1", store.get, store.set)
        self.assertTrue(store.get("HdCh2"))  # nothing cleared


class CheckBoxExclusionTests(unittest.TestCase):
    def test_checkbox8_clears_related_hdch_and_checkboxes(self) -> None:
        store = FakeBoolStore(
            CheckBox8=True, HdCh1=True, HdCh4=True, HdCh7=True,
            CheckBox9=True, CheckBox75=True,
        )
        apply_exclusion("CheckBox8", store.get, store.set)

        self.assertFalse(store.get("HdCh1"))
        self.assertFalse(store.get("HdCh4"))
        self.assertFalse(store.get("HdCh7"))
        self.assertFalse(store.get("CheckBox9"))
        self.assertFalse(store.get("CheckBox75"))
        self.assertTrue(store.get("CheckBox8"))

    def test_unknown_control_is_ignored(self) -> None:
        store = FakeBoolStore()
        apply_exclusion("SomeOtherControl", store.get, store.set)  # must not raise


if __name__ == "__main__":
    unittest.main()
