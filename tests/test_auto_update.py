"""auto_update の純粋関数・ファイル操作のユニットテスト。

ネットワーク・systemctl は一切呼ばない。すべて tmp_path 上で完結する。
"""
import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import auto_update
import config


# ─────────────────────────────────────────────────────────────
# _verify_signature
# ─────────────────────────────────────────────────────────────
@pytest.fixture()
def keypair():
    """（秘密鍵オブジェクト, 公開鍵の base64）を返す。"""
    key = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode()
    return key, pub_b64


def _sign(key: Ed25519PrivateKey, data: bytes) -> str:
    return base64.b64encode(key.sign(data)).decode()


def test_verify_signature_ok(tmp_path, keypair):
    key, pub_b64 = keypair
    tar = tmp_path / "pkg.tar.gz"
    tar.write_bytes(b"dummy tarball bytes")
    sig = _sign(key, tar.read_bytes())
    assert auto_update._verify_signature(tar, sig, pub_b64) is True


def test_verify_signature_tampered_file(tmp_path, keypair):
    key, pub_b64 = keypair
    tar = tmp_path / "pkg.tar.gz"
    tar.write_bytes(b"original")
    sig = _sign(key, tar.read_bytes())
    tar.write_bytes(b"tampered!")
    assert auto_update._verify_signature(tar, sig, pub_b64) is False


def test_verify_signature_wrong_key(tmp_path, keypair):
    key, _ = keypair
    other_pub_b64 = base64.b64encode(
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    tar = tmp_path / "pkg.tar.gz"
    tar.write_bytes(b"data")
    sig = _sign(key, tar.read_bytes())
    assert auto_update._verify_signature(tar, sig, other_pub_b64) is False


def test_verify_signature_garbage_inputs(tmp_path, keypair):
    _, pub_b64 = keypair
    tar = tmp_path / "pkg.tar.gz"
    tar.write_bytes(b"data")
    # base64 として不正な署名・鍵、長さ不正の鍵、いずれも False（例外にしない）
    assert auto_update._verify_signature(tar, "%%%not-base64%%%", pub_b64) is False
    assert auto_update._verify_signature(tar, "c2ln", "%%%not-base64%%%") is False
    assert auto_update._verify_signature(tar, "c2ln", base64.b64encode(b"short").decode()) is False
    assert auto_update._verify_signature(tar, "", pub_b64) is False
    assert auto_update._verify_signature(tar, "c2ln", "") is False


# ─────────────────────────────────────────────────────────────
# _prune_backups
# ─────────────────────────────────────────────────────────────
def test_prune_backups_keeps_newest_three(tmp_path):
    root = tmp_path / ".update-backup"
    stamps = [
        "20260101-000000",
        "20260201-000000",
        "20260301-000000",
        "20260401-000000",
        "20260501-000000",
    ]
    for s in stamps:
        (root / s / "code").mkdir(parents=True)
    auto_update._prune_backups(keep=3, backup_root=root)
    remaining = sorted(p.name for p in root.iterdir())
    assert remaining == ["20260301-000000", "20260401-000000", "20260501-000000"]


def test_prune_backups_noop_when_few_or_missing(tmp_path):
    root = tmp_path / ".update-backup"
    # ディレクトリ自体が無い → 何もしない（例外にしない）
    auto_update._prune_backups(keep=3, backup_root=root)
    (root / "20260101-000000").mkdir(parents=True)
    (root / "20260201-000000").mkdir()
    auto_update._prune_backups(keep=3, backup_root=root)
    assert len(list(root.iterdir())) == 2


# ─────────────────────────────────────────────────────────────
# _copy_code（MANIFEST ベースの同期。ファイル単位コピーで、利用者追加ファイルを保持する）
# ─────────────────────────────────────────────────────────────
def test_copy_code_without_any_manifest_only_overwrites_no_deletion(tmp_path):
    # MANIFEST が src にも dest にも無い（旧形式 tar／初回など）場合は、
    # 上書きコピーのみ行い削除は一切しない（安全側）。
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    (src / "parsers").mkdir(parents=True)
    (src / "app.py").write_text("new app", encoding="utf-8")
    (src / "parsers" / "new.py").write_text("new parser", encoding="utf-8")

    (dest / "parsers").mkdir(parents=True)
    (dest / "app.py").write_text("old app", encoding="utf-8")
    (dest / "obsolete.py").write_text("removed in new version", encoding="utf-8")
    (dest / "parsers" / "removed.py").write_text("gone", encoding="utf-8")
    (dest / "data").mkdir()
    (dest / "data" / "history.json").write_text("{}", encoding="utf-8")
    (dest / ".env").write_text("SECRET=1", encoding="utf-8")

    auto_update._copy_code(src, dest)

    assert (dest / "app.py").read_text(encoding="utf-8") == "new app"
    assert (dest / "parsers" / "new.py").exists()
    # MANIFEST が無いため、新版に存在しなくても削除されない
    assert (dest / "obsolete.py").exists()
    assert (dest / "parsers" / "removed.py").exists()
    # 保護対象（data/.env）は不可侵
    assert (dest / "data" / "history.json").exists()
    assert (dest / ".env").read_text(encoding="utf-8") == "SECRET=1"


def test_copy_code_excludes_protected_from_source(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    (src / "data").mkdir(parents=True)
    (src / "data" / "evil.json").write_text("x", encoding="utf-8")
    (src / "app.py").write_text("app", encoding="utf-8")
    dest.mkdir()
    auto_update._copy_code(src, dest)
    assert (dest / "app.py").exists()
    # 展開物側の保護対象名はコピーされない
    assert not (dest / "data").exists()


def test_copy_code_manifest_deletes_obsolete_and_preserves_user_added_files(tmp_path):
    # 新旧どちらの MANIFEST も読める場合: O − N（旧マニフェストにあり新マニフェストに
    # 無いファイル）だけを削除し、利用者が追加したファイル（parsers/mycard.py。
    # どちらのマニフェストにも載っていない）は保持する。
    src = tmp_path / "src"
    dest = tmp_path / "dest"

    (src / "parsers").mkdir(parents=True)
    (src / "app.py").write_text("new app", encoding="utf-8")
    (src / "parsers" / "new.py").write_text("new parser", encoding="utf-8")
    (src / "MANIFEST").write_text("MANIFEST\napp.py\nparsers/new.py\n", encoding="utf-8")

    (dest / "parsers").mkdir(parents=True)
    (dest / "olddir").mkdir()
    (dest / "app.py").write_text("old app", encoding="utf-8")
    (dest / "obsolete.py").write_text("removed in new version", encoding="utf-8")
    (dest / "parsers" / "removed.py").write_text("gone", encoding="utf-8")
    (dest / "olddir" / "x.py").write_text("x", encoding="utf-8")
    (dest / "parsers" / "mycard.py").write_text("my own parser, keep me", encoding="utf-8")
    (dest / "MANIFEST").write_text(
        "MANIFEST\napp.py\nobsolete.py\nparsers/removed.py\nolddir/x.py\n", encoding="utf-8"
    )
    (dest / "data").mkdir()
    (dest / "data" / "history.json").write_text("{}", encoding="utf-8")
    (dest / ".env").write_text("SECRET=1", encoding="utf-8")

    auto_update._copy_code(src, dest)

    assert (dest / "app.py").read_text(encoding="utf-8") == "new app"
    assert (dest / "parsers" / "new.py").exists()
    assert (dest / "MANIFEST").read_text(encoding="utf-8") == "MANIFEST\napp.py\nparsers/new.py\n"
    # 旧マニフェストにあり新マニフェストに無いファイルは削除される
    assert not (dest / "obsolete.py").exists()
    assert not (dest / "parsers" / "removed.py").exists()
    # 空になったディレクトリは掃除される
    assert not (dest / "olddir").exists()
    # 利用者追加ファイル（どちらのマニフェストにも無い）は保持される
    assert (dest / "parsers" / "mycard.py").exists()
    assert (dest / "parsers" / "mycard.py").read_text(encoding="utf-8") == "my own parser, keep me"
    # 保護対象は不可侵
    assert (dest / "data" / "history.json").exists()
    assert (dest / ".env").read_text(encoding="utf-8") == "SECRET=1"


def test_copy_code_missing_old_manifest_skips_deletion(tmp_path):
    # 新マニフェストはあるが dest 側に旧マニフェストが無い（例: MANIFEST 対応前の
    # 版からの初回更新）場合は削除しない。
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir(parents=True)
    (src / "app.py").write_text("new app", encoding="utf-8")
    (src / "MANIFEST").write_text("MANIFEST\napp.py\n", encoding="utf-8")

    dest.mkdir()
    (dest / "app.py").write_text("old app", encoding="utf-8")
    (dest / "obsolete.py").write_text("should stay", encoding="utf-8")

    auto_update._copy_code(src, dest)

    assert (dest / "app.py").read_text(encoding="utf-8") == "new app"
    assert (dest / "obsolete.py").exists()


def test_copy_code_missing_new_manifest_skips_deletion(tmp_path):
    # src（新版）に MANIFEST が無ければ、dest に旧 MANIFEST があっても削除しない。
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir(parents=True)
    (src / "app.py").write_text("new app", encoding="utf-8")

    dest.mkdir()
    (dest / "app.py").write_text("old app", encoding="utf-8")
    (dest / "obsolete.py").write_text("should stay", encoding="utf-8")
    (dest / "MANIFEST").write_text("MANIFEST\napp.py\nobsolete.py\n", encoding="utf-8")

    auto_update._copy_code(src, dest)

    assert (dest / "app.py").read_text(encoding="utf-8") == "new app"
    assert (dest / "obsolete.py").exists()


# ─────────────────────────────────────────────────────────────
# _rollback の削除同期（更新で追加されたファイルが残らない）
# ─────────────────────────────────────────────────────────────
def test_rollback_removes_files_added_by_update(tmp_path, monkeypatch):
    # MANIFEST が無い場合はトップレベル名比較の従来ロジックにフォールバックする。
    install = tmp_path / "install"
    backup = tmp_path / "backup"
    data_dir = install / "data"
    monkeypatch.setattr(config, "INSTALL_DIR", install)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)

    # バックアップ（旧コード一式）: app.py と removed_by_update.py
    (backup / "code").mkdir(parents=True)
    (backup / "code" / "app.py").write_text("old app", encoding="utf-8")
    (backup / "code" / "removed_by_update.py").write_text("was deleted", encoding="utf-8")

    # 更新後の install: 新 app.py・更新が追加した new_module.py・保護対象
    install.mkdir()
    (install / "app.py").write_text("new app", encoding="utf-8")
    (install / "new_module.py").write_text("added by update", encoding="utf-8")
    data_dir.mkdir()
    (data_dir / "history.json").write_text("{}", encoding="utf-8")
    (install / ".env").write_text("SECRET=1", encoding="utf-8")

    auto_update._rollback(backup)

    # 旧コードに戻り、更新で消えたファイルも復元される
    assert (install / "app.py").read_text(encoding="utf-8") == "old app"
    assert (install / "removed_by_update.py").exists()
    # 更新で追加されたファイルは残らない
    assert not (install / "new_module.py").exists()
    # 保護対象は不可侵
    assert (data_dir / "history.json").exists()
    assert (install / ".env").exists()


def test_rollback_manifest_based_removes_added_files_and_preserves_untracked(tmp_path, monkeypatch):
    # 新旧どちらの MANIFEST も読めれば N − O（失敗した更新が追加したファイル）だけを
    # 削除し、更新後にユーザーが手で足したファイル（どちらのマニフェストにも
    # 無いもの）は誤って消さない。
    install = tmp_path / "install"
    backup = tmp_path / "backup"
    data_dir = install / "data"
    monkeypatch.setattr(config, "INSTALL_DIR", install)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)

    # バックアップ（旧版一式・旧マニフェスト O）
    (backup / "code").mkdir(parents=True)
    (backup / "code" / "app.py").write_text("old app", encoding="utf-8")
    (backup / "code" / "removed_by_update.py").write_text("was deleted", encoding="utf-8")
    (backup / "code" / "MANIFEST").write_text("MANIFEST\napp.py\nremoved_by_update.py\n", encoding="utf-8")

    # 更新後（失敗した新版）の install・新マニフェスト N
    install.mkdir()
    (install / "app.py").write_text("new app", encoding="utf-8")
    (install / "new_module.py").write_text("added by update", encoding="utf-8")
    (install / "MANIFEST").write_text("MANIFEST\napp.py\nnew_module.py\n", encoding="utf-8")
    # どちらのマニフェストにも無い、ユーザーが後から足したファイル
    (install / "user_added.py").write_text("not tracked by any manifest", encoding="utf-8")
    data_dir.mkdir()
    (data_dir / "history.json").write_text("{}", encoding="utf-8")
    (install / ".env").write_text("SECRET=1", encoding="utf-8")

    auto_update._rollback(backup)

    # 旧コードに戻る
    assert (install / "app.py").read_text(encoding="utf-8") == "old app"
    assert (install / "removed_by_update.py").exists()
    # 更新（N）が追加し、旧版（O）に無いファイルは削除される
    assert not (install / "new_module.py").exists()
    # どちらのマニフェストにも無いファイルは誤って消されない
    assert (install / "user_added.py").exists()
    # 保護対象は不可侵
    assert (data_dir / "history.json").exists()
    assert (install / ".env").exists()


# ── _safe_extract のリンク検査 ──────────────────────────────────────────────

import io
import tarfile as _tarfile

import pytest as _pytest

import auto_update as _au


def _make_tar(tmp_path, members):
    """(name, kind, target/content) のリストから tar.gz を作る。"""
    tar_path = tmp_path / "pkg.tar.gz"
    with _tarfile.open(tar_path, "w:gz") as tar:
        for name, kind, payload in members:
            info = _tarfile.TarInfo(name)
            if kind == "file":
                data = payload.encode("utf-8")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            elif kind == "sym":
                info.type = _tarfile.SYMTYPE
                info.linkname = payload
                tar.addfile(info)
            elif kind == "hard":
                info.type = _tarfile.LNKTYPE
                info.linkname = payload
                tar.addfile(info)
    return tar_path


def test_safe_extract_ok(tmp_path):
    tar_path = _make_tar(tmp_path, [
        ("app/main.py", "file", "print('ok')"),
        ("app/link_inside", "sym", "main.py"),
    ])
    root = _au._safe_extract(tar_path, tmp_path / "out")
    assert (root / "main.py").exists()


def test_safe_extract_rejects_symlink_escape(tmp_path):
    tar_path = _make_tar(tmp_path, [
        ("evil", "sym", "../../outside"),
    ])
    with _pytest.raises(_au.UpdateError, match="リンク"):
        _au._safe_extract(tar_path, tmp_path / "out")


def test_safe_extract_rejects_absolute_symlink(tmp_path):
    tar_path = _make_tar(tmp_path, [
        ("evil", "sym", "/etc/passwd"),
    ])
    with _pytest.raises(_au.UpdateError, match="リンク"):
        _au._safe_extract(tar_path, tmp_path / "out")


def test_safe_extract_rejects_hardlink_escape(tmp_path):
    tar_path = _make_tar(tmp_path, [
        ("evil", "hard", "../outside"),
    ])
    with _pytest.raises(_au.UpdateError, match="リンク"):
        _au._safe_extract(tar_path, tmp_path / "out")


def test_safe_extract_rejects_path_traversal(tmp_path):
    tar_path = _make_tar(tmp_path, [
        ("../escape.py", "file", "x"),
    ])
    with _pytest.raises(_au.UpdateError, match="不正なパス"):
        _au._safe_extract(tar_path, tmp_path / "out")
