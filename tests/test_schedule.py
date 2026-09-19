import json
from datetime import datetime

import pytest

import config
import main
import schedule
import scheduler
import state as _state


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """config.SETTINGS_FILE を一時パスへ差し替える。"""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", path)
    return path


class TestGetSetNotifyTime:
    def test_default_when_missing(self, settings_file):
        assert schedule.get_notify_time() == (
            config.DEFAULT_NOTIFY_HOUR,
            config.DEFAULT_NOTIFY_MINUTE,
        )

    def test_roundtrip(self, settings_file):
        for h, m in [(0, 0), (8, 30), (23, 59), (12, 5)]:
            schedule.set_notify_time(h, m)
            assert schedule.get_notify_time() == (h, m)

    def test_persists_to_file(self, settings_file):
        schedule.set_notify_time(9, 15)
        data = json.loads(settings_file.read_text(encoding="utf-8"))
        assert data["notify_hour"] == 9
        assert data["notify_minute"] == 15

    def test_default_on_broken_json(self, settings_file):
        settings_file.write_text("{ this is not json", encoding="utf-8")
        assert schedule.get_notify_time() == (
            config.DEFAULT_NOTIFY_HOUR,
            config.DEFAULT_NOTIFY_MINUTE,
        )

    def test_default_on_out_of_range_stored(self, settings_file):
        settings_file.write_text(
            json.dumps({"notify_hour": 99, "notify_minute": 0}), encoding="utf-8"
        )
        assert schedule.get_notify_time() == (
            config.DEFAULT_NOTIFY_HOUR,
            config.DEFAULT_NOTIFY_MINUTE,
        )

    def test_preserves_other_keys(self, settings_file):
        settings_file.write_text(
            json.dumps({"other": "keep", "notify_hour": 1, "notify_minute": 1}),
            encoding="utf-8",
        )
        schedule.set_notify_time(7, 30)
        data = json.loads(settings_file.read_text(encoding="utf-8"))
        assert data["other"] == "keep"
        assert data["notify_hour"] == 7
        assert data["notify_minute"] == 30

    @pytest.mark.parametrize("h,m", [(-1, 0), (24, 0), (0, -1), (0, 60), (25, 61)])
    def test_out_of_range_raises(self, settings_file, h, m):
        with pytest.raises(ValueError):
            schedule.set_notify_time(h, m)

    def test_creates_parent_dir(self, tmp_path, monkeypatch):
        path = tmp_path / "nested" / "dir" / "settings.json"
        monkeypatch.setattr(config, "SETTINGS_FILE", path)
        schedule.set_notify_time(10, 0)
        assert path.exists()


class TestShouldRun:
    TODAY = "2026-07-06"

    def test_runs_at_exact_time(self):
        assert scheduler._should_run((8, 0), (8, 0), None, self.TODAY) is True

    def test_runs_after_time(self):
        assert scheduler._should_run((9, 30), (8, 0), None, self.TODAY) is True

    def test_not_before_time(self):
        assert scheduler._should_run((7, 59), (8, 0), None, self.TODAY) is False

    def test_minute_boundary_before(self):
        assert scheduler._should_run((8, 29), (8, 30), None, self.TODAY) is False

    def test_minute_boundary_at(self):
        assert scheduler._should_run((8, 30), (8, 30), None, self.TODAY) is True

    def test_skips_when_already_ran_today(self):
        assert scheduler._should_run((9, 0), (8, 0), self.TODAY, self.TODAY) is False

    def test_runs_when_last_run_was_yesterday(self):
        assert scheduler._should_run((9, 0), (8, 0), "2026-07-05", self.TODAY) is True

    # ── 失敗時リトライのクールダウン ──────────────────────────────

    def test_skips_during_cooldown(self):
        now_ts = 1_000_000.0
        last_attempt = now_ts - (scheduler._RETRY_COOLDOWN_SECONDS - 1)
        assert scheduler._should_run(
            (9, 0), (8, 0), None, self.TODAY, last_attempt, now_ts
        ) is False

    def test_retries_after_cooldown(self):
        now_ts = 1_000_000.0
        last_attempt = now_ts - scheduler._RETRY_COOLDOWN_SECONDS
        assert scheduler._should_run(
            (9, 0), (8, 0), None, self.TODAY, last_attempt, now_ts
        ) is True

    def test_runs_when_never_attempted(self):
        assert scheduler._should_run(
            (9, 0), (8, 0), None, self.TODAY, None, 1_000_000.0
        ) is True

    def test_completed_today_wins_over_cooldown_expiry(self):
        # 本日完了済みなら、クールダウンが明けていても再実行しない。
        now_ts = 1_000_000.0
        last_attempt = now_ts - scheduler._RETRY_COOLDOWN_SECONDS * 10
        assert scheduler._should_run(
            (9, 0), (8, 0), self.TODAY, self.TODAY, last_attempt, now_ts
        ) is False

    def test_cooldown_not_checked_before_notify_time(self):
        # 通知時刻前は試行歴に関係なく実行しない。
        assert scheduler._should_run(
            (7, 59), (8, 0), None, self.TODAY, None, 1_000_000.0
        ) is False


class TestRunDailyJob:
    @pytest.fixture(autouse=True)
    def _state_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "STATE_FILE", tmp_path / "notify_state.json")

    def test_records_completion_on_success(self, monkeypatch):
        monkeypatch.setattr(main, "run", lambda: True)
        scheduler._run_daily_job()
        s = _state.load()
        assert s.get("last_scheduled_run") == datetime.now(scheduler.JST).strftime("%Y-%m-%d")
        assert "last_attempt_ts" in s

    def test_no_completion_record_on_failure(self, monkeypatch):
        monkeypatch.setattr(main, "run", lambda: False)
        scheduler._run_daily_job()
        s = _state.load()
        assert "last_scheduled_run" not in s
        assert "last_attempt_ts" in s  # クールダウンの基準は記録される
