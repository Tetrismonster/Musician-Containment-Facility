import base64
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcf import app_update
from mcf.app_update import UpdateError

DIGEST = "sha256:" + "ab" * 32


def _release(tag, digest=DIGEST, **extra):
    version = tag.lstrip("v")
    return dict({"tag_name": tag, "draft": False, "prerelease": False, "body": f"# {tag}\n- Fixed {tag}",
                 "html_url": f"https://github.com/x/y/releases/tag/{tag}",
                 "assets": [{"name": f"MCF-{version}.zip", "digest": digest, "size": 10,
                             "browser_download_url": f"https://github.com/x/y/releases/download/{tag}/MCF.zip"}]},
                **extra)


def _key():
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, base64.b64encode(raw).decode()


def _package(folder):
    (folder / "_internal" / "data").mkdir(parents=True)
    (folder / "MCF.exe").write_bytes(b"new exe")
    (folder / "_internal" / "data" / "manifest.json").write_text("{}")
    return folder


def test_parse_version():
    assert app_update.parse_version("v1.2.3") == (1, 2, 3)
    assert app_update.parse_version("V10.0.1") == (10, 0, 1)
    for bad in ("1.2.3", "v1.2", "v1.2.3-beta", "", None):
        assert app_update.parse_version(bad) is None


def test_newest_skips_drafts_prereleases_and_bad_tags():
    releases = [_release("v0.2.0"), _release("v0.9.0", draft=True), _release("v0.8.0", prerelease=True),
                _release("nightly"), _release("v0.10.0")]
    assert app_update.newest(releases)["tag_name"] == "v0.10.0"
    assert app_update.newest([_release("v1.0.0", draft=True)]) is None


def test_find_asset_needs_matching_name_and_digest():
    assert app_update.find_asset(_release("v1.0.0"))["size"] == 10
    assert app_update.find_asset(_release("v1.0.0", digest="")) is None
    assert app_update.find_asset(_release("v1.0.0", digest="md5:abc")) is None
    wrong = _release("v1.0.0")
    wrong["assets"][0]["name"] = "MCF-0.9.0.zip"
    assert app_update.find_asset(wrong) is None
    insecure = _release("v1.0.0")
    insecure["assets"][0]["browser_download_url"] = "http://example.com/MCF-1.0.0.zip"
    assert app_update.find_asset(insecure) is None


def test_notes_between_lists_newer_releases_oldest_first():
    releases = [_release("v0.3.0"), _release("v0.1.0"), _release("v0.2.0")]
    notes = app_update.notes_between(releases, (0, 1, 0), (0, 3, 0))
    assert notes.index("v0.2.0") < notes.index("v0.3.0")
    assert "v0.1.0" not in notes
    # The repeated version heading is dropped, other heading marks are removed
    assert "# v0.2.0" not in notes
    assert app_update.format_notes("## Fixes\n\n\n- One", "v1.0.0") == "Fixes\n\n- One"


def test_check_due():
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    yesterday = (now - timedelta(hours=25)).isoformat()
    recent = (now - timedelta(hours=1)).isoformat()
    assert app_update.check_due("startup", recent, now)
    assert not app_update.check_due("never", "", now)
    assert app_update.check_due("daily", yesterday, now)
    assert not app_update.check_due("daily", recent, now)
    assert not app_update.check_due("weekly", yesterday, now)
    assert app_update.check_due("daily", "not a time", now)


def test_signed_package_verifies(tmp_path):
    key, public = _key()
    package = _package(tmp_path / "pkg")
    app_update.sign_package(package, "1.0.0", key)
    app_update.verify_package(package, "1.0.0", public)


@pytest.mark.parametrize("tamper", ["change", "add", "remove", "other_key", "version", "outside"])
def test_tampered_package_is_rejected(tmp_path, tamper):
    key, public = _key()
    package = _package(tmp_path / "pkg")
    if tamper == "outside":
        (package / "settings").mkdir()
        (package / "settings" / "settings.json").write_text("{}")
    app_update.sign_package(package, "1.0.0", key)
    version = "1.0.0"
    if tamper == "change":
        (package / "MCF.exe").write_bytes(b"evil exe")
    elif tamper == "add":
        (package / "_internal" / "extra.dll").write_bytes(b"x")
    elif tamper == "remove":
        (package / "_internal" / "data" / "manifest.json").unlink()
    elif tamper == "other_key":
        public = _key()[1]
    elif tamper == "version":
        version = "2.0.0"
    with pytest.raises(UpdateError):
        app_update.verify_package(package, version, public)


def test_hidden_manifest_name_is_still_checked(tmp_path):
    key, public = _key()
    package = _package(tmp_path / "pkg")
    app_update.sign_package(package, "1.0.0", key)
    (package / "_internal" / app_update.MANIFEST_NAME).write_text("sneaky")
    with pytest.raises(UpdateError):
        app_update.verify_package(package, "1.0.0", public)


def test_unsigned_package_is_rejected(tmp_path):
    with pytest.raises(UpdateError):
        app_update.verify_package(_package(tmp_path / "pkg"), "1.0.0", _key()[1])


@pytest.mark.parametrize("name", ["../evil.txt", "/evil.txt", "C:/evil.txt", "_internal/../../evil.txt"])
def test_safe_extract_rejects_unsafe_paths(tmp_path, name):
    archive = tmp_path / "u.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(name, "x")
    with pytest.raises(UpdateError):
        app_update.safe_extract(archive, tmp_path / "stage")
    assert not (tmp_path / "evil.txt").exists()


def test_safe_extract_rejects_duplicates(tmp_path):
    archive = tmp_path / "u.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("MCF.exe", "a")
        z.writestr("mcf.EXE", "b")
    with pytest.raises(UpdateError):
        app_update.safe_extract(archive, tmp_path / "stage")


def _installed(target):
    (target / "_internal").mkdir(parents=True)
    (target / "MCF.exe").write_bytes(b"old exe")
    (target / "_internal" / "old.dll").write_bytes(b"old")
    for folder in ("settings", "library", "models", "runtime"):
        (target / folder).mkdir()
        (target / folder / "keep.txt").write_text(folder)
    return target


def test_replace_program_keeps_user_data(tmp_path):
    target = _installed(tmp_path / "MCF")
    stage = _package(tmp_path / "MCF" / "temp" / "update" / "stage")
    app_update.replace_program(stage, target, stage.parent / "rollback")
    assert (target / "MCF.exe").read_bytes() == b"new exe"
    assert not (target / "_internal" / "old.dll").exists()
    for folder in ("settings", "library", "models", "runtime"):
        assert (target / folder / "keep.txt").read_text() == folder
    assert not (stage.parent / "rollback").exists()


def test_replace_program_rolls_back_on_failure(tmp_path, monkeypatch):
    target = _installed(tmp_path / "MCF")
    stage = _package(tmp_path / "MCF" / "temp" / "update" / "stage")

    def broken_copy(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(app_update.shutil, "copy2", broken_copy)
    with pytest.raises(OSError):
        app_update.replace_program(stage, target, stage.parent / "rollback")
    assert (target / "MCF.exe").read_bytes() == b"old exe"
    assert (target / "_internal" / "old.dll").exists()


def test_take_result_reads_once(tmp_path, monkeypatch):
    monkeypatch.setattr(app_update.paths, "settings_dir", lambda: tmp_path)
    assert app_update.take_result() is None
    (tmp_path / app_update.RESULT_NAME).write_text(json.dumps({"ok": True, "version": "1.0.0"}))
    assert app_update.take_result()["ok"] is True
    assert app_update.take_result() is None


def test_not_available_without_address(monkeypatch):
    monkeypatch.setattr(app_update, "RELEASES_URL", "")
    assert not app_update.available()
    with pytest.raises(UpdateError):
        app_update.check()
