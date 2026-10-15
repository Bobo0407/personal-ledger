"""備份測試共用工具：真的 age 金鑰、真的 rclone（local backend）、備份設定、寫入 backup_runs。

需要真的 age／rclone 執行檔：本機找不到就 skip；在 CI（環境變數 CI 存在）找不到則直接失敗，
避免 CI 忘了安裝卻默默跳過所有相關測試。雲端用 rclone 的 local backend 模擬，不連任何 Google 服務。
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.models import BackupRun
from app.services.backup import BackupConfig
from app.services.backup_remote import RemoteConfig


def require_age() -> str:
    found = shutil.which("age")
    keygen = shutil.which("age-keygen")
    if found and keygen:
        return found
    if os.environ.get("CI"):
        pytest.fail("CI 必須安裝 age（找不到 age／age-keygen）")
    pytest.skip("本機沒有 age／age-keygen")


def require_rclone() -> str:
    found = shutil.which("rclone")
    if found:
        return found
    if os.environ.get("CI"):
        pytest.fail("CI 必須安裝 rclone（找不到 rclone）")
    pytest.skip("本機沒有 rclone")


@dataclass(frozen=True)
class FakeRemote:
    config: RemoteConfig
    directory: Path  # local backend 的實體資料夾（等同雲端上的備份資料夾）


def make_local_remote(tmp_path: Path, name: str = "remote") -> FakeRemote:
    """用 rclone 的 local backend 當「雲端」：暫存 rclone.conf ＋ 一個暫存資料夾。"""
    rclone = require_rclone()
    conf = tmp_path / "rclone-test.conf"
    conf.write_text("[fake]\ntype = local\n", encoding="utf-8")
    directory = tmp_path / name
    directory.mkdir()
    return FakeRemote(RemoteConfig("fake", directory.as_posix(), rclone, conf), directory)


@dataclass(frozen=True)
class AgeKeys:
    age_bin: str
    recipient: str
    identity: Path


def make_age_keys(directory: Path, name: str = "test-key.txt") -> AgeKeys:
    age_bin = require_age()
    identity = directory / name
    subprocess.run(
        [shutil.which("age-keygen"), "-o", str(identity)], check=True, capture_output=True
    )
    match = re.search(r"public key: (age1[0-9a-z]+)", identity.read_text(encoding="utf-8"))
    assert match
    return AgeKeys(age_bin, match.group(1), identity)


def db_path_of(session) -> Path:
    return Path(session.get_bind().url.database)


def make_config(session, backup_dir: Path, keys: AgeKeys | None, **overrides) -> BackupConfig:
    values = {
        "database_path": db_path_of(session),
        "backup_dir": backup_dir,
        "age_bin": keys.age_bin if keys else "age",
        "recipient": keys.recipient if keys else None,
    }
    values.update(overrides)
    # 沒指定 remote 的測試等同 --local-only；要測預設行為（要求 remote）請明確傳 local_only=False
    values.setdefault("local_only", values.get("remote") is None)
    return BackupConfig(**values)


def add_run(session, status="SUCCESS", finished="2026-10-15T03:00:00+08:00", **fields) -> BackupRun:
    if status == "SUCCESS":
        fields.setdefault("file_name", "ledger-20261015-030000.db.gz.age")
        fields.setdefault("size_bytes", 1234)
    else:
        fields.setdefault("error", "boom")
    run = BackupRun(started_at=finished, finished_at=finished, status=status, **fields)
    session.add(run)
    session.commit()
    return run
