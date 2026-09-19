"""migrate.py のテスト。

storage / migrate はどちらも `config` モジュールの属性（HISTORY_FILE /
SCHEMA_VERSION_FILE）を呼び出し時に参照するため、config 側を monkeypatch すれば
両モジュールに確実に効く。
"""
import json

import config
import migrate
import storage


def _redirect_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SCHEMA_VERSION_FILE", tmp_path / "schema_version.json")
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "history.json")


def test_fresh_install_runs_to_latest(monkeypatch, tmp_path):
    """新規インストール（版数無し・history 空）でも run() が通り latest になる。"""
    _redirect_paths(monkeypatch, tmp_path)
    assert migrate.current_version() == 0

    applied = migrate.run()
    assert applied == ["v1: add_currency"]
    assert migrate.current_version() == migrate.latest_version()


def test_idempotent(monkeypatch, tmp_path):
    """再実行は空リスト（二重適用しない）。"""
    _redirect_paths(monkeypatch, tmp_path)
    migrate.run()
    assert migrate.run() == []
    assert migrate.current_version() == migrate.latest_version()


def test_m1_backfills_currency(monkeypatch, tmp_path):
    """currency 欠落の取引に run 後 'JPY' が補完される。既存値は保持。"""
    _redirect_paths(monkeypatch, tmp_path)
    storage.save_history({
        "transactions": [
            {"amount": 800, "store": "セブン"},                 # currency 欠落
            {"amount": 5, "store": "USD店", "currency": "USD"},  # 既存値あり
        ],
        "skipped_ids": [],
    })

    migrate.run()

    data = storage.load_history()
    assert data["transactions"][0]["currency"] == "JPY"
    assert data["transactions"][1]["currency"] == "USD"


def test_corrupt_version_file_treated_as_zero(monkeypatch, tmp_path):
    """壊れた schema_version ファイルでも current_version() は 0。"""
    _redirect_paths(monkeypatch, tmp_path)
    config.SCHEMA_VERSION_FILE.write_text("{ this is not json", encoding="utf-8")
    assert migrate.current_version() == 0

    applied = migrate.run()
    assert applied == ["v1: add_currency"]
    assert migrate.current_version() == migrate.latest_version()


def test_missing_version_file_is_zero(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    assert not config.SCHEMA_VERSION_FILE.exists()
    assert migrate.current_version() == 0
