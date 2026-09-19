"""日次通知の遅延登録セクションと、週次の同時点比較のテスト。"""
from datetime import date, timedelta

import analyzer
import notifier


def _analysis(**over):
    base = {
        "date": date(2026, 7, 14),
        "yesterday": {"by_type": {"smcc": 1000, "jcb": 0, "cash": 0}, "total": 1000},
        "week_total": 5000,
        "month_total": 20000,
        "last_week_same_day": 800,
        "diff": {"amount": 200, "rate": 25.0},
        "foreign": {},
    }
    base.update(over)
    return base


class TestLateArrivalsSection:
    def test_no_late_arrivals_no_section(self):
        text = notifier._build_daily_text(_analysis())
        assert "追加登録" not in text

    def test_late_arrivals_listed(self):
        late = [
            {"date": "2026-07-10", "amount": 3000, "currency": "JPY",
             "type": "smcc", "store": "イオン"},
            {"date": "2026-07-11", "amount": 12.5, "currency": "USD",
             "type": "smcc", "store": None},
        ]
        text = notifier._build_daily_text(_analysis(), late)
        assert "追加登録（過去日分）" in text
        assert "07/10  ¥3,000  イオン" in text
        assert "07/11  12.50 USD  （店舗名なし）" in text


class TestPrevPeriodReflectedFooter:
    """追加登録（過去日分）が先月・先週まとめに反映済みであることの案内。"""

    def _analysis_with_periods(self, **over):
        return _analysis(
            week_start=date(2026, 7, 13),
            month_start=date(2026, 7, 1),
            prev_week_total=12000,
            prev_month_total=98000,
            **over,
        )

    def test_item_before_month_start_shows_both_footers(self):
        late = [{"date": "2026-06-30", "amount": 500, "currency": "JPY", "store": "A"}]
        text = notifier._build_daily_text(self._analysis_with_periods(), late)
        assert "先月計（追加登録反映）  ¥98,000" in text
        assert "先週計（追加登録反映）  ¥12,000" in text

    def test_item_before_week_start_but_in_current_month_shows_only_week_footer(self):
        late = [{"date": "2026-07-10", "amount": 500, "currency": "JPY", "store": "A"}]
        text = notifier._build_daily_text(self._analysis_with_periods(), late)
        assert "先月計（追加登録反映）" not in text
        assert "先週計（追加登録反映）  ¥12,000" in text

    def test_item_within_current_week_shows_no_footer(self):
        late = [{"date": "2026-07-14", "amount": 500, "currency": "JPY", "store": "A"}]
        text = notifier._build_daily_text(self._analysis_with_periods(), late)
        assert "追加登録反映" not in text

    def test_mixed_items_show_both_footers_only_once(self):
        late = [
            {"date": "2026-06-30", "amount": 500, "currency": "JPY", "store": "A"},
            {"date": "2026-07-14", "amount": 300, "currency": "JPY", "store": "B"},
        ]
        text = notifier._build_daily_text(self._analysis_with_periods(), late)
        assert text.count("先月計（追加登録反映）") == 1
        assert text.count("先週計（追加登録反映）") == 1

    def test_foreign_currency_item_still_counts_toward_footer(self):
        late = [{"date": "2026-06-30", "amount": 12.5, "currency": "USD", "store": "A"}]
        text = notifier._build_daily_text(self._analysis_with_periods(), late)
        assert "先月計（追加登録反映）  ¥98,000" in text
        assert "先週計（追加登録反映）  ¥12,000" in text

    def test_missing_period_keys_backward_compat_no_footer(self):
        late = [{"date": "2026-06-30", "amount": 500, "currency": "JPY", "store": "A"}]
        text = notifier._build_daily_text(_analysis(), late)
        assert "追加登録（過去日分）" in text
        assert "追加登録反映" not in text


class TestAnalyzeNewPeriodKeys:
    def test_returns_period_starts_and_prev_totals(self, monkeypatch):
        today = date(2026, 7, 15)  # 水曜。今週月曜=7/13、今月1日=7/1
        monkeypatch.setattr(analyzer, "today_jst", lambda: today)
        monkeypatch.setattr(analyzer, "load_transactions", lambda: [
            {"id": "1", "date": "2026-07-14", "amount": 1000, "type": "smcc"},  # 今週
            {"id": "2", "date": "2026-07-10", "amount": 2000, "type": "smcc"},  # 先週(7/6-7/12)
            {"id": "3", "date": "2026-06-30", "amount": 3000, "type": "smcc"},  # 先月(6月)
            {"id": "4", "date": "2026-06-01", "amount": 4000, "type": "smcc"},  # 先月(6月)
            {"id": "5", "date": "2026-05-31", "amount": 9999, "type": "smcc"},  # 先々月(対象外)
        ])
        r = analyzer.analyze()
        assert r["week_start"] == date(2026, 7, 13)
        assert r["month_start"] == date(2026, 7, 1)
        assert r["prev_week_total"] == 2000
        assert r["prev_month_total"] == 3000 + 4000

    def test_foreign_excluded_from_prev_totals(self, monkeypatch):
        today = date(2026, 7, 15)
        monkeypatch.setattr(analyzer, "today_jst", lambda: today)
        monkeypatch.setattr(analyzer, "load_transactions", lambda: [
            {"id": "1", "date": "2026-07-10", "amount": 2000, "type": "smcc", "currency": "JPY"},
            {"id": "2", "date": "2026-07-10", "amount": 999, "type": "smcc", "currency": "USD"},
            {"id": "3", "date": "2026-06-15", "amount": 3000, "type": "smcc", "currency": "JPY"},
            {"id": "4", "date": "2026-06-15", "amount": 500, "type": "smcc", "currency": "KRW"},
        ])
        r = analyzer.analyze()
        assert r["prev_week_total"] == 2000
        assert r["prev_month_total"] == 3000


class TestWeekToDateComparison:
    def test_current_week_returns_prev_to_date(self, monkeypatch):
        today = date(2026, 7, 15)  # 水曜
        monkeypatch.setattr(analyzer, "today_jst", lambda: today)
        monkeypatch.setattr(analyzer, "load_transactions", lambda: [
            {"id": "1", "date": "2026-07-14", "amount": 1000, "type": "smcc"},
            # 先週の同時点まで(月〜水=7/6-7/8)
            {"id": "2", "date": "2026-07-07", "amount": 700, "type": "smcc"},
            # 先週後半 → 同時点比較には入らない
            {"id": "3", "date": "2026-07-10", "amount": 9999, "type": "smcc"},
        ])
        r = analyzer.analyze_week()
        assert r["is_current_week"] is True
        assert r["prev_week_to_date_total"] == 700
        assert r["prev_week_total"] == 700 + 9999

    def test_past_week_no_to_date_value(self, monkeypatch):
        today = date(2026, 7, 15)
        monkeypatch.setattr(analyzer, "today_jst", lambda: today)
        monkeypatch.setattr(analyzer, "load_transactions", lambda: [])
        r = analyzer.analyze_week(today - timedelta(days=14))
        assert r["is_current_week"] is False
        assert r["prev_week_to_date_total"] is None
