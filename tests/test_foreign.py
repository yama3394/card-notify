"""外貨決済の集計・通知に関するテスト。"""
from datetime import date

import analyzer
import notifier

TXNS = [
    {"id": "1", "date": "2026-07-04", "amount": 1200, "type": "smcc", "store": "A", "currency": "JPY"},
    {"id": "2", "date": "2026-07-04", "amount": 10000, "type": "smcc", "store": "B", "currency": "KRW"},
    {"id": "3", "date": "2026-07-04", "amount": 5000, "type": "smcc", "store": "C", "currency": "KRW"},
    {"id": "4", "date": "2026-07-04", "amount": 200, "type": "smcc", "store": "D", "currency": "USD"},
    {"id": "5", "date": "2026-07-04", "amount": 300, "type": "jcb", "store": "E"},  # currency 欠落=JPY
]


class TestAggregation:
    def test_jpy_only_in_sum_by_date(self):
        d = analyzer._sum_by_date(TXNS, date(2026, 7, 4))
        assert d["by_type"]["smcc"] == 1200
        assert d["by_type"]["jcb"] == 300
        assert d["by_type"]["cash"] == 0  # 未使用種別も0埋めされる
        assert d["total"] == 1500

    def test_foreign_by_date_groups_by_currency(self):
        assert analyzer._foreign_by_date(TXNS, date(2026, 7, 4)) == {"KRW": 15000, "USD": 200}

    def test_foreign_empty_when_no_foreign(self):
        jpy_only = [t for t in TXNS if t.get("currency", "JPY") == "JPY"]
        assert analyzer._foreign_by_date(jpy_only, date(2026, 7, 4)) == {}

    def test_sum_range_excludes_foreign(self):
        assert analyzer._sum_range(TXNS, date(2026, 7, 1), date(2026, 7, 31)) == 1500

    def test_is_jpy_backward_compat(self):
        assert analyzer.is_jpy({"amount": 1}) is True
        assert analyzer.is_jpy({"currency": "KRW"}) is False


def _base_analysis(foreign):
    return {
        "date": date(2026, 7, 4),
        "yesterday": {"by_type": {"smcc": 1200, "jcb": 300, "cash": 0}, "total": 1500},
        "week_total": 1500,
        "month_total": 1500,
        "last_week_same_day": 0,
        "diff": {"amount": 1500, "rate": None},
        "foreign": foreign,
    }


class TestDailyText:
    def test_foreign_section_present(self):
        text = notifier._build_daily_text(_base_analysis({"KRW": 15000, "USD": 200}))
        assert "外貨" in text
        assert "15,000 KRW" in text
        assert "200 USD" in text

    def test_no_foreign_section_when_empty(self):
        assert "外貨" not in notifier._build_daily_text(_base_analysis({}))

    def test_foreign_decimal_formatting(self):
        assert "12.50 USD" in notifier._build_daily_text(_base_analysis({"USD": 12.5}))
