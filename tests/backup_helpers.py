"""備份測試共用工具：真的 age 金鑰、備份設定、直接寫入 backup_runs 的小工具。

需要真的 age 執行檔：本機找不到就 skip；在 CI（環境變數 CI 存在）找不到則直接失敗，
避免 CI 忘了安裝 age 卻默默跳過所有加密測試。
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


def require_age() -> str:
    found = shutil.which("age")
    keygen = shutil.which("age-keygen")
    if found and keygen:
        return found
    if os.environ.get("CI"):
        pytest.fail("CI 必須安裝 age（找不到 age／age-keygen）")
    pytest.skip("本機沒有 age／age-keygen")


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
