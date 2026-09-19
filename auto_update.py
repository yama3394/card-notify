"""HTTPS 配信フィード（Cloudflare R2 等）からの自動アップデータ。

GitHub を使わず、配信元に置いた `version.json` と tar を見て、新しければ安全に
更新する。既定 OFF のオプトインで、systemd タイマーから定期実行される想定。

配信フォーマット（`{config.UPDATE_FEED_URL}/version.json`）:

    {"version": "1.1.0",
     "url": "card-notify-1.1.0.tar.gz",
     "sha256": "<hex>",
     "sig": "<base64>"}

- ``url`` はファイル名のみ（相対）でも絶対 URL でも可。相対なら
  ``UPDATE_FEED_URL`` を起点に解決する。
- ``sig`` は任意で、tar ファイルのバイト列に対する Ed25519 署名（base64）。
  ``config.UPDATE_PUBKEY``（CARD_NOTIFY_UPDATE_PUBKEY）が設定されている場合は
  必須となり、検証に失敗すると更新を中止する。未設定なら従来どおり
  sha256 のみ（警告ログを出す）。
- tar はアプリのコード一式（``data/`` と ``.env`` を含まない）で、展開すると
  ``INSTALL_DIR`` 直下に配置される構成。先頭に単一ディレクトリがある/無いは
  どちらも吸収する。tar 直下には ``release.sh`` が生成する ``MANIFEST``
  （同梱ファイルの相対パス一覧）が含まれる。

CLI:
    python auto_update.py            # タイマー用。AUTO_UPDATE=false なら何もしない
    python auto_update.py --check    # 取得と版数比較のみ（適用しない）
    python auto_update.py --force    # AUTO_UPDATE を無視して実行

更新手順（いずれかの段で失敗したらロールバックして再起動）:
    1. 設定検証   2. version.json 取得＋semver 比較
    3. バックアップ（data/＋現行コード）
    4. tar ダウンロード＋sha256 検証＋（公開鍵設定時）Ed25519 署名検証
    5. 展開してコードのみ MANIFEST ベースで同期（data/.env/token/credentials
       は除外。ファイル単位の上書きコピーで、ディレクトリを丸ごと消して
       置き換えることはしない。旧 MANIFEST にあり新 MANIFEST に無いファイルは
       削除して追従するが、利用者が追加したファイル（新旧どちらの MANIFEST にも
       無いもの。例: 独自 parsers/*.py）は保持する）
    6. pip install -r requirements.txt
    7. migrate.run()   8. systemctl restart
    9. ヘルスチェック  10. 古いバックアップ整理＋成否を LINE 通知
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests

import config

logger = logging.getLogger("auto_update")

# ── 定数 ──
_HTTP_TIMEOUT = 30           # version.json / tar 取得のタイムアウト（秒）
_DOWNLOAD_TIMEOUT = 300      # tar ダウンロードは大きくなり得るので長め
_HEALTH_RETRIES = 6          # ヘルスチェックの試行回数
_HEALTH_INTERVAL = 3         # ヘルスチェックの間隔（秒）
_HEALTH_OK = (200, 302)      # 成功とみなす HTTP ステータス
_BACKUP_DIRNAME = ".update-backup"

# コード上書き時・バックアップ時に絶対に触らないもの（データと機密）。
_PROTECTED = frozenset(
    {
        "data",
        ".env",
        "token.json",
        "credentials.json",
        _BACKUP_DIRNAME,
        ".git",
        "__pycache__",
        ".pytest_cache",
        "venv",
        ".venv",
    }
)


# ─────────────────────────────────────────────────────────────────────────
# 純粋関数（単体で検証可能）
# ─────────────────────────────────────────────────────────────────────────
def _parse_version(v: str) -> tuple[int, ...]:
    """"1.10.0" -> (1, 10, 0)。各要素の数値プレフィクスのみを見る簡易 semver。

    "1.2.0-rc1" のような接尾辞は数値部だけ取り出す。数値化できない要素は 0。
    """
    parts: list[int] = []
    for chunk in str(v).strip().split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)


def _is_newer(remote: str, local: str) -> bool:
    """remote が local より新しければ True（数値タプル比較）。

    要素数が違う場合は短い方を 0 埋めして比較する（"1.1" と "1.1.0" は同一）。
    """
    r = _parse_version(remote)
    l = _parse_version(local)
    width = max(len(r), len(l))
    r += (0,) * (width - len(r))
    l += (0,) * (width - len(l))
    return r > l


def _sha256_of_file(path: str | os.PathLike) -> str:
    """ファイルの sha256 を hex 文字列で返す（チャンク読み）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _verify_sha256(path: str | os.PathLike, expected: str) -> bool:
    """ファイルの sha256 が expected（hex, 大文字小文字問わず）と一致するか。"""
    if not expected:
        return False
    return _sha256_of_file(path).lower() == expected.strip().lower()


def _verify_signature(tar_path: str | os.PathLike, sig_b64: str, pubkey_b64: str) -> bool:
    """tar ファイルのバイト列に対する Ed25519 署名を検証する。

    - ``sig_b64``: 署名（base64）。
    - ``pubkey_b64``: raw 32byte の Ed25519 公開鍵（base64）。
    署名不一致・base64/鍵の形式不正はいずれも False（フェイルクローズ）。
    cryptography が未導入の場合は ImportError が伝播する（呼び出し側で失敗扱い）。
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not sig_b64 or not pubkey_b64:
        return False
    try:
        pubkey = Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64))
        signature = base64.b64decode(sig_b64)
        pubkey.verify(signature, Path(tar_path).read_bytes())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


# ─────────────────────────────────────────────────────────────────────────
# 通知・ログ補助
# ─────────────────────────────────────────────────────────────────────────
def _notify(text: str) -> None:
    """LINE 通知（失敗しても更新処理は止めない）。"""
    try:
        import notifier

        notifier.push(text)
    except Exception as e:  # noqa: BLE001 - 通知失敗は致命ではない
        logger.warning("LINE 通知に失敗しました: %s", e)


class UpdateError(Exception):
    """更新処理中の想定内エラー（ロールバック対象）。"""


# ─────────────────────────────────────────────────────────────────────────
# フィード取得
# ─────────────────────────────────────────────────────────────────────────
def _fetch_manifest() -> dict:
    """version.json を取得して dict で返す。"""
    url = f"{config.UPDATE_FEED_URL}/version.json"
    logger.info("version.json を取得します: %s", url)
    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"version.json の取得に失敗: {e}") from e

    if not isinstance(data, dict) or "version" not in data:
        raise UpdateError(f"version.json の形式が不正です: {data!r}")
    return data


def _resolve_tar_url(manifest: dict) -> str:
    """manifest の url を絶対 URL に解決する（相対ならフィード起点）。"""
    raw = str(manifest.get("url") or "").strip()
    if not raw:
        raise UpdateError("version.json に url がありません。")
    if raw.startswith(("http://", "https://")):
        return raw
    # 相対（ファイル名のみ）。UPDATE_FEED_URL 直下として解決する。
    return urljoin(config.UPDATE_FEED_URL + "/", raw)


# ─────────────────────────────────────────────────────────────────────────
# tar 展開・コピー
# ─────────────────────────────────────────────────────────────────────────
def _download_tar(url: str, dest: Path) -> None:
    logger.info("tar をダウンロードします: %s", url)
    try:
        with requests.get(url, timeout=_DOWNLOAD_TIMEOUT, stream=True) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"tar のダウンロードに失敗: {e}") from e


def _safe_extract(tar_path: Path, dest: Path) -> Path:
    """tar を dest へ安全に展開し、コード本体のルートを返す。

    - パストラバーサル（絶対パス/".."）を含むメンバーは拒否する。
    - 展開先の外を指すシンボリックリンク/ハードリンクは拒否する
      （symlink を先に作らせて後続メンバーで外部へ書く攻撃を防ぐ。
      install.sh の検査と同等）。
    - 展開後、先頭が単一ディレクトリだけならその中をルートとみなす
      （先頭ディレクトリの有無を吸収）。
    """
    dest.mkdir(parents=True, exist_ok=True)
    dest_real = dest.resolve()
    try:
        with tarfile.open(tar_path, "r:*") as tar:
            members = tar.getmembers()
            for m in members:
                name = m.name
                if name.startswith("/") or ".." in Path(name).parts:
                    raise UpdateError(f"tar に不正なパスが含まれます: {name}")
                if m.issym() or m.islnk():
                    target = m.linkname
                    if m.issym():
                        if os.path.isabs(target):
                            raise UpdateError(f"tar に絶対パスへのリンクが含まれます: {name} -> {target}")
                        resolved = dest_real / Path(name).parent / target
                    else:
                        # ハードリンクのターゲットは tar ルートからの相対
                        resolved = dest_real / target
                    resolved = Path(os.path.normpath(resolved))
                    if resolved != dest_real and dest_real not in resolved.parents:
                        raise UpdateError(f"tar に展開先の外を指すリンクが含まれます: {name} -> {target}")
            tar.extractall(dest)  # noqa: S202 - 上でメンバーを検証済み
    except tarfile.TarError as e:
        raise UpdateError(f"tar の展開に失敗: {e}") from e

    entries = [p for p in dest.iterdir() if not p.name.startswith("._")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest


_MANIFEST_NAME = "MANIFEST"


def _read_manifest(path: Path) -> set[str] | None:
    """MANIFEST（相対パスを1行1件）を読み集合で返す。無い/読めなければ None。

    None と空集合を区別する（「マニフェスト不明だから安全側で削除しない」と
    「マニフェストはあるが中身が空」は別の状況のため）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return {line.strip() for line in text.splitlines() if line.strip()}


def _top_level(rel_path: str) -> str:
    """マニフェストの相対パス（'/'区切り）からトップレベルのエントリ名を取り出す。"""
    return rel_path.split("/", 1)[0]


def _iter_relative_files(root: Path) -> list[str]:
    """root 配下の全ファイルを、root からの相対パス（'/'区切り文字列）のソート済みリストで返す。"""
    if not root.is_dir():
        return []
    return sorted(str(p.relative_to(root)).replace(os.sep, "/") for p in root.rglob("*") if p.is_file())


def _copy_file(src: Path, dest: Path) -> None:
    """1ファイルを dest へコピーする（親ディレクトリが無ければ作成。dest が別種なら退避してから置き換える）。"""
    if dest.is_dir() and not dest.is_symlink():
        shutil.rmtree(dest)
    elif dest.is_symlink() and not dest.exists():
        # 壊れたシンボリックリンク
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def _prune_empty_dirs(root: Path, protected: frozenset[str]) -> None:
    """root 配下の空ディレクトリを削除する（root 自体・_PROTECTED トップレベルは対象外）。"""
    if not root.is_dir():
        return
    dirs = sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True)
    for d in dirs:
        rel_parts = d.relative_to(root).parts
        if rel_parts and rel_parts[0] in protected:
            continue
        try:
            d.rmdir()  # 空でなければ OSError（無視して残す）
        except OSError:
            pass


def _copy_code(src_root: Path, dest_root: Path) -> None:
    """src_root のコードを dest_root へ MANIFEST ベースで同期する。

    利用者が追加したファイル（README が案内する parsers/mycard.py や独自テンプレ
    など、新旧どちらの MANIFEST にも載らないもの）を更新で消さないための方式。

    - コピーは常にファイル単位（ディレクトリを rmtree して丸ごと置き換えることは
      しない）。src_root 配下の全ファイルを dest_root へ上書きコピーする
      （_PROTECTED トップレベルは除外）。
    - 削除は「新しい配布物（新マニフェスト N）に無い、かつ旧マニフェスト（O）には
      あった」ファイルだけを対象にする。N か O のどちらかが読めない場合
      （MANIFEST を含まない旧形式の tar／初回更新など）は削除を一切行わない
      （安全側に倒し、全上書きのみ行う）。
    """
    new_manifest = _read_manifest(src_root / _MANIFEST_NAME)
    old_manifest = _read_manifest(dest_root / _MANIFEST_NAME)

    for rel in _iter_relative_files(src_root):
        if _top_level(rel) in _PROTECTED:
            continue
        _copy_file(src_root / rel, dest_root / rel)

    if new_manifest is None:
        logger.info("新バージョンに MANIFEST が無いため、削除同期はスキップします（全上書きのみ）。")
        return
    if old_manifest is None:
        logger.info("旧バージョンの MANIFEST が読めないため、削除同期はスキップします（全上書きのみ）。")
        return

    obsolete = sorted(old_manifest - new_manifest)
    for rel in obsolete:
        if _top_level(rel) in _PROTECTED:
            continue
        target = dest_root / rel
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists() or target.is_symlink():
            logger.info("新バージョンに存在しないため削除します: %s", rel)
            target.unlink(missing_ok=True)
    _prune_empty_dirs(dest_root, _PROTECTED)


# ─────────────────────────────────────────────────────────────────────────
# バックアップ・ロールバック
# ─────────────────────────────────────────────────────────────────────────
def _make_backup() -> Path:
    """data/ と現行コードを .update-backup/<timestamp>/ に退避し、そのパスを返す。"""
    install = Path(config.INSTALL_DIR)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = install / _BACKUP_DIRNAME / stamp
    backup_code = backup / "code"
    backup_code.mkdir(parents=True, exist_ok=True)

    logger.info("バックアップを作成します: %s", backup)

    # 現行コード（保護対象＝data/.env 等は除く）を退避。
    for item in install.iterdir():
        if item.name in _PROTECTED:
            continue
        target = backup_code / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)

    # data/ を丸ごと退避（存在すれば）。
    data_dir = Path(config.DATA_DIR)
    if data_dir.exists():
        shutil.copytree(data_dir, backup / "data", dirs_exist_ok=True)

    # 現行版数を記録（可読性・ロールバック確認用）。
    (backup / "VERSION.prev").write_text(config.APP_VERSION + "\n", encoding="utf-8")
    return backup


def _rollback(backup: Path) -> None:
    """バックアップからコードと data/ を復元する。

    バックアップ側の code/MANIFEST（旧 O）と現在（更新後）の install 直下の
    MANIFEST（新 N）が両方読めれば、N − O（＝失敗した更新が新規追加した
    ファイル）だけを削除してから復元する。どちらか読めなければ、トップレベルの
    エントリ名比較による従来ロジックにフォールバックする。復元自体は常に
    ファイル単位のコピーで、ディレクトリを rmtree して丸ごと置き換えることは
    しない（更新で削除されたファイルは backup_code に一式あるため、復元で
    そのまま戻る）。
    """
    logger.warning("ロールバックします: %s", backup)
    install = Path(config.INSTALL_DIR)

    backup_code = backup / "code"
    if backup_code.exists():
        new_manifest = _read_manifest(install / _MANIFEST_NAME)
        old_manifest = _read_manifest(backup_code / _MANIFEST_NAME)

        if new_manifest is not None and old_manifest is not None:
            for rel in sorted(new_manifest - old_manifest):
                if _top_level(rel) in _PROTECTED:
                    continue
                target = install / rel
                logger.info("更新で追加されたため削除します: %s", rel)
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists() or target.is_symlink():
                    target.unlink(missing_ok=True)
            _prune_empty_dirs(install, _PROTECTED)
        else:
            logger.info("MANIFEST が読めないため、トップレベル名の従来ロジックで削除同期します。")
            backup_names = {p.name for p in backup_code.iterdir()}
            for item in install.iterdir():
                if item.name in _PROTECTED or item.name in backup_names:
                    continue
                logger.info("バックアップに存在しないため削除します: %s", item.name)
                if item.is_dir() and not item.is_symlink():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)

        for rel in _iter_relative_files(backup_code):
            if _top_level(rel) in _PROTECTED:
                continue
            _copy_file(backup_code / rel, install / rel)

    backup_data = backup / "data"
    if backup_data.exists():
        data_dir = Path(config.DATA_DIR)
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.copytree(backup_data, data_dir)


def _prune_backups(keep: int = 3, backup_root: Path | None = None) -> None:
    """.update-backup/ 配下を新しい順に keep 世代だけ残し、古いものを削除する。

    世代ディレクトリ名はタイムスタンプ（%Y%m%d-%H%M%S）なので名前の降順＝新しい順。
    """
    root = backup_root if backup_root is not None else Path(config.INSTALL_DIR) / _BACKUP_DIRNAME
    if not root.is_dir():
        return
    generations = sorted(
        (p for p in root.iterdir() if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    for old in generations[keep:]:
        logger.info("古いバックアップを削除します: %s", old)
        shutil.rmtree(old, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────
# 外部コマンド
# ─────────────────────────────────────────────────────────────────────────
def _pip_install() -> None:
    """requirements.txt を現行 venv に適用する（sys.executable -m pip）。"""
    req = Path(config.INSTALL_DIR) / "requirements.txt"
    if not req.exists():
        logger.info("requirements.txt が無いため pip install はスキップします。")
        return
    logger.info("依存を更新します: pip install -r %s", req)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(req)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise UpdateError(f"pip install に失敗: {result.stderr.strip()[:500]}")


def _run_migrations() -> list[str]:
    """migrate.run() を呼び未適用マイグレーションを適用。無ければスキップ。"""
    try:
        import migrate
    except ImportError:
        logger.info("migrate モジュールが無いためマイグレーションはスキップします。")
        return []
    try:
        applied = migrate.run() or []
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"マイグレーションに失敗: {e}") from e
    if applied:
        logger.info("マイグレーション適用: %s", ", ".join(applied))
    return list(applied)


def _restart_service() -> None:
    """systemctl restart で対象ユニットを再起動する。

    非 root（専用ユーザー）で実行される場合は `sudo -n systemctl restart` を使う。
    install.sh が /etc/sudoers.d/card-notify にこのコマンドだけの NOPASSWD 許可を
    設置するため、sudoers のコマンド一致のためユニット名は ".service" 付きに揃える。
    """
    unit = config.UPDATE_SERVICE
    if "." not in unit:
        unit += ".service"
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", "systemctl", "restart", unit]
    else:
        cmd = ["systemctl", "restart", unit]
    logger.info("サービスを再起動します: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise UpdateError(
            f"{cmd[0]} が見つかりません。root では systemd 環境で実行し、"
            f"非 root では sudo を導入した上で /etc/sudoers.d/card-notify に "
            f"`systemctl restart {unit}` の NOPASSWD 許可（install.sh が設置）を"
            f"用意してください。"
        ) from e
    if result.returncode != 0:
        err = result.stderr.strip()
        raise UpdateError(
            f"{' '.join(cmd)} に失敗（rc={result.returncode}）: {err}. "
            f"権限不足の可能性があります。root で実行するか、専用ユーザーに "
            f"`systemctl restart {unit}` の sudoers 許可を与えてください"
            f"（install.sh が /etc/sudoers.d/card-notify に設置します）。"
        )


def _healthcheck() -> None:
    """HEALTHCHECK_URL を数秒待って GET し、200/302 なら成功。"""
    url = config.HEALTHCHECK_URL
    logger.info("ヘルスチェックします: %s", url)
    last: str = ""
    for attempt in range(1, _HEALTH_RETRIES + 1):
        time.sleep(_HEALTH_INTERVAL)
        try:
            resp = requests.get(url, timeout=_HTTP_TIMEOUT, allow_redirects=False)
            if resp.status_code in _HEALTH_OK:
                logger.info("ヘルスチェック成功（%s）", resp.status_code)
                return
            last = f"status={resp.status_code}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        logger.info("ヘルスチェック未成功（%d/%d）: %s", attempt, _HEALTH_RETRIES, last)
    raise UpdateError(f"ヘルスチェックに失敗: {last}")


# ─────────────────────────────────────────────────────────────────────────
# 高レベルフロー
# ─────────────────────────────────────────────────────────────────────────
def _cleanup_temp(paths: list[Path]) -> None:
    for p in paths:
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        except OSError:
            pass


def perform_update() -> int:
    """更新を実行する。成功で 0、失敗（ロールバック含む）で非 0 を返す。"""
    # 1. 設定検証
    if not config.UPDATE_FEED_URL:
        logger.error("UPDATE_FEED_URL（CARD_NOTIFY_UPDATE_FEED_URL）が未設定です。")
        return 2

    # 2. version.json 取得＋比較
    manifest = _fetch_manifest()
    remote_ver = str(manifest["version"]).strip()
    local_ver = config.APP_VERSION
    if not _is_newer(remote_ver, local_ver):
        logger.info("最新です（現在 %s / 配信 %s）。何もしません。", local_ver, remote_ver)
        return 0

    logger.info("更新を開始します: %s -> %s", local_ver, remote_ver)
    tar_url = _resolve_tar_url(manifest)
    expected_sha = str(manifest.get("sha256") or "").strip()

    backup: Path | None = None
    tmp_dir = Path(tempfile.mkdtemp(prefix="card-notify-update-"))
    tar_path = tmp_dir / "package.tar.gz"
    extract_dir = tmp_dir / "extract"

    try:
        # 3. バックアップ
        backup = _make_backup()

        # 4. ダウンロード＋sha256 検証＋（公開鍵設定時）Ed25519 署名検証
        _download_tar(tar_url, tar_path)
        if expected_sha:
            if not _verify_sha256(tar_path, expected_sha):
                raise UpdateError(
                    f"sha256 不一致（期待 {expected_sha[:12]}… / 実際 "
                    f"{_sha256_of_file(tar_path)[:12]}…）"
                )
            logger.info("sha256 検証 OK。")
        else:
            logger.warning("version.json に sha256 が無いため検証をスキップします。")

        sig_b64 = str(manifest.get("sig") or "").strip()
        if config.UPDATE_PUBKEY:
            if not sig_b64:
                raise UpdateError(
                    "CARD_NOTIFY_UPDATE_PUBKEY が設定されていますが、"
                    "version.json に sig（Ed25519 署名）がありません。"
                )
            if not _verify_signature(tar_path, sig_b64, config.UPDATE_PUBKEY):
                raise UpdateError(
                    "Ed25519 署名の検証に失敗しました（tar の改ざん、"
                    "または鍵/署名の形式不正の可能性）。"
                )
            logger.info("Ed25519 署名検証 OK。")
        else:
            logger.warning("署名検証なし（CARD_NOTIFY_UPDATE_PUBKEY 未設定）。")

        # 5. 展開してコードのみ上書き（破壊的操作の直前）
        code_root = _safe_extract(tar_path, extract_dir)
        logger.warning("コードを上書きします: %s -> %s", code_root, config.INSTALL_DIR)
        _copy_code(code_root, Path(config.INSTALL_DIR))

        # 6. 依存更新
        _pip_install()

        # 7. マイグレーション
        _run_migrations()

        # 8. 再起動
        _restart_service()

        # 9. ヘルスチェック
        _healthcheck()

    except UpdateError as e:
        logger.error("更新に失敗しました: %s", e)
        if backup is not None:
            try:
                _rollback(backup)
                # ロールバック後は元コードでサービスを戻す（失敗しても続行）。
                try:
                    _restart_service()
                except UpdateError as re:
                    logger.error("ロールバック後の再起動に失敗: %s", re)
            except Exception as re:  # noqa: BLE001
                logger.error("ロールバックにも失敗しました: %s", re)
                _notify(f"card-notify 更新失敗、ロールバックにも失敗しました: {e}")
                return 1
        _notify(f"card-notify 更新失敗、ロールバックしました: {e}")
        return 1
    except Exception as e:  # noqa: BLE001 - 想定外も安全側でロールバック
        logger.exception("想定外のエラーで更新に失敗しました。")
        if backup is not None:
            try:
                _rollback(backup)
                try:
                    _restart_service()
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass
        _notify(f"card-notify 更新失敗、ロールバックしました: {e}")
        return 1
    finally:
        _cleanup_temp([tmp_dir])

    # 10. 成功（古いバックアップ世代を整理してから通知）
    _prune_backups(keep=3)
    logger.info("v%s に更新しました。", remote_ver)
    _notify(f"card-notify を v{remote_ver} に更新しました。")
    return 0


def check_only() -> int:
    """取得と版数比較だけ行い、更新可否を表示する（適用しない）。"""
    if not config.UPDATE_FEED_URL:
        print("UPDATE_FEED_URL が未設定です。")
        return 2
    try:
        manifest = _fetch_manifest()
    except UpdateError as e:
        print(f"取得に失敗しました: {e}")
        return 2
    remote_ver = str(manifest["version"]).strip()
    local_ver = config.APP_VERSION
    if _is_newer(remote_ver, local_ver):
        print(f"更新あり: {local_ver} -> {remote_ver}")
        print(f"  tar: {_resolve_tar_url(manifest)}")
        print(f"  sha256: {manifest.get('sha256') or '(なし)'}")
        return 0
    print(f"最新です（現在 {local_ver} / 配信 {remote_ver}）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="card-notify 自動アップデータ")
    parser.add_argument(
        "--check",
        action="store_true",
        help="取得と版数比較だけ行い、更新は適用しない。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="AUTO_UPDATE が false でも実行する。",
    )
    args = parser.parse_args(argv)

    if args.check:
        return check_only()

    if not config.AUTO_UPDATE and not args.force:
        logger.info("AUTO_UPDATE が無効です。何もしません（--force で強制実行）。")
        return 0

    return perform_update()


if __name__ == "__main__":
    raise SystemExit(main())
