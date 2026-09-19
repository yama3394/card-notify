"""パーサレジストリの基本動作と拡張性。"""
from contextlib import contextmanager
from datetime import date

import analyzer
import notifier
import parsers
import parsers.base
from parsers.base import CardParser, register


@contextmanager
def _temp_registry():
    """レジストリはモジュールグローバルなので、テスト後に元へ戻す。"""
    saved = dict(parsers.base._registry)
    try:
        yield
    finally:
        parsers.base._registry.clear()
        parsers.base._registry.update(saved)


def _dummy_parser(key: str = "demo", label: str = "Demo") -> CardParser:
    return CardParser(
        key=key,
        label=label,
        from_addrs=["demo@example.com"],
        amount_markers=["利用金額"],
        date_markers=[r"利用日"],
        store_patterns=[r"利用先[：:\s]+(.+?)[\n\r]"],
        is_cancellation=lambda text: "取消" in text,
        is_ignorable=lambda text: False,
    )


def test_builtin_parsers_registered():
    keys = {p.key for p in parsers.all_parsers()}
    assert {"smcc", "jcb"} <= keys


def test_type_labels_includes_cash():
    labels = parsers.type_labels()
    assert labels["smcc"] == "SMCC"
    assert labels["jcb"] == "JCB"
    assert labels["cash"] == "現金"


def test_type_keys_order_and_cash_last():
    keys = parsers.type_keys()
    assert keys == [p.key for p in parsers.all_parsers()] + ["cash"]
    assert keys[-1] == "cash"
    # type_labels() と同じキー集合を返す（画面・通知の表示順の基準）
    assert set(keys) == set(parsers.type_labels())


def test_get_unknown_returns_none():
    assert parsers.get("does-not-exist") is None


def test_adding_a_parser_is_one_call():
    """新カード対応 = CardParser を1つ register するだけ、を検証。"""
    with _temp_registry():
        register(_dummy_parser())
        p = parsers.get("demo")
        assert p is not None
        r = p.parse("利用日：2026/07/06\n利用先：テスト店\n利用金額：1,234円\n")
        assert (r.amount, r.currency, r.date, r.store) == (1234, "JPY", "2026-07-06", "テスト店")


class TestRegistryPropagation:
    """パーサを register するだけで集計・通知に新種別が現れることを検証。"""

    def test_type_keys_and_labels_pick_up_new_parser(self):
        with _temp_registry():
            register(_dummy_parser(key="dummy", label="DUMMY"))
            keys = parsers.type_keys()
            assert "dummy" in keys
            assert keys[-1] == "cash"  # 現金は常に末尾
            assert parsers.type_labels()["dummy"] == "DUMMY"

    def test_analyzer_by_type_includes_new_parser(self):
        txns = [
            {"id": "1", "date": "2026-07-04", "amount": 1200, "type": "smcc", "store": "A"},
            {"id": "2", "date": "2026-07-04", "amount": 500, "type": "dummy", "store": "B"},
        ]
        with _temp_registry():
            register(_dummy_parser(key="dummy", label="DUMMY"))
            d = analyzer._sum_by_date(txns, date(2026, 7, 4))
            assert d["by_type"]["dummy"] == 500
            assert d["by_type"]["jcb"] == 0  # 取引がない種別も0埋めされる
            assert d["total"] == 1700

    def test_daily_text_includes_new_parser_line(self):
        txns = [
            {"id": "1", "date": "2026-07-04", "amount": 1200, "type": "smcc", "store": "A"},
            {"id": "2", "date": "2026-07-04", "amount": 500, "type": "dummy", "store": "B"},
        ]
        with _temp_registry():
            register(_dummy_parser(key="dummy", label="DUMMY"))
            analysis = {
                "date": date(2026, 7, 4),
                "yesterday": analyzer._sum_by_date(txns, date(2026, 7, 4)),
                "week_total": 1700,
                "month_total": 1700,
                "last_week_same_day": 0,
                "diff": {"amount": 1700, "rate": None},
                "foreign": {},
            }
            text = notifier._build_daily_text(analysis)
            assert "DUMMY  ¥500" in text
            assert "SMCC  ¥1,200" in text
            assert "現金  ¥0" in text  # 0円の種別も表示される
