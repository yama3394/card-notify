"""late_arrivals.is_late / next_daily_target のテスト。

gmail_fetcher・manual_entry の両方が共有する late arrival 判定基準そのものを
ここでまとめて検証する（個別モジュール側のテストでは呼び出され方だけ見る）。
"""
from datetime import date

import late_arrivals

TODAY = date(2026, 5, 29)


class TestNextDailyTarget:
    def test_not_yet_notified_targets_yesterday(self):
        # 今日分（対象=前日）がまだ送信されていない → 次の通知は前日を拾う
        assert late_arrivals.next_daily_target(TODAY, "2026-05-28") == date(2026, 5, 28)

    def test_already_notified_targets_today(self):
        # 今日分は送信済み → 次の通知が拾うのは今日
        assert late_arrivals.next_daily_target(TODAY, "2026-05-29") == TODAY

    def test_no_last_daily_targets_yesterday(self):
        assert late_arrivals.next_daily_target(TODAY, None) == date(2026, 5, 28)


class TestIsLate:
    def test_next_target_day_not_late(self):
        assert late_arrivals.is_late(date(2026, 5, 28), TODAY, "2026-05-28") is False

    def test_before_next_target_day_is_late(self):
        assert late_arrivals.is_late(date(2026, 5, 27), TODAY, "2026-05-28") is True

    def test_today_never_late_when_not_yet_notified(self):
        assert late_arrivals.is_late(TODAY, TODAY, "2026-05-28") is False

    def test_yesterday_late_once_todays_report_sent(self):
        # 今日分の通知が送信済みになった瞬間、前日はもう拾われない
        assert late_arrivals.is_late(date(2026, 5, 28), TODAY, "2026-05-29") is True
