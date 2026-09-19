"""自動アップデート配信の署名用 Ed25519 鍵ペアを生成する（配布者向け）。

使い方:
    python gen_signing_key.py 秘密鍵の保存先ファイル
秘密鍵（raw 32byte の base64）を指定ファイルへ権限 600 で保存し、対応する
公開鍵（raw 32byte の base64）を標準出力に表示する。
- 秘密鍵はリリース時に `SIGNING_KEY=<パス> ./release.sh` で使う（絶対に公開しない）。
- 公開鍵は利用者側の環境変数 CARD_NOTIFY_UPDATE_PUBKEY に設定してもらう。
"""
import base64
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1]:
        sys.exit("使い方: python gen_signing_key.py 秘密鍵の保存先ファイル")
    path = sys.argv[1]
    if os.path.exists(path):
        sys.exit(f"既にファイルが存在するため上書きしません: {path}")

    key = Ed25519PrivateKey.generate()
    private_b64 = base64.b64encode(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    ).decode()
    public_b64 = base64.b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).decode()

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(private_b64 + "\n")

    print(f"秘密鍵を保存しました（権限 600）: {path}")
    print("公開鍵（利用者が CARD_NOTIFY_UPDATE_PUBKEY に設定する値）:")
    print(public_b64)


if __name__ == "__main__":
    main()
