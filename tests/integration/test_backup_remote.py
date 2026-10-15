"""rclone 薄轉接層 backup_remote（P3-5B）。

用真的 rclone（local backend 當「雲端」，不連任何 Google 服務）驗證上傳、列表、刪除、下載；
失敗注入與 token 不外洩用 mock 的 subprocess（不需要真 rclone）。
"""

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from app.services import backup_remote
from app.services.backup_remote import RemoteConfig, RemoteError
from tests.backup_helpers import make_local_remote

NAME = "ledger-20261015-120000.db.gz.age"
OTHER = "ledger-20261014-030000.db.gz.age"


@pytest.fixture
def remote(tmp_path):
    return make_local_remote(tmp_path)


@pytest.fixture
def local_dir(tmp_path) -> Path:
    directory = tmp_path / "local"
    directory.mkdir()
    return directory


# --- 設定格式 --------------------------------------------------------------------------------


def cfg(remote_name="gdrive", path="ledger-backups", **kw) -> RemoteConfig:
    return RemoteConfig(remote_name, path, **kw)


def test_valid_config_passes_validation():
    backup_remote.validate(cfg())
    backup_remote.validate(cfg("my-drive_2.test", "a/b c/ledger"))


@pytest.mark.parametrize("name", ["", "a:b", "-x", "a b", "a/b", "名稱", "a;rm"])
def test_invalid_remote_names_are_rejected(name):
    with pytest.raises(RemoteError, match="RCLONE_REMOTE"):
        backup_remote.validate(cfg(name))


@pytest.mark.parametrize("path", ["", "   ", "-flag", "a/../b", "..", "a\\..\\b", "x\ny"])
def test_invalid_paths_are_rejected(path):
    with pytest.raises(RemoteError, match="RCLONE_PATH"):
        backup_remote.validate(cfg(path=path))


def test_spec_joins_remote_and_path():
    assert cfg().spec == "gdrive:ledger-backups"


# --- 上傳（真 rclone）------------------------------------------------------------------------


def test_upload_puts_the_file_on_the_remote(remote, local_dir):
    (local_dir / NAME).write_bytes(b"ciphertext-bytes")
    backup_remote.upload(remote.config, local_dir, NAME)
    assert (remote.directory / NAME).read_bytes() == b"ciphertext-bytes"


def test_upload_creates_a_missing_remote_folder(tmp_path, local_dir):
    nested = make_local_remote(tmp_path, "r2")
    config = RemoteConfig(
        "fake", (nested.directory / "deep" / "er").as_posix(), nested.config.rclone_bin,
        nested.config.config_file,
    )  # fmt: skip
    (local_dir / NAME).write_bytes(b"x")
    backup_remote.upload(config, local_dir, NAME)
    assert (nested.directory / "deep" / "er" / NAME).read_bytes() == b"x"


def test_upload_sends_only_the_named_file(remote, local_dir):
    (local_dir / NAME).write_bytes(b"one")
    (local_dir / OTHER).write_bytes(b"two")
    (local_dir / "notes.txt").write_bytes(b"three")
    (local_dir / ".ledger-20261015-120000-1.part").write_bytes(b"partial")
    backup_remote.upload(remote.config, local_dir, NAME)
    assert [p.name for p in remote.directory.iterdir()] == [NAME]


def test_upload_refuses_names_that_are_not_backup_files(remote, local_dir):
    (local_dir / "notes.txt").write_bytes(b"secret")
    for bad in ("notes.txt", "../x", "ledger-x.db.gz.age", "ledger-20261015-120000.db"):
        with pytest.raises(RemoteError, match="備份檔名"):
            backup_remote.upload(remote.config, local_dir, bad)
    assert list(remote.directory.iterdir()) == []


def test_upload_refuses_to_overwrite_a_different_remote_file(remote, local_dir):
    (remote.directory / NAME).write_bytes(b"original-remote")
    (local_dir / NAME).write_bytes(b"different-local-content")
    with pytest.raises(RemoteError):
        backup_remote.upload(remote.config, local_dir, NAME)
    assert (remote.directory / NAME).read_bytes() == b"original-remote"


def test_upload_of_a_missing_local_file_leaves_nothing_on_the_remote(remote, local_dir):
    backup_remote.upload(
        remote.config, local_dir, NAME
    )  # rclone 本身 exit 0；由上傳後的 listing 驗證抓出
    assert backup_remote.list_files(remote.config) == []


# --- 列表 ------------------------------------------------------------------------------------


def test_list_files_returns_names_and_sizes_files_only(remote):
    (remote.directory / NAME).write_bytes(b"12345")
    (remote.directory / "notes.txt").write_bytes(b"ab")
    (remote.directory / "sub").mkdir()
    (remote.directory / "sub" / OTHER).write_bytes(b"nested")
    listing = {f.name: f.size for f in backup_remote.list_files(remote.config)}
    assert listing == {NAME: 5, "notes.txt": 2}  # 子資料夾不列、也不遞迴


def test_list_files_of_an_empty_folder_is_empty(remote):
    assert backup_remote.list_files(remote.config) == []


def test_list_files_of_a_missing_folder_is_an_error(tmp_path, remote):
    config = RemoteConfig(
        "fake",
        (tmp_path / "nowhere").as_posix(),
        remote.config.rclone_bin,
        remote.config.config_file,
    )
    with pytest.raises(RemoteError):
        backup_remote.list_files(config)


# --- 刪除 ------------------------------------------------------------------------------------


def test_delete_removes_exactly_one_file(remote):
    for name in (NAME, OTHER, "notes.txt"):
        (remote.directory / name).write_bytes(b"x")
    backup_remote.delete(remote.config, OTHER)
    assert sorted(p.name for p in remote.directory.iterdir()) == sorted([NAME, "notes.txt"])


def test_delete_refuses_names_that_are_not_backup_files_without_calling_rclone(remote, monkeypatch):
    (remote.directory / "notes.txt").write_bytes(b"x")
    monkeypatch.setattr(
        backup_remote.subprocess, "run", lambda *a, **k: pytest.fail("不應呼叫 rclone")
    )
    for bad in ("notes.txt", "../notes.txt", "sub/" + OTHER, "*", ""):
        with pytest.raises(RemoteError, match="備份檔名"):
            backup_remote.delete(remote.config, bad)
    assert (remote.directory / "notes.txt").exists()


def test_delete_of_a_missing_file_is_an_error(remote):
    with pytest.raises(RemoteError):
        backup_remote.delete(remote.config, NAME)


# --- 下載 ------------------------------------------------------------------------------------


def test_download_round_trip(remote, tmp_path):
    (remote.directory / NAME).write_bytes(b"cipher")
    dest = tmp_path / "dl"
    dest.mkdir()
    path = backup_remote.download(remote.config, NAME, dest)
    assert path == dest / NAME and path.read_bytes() == b"cipher"


def test_download_of_a_missing_file_is_an_error(remote, tmp_path):
    with pytest.raises(RemoteError):
        backup_remote.download(remote.config, NAME, tmp_path)


def test_download_refuses_non_backup_names(remote, tmp_path):
    (remote.directory / "notes.txt").write_bytes(b"x")
    with pytest.raises(RemoteError, match="備份檔名"):
        backup_remote.download(remote.config, "notes.txt", tmp_path)
    assert not (tmp_path / "notes.txt").exists()


# --- rclone 不存在／設定無效 -----------------------------------------------------------------


def test_missing_rclone_binary_is_a_clear_error(local_dir):
    config = RemoteConfig("fake", "x", "no-such-rclone-binary-xyz")
    with pytest.raises(RemoteError, match="找不到 rclone"):
        backup_remote.list_files(config)


def test_unknown_remote_name_fails_without_leaking_details(remote, caplog):
    config = RemoteConfig(
        "nosuch", remote.directory.as_posix(), remote.config.rclone_bin, remote.config.config_file
    )
    with caplog.at_level(logging.ERROR), pytest.raises(RemoteError) as excinfo:
        backup_remote.list_files(config)
    assert "nosuch" not in str(excinfo.value)  # 例外訊息是固定短句，不含 rclone 的原始輸出
    assert str(remote.config.config_file) not in caplog.text  # 設定檔路徑已被遮蔽


def test_missing_config_file_fails(remote):
    config = RemoteConfig(
        "fake", remote.directory.as_posix(), remote.config.rclone_bin, remote.directory / "no.conf"
    )
    with pytest.raises(RemoteError):
        backup_remote.list_files(config)


# --- 指令形狀與安全（mock，不需要真 rclone）--------------------------------------------------


class Recorder:
    def __init__(self, stdout=b"[]", stderr=b"", returncode=0):
        self.calls: list[tuple[list[str], dict]] = []
        self.result = subprocess.CompletedProcess([], returncode, stdout, stderr)

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        return self.result


def fake_config(**kw) -> RemoteConfig:
    # 任何存在的執行檔都行：subprocess.run 已被 mock，不會真的執行
    return RemoteConfig("gdrive", "ledger-backups", sys.executable, **kw)


def test_commands_are_lists_never_shell_and_never_destructive_bulk_commands(monkeypatch, tmp_path):
    recorder = Recorder()
    monkeypatch.setattr(backup_remote.subprocess, "run", recorder)
    config = fake_config(config_file=tmp_path / "rc.conf")
    backup_remote.upload(config, tmp_path, NAME)
    backup_remote.list_files(config)
    backup_remote.delete(config, NAME)
    (tmp_path / NAME).write_bytes(b"x")
    backup_remote.download(config, NAME, tmp_path)
    subcommands = []
    for command, kwargs in recorder.calls:
        assert isinstance(command, list) and not kwargs.get("shell")
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["timeout"] > 0
        assert command[1:3] == ["--config", str(tmp_path / "rc.conf")]
        assert "--ask-password=false" in command
        subcommands.append(next(a for a in command[3:] if not a.startswith("-")))
    assert subcommands == ["copy", "lsjson", "deletefile", "copyto"]
    assert not {"sync", "purge", "delete", "move", "moveto"} & set(subcommands)
    upload = recorder.calls[0][0]
    assert "--immutable" in upload and upload[upload.index("--include") + 1] == f"/{NAME}"


def test_deletefile_targets_exactly_one_file_path(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(backup_remote.subprocess, "run", recorder)
    backup_remote.delete(fake_config(), OTHER)
    assert recorder.calls[0][0][-2:] == ["deletefile", f"gdrive:ledger-backups/{OTHER}"]


def test_timeout_becomes_a_remote_error(monkeypatch):
    def boom(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(backup_remote.subprocess, "run", boom)
    with pytest.raises(RemoteError, match="逾時"):
        backup_remote.list_files(fake_config())


def test_unparseable_listing_is_a_remote_error(monkeypatch):
    monkeypatch.setattr(backup_remote.subprocess, "run", Recorder(stdout=b"not json"))
    with pytest.raises(RemoteError, match="無法解析"):
        backup_remote.list_files(fake_config())


SECRET_STDERR = (
    b'2026/10/15 ERROR : oauth2: token expired {"access_token":"ya29.SECRETACCESS",'
    b'"refresh_token":"1//SECRETREFRESH"} Authorization: Bearer ya29.SECRETBEARER '
    b"client_secret=SECRETCLIENT config /home/x/rclone-test.conf"
)


def test_token_and_config_path_never_reach_the_log_or_the_exception(monkeypatch, caplog):
    monkeypatch.setattr(
        backup_remote.subprocess, "run", Recorder(stderr=SECRET_STDERR, returncode=1)
    )
    config = fake_config(config_file=Path("/home/x/rclone-test.conf"))
    with caplog.at_level(logging.ERROR), pytest.raises(RemoteError) as excinfo:
        backup_remote.list_files(config)
    everything = caplog.text + str(excinfo.value)
    for secret in (
        "SECRETACCESS",
        "SECRETREFRESH",
        "SECRETBEARER",
        "SECRETCLIENT",
        "rclone-test.conf",
    ):
        assert secret not in everything
    assert "token expired" in caplog.text  # 有留下可診斷的內容，只是遮掉了秘密
    assert "exit 1" in str(excinfo.value)


def test_logged_stderr_is_truncated(monkeypatch, caplog):
    monkeypatch.setattr(backup_remote.subprocess, "run", Recorder(stderr=b"x" * 5000, returncode=1))
    with caplog.at_level(logging.ERROR), pytest.raises(RemoteError):
        backup_remote.list_files(fake_config())
    assert len(caplog.text) < backup_remote.STDERR_LOG_MAX + 400
