"""auth.py のログイン失敗記録の並行アクセス安全性のテスト。"""
import threading

import auth
import config


def test_concurrent_record_failure_does_not_lose_updates(tmp_path, monkeypatch):
    # 同一IPからの並列リクエストで read-modify-write が競合し、カウントを
    # 取りこぼす（ロックを回避できてしまう）ことがないことを確認する。
    monkeypatch.setattr(config, "LOGIN_FAILURES_FILE", tmp_path / "login_failures.json")
    ip = "203.0.113.99"
    n = 50
    threads = [threading.Thread(target=auth.record_failure, args=(ip,)) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = auth._load_failures()
    assert data[ip][0] == n
