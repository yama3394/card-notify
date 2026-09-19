"""v1.1.0 の残修正（分割送信・state.update・reload優先順位）のテスト。"""
import os
import threading

import pytest

import config
import notifier
import state as _state

_CONFIG_GLOBALS = (
    "LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET", "SECRET_KEY",
    "WEB_PASSWORD", "SITE_URL", "LINE_USER_ID",
)


@pytest.fixture
def restore_config_env():
    """config.reload() が書き換えるグローバルと os.environ をテスト後に復元する。"""
    saved_env = os.environ.copy()
    saved = {k: getattr(config, k) for k in _CONFIG_GLOBALS}
    yield
    os.environ.clear()
    os.environ.update(saved_env)
    for k, v in saved.items():
        setattr(config, k, v)


# ── notifier._split_text（LINE 5000字制限の分割） ──────────────────────────────

class TestSplitText:
    def test_short_text_is_single_chunk(self):
        assert notifier._split_text("hello") == ["hello"]

    def test_long_text_splits_on_line_boundaries(self):
        lines = [f"行{i} " + "x" * 100 for i in range(200)]  # 合計2万字超
        text = "\n".join(lines)
        chunks = notifier._split_text(text)
        assert len(chunks) > 1
        assert all(len(c) <= notifier._MAX_TEXT_LEN for c in chunks)
        # 改行で再結合すると元に戻る（行の欠落・重複なし）
        assert "\n".join(chunks) == text

    def test_single_overlong_line_is_force_split(self):
        text = "y" * (notifier._MAX_TEXT_LEN * 2 + 10)
        chunks = notifier._split_text(text)
        assert all(len(c) <= notifier._MAX_TEXT_LEN for c in chunks)
        assert "".join(chunks) == text

    def test_push_sends_batches_of_five(self, monkeypatch):
        monkeypatch.setattr(config, "LINE_USER_ID", "U-owner")
        monkeypatch.setattr(config, "LINE_CHANNEL_ACCESS_TOKEN", "t")
        calls = []

        class _Resp:
            def raise_for_status(self):
                pass

        def _post(url, headers=None, json=None, timeout=None):
            calls.append(json["messages"])
            return _Resp()

        monkeypatch.setattr(notifier.requests, "post", _post)
        # 7チャンク相当（5+2 の2回に分かれる）
        text = "\n".join("z" * 4000 for _ in range(9))
        notifier.push(text)
        assert sum(len(m) for m in calls) == len(notifier._split_text(text))
        assert all(len(m) <= notifier._MAX_MESSAGES_PER_PUSH for m in calls)
        assert len(calls) >= 2


# ── state.update（排他ロック下の read-modify-write） ──────────────────────────

class TestStateUpdate:
    def test_update_preserves_other_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "STATE_FILE", tmp_path / "notify_state.json")
        _state.save({"a": 1})

        def _set(s):
            s["b"] = 2

        _state.update(_set)
        assert _state.load() == {"a": 1, "b": 2}

    def test_update_creates_file_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "STATE_FILE", tmp_path / "notify_state.json")

        def _set(s):
            s["x"] = "y"

        _state.update(_set)
        assert _state.load() == {"x": "y"}

    def test_concurrent_updates_do_not_lose_writes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "STATE_FILE", tmp_path / "notify_state.json")

        def _incr(s):
            s["count"] = s.get("count", 0) + 1

        threads = [threading.Thread(target=lambda: _state.update(_incr)) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert _state.load()["count"] == 20


# ── config.reload の優先順位（プロセス環境変数 > env ファイル） ────────────────

class TestReloadPrecedence:
    def test_process_env_wins_over_env_file(self, tmp_path, monkeypatch, restore_config_env):
        env_file = tmp_path / ".env"
        env_file.write_text("SITE_URL=https://from-file.example.com\n", encoding="utf-8")
        monkeypatch.setattr(config, "_ENV_FILE", env_file)
        # プロセス起動時から存在した扱いにする。
        monkeypatch.setenv("SITE_URL", "https://from-process.example.com")
        monkeypatch.setattr(config, "_PROCESS_ENV_KEYS", frozenset({"SITE_URL"}))

        config.reload()
        assert config.SITE_URL == "https://from-process.example.com"

    def test_env_file_used_when_not_in_process_env(self, tmp_path, monkeypatch, restore_config_env):
        env_file = tmp_path / ".env"
        env_file.write_text("SITE_URL=https://from-file.example.com\n", encoding="utf-8")
        monkeypatch.setattr(config, "_ENV_FILE", env_file)
        monkeypatch.delenv("SITE_URL", raising=False)
        monkeypatch.setattr(config, "_PROCESS_ENV_KEYS", frozenset())

        config.reload()
        assert config.SITE_URL == "https://from-file.example.com"
