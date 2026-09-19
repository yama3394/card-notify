"""main.run() の失敗時リトライ・重複送信防止のテスト。

notifier / gmail_fetcher / analyzer / reports は monkeypatch でスタブ化し、
state の保存先は tmp_path へ向ける。
"""
import stat
from datetime import datetime as _real_datetime
from pathlib import Path

import pytest

import analyzer
import config
import gmail_fetcher
import main
import notifier
import reports
import state as _state


@pytest.fixture
def calls(tmp_path, monkeypatch):
    """外部I/Oをすべてスタブ化し、呼び出し記録の dict を返す。"""
    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "notify_state.json")
    monkeypatch.setattr(config, "require_secrets", lambda: None)

    rec = {"daily": [], "errors": [], "monthly": [], "weekly": []}
    monkeypatch.setattr(gmail_fetcher, "fetch_all", lambda: 0)
    monkeypatch.setattr(analyzer, "analyze", lambda: {"dummy": True})
    monkeypatch.setattr(notifier, "send_daily_report",
                        lambda data, late_arrivals=None: rec["daily"].append(data))
    monkeypatch.setattr(notifier, "notify_error", lambda msg: rec["errors"].append(msg))
    monkeypatch.setattr(reports, "send_monthly_report", lambda y, m: rec["monthly"].append((y, m)))
    monkeypatch.setattr(reports, "send_weekly_report", lambda: rec["weekly"].append(True))
    return rec


def _fix_today(monkeypatch, year: int, month: int, day: int) -> None:
    """main モジュール内の datetime.now() を固定日時に差し替える。"""

    class _Fixed(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(year, month, day, 12, 0, tzinfo=tz)

    monkeypatch.setattr(main, "datetime", _Fixed)


def _raise(*args, **kwargs):
    raise RuntimeError("boom")


class TestFetchFailure:
    def test_returns_false_and_skips_all_notifications(self, calls, monkeypatch):
        _fix_today(monkeypatch, 2026, 8, 3)  # 月曜（週次対象日）でも進まないこと
        monkeypatch.setattr(gmail_fetcher, "fetch_all", _raise)

        assert main.run() is False
        assert calls["daily"] == []
        assert calls["weekly"] == []
        assert len(calls["errors"]) == 1
        assert _state.load().get("last_fetch_error_date") == "2026-08-03"
        assert "last_daily" not in _state.load()

    def test_error_notify_suppressed_same_day(self, calls, monkeypatch):
        _fix_today(monkeypatch, 2026, 7, 8)
        monkeypatch.setattr(gmail_fetcher, "fetch_all", _raise)

        assert main.run() is False
        assert main.run() is False
        assert len(calls["errors"]) == 1  # 同日2回目以降は通知しない

    def test_error_notify_resets_next_day(self, calls, monkeypatch):
        monkeypatch.setattr(gmail_fetcher, "fetch_all", _raise)
        _fix_today(monkeypatch, 2026, 7, 8)
        main.run()
        _fix_today(monkeypatch, 2026, 7, 9)
        main.run()
        assert len(calls["errors"]) == 2

    def test_recovery_after_failure_sends_daily(self, calls, monkeypatch):
        _fix_today(monkeypatch, 2026, 7, 8)
        monkeypatch.setattr(gmail_fetcher, "fetch_all", _raise)
        assert main.run() is False

        monkeypatch.setattr(gmail_fetcher, "fetch_all", lambda: 3)
        assert main.run() is True
        assert len(calls["daily"]) == 1
        assert _state.load().get("last_daily") == "2026-07-08"


class TestDaily:
    def test_success_marks_and_second_run_skips(self, calls, monkeypatch):
        _fix_today(monkeypatch, 2026, 7, 8)
        assert main.run() is True
        assert main.run() is True
        assert len(calls["daily"]) == 1
        assert _state.load().get("last_daily") == "2026-07-08"

    def test_send_failure_returns_false_without_marking(self, calls, monkeypatch):
        _fix_today(monkeypatch, 2026, 7, 8)
        monkeypatch.setattr(notifier, "send_daily_report", _raise)

        assert main.run() is False
        assert "last_daily" not in _state.load()
        assert any("日次通知失敗" in e for e in calls["errors"])


class TestLateArrivalsHelpers:
    def test_pop_is_atomic_clear(self, calls):
        _state.update(lambda s: s.__setitem__("late_arrivals", [{"store": "A"}]))

        popped = main._pop_late_arrivals()

        assert popped == [{"store": "A"}]
        assert _state.load().get("late_arrivals") == []

    def test_pop_does_not_drop_concurrent_push(self, calls):
        # main.run() が pop した直後（送信中）に別プロセス（webhook）が積んだ分は、
        # pop 自体では失われず次回に残ること。
        _state.update(lambda s: s.__setitem__("late_arrivals", [{"store": "A"}]))

        popped = main._pop_late_arrivals()
        _state.update(lambda s: s.setdefault("late_arrivals", []).append({"store": "B"}))

        assert popped == [{"store": "A"}]
        assert _state.load()["late_arrivals"] == [{"store": "B"}]

    def test_restore_on_send_failure(self, calls):
        popped = main._pop_late_arrivals()
        assert popped == []
        main._restore_late_arrivals([{"store": "A"}])

        assert _state.load()["late_arrivals"] == [{"store": "A"}]

    def test_run_restores_late_arrivals_when_daily_send_fails(self, calls, monkeypatch):
        _fix_today(monkeypatch, 2026, 7, 8)
        _state.update(lambda s: s.__setitem__("late_arrivals", [{"store": "A"}]))
        monkeypatch.setattr(notifier, "send_daily_report", _raise)

        assert main.run() is False

        # 送信に失敗しても late_arrivals を失わない（次回に持ち越し）
        assert _state.load()["late_arrivals"] == [{"store": "A"}]


def _seed_established(monthly: str = "2026-06", weekly: str = "2026-07-27") -> None:
    """稼働済みインストール相当の state を用意する。

    state が空だと _init_period_marks が「送信済み」で初期化する（導入直後の
    部分データ送信防止）ため、送信系のテストは過去の送信記録を先に置く。
    """
    _state.save({"last_monthly": monthly, "last_weekly": weekly})


class TestMonthly:
    def test_sent_on_first_day_and_deduplicated(self, calls, monkeypatch):
        _fix_today(monkeypatch, 2026, 8, 1)
        _seed_established()
        assert main.run() is True
        assert calls["monthly"] == [(2026, 7)]
        assert _state.load().get("last_monthly") == "2026-07"

        # リトライ相当の再実行でも再送しない。
        assert main.run() is True
        assert calls["monthly"] == [(2026, 7)]

    def test_first_run_initializes_without_sending(self, calls, monkeypatch):
        # 導入直後（state 無し）は部分データで送らず、送信済みマークだけ入れる。
        _fix_today(monkeypatch, 2026, 8, 1)
        assert main.run() is True
        assert calls["monthly"] == []
        assert _state.load().get("last_monthly") == "2026-07"

    def test_catchup_within_first_three_days(self, calls, monkeypatch):
        # 月初1日にサーバー停止していても、3日以内の復帰で送信される。
        _fix_today(monkeypatch, 2026, 8, 3)
        _seed_established()
        assert main.run() is True
        assert calls["monthly"] == [(2026, 7)]

    def test_january_targets_previous_december(self, calls, monkeypatch):
        _fix_today(monkeypatch, 2027, 1, 1)
        _seed_established(monthly="2026-11", weekly="2026-12-28")
        assert main.run() is True
        assert calls["monthly"] == [(2026, 12)]
        assert _state.load().get("last_monthly") == "2026-12"

    def test_failure_returns_false_and_retries(self, calls, monkeypatch):
        _fix_today(monkeypatch, 2026, 8, 1)
        _seed_established()
        monkeypatch.setattr(reports, "send_monthly_report", _raise)

        assert main.run() is False
        assert _state.load().get("last_monthly") == "2026-06"  # 未更新のまま
        # 日次は成功しているので記録済み。
        assert _state.load().get("last_daily") == "2026-08-01"

        # 復旧後の再実行: 月次だけ再送され、日次は重複しない。
        monkeypatch.setattr(
            reports, "send_monthly_report", lambda y, m: calls["monthly"].append((y, m))
        )
        assert main.run() is True
        assert calls["monthly"] == [(2026, 7)]
        assert len(calls["daily"]) == 1

    def test_not_sent_on_other_days(self, calls, monkeypatch):
        _fix_today(monkeypatch, 2026, 7, 8)
        _seed_established(monthly="2026-05")  # 窓外なら未送信でも送らない
        assert main.run() is True
        assert calls["monthly"] == []


class TestWeekly:
    def test_sent_on_monday_and_deduplicated(self, calls, monkeypatch):
        _fix_today(monkeypatch, 2026, 8, 3)  # 月曜
        _seed_established()
        assert main.run() is True
        assert len(calls["weekly"]) == 1
        assert _state.load().get("last_weekly") == "2026-08-03"

        assert main.run() is True
        assert len(calls["weekly"]) == 1

    def test_first_run_initializes_without_sending(self, calls, monkeypatch):
        _fix_today(monkeypatch, 2026, 8, 3)  # 月曜・導入直後
        assert main.run() is True
        assert calls["weekly"] == []
        assert _state.load().get("last_weekly") == "2026-08-03"

    def test_catchup_on_tuesday(self, calls, monkeypatch):
        # 月曜にサーバー停止していても、火曜の復帰で同じ週キーとして送信される。
        _fix_today(monkeypatch, 2026, 8, 4)  # 火曜
        _seed_established()
        assert main.run() is True
        assert len(calls["weekly"]) == 1
        assert _state.load().get("last_weekly") == "2026-08-03"  # 今週月曜のキー

    def test_failure_returns_false_without_marking(self, calls, monkeypatch):
        _fix_today(monkeypatch, 2026, 8, 3)
        _seed_established()
        monkeypatch.setattr(reports, "send_weekly_report", _raise)

        assert main.run() is False
        assert _state.load().get("last_weekly") == "2026-07-27"  # 未更新のまま
        assert any("週次レポート失敗" in e for e in calls["errors"])

    def test_not_sent_on_other_weekdays(self, calls, monkeypatch):
        _fix_today(monkeypatch, 2026, 8, 5)  # 水曜（キャッチアップ窓外）
        _seed_established(weekly="2026-07-20")  # 未送信でも窓外なら送らない
        assert main.run() is True
        assert calls["weekly"] == []


def _perm_bits(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestStatePermissions:
    """late_arrivals に店舗名・金額を含むため、state ファイルは他ユーザから読めないこと。"""

    def test_save_sets_permissions_to_0600(self, tmp_path, monkeypatch):
        state_file = tmp_path / "notify_state.json"
        monkeypatch.setattr(config, "STATE_FILE", state_file)

        _state.save({"last_daily": "2026-09-07"})

        assert _perm_bits(state_file) == 0o600

    def test_update_sets_permissions_to_0600(self, tmp_path, monkeypatch):
        state_file = tmp_path / "notify_state.json"
        monkeypatch.setattr(config, "STATE_FILE", state_file)
        # 事前に緩いパーミッションで作成されていても update() で 0600 に締め直す
        state_file.write_text("{}", encoding="utf-8")
        state_file.chmod(0o644)

        _state.update(lambda s: s.__setitem__("last_daily", "2026-09-07"))

        assert _perm_bits(state_file) == 0o600

    def test_run_leaves_state_file_at_0600(self, calls, monkeypatch):
        # main.run() 経由の通常フローでも締まっていること
        _fix_today(monkeypatch, 2026, 7, 8)
        assert main.run() is True
        assert _perm_bits(config.STATE_FILE) == 0o600
