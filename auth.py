"""WebUI の認証・ログイン試行ロック・CSRF。"""
import functools
import hmac
import json
import logging
import os
import threading
import time

from flask import redirect, request, session
from werkzeug.security import check_password_hash

import config

logger = logging.getLogger(__name__)

_LOCK_SECONDS = 600
_MAX_ATTEMPTS = 3
# Flask は threaded=True で動くため、同一IPからの並列リクエストによる
# ファイルの read-modify-write 競合（ロック回数の取りこぼし）を防ぐ。
_failures_lock = threading.Lock()


# ── ログイン試行ロック（ファイル永続化。再起動でロックが消えないように） ──

def _load_failures() -> dict:
    try:
        data = json.loads(config.LOGIN_FAILURES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_failures(data: dict) -> None:
    """アトミック保存（tmp→os.replace）。失敗してもログインは継続させる。"""
    try:
        config.LOGIN_FAILURES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = config.LOGIN_FAILURES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, config.LOGIN_FAILURES_FILE)
    except OSError as e:
        logger.error(f"ログイン試行の保存に失敗（ロックはベストエフォート）: {e}")


def _prune(data: dict) -> dict:
    """ロック窓を過ぎた古いエントリを掃除してファイル肥大を防ぐ。"""
    now = time.time()
    return {ip: v for ip, v in data.items() if now - v[1] < _LOCK_SECONDS}


def _get_ip() -> str:
    """ロック対象のクライアント IP。

    信用するヘッダは CARD_NOTIFY_TRUSTED_IP_HEADER で明示指定されたものだけ
    （リバースプロキシが必ず上書きする前提）。未指定なら remote_addr のみを
    使い、クライアントが自由に送れるヘッダによるロック回避を防ぐ。
    """
    if config.TRUSTED_IP_HEADER:
        header_value = request.headers.get(config.TRUSTED_IP_HEADER)
        if header_value:
            return header_value
    return request.remote_addr or "0.0.0.0"


def lock_remaining(ip: str) -> int | None:
    """ロック中なら残り分数、そうでなければ None。"""
    with _failures_lock:
        data = _load_failures()
        entry = data.get(ip)
        if not entry:
            return None
        count, first_time = entry
        if count < _MAX_ATTEMPTS:
            return None
        elapsed = time.time() - first_time
        if elapsed >= _LOCK_SECONDS:
            data = _prune(data)
            _save_failures(data)
            return None
        return max(1, int((_LOCK_SECONDS - elapsed) / 60) + 1)


def record_failure(ip: str) -> None:
    with _failures_lock:
        data = _prune(_load_failures())
        entry = data.get(ip)
        if entry is None or time.time() - entry[1] >= _LOCK_SECONDS:
            data[ip] = [1, time.time()]
        else:
            data[ip] = [entry[0] + 1, entry[1]]
        _save_failures(data)


def clear_failure(ip: str) -> None:
    with _failures_lock:
        data = _load_failures()
        if ip in data:
            del data[ip]
            _save_failures(data)


# ── パスワード / CSRF / セッション ──

def verify_password(password: str) -> bool:
    if not config.WEB_PASSWORD:
        return False
    return check_password_hash(config.WEB_PASSWORD, password)


def generate_csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = os.urandom(16).hex()
    return session["csrf_token"]


def verify_csrf(token: str) -> bool:
    expected = session.get("csrf_token") or ""
    if not expected or not token:
        return False
    # 非ASCII入力で compare_digest が TypeError にならないよう bytes で比較する。
    return hmac.compare_digest(str(token).encode("utf-8"), str(expected).encode("utf-8"))


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


def do_login() -> None:
    session.clear()
    session["logged_in"] = True
    session.permanent = True
    generate_csrf_token()


def do_logout() -> None:
    session.clear()
