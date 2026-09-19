"""Web層（認証・CSRF・セットアップゲート・webhookオーナー認可）のテスト。

Flask test client を使い、config のモジュールグローバルと DATA_DIR / ENV_FILE を
テストごとに tmp_path へ隔離する。LINE への外部HTTP（_line_reply / notifier.push）
は monkeypatch で遮断する。
"""
import base64
import hashlib
import hmac
import json
import os
import threading

import pytest
from werkzeug.security import generate_password_hash

import app as app_module
import auth
import config
import manual_entry
import notifier
import schedule
import storage

# パスワードハッシュの生成は重いので、軽量な方式でモジュールロード時に1回だけ作る。
PASSWORD = "correct-password-123"
PASSWORD_HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")
CSRF_TOKEN = "test-csrf-token"

# config.reload() が os.environ を直接書き換えるため、テスト前に退避・掃除し
# テスト後に完全復元する対象のキー。
_SECRET_ENV_KEYS = (
    "LINE_CHANNEL_ACCESS_TOKEN",
    "LINE_CHANNEL_SECRET",
    "SECRET_KEY",
    "WEB_PASSWORD",
    "SITE_URL",
    "SESSION_COOKIE_SECURE",
    "CARD_NOTIFY_TRUSTED_IP_HEADER",
)

_DATA_FILES = {
    "HISTORY_FILE": "history.json",
    "STATE_FILE": "notify_state.json",
    "RUNTIME_STATE_FILE": "runtime_state.json",
    "LOGIN_FAILURES_FILE": "login_failures.json",
    "SETTINGS_FILE": "settings.json",
    "SCHEMA_VERSION_FILE": "schema_version.json",
    "TOKEN_FILE": "token.json",
    "CREDENTIALS_FILE": "credentials.json",
    "SETUP_TOKEN_FILE": "setup_token",
    "WEBHOOK_EVENTS_FILE": "webhook_events.json",
}


@pytest.fixture
def web_app(tmp_path, monkeypatch):
    """未設定状態の隔離済み Flask アプリを返す。

    - ENV_FILE / DATA_DIR 配下の全パスを tmp_path へ差し替え
    - config の機密値グローバルを空（＝未設定状態）に
    - LINE への外部HTTPを遮断
    - os.environ と app.secret_key はテスト後に復元（グローバル状態のリーク防止）
    """
    env_backup = os.environ.copy()
    for key in _SECRET_ENV_KEYS:
        os.environ.pop(key, None)

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "_ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    for attr, name in _DATA_FILES.items():
        monkeypatch.setattr(config, attr, data_dir / name)

    for key in ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET",
                "SECRET_KEY", "WEB_PASSWORD", "SITE_URL", "LINE_USER_ID"):
        monkeypatch.setattr(config, key, "")
    monkeypatch.setattr(config, "TRUSTED_IP_HEADER", "")

    # LINE への外部HTTPは必ず遮断する。
    monkeypatch.setattr(app_module, "_line_reply", lambda reply_token, text: None)
    monkeypatch.setattr(notifier, "push", lambda text: None)

    saved_secret_key = app_module.app.secret_key
    yield app_module.app
    # POST /setup 成功時に app.secret_key が差し替わるため元へ戻す。
    app_module.app.secret_key = saved_secret_key
    os.environ.clear()
    os.environ.update(env_backup)


@pytest.fixture
def configured_app(web_app, monkeypatch):
    """設定済み状態（機密値が揃っている）のアプリを返す。"""
    monkeypatch.setattr(config, "LINE_CHANNEL_ACCESS_TOKEN", "test-line-token")
    monkeypatch.setattr(config, "LINE_CHANNEL_SECRET", "test-line-secret")
    monkeypatch.setattr(config, "SECRET_KEY", "0" * 64)
    monkeypatch.setattr(config, "WEB_PASSWORD", PASSWORD_HASH)
    return web_app


def _login_session(client) -> str:
    """セッションに直接ログイン状態と CSRF トークンを書き込む。"""
    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["csrf_token"] = CSRF_TOKEN
    return CSRF_TOKEN


def _sign(secret: str, body: bytes) -> str:
    """LINE の X-Line-Signature（HMAC-SHA256 → base64）を自作する。"""
    return base64.b64encode(
        hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("utf-8")


def _webhook_body(user_id: str, text: str = "1200 ランチ") -> bytes:
    return json.dumps({
        "events": [{
            "type": "message",
            "replyToken": "test-reply-token",
            "source": {"userId": user_id},
            "message": {"type": "text", "text": text},
        }]
    }).encode("utf-8")


def _post_webhook(client, body: bytes, signature: str):
    return client.post(
        "/webhook", data=body, content_type="application/json",
        headers={"X-Line-Signature": signature},
    )


# ── 1. セットアップゲート ──────────────────────────────────────────────────────

class TestSetupGate:
    def test_root_redirects_to_setup_when_unconfigured(self, web_app):
        resp = web_app.test_client().get("/")
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/setup"

    def test_dashboard_redirects_to_setup_when_unconfigured(self, web_app):
        resp = web_app.test_client().get("/dashboard")
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/setup"

    def test_webhook_returns_503_when_unconfigured(self, web_app):
        """旧脆弱性の回帰テスト: 未設定時は LINE_CHANNEL_SECRET が空のため、
        攻撃者が「空シークレット」で正しい形式の署名を自作できてしまう。
        その署名を付けても 503 で拒否されること。"""
        body = _webhook_body("U-attacker")
        forged_signature = _sign("", body)  # 空シークレットで自作した署名
        resp = _post_webhook(web_app.test_client(), body, forged_signature)
        assert resp.status_code == 503

    def test_static_is_exempt_from_gate(self, web_app):
        resp = web_app.test_client().get("/static/htmx.min.js")
        assert resp.status_code == 200

    def test_setup_redirects_to_login_when_configured(self, configured_app):
        resp = configured_app.test_client().get("/setup")
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/login"


# ── 2. セットアップウィザード ──────────────────────────────────────────────────

class TestSetupWizard:
    def _csrf(self, client) -> str:
        """GET /setup でセッションに発行された CSRF トークンを取り出す。"""
        assert client.get("/setup").status_code == 200
        with client.session_transaction() as sess:
            return sess["csrf_token"]

    SETUP_TOKEN = "test-setup-token"

    def _form(self, csrf_token: str, **overrides) -> dict:
        # 所有者確認用のセットアップトークンを設置した上でフォームを組む。
        config.SETUP_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.SETUP_TOKEN_FILE.write_text(self.SETUP_TOKEN + "\n", encoding="utf-8")
        form = {
            "csrf_token": csrf_token,
            "setup_token": self.SETUP_TOKEN,
            "password": "long-enough-password",
            "password_confirm": "long-enough-password",
            "line_token": "wizard-line-token",
            "line_secret": "wizard-line-secret",
            "site_url": "",
        }
        form.update(overrides)
        return form

    def test_post_without_csrf_is_rejected(self, web_app):
        client = web_app.test_client()
        self._csrf(client)
        resp = client.post("/setup", data=self._form(""))
        assert "CSRFエラー" in resp.get_data(as_text=True)
        assert not config.is_configured()

    def test_wrong_setup_token_is_rejected(self, web_app):
        client = web_app.test_client()
        token = self._csrf(client)
        resp = client.post("/setup", data=self._form(token, setup_token="wrong-token"))
        assert "セットアップトークンが違います" in resp.get_data(as_text=True)
        assert not config.is_configured()

    def test_missing_setup_token_is_rejected(self, web_app):
        client = web_app.test_client()
        token = self._csrf(client)
        resp = client.post("/setup", data=self._form(token, setup_token=""))
        assert "セットアップトークンが違います" in resp.get_data(as_text=True)
        assert not config.is_configured()

    def test_short_password_is_rejected(self, web_app):
        client = web_app.test_client()
        token = self._csrf(client)
        resp = client.post("/setup", data=self._form(token, password="short", password_confirm="short"))
        assert "8文字以上" in resp.get_data(as_text=True)
        assert not config.is_configured()

    def test_password_mismatch_is_rejected(self, web_app):
        client = web_app.test_client()
        token = self._csrf(client)
        resp = client.post("/setup", data=self._form(token, password_confirm="different-password"))
        assert "一致しません" in resp.get_data(as_text=True)
        assert not config.is_configured()

    def test_valid_post_writes_env_and_configures(self, web_app, tmp_path):
        client = web_app.test_client()
        token = self._csrf(client)
        resp = client.post("/setup", data=self._form(token))
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/setup/gmail"

        env_text = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "LINE_CHANNEL_ACCESS_TOKEN=wizard-line-token" in env_text
        assert "LINE_CHANNEL_SECRET=wizard-line-secret" in env_text
        assert "WEB_PASSWORD=" in env_text
        assert "SECRET_KEY=" in env_text
        assert config.is_configured()
        # トークンは使い捨て（成功後に削除される）。
        assert not config.SETUP_TOKEN_FILE.exists()

    def test_https_site_url_writes_secure_cookie(self, web_app, tmp_path):
        client = web_app.test_client()
        token = self._csrf(client)
        resp = client.post("/setup", data=self._form(token, site_url="https://kakeibo.example.com"))
        assert resp.status_code == 302
        env_text = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "SITE_URL=https://kakeibo.example.com" in env_text
        assert "SESSION_COOKIE_SECURE=true" in env_text

    def test_http_site_url_does_not_write_secure_cookie(self, web_app, tmp_path):
        client = web_app.test_client()
        token = self._csrf(client)
        resp = client.post("/setup", data=self._form(token, site_url="http://192.168.1.10:5000"))
        assert resp.status_code == 302
        env_text = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "SESSION_COOKIE_SECURE" not in env_text


# ── 3. ログイン / 試行ロック ───────────────────────────────────────────────────

class TestLoginLock:
    def test_correct_password_redirects_to_gmail_setup_without_token(self, configured_app):
        # Gmail token 未連携ならセットアップの続きへ誘導される。
        resp = configured_app.test_client().post("/login", data={"password": PASSWORD})
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/setup/gmail"

    def test_correct_password_redirects_to_dashboard_with_token(self, configured_app):
        config.TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.TOKEN_FILE.write_text("{}", encoding="utf-8")
        resp = configured_app.test_client().post("/login", data={"password": PASSWORD})
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/dashboard"

    def test_wrong_password_shows_error(self, configured_app):
        resp = configured_app.test_client().post("/login", data={"password": "wrong"})
        assert resp.status_code == 200
        assert "パスワードが違います" in resp.get_data(as_text=True)

    def test_three_failures_lock_even_correct_password(self, configured_app):
        client = configured_app.test_client()
        for _ in range(3):
            client.post("/login", data={"password": "wrong"})
        resp = client.post("/login", data={"password": PASSWORD})
        assert resp.status_code == 200
        assert "ロック中" in resp.get_data(as_text=True)

    def test_spoofed_header_cannot_bypass_lock(self, configured_app):
        """回帰テスト: TRUSTED_IP_HEADER 未設定時は X-Real-IP 等の偽装ヘッダを
        無視し、remote_addr 基準でロックされること。"""
        client = configured_app.test_client()
        for i in range(3):
            client.post(
                "/login", data={"password": "wrong"},
                headers={"X-Real-IP": f"10.0.0.{i}"},
                environ_overrides={"REMOTE_ADDR": "203.0.113.7"},
            )
        # 4回目: ヘッダを変えても remote_addr が同じならロックされている。
        resp = client.post(
            "/login", data={"password": PASSWORD},
            headers={"X-Real-IP": "10.0.0.99"},
            environ_overrides={"REMOTE_ADDR": "203.0.113.7"},
        )
        assert resp.status_code == 200
        assert "ロック中" in resp.get_data(as_text=True)

    def test_trusted_header_is_used_when_configured(self, configured_app, monkeypatch):
        """TRUSTED_IP_HEADER を明示指定したときだけそのヘッダを IP として使う
        （ヘッダ値が毎回違えば別クライアント扱いでロックされない）。"""
        monkeypatch.setattr(config, "TRUSTED_IP_HEADER", "X-Real-IP")
        client = configured_app.test_client()
        for i in range(3):
            client.post(
                "/login", data={"password": "wrong"},
                headers={"X-Real-IP": f"10.0.0.{i}"},
            )
        resp = client.post(
            "/login", data={"password": PASSWORD},
            headers={"X-Real-IP": "10.0.0.99"},
        )
        assert resp.status_code == 302  # ロックされず成功

    def test_successful_login_clears_failures(self, configured_app):
        client = configured_app.test_client()
        for _ in range(2):
            client.post("/login", data={"password": "wrong"},
                        environ_overrides={"REMOTE_ADDR": "203.0.113.8"})
        assert "203.0.113.8" in auth._load_failures()

        resp = client.post("/login", data={"password": PASSWORD},
                           environ_overrides={"REMOTE_ADDR": "203.0.113.8"})
        assert resp.status_code == 302
        assert "203.0.113.8" not in auth._load_failures()


# ── 4. login_required ─────────────────────────────────────────────────────────

class TestLoginRequired:
    @pytest.mark.parametrize("path", [
        "/dashboard", "/settings", "/cash", "/transactions", "/export/csv",
    ])
    def test_redirects_to_login_when_not_logged_in(self, configured_app, path):
        resp = configured_app.test_client().get(path)
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/login"


# ── 5. CSRF ───────────────────────────────────────────────────────────────────

class TestCsrf:
    def test_entry_without_token_returns_403(self, configured_app):
        client = configured_app.test_client()
        _login_session(client)
        resp = client.post("/entry", data={"amount": "1000"})
        assert resp.status_code == 403
        assert "CSRFエラー" in resp.get_data(as_text=True)

    def test_entry_with_wrong_token_returns_403(self, configured_app):
        client = configured_app.test_client()
        _login_session(client)
        resp = client.post("/entry", data={"amount": "1000", "csrf_token": "wrong-token"})
        assert resp.status_code == 403

    def test_entry_with_valid_token_succeeds(self, configured_app, monkeypatch):
        client = configured_app.test_client()
        token = _login_session(client)
        monkeypatch.setattr(manual_entry, "process", lambda msg: "登録しました")
        resp = client.post("/entry", data={"amount": "1000", "csrf_token": token})
        assert resp.status_code == 200
        assert "登録しました" in resp.get_data(as_text=True)

    def test_notify_time_without_token_returns_403(self, configured_app):
        client = configured_app.test_client()
        _login_session(client)
        resp = client.post("/settings/notify-time",
                           data={"hour": "9", "minute": "30", "password": PASSWORD})
        assert resp.status_code == 403
        assert schedule.get_notify_time() == (config.DEFAULT_NOTIFY_HOUR, config.DEFAULT_NOTIFY_MINUTE)

    def test_notify_time_with_valid_token_succeeds(self, configured_app):
        client = configured_app.test_client()
        token = _login_session(client)
        resp = client.post("/settings/notify-time", data={
            "hour": "9", "minute": "30", "password": PASSWORD, "csrf_token": token,
        })
        assert resp.status_code == 200
        assert "09:30" in resp.get_data(as_text=True)
        assert schedule.get_notify_time() == (9, 30)


# ── 6. webhook オーナー認可 ────────────────────────────────────────────────────

class TestWebhookOwner:
    SECRET = "test-line-secret"  # configured_app の LINE_CHANNEL_SECRET

    @pytest.fixture
    def process_calls(self, monkeypatch):
        """manual_entry.process の呼び出しを記録するスタブ。"""
        calls = []
        monkeypatch.setattr(
            manual_entry, "process",
            lambda msg: calls.append(msg) or "OKです",
        )
        return calls

    def test_first_message_registers_user_id(self, configured_app, process_calls):
        body = _webhook_body("U-first-owner")
        resp = _post_webhook(configured_app.test_client(), body, _sign(self.SECRET, body))
        assert resp.status_code == 200

        state = json.loads(config.RUNTIME_STATE_FILE.read_text(encoding="utf-8"))
        assert state["line_user_id"] == "U-first-owner"
        assert config.LINE_USER_ID == "U-first-owner"
        assert process_calls == ["1200 ランチ"]

    def test_other_user_is_ignored(self, configured_app, process_calls, monkeypatch):
        monkeypatch.setattr(config, "LINE_USER_ID", "U-owner")
        body = _webhook_body("U-mallory")
        resp = _post_webhook(configured_app.test_client(), body, _sign(self.SECRET, body))
        assert resp.status_code == 200
        assert process_calls == []  # 本人以外は process が呼ばれない
        assert config.LINE_USER_ID == "U-owner"  # 登録も上書きされない

    def test_owner_message_is_processed(self, configured_app, process_calls, monkeypatch):
        monkeypatch.setattr(config, "LINE_USER_ID", "U-owner")
        body = _webhook_body("U-owner", text="500 コーヒー")
        resp = _post_webhook(configured_app.test_client(), body, _sign(self.SECRET, body))
        assert resp.status_code == 200
        assert process_calls == ["500 コーヒー"]

    def test_redelivered_event_is_processed_once(self, configured_app, process_calls, monkeypatch):
        # LINE は at-least-once 配信。同じ webhookEventId の再送は1回だけ処理する。
        monkeypatch.setattr(config, "LINE_USER_ID", "U-owner")
        body = json.dumps({
            "events": [{
                "type": "message",
                "webhookEventId": "01ABCDEF",
                "replyToken": "test-reply-token",
                "source": {"userId": "U-owner"},
                "message": {"type": "text", "text": "800 セブン"},
            }]
        }).encode("utf-8")
        client = configured_app.test_client()
        assert _post_webhook(client, body, _sign(self.SECRET, body)).status_code == 200
        assert _post_webhook(client, body, _sign(self.SECRET, body)).status_code == 200
        assert process_calls == ["800 セブン"]  # 2回目はスキップ

    def test_invalid_signature_returns_403(self, configured_app, process_calls):
        body = _webhook_body("U-owner")
        resp = _post_webhook(configured_app.test_client(), body, _sign("wrong-secret", body))
        assert resp.status_code == 403
        assert process_calls == []


# ── CSV エクスポートのインジェクション対策 ─────────────────────────────────────

class TestCsvExport:
    def test_formula_cells_are_quoted(self, configured_app, monkeypatch):
        import storage
        monkeypatch.setattr(storage, "load_history", lambda: {
            "transactions": [
                {"id": "1", "date": "2026-07-01", "amount": 100,
                 "type": "cash", "store": "=SUM(A1:A9)"},
                {"id": "2", "date": "2026-07-02", "amount": 200,
                 "type": "cash", "store": "セブン"},
            ],
            "skipped_ids": [],
        })
        client = configured_app.test_client()
        _login_session(client)
        resp = client.get("/export/csv?year=2026&month=7")
        text = resp.get_data(as_text=True)
        assert "'=SUM(A1:A9)" in text  # 先頭 = はクォートされる
        assert "セブン" in text
        assert ",=SUM" not in text

    def test_currency_column_separates_foreign(self, configured_app, monkeypatch):
        # 外貨取引が円と無区別に「金額」列へ混ざらないよう、通貨列を持つこと
        import storage
        monkeypatch.setattr(storage, "load_history", lambda: {
            "transactions": [
                {"id": "1", "date": "2026-07-01", "amount": 1200,
                 "type": "smcc", "store": "コンビニ"},
                {"id": "2", "date": "2026-07-02", "amount": 10950,
                 "type": "smcc", "store": "OLIVE YOUNG", "currency": "KRW"},
            ],
            "skipped_ids": [],
        })
        client = configured_app.test_client()
        _login_session(client)
        resp = client.get("/export/csv?year=2026&month=7")
        text = resp.get_data(as_text=True).replace("\r", "")
        assert "通貨" in text.splitlines()[0]
        assert "1200,JPY" in text
        assert "10950,KRW" in text


# ── 7. 設定変更の再認証 ────────────────────────────────────────────────────────

class TestSettingsReauth:
    def test_wrong_password_does_not_change_notify_time(self, configured_app):
        client = configured_app.test_client()
        token = _login_session(client)
        resp = client.post("/settings/notify-time", data={
            "hour": "9", "minute": "30", "password": "wrong-password", "csrf_token": token,
        })
        assert resp.status_code == 200
        assert "パスワードが違います" in resp.get_data(as_text=True)


# ── 8. flush 漏れの回帰テスト ───────────────────────────────────────────────────

class TestDedupFlush:
    """ロック保持中に f.flush() してから解放する規律（他モジュールと同じ）の回帰テスト。"""

    def test_content_is_readable_immediately_after_write(self, configured_app):
        assert app_module._is_duplicate_event("event-xyz") is False
        raw = json.loads(config.WEBHOOK_EVENTS_FILE.read_text(encoding="utf-8"))
        assert "event-xyz" in raw

    def test_file_permissions_are_0600(self, configured_app):
        app_module._is_duplicate_event("event-perm")
        mode = config.WEBHOOK_EVENTS_FILE.stat().st_mode & 0o777
        assert mode == 0o600

    def test_concurrent_calls_not_lost(self, configured_app):
        ids = [f"event-{i}" for i in range(20)]
        threads = [
            threading.Thread(target=app_module._is_duplicate_event, args=(eid,))
            for eid in ids
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        raw = json.loads(config.WEBHOOK_EVENTS_FILE.read_text(encoding="utf-8"))
        assert set(raw.keys()) == set(ids)


# ── 9. /errors: 金額抽出不可メール（skipped）一覧 ───────────────────────────────

class TestErrorsSkipped:
    def _seed_skipped(self, entries):
        def _seed(data):
            data["skipped"] = entries
        storage.update_history(_seed)

    def test_errors_page_lists_skipped_sorted_desc(self, configured_app):
        self._seed_skipped([
            {"id": "m1", "type": "smcc", "date": "2026-07-01", "subject": "Aメール", "reason": "no_amount"},
            {"id": "m2", "type": "jcb", "date": "2026-07-03", "subject": "Bメール", "reason": "no_amount"},
        ])
        client = configured_app.test_client()
        _login_session(client)
        resp = client.get("/errors")
        assert resp.status_code == 200
        text = resp.get_data(as_text=True)
        assert text.index("Bメール") < text.index("Aメール")  # 日付降順

    def test_skipped_dismiss_removes_entry_keeps_skipped_ids(self, configured_app):
        self._seed_skipped([
            {"id": "m1", "type": "smcc", "date": "2026-07-01", "subject": "A", "reason": "no_amount"},
        ])

        def _seed_ids(data):
            data["skipped_ids"] = ["m1"]
        storage.update_history(_seed_ids)

        client = configured_app.test_client()
        token = _login_session(client)
        resp = client.post("/errors/skipped/dismiss", data={"csrf_token": token, "id": "m1"})
        assert resp.status_code == 200

        data = storage.load_history()
        assert data["skipped"] == []
        assert "m1" in data["skipped_ids"]  # skipped_ids は残す

    def test_skipped_register_creates_transaction_and_removes_from_skipped(self, configured_app):
        self._seed_skipped([
            {"id": "m1", "type": "smcc", "date": "2026-07-01", "subject": "A", "reason": "no_amount"},
        ])
        client = configured_app.test_client()
        token = _login_session(client)
        resp = client.post("/errors/skipped/register", data={
            "csrf_token": token, "id": "m1", "type": "smcc",
            "amount": "1500", "store": "セブン", "date": "2026-07-01",
        })
        assert resp.status_code == 200

        data = storage.load_history()
        assert data["skipped"] == []
        assert data["transactions"] == [{
            "id": "m1", "date": "2026-07-01", "amount": 1500,
            "type": "smcc", "store": "セブン", "currency": "JPY",
        }]

    def test_skipped_register_without_store_is_none(self, configured_app):
        self._seed_skipped([
            {"id": "m1", "type": "smcc", "date": "2026-07-01", "subject": "A", "reason": "no_amount"},
        ])
        client = configured_app.test_client()
        token = _login_session(client)
        resp = client.post("/errors/skipped/register", data={
            "csrf_token": token, "id": "m1", "type": "smcc", "amount": "1500", "date": "2026-07-01",
        })
        assert resp.status_code == 200
        assert storage.load_history()["transactions"][0]["store"] is None

    @pytest.mark.parametrize("amount", ["0", "-100", "abc", ""])
    def test_skipped_register_rejects_invalid_amount(self, configured_app, amount):
        self._seed_skipped([
            {"id": "m1", "type": "smcc", "date": "2026-07-01", "subject": "A", "reason": "no_amount"},
        ])
        client = configured_app.test_client()
        token = _login_session(client)
        resp = client.post("/errors/skipped/register", data={
            "csrf_token": token, "id": "m1", "type": "smcc", "amount": amount, "date": "2026-07-01",
        })
        assert resp.status_code == 400
        assert storage.load_history()["transactions"] == []

    def test_skipped_register_rejects_invalid_date(self, configured_app):
        self._seed_skipped([
            {"id": "m1", "type": "smcc", "date": "2026-07-01", "subject": "A", "reason": "no_amount"},
        ])
        client = configured_app.test_client()
        token = _login_session(client)
        resp = client.post("/errors/skipped/register", data={
            "csrf_token": token, "id": "m1", "type": "smcc", "amount": "1000", "date": "not-a-date",
        })
        assert resp.status_code == 400
        assert storage.load_history()["transactions"] == []

    def test_skipped_register_rejects_invalid_type(self, configured_app):
        self._seed_skipped([
            {"id": "m1", "type": "smcc", "date": "2026-07-01", "subject": "A", "reason": "no_amount"},
        ])
        client = configured_app.test_client()
        token = _login_session(client)
        resp = client.post("/errors/skipped/register", data={
            # "cash" は type_keys() - {cash} に含まれないため不正
            "csrf_token": token, "id": "m1", "type": "cash", "amount": "1000", "date": "2026-07-01",
        })
        assert resp.status_code == 400
        assert storage.load_history()["transactions"] == []

    def test_skipped_register_requires_csrf(self, configured_app):
        self._seed_skipped([
            {"id": "m1", "type": "smcc", "date": "2026-07-01", "subject": "A", "reason": "no_amount"},
        ])
        client = configured_app.test_client()
        _login_session(client)
        resp = client.post("/errors/skipped/register", data={
            "id": "m1", "type": "smcc", "amount": "1000", "date": "2026-07-01",
        })
        assert resp.status_code == 403


# ── 10. /transaction/edit: raw_text の後始末 ────────────────────────────────────

class TestTransactionEditRawText:
    def test_removes_raw_text_when_store_given(self, configured_app):
        def _seed(data):
            data["transactions"] = [{
                "id": "t1", "date": "2026-07-01", "amount": 500, "type": "smcc",
                "store": None, "currency": "JPY", "raw_text": "raw mail body",
            }]
        storage.update_history(_seed)

        client = configured_app.test_client()
        token = _login_session(client)
        resp = client.post("/transaction/edit", data={
            "csrf_token": token, "id": "t1", "amount": "500",
            "store": "セブン", "date": "2026-07-01",
        })
        assert resp.status_code == 200

        tx = storage.load_history()["transactions"][0]
        assert tx["store"] == "セブン"
        assert "raw_text" not in tx

    def test_keeps_raw_text_when_store_left_blank(self, configured_app):
        def _seed(data):
            data["transactions"] = [{
                "id": "t1", "date": "2026-07-01", "amount": 500, "type": "smcc",
                "store": None, "currency": "JPY", "raw_text": "raw mail body",
            }]
        storage.update_history(_seed)

        client = configured_app.test_client()
        token = _login_session(client)
        resp = client.post("/transaction/edit", data={
            "csrf_token": token, "id": "t1", "amount": "500", "store": "", "date": "2026-07-01",
        })
        assert resp.status_code == 200

        tx = storage.load_history()["transactions"][0]
        assert tx["store"] is None
        assert tx["raw_text"] == "raw mail body"
        assert schedule.get_notify_time() == (config.DEFAULT_NOTIFY_HOUR, config.DEFAULT_NOTIFY_MINUTE)
