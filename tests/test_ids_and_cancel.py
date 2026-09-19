"""取引の連番 ID（"no"）と、LINE からの現金取消のテスト。

history.json / notify_state.json / schema_version.json は tmp_path へ隔離する。
時刻は clock["now"] で固定する（created_at と「今日」の両方をこれに揃える）。
"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

import config
import main
import manual_entry
import migrate
import notifier
import state
import storage
from manual_entry import process, process_line

JST = timezone(timedelta(hours=9))


@pytest.fixture
def clock(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "history.json")
    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "notify_state.json")
    monkeypatch.setattr(config, "SCHEMA_VERSION_FILE", tmp_path / "schema_version.json")
    c = {"now": datetime(2026, 9, 18, 0, 20, tzinfo=JST)}
    monkeypatch.setattr(storage, "_now_jst", lambda: c["now"])
    monkeypatch.setattr(manual_entry, "today_jst", lambda: c["now"].date())
    return c


def _txs() -> list[dict]:
    return storage.load_history()["transactions"]


def _write_legacy(txs: list[dict]) -> None:
    """v1.2.0 より前の history.json（"no" も "next_no" も無い）を用意する。"""
    config.HISTORY_FILE.write_text(
        json.dumps({"transactions": txs, "skipped_ids": [], "skipped": []}), encoding="utf-8",
    )


def _cash(id_: str, date_: str, amount: int = 800) -> dict:
    return {"id": id_, "date": date_, "amount": amount, "type": "cash", "store": None, "currency": "JPY"}


def _card(date_: str = "2026-09-18") -> dict:
    return {"id": "msg1", "date": date_, "amount": 1200, "type": "smcc", "store": "X", "currency": "JPY"}


def _delete_on_webui(tx_id: str) -> None:
    # app.py の /cash/delete と同じ処理
    storage.update_history(
        lambda d: d.__setitem__("transactions", [t for t in d["transactions"] if t["id"] != tx_id])
    )


# ---------------------------------------------------------------------------
# 連番の付与・移行
# ---------------------------------------------------------------------------

class TestNumbering:
    def test_legacy_data_is_numbered_in_list_order_without_created_at(self, clock):
        # リスト順＝登録順。日付順ではない（メール遅延で過去日付が後から入る）
        _write_legacy([_cash("a", "2026-09-10"), _cash("b", "2026-09-01"), _cash("c", "2026-09-05")])
        storage.update_history(lambda d: None)

        data = storage.load_history()
        assert [(t["id"], t["no"]) for t in data["transactions"]] == [("a", 1), ("b", 2), ("c", 3)]
        assert data["next_no"] == 4
        assert all("created_at" not in t for t in data["transactions"])

    def test_migration_m2_numbers_existing_data(self, clock):
        # 自動更新（auto_update → migrate.run）で、更新直後から WebUI・CSV に ID が出る
        _write_legacy([_cash("a", "2026-09-10"), _cash("b", "2026-09-11")])
        config.SCHEMA_VERSION_FILE.write_text(json.dumps({"version": 1}), encoding="utf-8")

        assert migrate.run() == ["v2: add_transaction_numbers"]
        assert [t["no"] for t in _txs()] == [1, 2]
        assert migrate.run() == []  # 冪等
        assert [t["no"] for t in _txs()] == [1, 2]

    def test_cash_entry_gets_no_and_created_at(self, clock):
        _write_legacy([_cash("a", "2026-09-10")])
        reply = process("800 セブン")

        t = _txs()[-1]
        assert t["no"] == 2
        assert t["created_at"] == "2026-09-18T00:20:00+09:00"
        assert reply.splitlines()[1] == "　ID: #2"

    def test_card_transaction_is_numbered_too(self, clock):
        # メール取込（_merge）も /errors の手動登録も update_history 経由なので同じく採番される
        storage.update_history(lambda d: d["transactions"].append(_card("2026-09-17")))
        t = _txs()[0]
        assert t["no"] == 1
        assert t["created_at"].startswith("2026-09-18")

    def test_numbers_are_not_reused_after_delete(self, clock):
        process("800")
        _delete_on_webui(_txs()[0]["id"])
        process("500")
        assert [t["no"] for t in _txs()] == [2]

    def test_today_list_shows_id_for_cash_only(self, clock):
        storage.update_history(lambda d: d["transactions"].append(_card()))
        process("800 セブン")
        lines = process_line("今日").splitlines()
        assert any("¥1,200" in l and "#" not in l for l in lines)
        assert any("¥800 (#2)" in l for l in lines)


# ---------------------------------------------------------------------------
# 「取消」（直前の1件）
# ---------------------------------------------------------------------------

class TestCancelLast:
    @pytest.mark.parametrize("word", ["取消", "取消し", "取り消し", "とりけし", " 取消 ", "取消　"])
    def test_cancel_words(self, clock, word):
        process("800 セブン")
        reply = process_line(word)
        assert reply.startswith("🗑 2026/9/18 (金) の現金支出 ¥800 を取り消しました")
        assert "　ID: #1" in reply
        assert "本日現金合計: ¥0" in reply
        assert _txs() == []

    def test_only_the_last_one_can_be_cancelled(self, clock):
        process("800")
        process("500")
        assert "¥500 を取り消しました" in process_line("取消")
        assert "取り消せる直前の登録がありません" in process_line("取消")
        assert [t["amount"] for t in _txs()] == [800]

    def test_sent_at_0020_can_be_cancelled_at_2359(self, clock):
        process("800")
        clock["now"] = datetime(2026, 9, 18, 23, 59, tzinfo=JST)
        assert "取り消しました" in process_line("取消")
        assert _txs() == []

    def test_sent_at_2359_cannot_be_cancelled_after_midnight(self, clock):
        clock["now"] = datetime(2026, 9, 18, 23, 59, tzinfo=JST)
        process("800")
        clock["now"] = datetime(2026, 9, 19, 0, 1, tzinfo=JST)
        assert "#1 は今日送信した登録ではないため取り消せません" in process_line("取消")
        assert len(_txs()) == 1

    def test_past_dated_entry_sent_today_can_be_cancelled(self, clock):
        # 「当日」は支出日ではなく送信日で判定する
        process("800 セブン 昨日")
        reply = process_line("取消")
        assert reply.startswith("🗑 2026/9/17 (木) の現金支出 ¥800 を取り消しました")
        assert "9/17の現金合計: ¥0" in reply
        assert _txs() == []

    def test_last_entry_deleted_on_webui(self, clock):
        process("800")
        _delete_on_webui(_txs()[0]["id"])
        assert "取り消せる直前の登録がありません" in process_line("取消")

    def test_nothing_registered_yet(self, clock):
        assert "取り消せる直前の登録がありません" in process_line("取消")


# ---------------------------------------------------------------------------
# 「#ID 取消」
# ---------------------------------------------------------------------------

class TestCancelById:
    @pytest.mark.parametrize("text", ["#1 取消し", "取消 #1", "＃１　取消", "#1取消", "取り消し#1"])
    def test_forms(self, clock, text):
        process("800")
        process("500")
        assert "¥800 を取り消しました" in process_line(text)
        assert [t["amount"] for t in _txs()] == [500]
        # 直前の登録（#2）は ID 指定取消の影響を受けず、引き続き「取消」で消せる
        assert "¥500 を取り消しました" in process_line("取消")

    def test_card_is_rejected(self, clock):
        storage.update_history(lambda d: d["transactions"].append(_card()))
        assert "カードの取引のため LINE からは取り消せません" in process_line("#1 取消")
        assert len(_txs()) == 1

    def test_not_found(self, clock):
        process("800")
        assert "#999 の取引は見つかりません" in process_line("#999 取消")
        assert len(_txs()) == 1

    def test_sent_yesterday_is_rejected(self, clock):
        clock["now"] = datetime(2026, 9, 17, 12, 0, tzinfo=JST)
        process("800")
        clock["now"] = datetime(2026, 9, 18, 12, 0, tzinfo=JST)
        assert "今日送信した登録ではない" in process_line("#1 取消")
        assert len(_txs()) == 1

    def test_legacy_entry_without_created_at_is_rejected(self, clock):
        _write_legacy([_cash("a", "2026-09-18")])
        storage.update_history(lambda d: None)
        assert "今日送信した登録ではない" in process_line("#1 取消")
        assert len(_txs()) == 1

    def test_cancelling_the_last_entry_by_id_consumes_it(self, clock):
        process("800")
        process_line("#1 取消")
        assert "取り消せる直前の登録がありません" in process_line("取消")


# ---------------------------------------------------------------------------
# 取消として扱わないもの
# ---------------------------------------------------------------------------

class TestNotACancel:
    def test_amount_with_cancel_word_is_still_a_registration(self, clock):
        # ID には # が必須。「800 取消」は従来どおり店舗名「取消」での登録
        assert "登録しました" in process_line("800 取消")
        assert _txs()[0]["store"] == "取消"

    def test_webui_entry_path_does_not_cancel(self, clock):
        # /entry は process() を直接呼ぶ。WebUI の登録フォームから取消は走らない
        process("800")
        assert "金額を送ってください" in process("取消")
        assert len(_txs()) == 1


# ---------------------------------------------------------------------------
# 日次通知の保留分との整合・「取消反映」欄
# ---------------------------------------------------------------------------

class TestNotices:
    def test_pending_late_arrival_is_withdrawn(self, clock):
        # 今日の日次通知は送信済み → 昨日付けの登録は「追加登録」として翌朝通知される予定
        clock["now"] = datetime(2026, 9, 18, 9, 0, tzinfo=JST)
        state.update(lambda s: s.update(last_daily="2026-09-18"))
        process("800 セブン 昨日")
        assert [a["no"] for a in state.load()["late_arrivals"]] == [1]

        process_line("取消")
        s = state.load()
        assert s["late_arrivals"] == []
        assert not s.get("cancellations")

    def test_already_reported_day_goes_to_cancellations(self, clock):
        # 00:20 に昨日付けで登録 → 朝の日次通知（9/17分）に載る → 夜に取消
        state.update(lambda s: s.update(last_daily="2026-09-17"))
        process("800 セブン 昨日")
        assert not state.load().get("late_arrivals")
        state.update(lambda s: s.update(last_daily="2026-09-18"))  # 朝の日次通知が送信された

        clock["now"] = datetime(2026, 9, 18, 23, 59, tzinfo=JST)
        process_line("取消")
        assert state.load()["cancellations"] == [
            {"date": "2026-09-17", "amount": 800, "currency": "JPY", "type": "cash", "store": "セブン"},
        ]

    def test_today_entry_needs_no_notice(self, clock):
        state.update(lambda s: s.update(last_daily="2026-09-17"))
        process("800")
        process_line("取消")
        s = state.load()
        assert not s.get("late_arrivals")
        assert not s.get("cancellations")

    def test_daily_text_has_cancellation_section(self):
        analysis = {
            "date": date(2026, 9, 18),
            "yesterday": {"by_type": {"cash": 0}, "total": 0},
            "week_total": 0, "month_total": 0,
            "diff": {"amount": 0, "rate": None},
            "foreign": {},
        }
        cancellations = [{"date": "2026-09-17", "amount": 800, "currency": "JPY", "type": "cash", "store": "セブン"}]
        assert "取消反映" not in notifier._build_daily_text(analysis)
        text = notifier._build_daily_text(analysis, [], cancellations)
        assert text.endswith("取消反映\n2026/09/17  -¥800  セブン")

    def test_pending_notices_are_popped_and_restored_together(self, clock):
        state.update(lambda s: s.update(late_arrivals=[{"store": "A"}], cancellations=[{"store": "B"}]))
        notices = main._pop_pending_notices()
        assert notices == {"late_arrivals": [{"store": "A"}], "cancellations": [{"store": "B"}]}
        assert state.load()["cancellations"] == []

        main._restore_pending_notices(notices)  # 送信失敗時
        assert state.load()["cancellations"] == [{"store": "B"}]
        assert state.load()["late_arrivals"] == [{"store": "A"}]
