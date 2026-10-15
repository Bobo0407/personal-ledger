"""rclone 薄轉接層（P3-5B）：只負責呼叫 rclone CLI，不含任何保留或帳務規則。

- 直接用 subprocess（list 參數、不經 shell）呼叫 rclone；不引入任何 Python 雲端 SDK。
- 只用 copy／lsjson／deletefile／copyto（下載）。絕不使用 sync／purge／delete。
- 上傳只會送出「檔名完全符合備份格式」的那一個 .age 檔（--include 精確比對）。
  上傳用 `copy --immutable`：遠端已有同名但不同的檔案時 rclone 會失敗、不覆蓋。
  （實測：rclone 1.75.2 的 `copyto --immutable` 在單檔模式下不會擋覆蓋，所以不用 copyto 上傳。）
- rclone 的原始 stderr 只會在 scrub（遮 token／設定檔路徑）並截斷後寫進 log；
  RemoteError 的訊息是固定短句，不含 stderr，因此不會進 backup_runs 或畫面。
- rclone.conf 可能含 OAuth token：視為 secret，路徑與內容都不寫進 log。
"""

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# 本系統備份檔名（本機與遠端共用）：ledger-YYYYMMDD-HHMMSS[-N].db.gz.age
BACKUP_FILE_NAME = re.compile(r"ledger-(\d{8})-(\d{6})(?:-(\d+))?\.db\.gz\.age")
_REMOTE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")

UPLOAD_TIMEOUT_SECONDS = 600
DOWNLOAD_TIMEOUT_SECONDS = 600
LIST_TIMEOUT_SECONDS = 120
DELETE_TIMEOUT_SECONDS = 120
STDERR_LOG_MAX = 300

_SECRET_PATTERNS = (
    re.compile(
        r"""(?i)((?:access_token|refresh_token|id_token|client_secret|token)["']?\s*[:=]\s*)"""
        r"""("[^"]*"|'[^']*'|[^\s,;&]+)"""
    ),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(authorization:\s*).*"),
)


class RemoteError(Exception):
    """rclone 相關失敗。訊息是固定短句，不含 rclone 的原始輸出、token 或設定檔內容。"""


@dataclass(frozen=True)
class RemoteConfig:
    remote: str  # rclone remote 名稱（rclone config 裡的 [section]）
    path: str  # remote 內的資料夾
    rclone_bin: str = "rclone"
    config_file: Path | None = None  # --config；內容是 secret，永遠不寫進 log

    @property
    def spec(self) -> str:
        return f"{self.remote}:{self.path}"


@dataclass(frozen=True)
class RemoteFile:
    name: str
    size: int


def validate(config: RemoteConfig) -> None:
    """設定格式檢查（不連線）。不回顯設定值。"""
    if not _REMOTE_NAME.fullmatch(config.remote):
        raise RemoteError("RCLONE_REMOTE 格式不正確（只能含英數、底線、點、連字號）")
    path = config.path
    if (
        not path.strip()
        or path.startswith("-")
        or "\n" in path
        or "\x00" in path
        or ".." in path.replace("\\", "/").split("/")
    ):
        raise RemoteError("RCLONE_PATH 格式不正確（不可為空、以 - 開頭或含 ..）")


def _scrub(text: str, config: RemoteConfig) -> str:
    if config.config_file is not None:
        text = text.replace(str(config.config_file), "<config>")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}<redacted>", text)
    return text


def _find(config: RemoteConfig) -> str:
    found = shutil.which(config.rclone_bin)
    if found is None:
        raise RemoteError("找不到 rclone 執行檔（請安裝 rclone，或設定 RCLONE_BIN）")
    return found


def _run(config: RemoteConfig, args: list[str], timeout: int, what: str) -> str:
    rclone = _find(config)
    command = [rclone]
    if config.config_file is not None:
        command += ["--config", str(config.config_file)]
    command += ["--ask-password=false", *args]
    try:
        proc = subprocess.run(  # noqa: S603  list 參數、不經 shell
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.error("rclone %s timed out", what)
        raise RemoteError(f"rclone {what}逾時") from None
    except OSError:
        logger.error("rclone %s could not be started", what)
        raise RemoteError(f"無法執行 rclone（{what}）") from None
    if proc.returncode != 0:
        detail = _scrub(proc.stderr.decode("utf-8", "replace").strip(), config)
        logger.error(
            "rclone %s failed (exit %d): %s", what, proc.returncode, detail[:STDERR_LOG_MAX]
        )
        raise RemoteError(f"rclone {what}失敗（exit {proc.returncode}）")
    return proc.stdout.decode("utf-8", "replace")


def _require_backup_name(name: str) -> None:
    if not BACKUP_FILE_NAME.fullmatch(name):
        raise RemoteError("不是合法的備份檔名")


def _file_spec(config: RemoteConfig, name: str) -> str:
    return f"{config.spec.rstrip('/')}/{name}"


def upload(config: RemoteConfig, backup_dir: Path, name: str) -> None:
    """把 backup_dir 裡的 name 上傳到遠端。同名但不同的遠端檔案 → 失敗，不覆蓋。"""
    _require_backup_name(name)
    _run(
        config,
        ["copy", "--immutable", "--include", f"/{name}", str(backup_dir.resolve()), config.spec],
        UPLOAD_TIMEOUT_SECONDS,
        "上傳",
    )


def list_files(config: RemoteConfig) -> list[RemoteFile]:
    """列出遠端資料夾內的檔案（不含子資料夾；包含不屬於本系統的檔案，由呼叫端自行過濾）。"""
    out = _run(
        config,
        ["lsjson", "--files-only", "--no-mimetype", "--no-modtime", config.spec],
        LIST_TIMEOUT_SECONDS,
        "列出檔案",
    )
    try:
        entries = json.loads(out)
        return [RemoteFile(str(e["Name"]), int(e["Size"])) for e in entries if not e["IsDir"]]
    except (ValueError, KeyError, TypeError):
        logger.error("rclone lsjson returned unexpected output")
        raise RemoteError("rclone 列出檔案的輸出無法解析") from None


def delete(config: RemoteConfig, name: str) -> None:
    """刪除遠端單一備份檔（deletefile）。非本系統備份檔名一律拒絕，不呼叫 rclone。"""
    _require_backup_name(name)
    _run(config, ["deletefile", _file_spec(config, name)], DELETE_TIMEOUT_SECONDS, "刪除檔案")


def download(config: RemoteConfig, name: str, dest_dir: Path) -> Path:
    """下載遠端備份（密文）到 dest_dir，回傳本機路徑。"""
    _require_backup_name(name)
    target = dest_dir / name
    _run(
        config,
        ["copyto", _file_spec(config, name), str(target)],
        DOWNLOAD_TIMEOUT_SECONDS,
        "下載",
    )
    if not target.is_file():
        raise RemoteError("下載後找不到檔案")
    return target
