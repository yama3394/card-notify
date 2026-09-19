"""Flask アプリ本体: LINE Webhook + WebUI。

ローカル 127.0.0.1:5000 で待ち受け、前段のリバースプロキシ(nginx 等)で
HTTPS 終端する前提。単体では起動用エントリだが、本番は gunicorn/systemd 推奨。
"""
import base64
import fcntl
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import requests
from flask import (
    Flask, g, jsonify, make_response, redirect,
    render_template, request, session,
)
from werkzeug.security import generate_password_hash

import analyzer
import auth
import config
import gmail_fetcher
import manual_entry
import notifier
import oauth_setup
import parsers
import reports
import schedule
import storage

app = Flask(__name__, template_folder="web/templates", static_folder="web/static")
# 初回セットアップ完了前は config.SECRET_KEY が空なので、プロセス内だけの一時鍵で
# セッション/CSRFを動かす（/setup 完了時に config.reload() 後の値へ差し替える）。
app.secret_key = config.SECRET_KEY or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=config.SESSION_LIFETIME_DAYS)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=config.SESSION_COOKIE_SECURE,
    SESSION_COOKIE_SAMESITE="Lax",
    # 受け付ける最大リクエストサイズ。アップロードは credentials.json（数KB）のみ
    # なので 1MB で十分（超過は Flask が 413 を返す）。
    MAX_CONTENT_LENGTH=1024 * 1024,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Security headers ──────────────────────────────────────────────────────────

@app.after_request
def set_security_headers(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    # テンプレートに inline <script>/onchange と inline <style>/style= があるため
    # script-src・style-src に 'unsafe-inline' を許可。他はローカル配信で 'self'。
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'"
    )
    return response


@app.before_request
def inject_csrf():
    # /setup* は未ログインだが CSRF トークンが要る（フォームの検証に使う）。
    needs_token = session.get("logged_in") or request.path.startswith("/setup")
    g.csrf_token = auth.generate_csrf_token() if needs_token else ""


@app.context_processor
def inject_types():
    # 全テンプレートに種別一覧（レジストリ由来）を渡す。パーサを register する
    # だけでダッシュボード・週次・月次・履歴の表示に新種別が現れる。
    return {"type_keys": parsers.type_keys(), "type_labels": parsers.type_labels()}


# ── セットアップゲート ──────────────────────────────────────────────────────────
# 必須の機密値（パスワード・LINEトークン等）が未設定なら /setup 以外へのアクセスを
# すべて /setup に誘導する。設定済みなら逆に /setup への再アクセスを /login に流す
# （インストール後は Web ウィザードでのみ初期設定できるため CLI setup.py 相当）。
_SETUP_EXEMPT_PREFIXES = ("/static", "/webhook")


@app.before_request
def setup_gate():
    if request.path.startswith(_SETUP_EXEMPT_PREFIXES):
        return None
    if config.is_configured():
        if request.path == "/setup":
            return redirect("/login")
        return None
    if request.path != "/setup":
        return redirect("/setup")
    return None


# ── セットアップトークン ────────────────────────────────────────────────────────
# 未設定状態の /setup は認証を持たないため、公開URL経由で第三者が先に設定を
# 完了する「初期セットアップの乗っ取り」が成立し得る。インストーラ（install.sh）
# またはアプリが生成するワンタイムトークンの提示を必須にして所有者を確認する。

def _get_or_create_setup_token() -> str:
    """セットアップトークンを取得する（無ければ生成してログに出す）。

    install.sh がインストール時に生成・表示するのが正規ルート。手動起動
    （python app.py）ではここで生成され、起動ログ/ジャーナルで確認できる。
    """
    path = config.SETUP_TOKEN_FILE
    try:
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    logger.warning(
        "セットアップトークンを生成しました: %s "
        "（/setup で入力してください。%s にも保存されています）", token, path,
    )
    return token


def _verify_setup_token(submitted: str) -> bool:
    expected = _get_or_create_setup_token()
    # 非ASCII入力で compare_digest が TypeError にならないよう bytes で比較する。
    return bool(submitted) and hmac.compare_digest(
        submitted.strip().encode("utf-8"), expected.encode("utf-8")
    )


def _discard_setup_token() -> None:
    try:
        config.SETUP_TOKEN_FILE.unlink()
    except OSError:
        pass


# ── LINE Webhook ───────────────────────────────────────────────────────────────

def _verify_line_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(config.LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


_EVENT_DEDUP_TTL = 24 * 3600  # webhookEventId の記録保持期間（秒）


def _is_duplicate_event(event_id: str) -> bool:
    """LINE の再配達（at-least-once 配信）を webhookEventId で重複排除する。

    排他ロック下で「既出判定＋記録」を1トランザクションで行い、多重登録
    （現金支出の二重計上等）を防ぐ。ID が取れないイベントは判定しない。
    """
    if not event_id:
        return False
    path = config.WEBHOOK_EVENTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    duplicate = False
    with path.open("a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            content = f.read()
            try:
                seen = json.loads(content) if content.strip() else {}
            except ValueError:
                seen = {}
            if not isinstance(seen, dict):
                seen = {}
            if event_id in seen:
                duplicate = True
            else:
                seen = {k: v for k, v in seen.items() if now - v < _EVENT_DEDUP_TTL}
                seen[event_id] = now
                f.seek(0)
                f.truncate()
                json.dump(seen, f)
                f.flush()  # ロック解放前に OS へ書き切る（他モジュールの update() と同じ理由）
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    if not duplicate:
        os.chmod(path, 0o600)
    return duplicate


@app.route("/webhook", methods=["POST"])
def webhook():
    # セットアップ完了前は拒否。未設定時は LINE_CHANNEL_SECRET が空文字のため
    # 署名検証が空鍵 HMAC となり無意味（攻撃者が署名を自作できてしまう）。
    if not config.is_configured():
        return "Service Unavailable", 503

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not _verify_line_signature(body, signature):
        logger.warning("LINE署名検証失敗")
        return "Forbidden", 403

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return "Bad Request", 400

    for event in data.get("events", []):
        if event.get("type") != "message":
            continue
        msg = event.get("message", {})
        if msg.get("type") != "text":
            continue

        user_id = event.get("source", {}).get("userId", "")

        # オーナー認可: LINE署名は「LINEから来たこと」しか保証しないため、Botを
        # 友だち追加した第三者のメッセージも署名は通る。未登録の初回のみ登録し、
        # 登録済みなら本人以外のイベントは無視（通知乗っ取り・データ注入を防止）。
        if not config.LINE_USER_ID:
            if user_id:
                _save_user_id(user_id)
        elif user_id != config.LINE_USER_ID:
            logger.warning(f"未登録ユーザーからのメッセージを無視: {user_id}")
            continue

        # LINE は at-least-once 配信のため同一イベントが再送され得る。
        if _is_duplicate_event(event.get("webhookEventId", "")):
            logger.info("再配達イベントをスキップ: %s", event.get("webhookEventId"))
            continue

        reply = manual_entry.process(msg.get("text", ""))
        _line_reply(event.get("replyToken"), reply)

    return "OK", 200


def _save_user_id(user_id: str) -> None:
    """初回メッセージ時に LINE_USER_ID を runtime_state.json にアトミック保存する。"""
    if config.LINE_USER_ID == user_id:
        return
    config.LINE_USER_ID = user_id
    path = config.RUNTIME_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)

    state = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            state = loaded
    except (OSError, ValueError):
        pass
    state["line_user_id"] = user_id
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(state, ensure_ascii=False))
    os.replace(tmp, path)
    logger.info(f"LINE_USER_ID を登録しました: {user_id}")


def _line_reply(reply_token: str, text: str) -> None:
    if not reply_token:
        return
    try:
        requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Authorization": f"Bearer {config.LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]},
            timeout=10,
        ).raise_for_status()
    except Exception as e:
        logger.error(f"LINE返信失敗: {e}")


# ── Auth ───────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect("/dashboard" if session.get("logged_in") else "/login")


@app.route("/login", methods=["GET"])
def login_page():
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def login_post():
    ip = auth._get_ip()
    remaining = auth.lock_remaining(ip)
    if remaining:
        return render_template("login.html", error=f"ロック中: あと{remaining}分")

    if auth.verify_password(request.form.get("password", "")):
        auth.clear_failure(ip)
        auth.do_login()
        # Gmail 未連携ならセットアップの続き（/setup/gmail）へ誘導する。
        if not config.TOKEN_FILE.exists():
            return redirect("/setup/gmail")
        return redirect("/dashboard")

    auth.record_failure(ip)
    return render_template("login.html", error="パスワードが違います")


@app.route("/logout")
def logout():
    auth.do_logout()
    return redirect("/login")


# ── Setup wizard（初回セットアップ。curl|bash でTTYが無い環境でも完結させるため
#    CLI の setup.py 相当をブラウザで行えるようにする） ─────────────────────────

@app.route("/setup", methods=["GET"])
def setup_page():
    # トークンが未生成ならここで生成しログへ出す（install.sh 経由なら生成済み）。
    _get_or_create_setup_token()
    return render_template("setup.html", error=None, csrf_token=g.csrf_token, site_url="")


@app.route("/setup", methods=["POST"])
def setup_post():
    def _error(message: str, site_url: str = ""):
        return render_template("setup.html", error=message, csrf_token=g.csrf_token, site_url=site_url)

    if not auth.verify_csrf(request.form.get("csrf_token", "")):
        return _error("CSRFエラー。もう一度お試しください")

    # 所有者確認: インストーラが表示したセットアップトークンの提示を必須にする
    # （公開URL到達者による初期設定の乗っ取り防止）。
    if not _verify_setup_token(request.form.get("setup_token", "")):
        return _error(
            "セットアップトークンが違います。インストール時に表示されたトークン"
            f"（サーバーの {config.SETUP_TOKEN_FILE} でも確認できます）を入力してください"
        )

    password = request.form.get("password", "")
    password_confirm = request.form.get("password_confirm", "")
    line_token = request.form.get("line_token", "").strip()
    line_secret = request.form.get("line_secret", "").strip()
    site_url = request.form.get("site_url", "").strip().rstrip("/")

    if len(password) < 8:
        return _error("パスワードは8文字以上にしてください", site_url)
    if password != password_confirm:
        return _error("パスワードが一致しません", site_url)
    if not line_token or not line_secret:
        return _error("LINEのチャンネルアクセストークンとチャンネルシークレットは必須です", site_url)

    values = {
        "LINE_CHANNEL_ACCESS_TOKEN": line_token,
        "LINE_CHANNEL_SECRET": line_secret,
        "WEB_PASSWORD": generate_password_hash(password),
        "SECRET_KEY": secrets.token_hex(32),
    }
    if site_url:
        values["SITE_URL"] = site_url
        # 公開 URL が HTTPS なら Secure Cookie を env ファイルに書き込む。
        # ただし実行中プロセスの app.config には反映しない（HTTP の LAN アクセスで
        # ウィザードを進めている場合、即時反映すると次の /setup/gmail でセッション
        # Cookie が保存されず壊れるため）。サービス再起動後に有効になる。
        if site_url.startswith("https://"):
            values["SESSION_COOKIE_SECURE"] = "true"

    config.write_secrets(values)
    config.reload()
    app.secret_key = config.SECRET_KEY
    _discard_setup_token()  # 使い捨て（以降 /setup は設定済みゲートで閉じる）

    auth.do_login()
    return redirect("/setup/gmail")


def _gmail_setup_page(error: str | None = None):
    credentials_exists = config.CREDENTIALS_FILE.exists()
    auth_url = None
    if credentials_exists and not error:
        try:
            auth_url, verifier = oauth_setup.build_auth_url()
            session["oauth_verifier"] = verifier
        except (FileNotFoundError, ValueError) as e:
            error = str(e)
            credentials_exists = False
    return render_template(
        "gmail_setup.html", error=error, credentials_exists=credentials_exists,
        auth_url=auth_url, csrf_token=g.csrf_token,
    )


@app.route("/setup/gmail", methods=["GET"])
@auth.login_required
def setup_gmail_page():
    return _gmail_setup_page()


@app.route("/setup/gmail/credentials", methods=["POST"])
@auth.login_required
def setup_gmail_credentials():
    if not auth.verify_csrf(request.form.get("csrf_token", "")):
        return _gmail_setup_page(error="CSRFエラー。もう一度お試しください")

    file = request.files.get("credentials_file")
    if not file or not file.filename:
        return _gmail_setup_page(error="ファイルを選択してください")

    try:
        oauth_setup.save_credentials(file.read())
    except ValueError as e:
        return _gmail_setup_page(error=str(e))
    return redirect("/setup/gmail")


@app.route("/setup/gmail/exchange", methods=["POST"])
@auth.login_required
def setup_gmail_exchange():
    if not auth.verify_csrf(request.form.get("csrf_token", "")):
        return _gmail_setup_page(error="CSRFエラー。もう一度お試しください")

    verifier = session.get("oauth_verifier")
    if not verifier:
        return _gmail_setup_page(error="認証URLの有効期限が切れました。もう一度やり直してください")

    redirect_url = request.form.get("redirect_url", "").strip()
    try:
        code = parse_qs(urlparse(redirect_url).query)["code"][0]
    except (KeyError, IndexError):
        return _gmail_setup_page(error="URLからcodeを取得できませんでした。貼り付けたURLを確認してください")

    try:
        oauth_setup.exchange_code(code, verifier)
    except Exception as e:
        logger.error(f"Gmail OAuth 認証に失敗: {e}")
        return _gmail_setup_page(error="Gmail認証に失敗しました。もう一度やり直してください")

    session.pop("oauth_verifier", None)
    return redirect("/dashboard")


@app.route("/setup/gmail/skip")
@auth.login_required
def setup_gmail_skip():
    return redirect("/dashboard")


# ── Dashboard / resend ─────────────────────────────────────────────────────────

@app.route("/dashboard")
@auth.login_required
def dashboard():
    try:
        data = analyzer.analyze()
    except Exception:
        data = None
    return render_template("dashboard.html", data=data, csrf_token=g.csrf_token)


@app.route("/resend", methods=["POST"])
@auth.login_required
def resend():
    if not auth.verify_csrf(request.form.get("csrf_token", "")):
        return '<p style="color:red">CSRFエラー</p>', 403
    try:
        notifier.send_daily_report(analyzer.analyze())
        return '<p style="color:green">LINEに再送信しました</p>'
    except Exception as e:
        logger.error(f"LINE再送信失敗: {e}")
        return '<p style="color:red">再送信に失敗しました</p>', 500


# ── Monthly ────────────────────────────────────────────────────────────────────

@app.route("/monthly")
@auth.login_required
def monthly():
    today = analyzer.today_jst()
    year, month = today.year, today.month
    month_param = request.args.get("month", "")
    if month_param:
        try:
            parts = month_param.split("-")
            year, month = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            pass

    try:
        report = reports.generate_monthly(year, month)
    except Exception:
        report = None

    months = []
    y, m = today.year, today.month
    for _ in range(12):
        months.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1

    if request.headers.get("HX-Request") == "true":
        return render_template("monthly_partial.html", report=report)
    return render_template(
        "monthly.html", report=report, months=months,
        selected_year=year, selected_month=month, csrf_token=g.csrf_token,
    )


# ── Manual entry (HTMX) ────────────────────────────────────────────────────────

@app.route("/entry", methods=["POST"])
@auth.login_required
def entry():
    if not auth.verify_csrf(request.form.get("csrf_token", "")):
        return '<p style="color:red">CSRFエラー</p>', 403

    parts = [request.form.get("amount", "").strip()]
    if request.form.get("store", "").strip():
        parts.append(request.form.get("store", "").strip())
    if request.form.get("date", "").strip():
        parts.append(request.form.get("date", "").strip())

    reply = manual_entry.process(" ".join(parts))
    safe = (reply.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("\n", "<br>"))
    return f"<p>{safe}</p>"


# ── Weekly ─────────────────────────────────────────────────────────────────────

@app.route("/weekly")
@auth.login_required
def weekly():
    from datetime import date, timedelta as _td
    target = analyzer.today_jst()
    week_param = request.args.get("week", "")
    if week_param:
        try:
            target = date.fromisoformat(week_param)
        except ValueError:
            pass

    week_start = target - _td(days=target.weekday())
    report = analyzer.analyze_week(target)
    return render_template(
        "weekly.html", report=report,
        prev_week=(week_start - _td(days=1)).isoformat(),
        next_week=(week_start + _td(days=7)).isoformat(),
        can_next=(week_start + _td(days=7)) <= analyzer.today_jst(),
        csrf_token=g.csrf_token,
    )


# ── Settings（通知時刻） ─────────────────────────────────────────────────────────

@app.route("/settings")
@auth.login_required
def settings_page():
    hour, minute = schedule.get_notify_time()
    return render_template(
        "settings.html", hour=hour, minute=minute,
        hours=range(24), minutes=range(60), csrf_token=g.csrf_token,
    )


@app.route("/settings/notify-time", methods=["POST"])
@auth.login_required
def settings_notify_time():
    if not auth.verify_csrf(request.form.get("csrf_token", "")):
        return '<p style="color:red">CSRFエラー</p>', 403

    # 重要操作なので変更時はログインパスワードを再入力させる。
    if not auth.verify_password(request.form.get("password", "")):
        return '<p style="color:red">⚠️ パスワードが違います。変更していません。</p>'

    try:
        hour = int(request.form.get("hour", ""))
        minute = int(request.form.get("minute", ""))
    except ValueError:
        return '<p style="color:red">⚠️ 時刻が不正です。</p>'

    try:
        schedule.set_notify_time(hour, minute)
    except ValueError as e:
        return f'<p style="color:red">⚠️ {e}</p>'
    except Exception as e:
        logger.error(f"通知時刻の変更に失敗: {e}")
        return '<p style="color:red">⚠️ 変更に失敗しました（サーバーログを確認）。</p>'

    return f'<p style="color:green">✅ 通知時刻を {hour:02d}:{minute:02d} に変更しました</p>'


# ── Cash ───────────────────────────────────────────────────────────────────────

@app.route("/cash")
@auth.login_required
def cash_list():
    txs = [t for t in storage.load_history().get("transactions", []) if t.get("type") == "cash"]
    txs.sort(key=lambda t: t.get("date", ""), reverse=True)
    return render_template("cash.html", transactions=txs, csrf_token=g.csrf_token)


@app.route("/cash/delete", methods=["POST"])
@auth.login_required
def cash_delete():
    if not auth.verify_csrf(request.form.get("csrf_token", "")):
        return '<p style="color:red">CSRFエラー</p>', 403
    tx_id = request.form.get("id", "")

    def _delete(data):
        data["transactions"] = [t for t in data["transactions"] if t["id"] != tx_id]

    storage.update_history(_delete)
    return ""


@app.route("/cash/edit", methods=["POST"])
@auth.login_required
def cash_edit():
    if not auth.verify_csrf(request.form.get("csrf_token", "")):
        return '<p style="color:red">CSRFエラー</p>', 403
    tx_id = request.form.get("id", "")
    store = request.form.get("store", "").strip() or None
    date_str = request.form.get("date", "").strip()

    try:
        amount = int(request.form.get("amount", "").strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        return '<p style="color:red">⚠️ 金額が不正です</p>', 400

    from datetime import date as _date
    try:
        _date.fromisoformat(date_str)
    except ValueError:
        return '<p style="color:red">⚠️ 日付が不正です</p>', 400

    def _edit(data):
        for t in data["transactions"]:
            if t["id"] == tx_id and t.get("type") == "cash":
                t["amount"], t["store"], t["date"] = amount, store, date_str
                break

    storage.update_history(_edit)
    return '<p style="color:green">✅ 更新しました</p>'


# ── Errors（店舗名なしカード取引） ──────────────────────────────────────────────

@app.route("/errors")
@auth.login_required
def errors():
    data = storage.load_history()
    txs = [
        t for t in data.get("transactions", [])
        if t.get("store") is None and t.get("type") != "cash"
    ]
    txs.sort(key=lambda t: t.get("date", ""), reverse=True)
    skipped = sorted(data.get("skipped", []), key=lambda s: s.get("date", ""), reverse=True)
    return render_template("errors.html", transactions=txs, skipped=skipped, csrf_token=g.csrf_token)


@app.route("/errors/delete", methods=["POST"])
@auth.login_required
def errors_delete():
    if not auth.verify_csrf(request.form.get("csrf_token", "")):
        return '<p style="color:red">CSRFエラー</p>', 403
    tx_id = request.form.get("id", "")

    def _delete(data):
        data["transactions"] = [t for t in data["transactions"] if t["id"] != tx_id]

    storage.update_history(_delete)
    return ""


@app.route("/errors/fix", methods=["POST"])
@auth.login_required
def errors_fix():
    if not auth.verify_csrf(request.form.get("csrf_token", "")):
        return '<p style="color:red">CSRFエラー</p>', 403
    tx_id = request.form.get("id", "")
    store = request.form.get("store", "").strip()
    if not tx_id:
        return '<p style="color:red">IDが不正です</p>', 400

    def _fix(data):
        for t in data["transactions"]:
            if t["id"] == tx_id:
                t["store"] = store or None
                if "raw_text" in t and store:
                    del t["raw_text"]
                break

    storage.update_history(_fix)
    return '<p style="color:green">✅ 更新しました</p>'


@app.route("/errors/skipped/dismiss", methods=["POST"])
@auth.login_required
def errors_skipped_dismiss():
    if not auth.verify_csrf(request.form.get("csrf_token", "")):
        return '<p style="color:red">CSRFエラー</p>', 403
    skip_id = request.form.get("id", "")

    def _dismiss(data):
        # skipped_ids（再取得防止用）は残したまま、一覧表示用の skipped だけから消す。
        data["skipped"] = [s for s in data.get("skipped", []) if s.get("id") != skip_id]

    storage.update_history(_dismiss)
    return ""


@app.route("/errors/skipped/register", methods=["POST"])
@auth.login_required
def errors_skipped_register():
    if not auth.verify_csrf(request.form.get("csrf_token", "")):
        return '<p style="color:red">CSRFエラー</p>', 403

    skip_id = request.form.get("id", "")
    type_ = request.form.get("type", "").strip()
    store = request.form.get("store", "").strip() or None
    amount_str = request.form.get("amount", "").strip()
    date_str = request.form.get("date", "").strip()

    if not skip_id:
        return '<p style="color:red">IDが不正です</p>', 400
    if type_ not in set(parsers.type_keys()) - {"cash"}:
        return '<p style="color:red">⚠️ 種別が不正です</p>', 400
    try:
        amount = int(amount_str)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return '<p style="color:red">⚠️ 金額が不正です</p>', 400
    from datetime import date as _date
    try:
        _date.fromisoformat(date_str)
    except ValueError:
        return '<p style="color:red">⚠️ 日付が不正です</p>', 400

    new_tx = {
        "id": skip_id,
        "date": date_str,
        "amount": amount,
        "type": type_,
        "store": store,
        "currency": "JPY",
    }

    def _register(data):
        data.setdefault("transactions", []).append(new_tx)
        data["skipped"] = [s for s in data.get("skipped", []) if s.get("id") != skip_id]

    storage.update_history(_register)
    return '<p style="color:green">✅ 登録しました</p>'


# ── Transactions（全種別） ──────────────────────────────────────────────────────

@app.route("/transactions")
@auth.login_required
def transactions_list():
    today = analyzer.today_jst()
    year, month = today.year, today.month
    month_param = request.args.get("month", "")
    if month_param:
        try:
            parts = month_param.split("-")
            year, month = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            pass

    month_str = f"{year}-{month:02d}"
    txs = sorted(
        [t for t in storage.load_history().get("transactions", []) if t.get("date", "").startswith(month_str)],
        key=lambda t: t.get("date", ""), reverse=True,
    )
    months = []
    y, m = today.year, today.month
    for _ in range(12):
        months.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1

    return render_template(
        "transactions.html", transactions=txs, months=months,
        selected_year=year, selected_month=month, csrf_token=g.csrf_token,
    )


@app.route("/transaction/delete", methods=["POST"])
@auth.login_required
def transaction_delete():
    if not auth.verify_csrf(request.form.get("csrf_token", "")):
        return '<p style="color:red">CSRFエラー</p>', 403
    tx_id = request.form.get("id", "")

    def _delete(data):
        data["transactions"] = [t for t in data["transactions"] if t["id"] != tx_id]

    storage.update_history(_delete)
    return ""


@app.route("/transaction/edit", methods=["POST"])
@auth.login_required
def transaction_edit():
    if not auth.verify_csrf(request.form.get("csrf_token", "")):
        return '<p style="color:red">CSRFエラー</p>', 403
    tx_id = request.form.get("id", "")
    store = request.form.get("store", "").strip() or None
    date_str = request.form.get("date", "").strip()

    try:
        # 外貨決済は小数額があり得る（例: 12.50 USD）。整数に収まる値は int に戻す。
        amount_f = float(request.form.get("amount", "").strip())
        if amount_f <= 0:
            raise ValueError
        amount = int(amount_f) if amount_f == int(amount_f) else amount_f
    except ValueError:
        return '<p style="color:red">⚠️ 金額が不正です</p>', 400

    from datetime import date as _date
    try:
        _date.fromisoformat(date_str)
    except ValueError:
        return '<p style="color:red">⚠️ 日付が不正です</p>', 400

    def _edit(data):
        for t in data["transactions"]:
            if t["id"] == tx_id:
                t["amount"], t["store"], t["date"] = amount, store, date_str
                if "raw_text" in t and store:
                    del t["raw_text"]
                break

    storage.update_history(_edit)
    return '<p style="color:green">✅ 更新しました</p>'


# ── CSV Export ─────────────────────────────────────────────────────────────────

@app.route("/export/csv")
@auth.login_required
def export_csv():
    import csv
    import io
    today = analyzer.today_jst()
    year = request.args.get("year", today.year, type=int)
    month = request.args.get("month", today.month, type=int)

    month_str = f"{year}-{month:02d}"
    txs = sorted(
        [t for t in storage.load_history().get("transactions", []) if t.get("date", "").startswith(month_str)],
        key=lambda t: t.get("date", ""),
    )
    labels = parsers.type_labels()

    # 店舗名はメール由来（外部入力）。Excel 等が数式として解釈する先頭文字は
    # クォートして CSV インジェクションを防ぐ。
    def _csv_safe(value: str) -> str:
        if value and value[0] in "=+-@\t\r":
            return "'" + value
        return value

    output = io.StringIO()
    writer = csv.writer(output)
    # 外貨決済（KRW等）が円と無区別に合算されないよう、通貨列を分けて出力する
    writer.writerow(["日付", "種別", "金額", "通貨", "店舗名"])
    for t in txs:
        writer.writerow([
            t.get("date", ""),
            labels.get(t.get("type", ""), t.get("type", "")),
            t.get("amount", 0),
            t.get("currency", "JPY"),
            _csv_safe(t.get("store", "") or ""),
        ])

    resp = make_response(output.getvalue().encode("utf-8-sig"))
    resp.headers["Content-Type"] = "text/csv; charset=utf-8-sig"
    resp.headers["Content-Disposition"] = f"attachment; filename=card_{year}_{month:02d}.csv"
    return resp


# ── Chart data APIs ────────────────────────────────────────────────────────────

def _type_list() -> list[dict]:
    """グラフの凡例生成用に種別一覧を [{key, label}, ...] で返す。"""
    labels = parsers.type_labels()
    return [{"key": k, "label": labels.get(k, k)} for k in parsers.type_keys()]


@app.route("/api/monthly-trend")
@auth.login_required
def api_monthly_trend():
    return jsonify({"types": _type_list(), "months": analyzer.monthly_trend(6)})


@app.route("/api/weekly-data")
@auth.login_required
def api_weekly_data():
    from datetime import date
    target = analyzer.today_jst()
    week_param = request.args.get("week", "")
    if week_param:
        try:
            target = date.fromisoformat(week_param)
        except ValueError:
            pass
    report = analyzer.analyze_week(target)
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    days = [
        {
            "date": d["date"].isoformat(),
            "label": f"{d['date'].month}/{d['date'].day}({weekdays[i]})",
            "by_type": d["by_type"], "total": d["total"],
        }
        for i, d in enumerate(report["days"])
    ]
    return jsonify({"types": _type_list(), "days": days})


if __name__ == "__main__":
    import scheduler

    # scheduler.start() は secrets を要求しない（tick 内で main.run() が
    # require_secrets するだけなので、未セットアップ中は無害な例外ログのみ）。
    # /setup 完了後にプロセス再起動なしで日次ジョブが動くよう常に起動しておく。
    scheduler.start()
    app.run(host="127.0.0.1", port=config.PORT, threaded=True)
