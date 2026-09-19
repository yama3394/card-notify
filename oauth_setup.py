"""Gmail OAuth を対話的に通し token.json を生成する（一度だけ実行）。

手順:
  1. Google Cloud で OAuth クライアント(デスクトップ)を作り credentials.json を
     ダウンロードして DATA_DIR（既定 data/）に置く。
  2. このスクリプトを実行 → 表示された URL をブラウザで開いて許可。
  3. 「このサイトにアクセスできません」の画面になったら、アドレスバーの
     http://localhost/?code=... を丸ごとコピーして貼り付ける。

client_secret や PKCE verifier は一時ファイルに書かず、プロセスのメモリ内だけで
扱う（旧実装の /tmp 平文保存を廃止）。

save_credentials / build_auth_url / exchange_code は、ブラウザ版セットアップ
ウィザード（app.py の Flask ルート）からも呼び出される再利用可能な関数。
"""
import base64
import hashlib
import json
import os
import sys
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from google.oauth2.credentials import Credentials

import config

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
REDIRECT_URI = "http://localhost"


def _parse_credentials(raw: bytes) -> dict:
    """credentials.json のバイト列を検証し installed/web 部分を返す。

    構造上の問題があれば ValueError（日本語メッセージ）を送出する。
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(
            "credentials.json の形式が不正です（JSON として読み込めません）。"
        ) from e

    creds_data = data.get("installed") or data.get("web")
    if not creds_data:
        raise ValueError(
            "credentials.json の形式が不正です（installed/web が見つからない）。"
        )
    if not creds_data.get("client_id") or not creds_data.get("client_secret"):
        raise ValueError(
            "credentials.json の形式が不正です（client_id/client_secret が見つからない）。"
        )
    return creds_data


def _read_credentials() -> dict:
    """config.CREDENTIALS_FILE を都度読み直して installed/web 部分を返す。"""
    if not config.CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"{config.CREDENTIALS_FILE} がありません。OAuth クライアントの "
            f"credentials.json を配置してください。"
        )
    raw = config.CREDENTIALS_FILE.read_bytes()
    return _parse_credentials(raw)


def save_credentials(raw: bytes) -> None:
    """アップロードされた credentials.json のバイト列を検証して保存する。"""
    _parse_credentials(raw)
    config.CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CREDENTIALS_FILE.write_bytes(raw)
    os.chmod(config.CREDENTIALS_FILE, 0o600)


def build_auth_url() -> tuple[str, str]:
    """認可 URL と PKCE verifier を返す。

    config.CREDENTIALS_FILE が存在しなければ FileNotFoundError、
    形式が不正なら ValueError を送出する。呼び出す度にファイルを読み直す。
    """
    creds_data = _read_credentials()
    client_id = creds_data["client_id"]
    auth_uri = creds_data.get("auth_uri", "https://accounts.google.com/o/oauth2/auth")

    # PKCE
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = auth_uri + "?" + urlencode(params)
    return auth_url, verifier


def exchange_code(code: str, verifier: str) -> None:
    """認可コードをトークンに交換し config.TOKEN_FILE に保存する。

    config.CREDENTIALS_FILE を読み直して client_id/client_secret を取得する。
    HTTP エラーは resp.raise_for_status() でそのまま送出する。
    """
    creds_data = _read_credentials()
    client_id = creds_data["client_id"]
    client_secret = creds_data["client_secret"]

    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json()

    creds = Credentials(
        token=token["access_token"],
        refresh_token=token.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=[SCOPE],
    )
    config.TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(config.TOKEN_FILE, 0o600)


def main() -> None:
    try:
        auth_url, verifier = build_auth_url()
    except FileNotFoundError as e:
        sys.exit(f"❌ {e}")
    except ValueError as e:
        sys.exit(f"❌ {e}")

    print("\n" + "=" * 60)
    print("次の URL をブラウザで開いて許可してください:\n")
    print(auth_url)
    print("\n許可後の http://localhost/?code=... の URL を貼り付けてください。")
    print("=" * 60)
    redirect_url = input("\nURL: ").strip()

    try:
        code = parse_qs(urlparse(redirect_url).query)["code"][0]
    except (KeyError, IndexError):
        sys.exit("❌ URL から code を取得できませんでした。")

    exchange_code(code, verifier)

    print(f"\n✅ 認証成功。{config.TOKEN_FILE} を保存しました。")
    token = json.loads(config.TOKEN_FILE.read_text(encoding="utf-8"))
    if not token.get("refresh_token"):
        print("⚠️ refresh_token が返っていません。Google 側で一度アクセスを取り消してから"
              "再実行してください（access_type=offline & prompt=consent が必要）。")


if __name__ == "__main__":
    main()
