from datetime import date

import pytest

import config
import manual_entry
import state
from manual_entry import _parse_date, process

FIXED_TODAY = date(2026, 5, 29)  # 金曜日


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    """manual_entry.process() は late arrival 判定で state.py を読み書きする。
    パッチ漏れで本番の data/notify_state.json に触れないよう、このファイルの
    全テストで強制的に隔離する。"""
    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "notify_state.json")


def _patch_storage(monkeypatch):
    """history.json を触らずに process() をテストするためのスタブ。"""
    store = {"transactions": [], "skipped_ids": []}
    monkeypatch.setattr(manual_entry, "load_history", lambda: store)
    # 更新系は update_history(mutator) に統一。共有の store dict へ適用するスタブ。
    monkeypatch.setattr(manual_entry, "update_history", lambda mutator: mutator(store))
    monkeypatch.setattr(manual_entry, "today_jst", lambda: FIXED_TODAY)
    return store


class TestAmountParsing:
    def test_three_digit(self, monkeypatch):
        store = _patch_storage(monkeypatch)
        assert "¥800" in process("800")
        assert store["transactions"][-1]["amount"] == 800

    def test_four_digit_without_comma(self, monkeypatch):
        store = _patch_storage(monkeypatch)
        assert "¥5,000" in process("5000")
        assert store["transactions"][-1]["amount"] == 5000

    def test_with_comma(self, monkeypatch):
        store = _patch_storage(monkeypatch)
        process("12,345")
        assert store["transactions"][-1]["amount"] == 12345

    def test_non_numeric_rejected(self, monkeypatch):
        _patch_storage(monkeypatch)
        assert "金額を送ってください" in process("あいう")


class TestStoreAndDate:
    def test_amount_and_store(self, monkeypatch):
        store = _patch_storage(monkeypatch)
        process("800 セブン")
        assert store["transactions"][-1]["store"] == "セブン"

    def test_amount_store_date(self, monkeypatch):
        store = _patch_storage(monkeypatch)
        process("800 セブン 昨日")
        tx = store["transactions"][-1]
        assert tx["store"] == "セブン"
        assert tx["date"] == "2026-05-28"

    def test_cash_prefix_stripped(self, monkeypatch):
        store = _patch_storage(monkeypatch)
        process("現金 800 セブン")
        assert store["transactions"][-1]["amount"] == 800
        assert store["transactions"][-1]["store"] == "セブン"


class TestParseDate:
    def test_invalid_date_token_treated_as_store(self, monkeypatch):
        store = _patch_storage(monkeypatch)
        process("800 13/45")  # 不正日付 → 店舗名扱い
        assert store["transactions"][-1]["store"] == "13/45"

    def test_parse_date_helper(self):
        assert _parse_date("昨日", FIXED_TODAY) == date(2026, 5, 28)
        assert _parse_date("今日", FIXED_TODAY) == FIXED_TODAY
        assert _parse_date("6/1", FIXED_TODAY) == date(2026, 6, 1)
        assert _parse_date("13/45", FIXED_TODAY) is None

    def test_year_boundary_treats_future_mmdd_as_last_year(self):
        # 年始（1月）に前年12月分を入力した場合、今年の未来日付ではなく前年扱い
        assert _parse_date("12/31", date(2026, 1, 5)) == date(2025, 12, 31)

    def test_mid_year_future_date_not_pushed_to_last_year(self):
        # 6月に12月分（半年以上先）を入力しても、1月始まりの補正対象ではない
        # ので今年のまま（以前は182日超で無条件に前年へ倒していた）
        assert _parse_date("12/25", date(2026, 6, 1)) == date(2026, 12, 25)

    def test_year_boundary_correction_only_applies_in_january(self):
        # 2月に12月分（未来日付）を入力しても、年始（1月）補正の対象外
        assert _parse_date("12/31", date(2026, 2, 5)) == date(2026, 12, 31)

    def test_leap_day_in_non_leap_year_raises(self):
        # 2026年はうるう年ではないため2/29は存在しない。13/45のような明らかな
        # 非日付と違い入力ミスに気づきにくいので、店舗名扱いにせず例外で伝える
        with pytest.raises(manual_entry._NonexistentDate):
            _parse_date("2/29", FIXED_TODAY)

    def test_leap_day_in_leap_year_parses(self):
        assert _parse_date("2/29", date(2024, 5, 1)) == date(2024, 2, 29)

    def test_leap_day_entry_returns_error_without_registering(self, monkeypatch):
        store = _patch_storage(monkeypatch)
        reply = process("800 2/29")
        assert "存在しない日付" in reply
        assert store["transactions"] == []


# ---------------------------------------------------------------------------
# 回帰テスト: 前日の日次通知が送信済みの後に「昨日」で手入力すると、
# その分はどの日の内訳にも二度と現れず week_total/month_total にだけ
# 無言で加算されてしまっていた（late_arrivals に積んで案内する）
# ---------------------------------------------------------------------------

class TestLateArrival:
    def test_yesterday_entry_flagged_late_when_todays_report_already_sent(self, monkeypatch):
        _patch_storage(monkeypatch)
        # 今日(5/29)分の日次通知（対象=5/28）は送信済み → 次の対象は5/29
        state.update(lambda s: s.__setitem__("last_daily", "2026-05-29"))

        reply = process("1400 スーパー 昨日")

        late = state.load().get("late_arrivals")
        assert late == [
            {"date": "2026-05-28", "amount": 1400, "currency": "JPY", "type": "cash", "store": "スーパー"}
        ]
        # 過去日付登録では「本日現金合計」ではなくその日付のラベルにする
        assert "本日現金合計" not in reply
        assert "5/28の現金合計" in reply
        # 誤補正の検知漏れ防止のため確認メッセージにも年を表示する
        assert "2026/5/28" in reply

    def test_yesterday_entry_not_late_when_todays_report_not_sent_yet(self, monkeypatch):
        _patch_storage(monkeypatch)
        # 今日(5/29)分の日次通知（対象=5/28）はまだ未送信 → 5/28はこの後の通知で拾われる
        state.update(lambda s: s.__setitem__("last_daily", "2026-05-28"))

        process("1400 スーパー 昨日")

        assert not state.load().get("late_arrivals")

    def test_today_entry_never_late(self, monkeypatch):
        _patch_storage(monkeypatch)
        state.update(lambda s: s.__setitem__("last_daily", "2026-05-29"))

        reply = process("1400 スーパー 今日")

        assert not state.load().get("late_arrivals")
        assert "本日現金合計" in reply

    def test_late_push_does_not_clobber_concurrent_gmail_late_arrival(self, monkeypatch):
        # main.py が pop する前に、gmail_fetcher 側が積んだ分と手入力側が積んだ分の
        # 両方が失われずに残ること（atomic な state.update を共有しているため）。
        _patch_storage(monkeypatch)
        state.update(lambda s: s.__setitem__("last_daily", "2026-05-29"))
        state.update(lambda s: s.setdefault("late_arrivals", []).append(
            {"date": "2026-05-20", "amount": 500, "currency": "JPY", "type": "smcc", "store": "既存"}
        ))

        process("1400 スーパー 昨日")

        late = state.load().get("late_arrivals")
        assert len(late) == 2
        assert late[0]["store"] == "既存"
        assert late[1]["store"] == "スーパー"
