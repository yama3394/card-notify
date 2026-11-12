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


# ─────────────────────────────────────────────────────────────
# data/ の復元（data_dir 自体を削除できない本番構成でも失敗しない）
# ─────────────────────────────────────────────────────────────
def _rollback_fixture(tmp_path, monkeypatch):
    install = tmp_path / "install"
    data_dir = tmp_path / "varlib" / "card-notify"
    backup = tmp_path / "backup"
    monkeypatch.setattr(config, "INSTALL_DIR", install)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    install.mkdir()
    data_dir.mkdir(parents=True)
    (data_dir / "history.json").write_text("after", encoding="utf-8")
    (data_dir / "added_after_backup.json").write_text("x", encoding="utf-8")
    (backup / "data" / "sub").mkdir(parents=True)
    (backup / "data" / "history.json").write_text("before", encoding="utf-8")
    (backup / "data" / "sub" / "nested.json").write_text("n", encoding="utf-8")
    return data_dir, backup


def test_rollback_restores_data_without_removing_data_dir(tmp_path, monkeypatch):
    data_dir, backup = _rollback_fixture(tmp_path, monkeypatch)

    # 本番（親が root 所有の /var/lib）では専用ユーザーは data_dir 自体を消せない。
    # data_dir を消そうとしたら失敗させ、消さずに復元できることを確認する。
    real_rmtree = auto_update.shutil.rmtree

    def _rmtree(path, *args, **kwargs):
        assert auto_update.Path(path) != data_dir, "data_dir 自体を削除してはいけない"
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(auto_update.shutil, "rmtree", _rmtree)

    auto_update._rollback(backup)

    assert (data_dir / "history.json").read_text(encoding="utf-8") == "before"
    assert (data_dir / "sub" / "nested.json").read_text(encoding="utf-8") == "n"
    assert not (data_dir / "added_after_backup.json").exists()


def test_rollback_keeps_data_when_restore_data_is_false(tmp_path, monkeypatch):
    data_dir, backup = _rollback_fixture(tmp_path, monkeypatch)

    auto_update._rollback(backup, restore_data=False)

    assert (data_dir / "history.json").read_text(encoding="utf-8") == "after"
    assert (data_dir / "added_after_backup.json").exists()


def _stub_update_flow(tmp_path, monkeypatch):
    """perform_update の外部依存（配信元・再起動・通知）を潰し、呼び出しを記録する。"""
    data_dir, backup = _rollback_fixture(tmp_path, monkeypatch)
    calls: list = []
    monkeypatch.setattr(config, "UPDATE_FEED_URL", "https://feed.example")
    monkeypatch.setattr(config, "UPDATE_PUBKEY", "")
    monkeypatch.setattr(config, "APP_VERSION", "1.0.0")
    monkeypatch.setattr(auto_update, "_fetch_manifest",
                        lambda: {"version": "9.9.9", "url": "x.tar.gz", "sha256": ""})
    monkeypatch.setattr(auto_update, "_make_backup", lambda: backup)
    monkeypatch.setattr(auto_update, "_download_tar", lambda url, dest: None)
    monkeypatch.setattr(auto_update, "_safe_extract", lambda tar, dest: dest)
    monkeypatch.setattr(auto_update, "_copy_code", lambda src, dest: None)
    monkeypatch.setattr(auto_update, "_pip_install", lambda: None)
    monkeypatch.setattr(auto_update, "_run_migrations", lambda: [])
    monkeypatch.setattr(auto_update, "_healthcheck", lambda: None)
    monkeypatch.setattr(auto_update, "_restart_service", lambda: calls.append("restart"))
    monkeypatch.setattr(auto_update, "_notify", lambda text: calls.append(("notify", text)))
    real_rollback = auto_update._rollback

    def _rollback(b, restore_data=True):
        calls.append(("rollback", restore_data))
        real_rollback(b, restore_data=restore_data)

    monkeypatch.setattr(auto_update, "_rollback", _rollback)
    return data_dir, calls


def _raise_update_error(*args, **kwargs):
    raise auto_update.UpdateError("boom")


def test_failure_before_code_change_touches_nothing(tmp_path, monkeypatch):
    data_dir, calls = _stub_update_flow(tmp_path, monkeypatch)
    monkeypatch.setattr(auto_update, "_download_tar", _raise_update_error)

    assert auto_update.perform_update() == 1

    # ダウンロード失敗はコードにもデータにも触れる前なので、ロールバックも再起動もしない
    assert [c for c in calls if c == "restart" or c[0] == "rollback"] == []
    assert (data_dir / "history.json").read_text(encoding="utf-8") == "after"
    assert any(c[0] == "notify" and "変更はありません" in c[1] for c in calls)


def test_failure_before_migration_rolls_back_code_only(tmp_path, monkeypatch):
    data_dir, calls = _stub_update_flow(tmp_path, monkeypatch)
    monkeypatch.setattr(auto_update, "_pip_install", _raise_update_error)

    assert auto_update.perform_update() == 1

    assert ("rollback", False) in calls
    # バックアップ取得後に登録されたデータを巻き戻さない
    assert (data_dir / "history.json").read_text(encoding="utf-8") == "after"


def test_failure_after_migration_restores_data(tmp_path, monkeypatch):
    data_dir, calls = _stub_update_flow(tmp_path, monkeypatch)
    monkeypatch.setattr(auto_update, "_healthcheck", _raise_update_error)

    assert auto_update.perform_update() == 1

    assert ("rollback", True) in calls
    assert (data_dir / "history.json").read_text(encoding="utf-8") == "before"
