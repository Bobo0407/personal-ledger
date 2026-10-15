"""加密備份：本機（P3-5A）＋雲端上傳（P3-5B）（規格 F29、7.2）。

備份和網站分開執行，不經過 LedgerService。

流程：SQLite backup API → 記憶體快照（integrity_check）→ serialize → gzip → age 加密 → 檔案
- 磁碟上不會出現未加密的 DB 或壓縮檔：快照與壓縮都在記憶體裡，只有 age 的輸出（密文）會落地。
- 金鑰：只用 age「公鑰」（recipient，來自環境變數）；私鑰不在這台機器，只有還原驗證時由
  --identity 傳入。recipient、identity 的內容與路徑都不寫進 log、error 或畫面。
- 來源 DB 用唯讀 URI 開啟，不可能被改動，檔案不存在時也不會被建立。

檔名：ledger-YYYYMMDD-HHMMSS.db.gz.age（台北時間）；同一秒重名加 -2、-3。

保留（規格 F29）：每日備份保留 30 天，加上每月第一份保留 12 個月（當月 + 前 11 個月）。
只刪除檔名完全符合上述格式的檔案，永遠不刪剛產生的那份。

雲端（P3-5B）：本機密文完成後，用 rclone 只上傳那一個 .age 檔，再列出遠端資料夾，確認該檔存在且
大小等於本機檔，才算上傳成功（不依賴任何 backend 的 hash 行為）。遠端保留規則與本機相同，重用
select_deletions，只刪檔名符合備份格式的單一檔案；遠端資料夾內其他檔案永遠不碰。

SUCCESS 的語意（P3-5B 起）：本機加密檔 ＋ 已設定的雲端上傳與驗證都完成。
- 未設定 RCLONE_REMOTE 且沒有 --local-only → 設定錯誤：寫 FAILED、不產生本機檔。
- 明確指定 --local-only → SUCCESS，但 error 存固定的「僅本機」警告（首頁黃色）。
- 本機檔完成、雲端上傳或驗證失敗 → FAILED，file_name／size_bytes 保留、本機檔保留、
  不做任何清理（降級時不動舊備份）。
- P3-5B 之前寫入的 SUCCESS 一律視為「僅本機」（正式環境的第一次排程備份在 P3-6，沒有這種歷史）。

backup_runs（只在結束時寫一筆）
- 流程失敗且來源 DB 仍可寫 → 嘗試寫一筆 FAILED，然後丟出 BackupFailed。
- 來源 DB 不存在／嚴重損毀／無法寫入，或 backup_runs 的 INSERT 本身失敗 → 無法保證有 FAILED
  紀錄：丟出 BackupRecordError（腳本 exit 2），完整例外寫進 log。不會建立第二份 metadata。
- 備份成功但清理舊檔（本機或雲端）失敗 → 仍是 SUCCESS，error 存簡短警告，首頁顯示黃色。
"""

import gzip
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app import clock
from app.config import BackupSettings
from app.models import BackupRun
from app.repositories import backup_runs as run_repo
from app.services import backup_remote
from app.services.backup_remote import RemoteConfig, RemoteError

logger = logging.getLogger(__name__)

FILE_NAME_FORMAT = "ledger-{stamp}.db.gz.age"
_FILE_NAME = backup_remote.BACKUP_FILE_NAME
_RECIPIENT = re.compile(r"age1[0-9a-z]{20,}")
_SECRET = re.compile(r"AGE-SECRET-KEY-[0-9A-Z]+")

DAILY_KEEP_DAYS = 30
MONTHLY_KEEP_MONTHS = 12  # 當月 + 前 11 個月
STALE_AFTER = timedelta(hours=36)
ERROR_MAX = 500
STEP_STDERR_MAX = 300
AGE_TIMEOUT_SECONDS = 120
SQLITE_TIMEOUT_SECONDS = 30

CLEANUP_WARNING = "舊備份清理失敗（詳見 log）"  # 本機
REMOTE_CLEANUP_WARNING = "雲端舊備份清理失敗（詳見 log）"
LOCAL_ONLY_WARNING = "僅本機備份（未上傳雲端）"
UPLOAD_FAILED = "雲端上傳失敗（詳見 log）"
WARNING_SEPARATOR = "；"


class BackupFailed(Exception):
    """備份流程失敗（已盡力寫入 FAILED 紀錄）。原始例外在 __cause__。"""


class BackupRecordError(Exception):
    """無法寫入 backup_runs（來源 DB 不可寫、或 INSERT 失敗）。不保證有紀錄。"""


class BackupStepError(Exception):
    """備份或還原某一步失敗。訊息不含 recipient、identity 或任何金鑰內容。"""


@dataclass(frozen=True)
class BackupConfig:
    database_path: Path
    backup_dir: Path
    age_bin: str
    recipient: str | None
    remote: RemoteConfig | None = None
    # 明確 opt-in 只做本機（開發／CI）。正式排程不得使用；忘了設定 remote 時要失敗，而不是降級。
    local_only: bool = False


def config_from_settings(settings: BackupSettings, local_only: bool = False) -> BackupConfig:
    from sqlalchemy.engine import make_url

    database = make_url(settings.database_url).database or ""
    remote_name = (settings.rclone_remote or "").strip()
    remote = None
    if remote_name:
        config_file = (settings.rclone_config or "").strip()
        remote = RemoteConfig(
            remote=remote_name,
            path=settings.rclone_path.strip(),
            rclone_bin=settings.rclone_bin,
            config_file=Path(config_file) if config_file else None,
        )
    return BackupConfig(
        database_path=Path(database),
        backup_dir=settings.backup_dir,
        age_bin=settings.backup_age_bin,
        recipient=(settings.backup_age_recipient or "").strip() or None,
        remote=remote,
        local_only=local_only,
    )


# --- 加密／解密（age CLI）------------------------------------------------------------------


def _scrub(text: str, *secrets: str | None) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return _SECRET.sub("<redacted>", text)


def _find_age(age_bin: str) -> str:
    found = shutil.which(age_bin)
    if found is None:
        raise BackupStepError("找不到 age 執行檔（請安裝 age，或設定 BACKUP_AGE_BIN）")
    return found


def _check_recipient(recipient: str | None) -> str:
    if recipient is None:
        raise BackupStepError("未設定 BACKUP_AGE_RECIPIENT（age 公鑰）")
    if not _RECIPIENT.fullmatch(recipient):
        # 不回顯設定值：使用者可能誤貼了私鑰
        raise BackupStepError("BACKUP_AGE_RECIPIENT 格式不正確（應為 age1 開頭的公鑰）")
    return recipient


def _run_age(args: list[str], data: bytes | None, *secrets: str | None) -> bytes:
    try:
        proc = subprocess.run(  # noqa: S603  list 參數、不經 shell
            args, input=data, capture_output=True, timeout=AGE_TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired:
        raise BackupStepError("age 執行逾時") from None
    if proc.returncode != 0:
        detail = _scrub(proc.stderr.decode("utf-8", "replace").strip(), *secrets)
        raise BackupStepError(f"age 失敗（exit {proc.returncode}）：{detail[:STEP_STDERR_MAX]}")
    return proc.stdout


# --- 快照 ----------------------------------------------------------------------------------


def snapshot(database_path: Path) -> bytes:
    """來源 DB 的一致快照（SQLite backup API），回傳 serialize 後的 bytes。全程在記憶體。"""
    if not database_path.is_file():
        raise BackupStepError("來源資料庫不存在")
    uri = database_path.resolve().as_uri() + "?mode=ro"
    source = sqlite3.connect(uri, uri=True, timeout=SQLITE_TIMEOUT_SECONDS)
    copy = sqlite3.connect(":memory:")
    try:
        source.backup(copy)
        result = copy.execute("PRAGMA integrity_check").fetchall()
        if result != [("ok",)]:
            raise BackupStepError("快照 integrity_check 未通過")
        return copy.serialize()
    finally:
        copy.close()
        source.close()


# --- 檔名與保留 ----------------------------------------------------------------------------


def parse_file_name(name: str) -> tuple[datetime, int] | None:
    """符合備份檔名格式 → (台北時間, 序號)；其他檔名 → None。"""
    match = _FILE_NAME.fullmatch(name)
    if match is None:
        return None
    try:
        stamp = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return stamp.replace(tzinfo=clock.APP_TIMEZONE), int(match.group(3) or 1)


def _month_index(moment: datetime) -> int:
    return moment.year * 12 + moment.month


def select_deletions(names: Iterable[str], now: datetime) -> list[str]:
    """依保留規則挑出可刪除的備份檔名（純函式）。不符合備份檔名格式的一律忽略。

    保留：(a) 時間戳距今 ≤ 30 天；(b) 其所屬月份目前存在的最早一份，且該月在當月與前 11 個月內。
    """
    parsed = {name: parse_file_name(name) for name in names}
    backups = {name: key for name, key in parsed.items() if key is not None}
    earliest: dict[int, str] = {}
    for name, (moment, seq) in sorted(backups.items(), key=lambda item: item[1]):
        earliest.setdefault(_month_index(moment), name)
    now_month = _month_index(now.astimezone(clock.APP_TIMEZONE))
    doomed = []
    for name, (moment, _) in backups.items():
        if now - moment <= timedelta(days=DAILY_KEEP_DAYS):
            continue
        month = _month_index(moment)
        if earliest[month] == name and 0 <= now_month - month < MONTHLY_KEEP_MONTHS:
            continue
        doomed.append(name)
    return sorted(doomed)


def cleanup(backup_dir: Path, now: datetime, protect: str | None = None) -> list[str]:
    """刪除過期的備份檔，回傳已刪除的檔名。逐檔嘗試；有失敗就在最後丟 BackupStepError。"""
    names = [p.name for p in backup_dir.iterdir() if p.is_file()]
    removed, failed = [], 0
    for name in select_deletions(names, now):
        if name == protect:
            continue
        try:
            (backup_dir / name).unlink()
            removed.append(name)
        except OSError:
            logger.exception("backup cleanup: could not delete %s", name)
            failed += 1
    if failed:
        raise BackupStepError(f"{failed} 個舊備份檔無法刪除")
    return removed


def _final_path(backup_dir: Path, started: datetime) -> Path:
    stamp = started.strftime("%Y%m%d-%H%M%S")
    candidate = backup_dir / FILE_NAME_FORMAT.format(stamp=stamp)
    seq = 2
    while candidate.exists():
        candidate = backup_dir / FILE_NAME_FORMAT.format(stamp=f"{stamp}-{seq}")
        seq += 1
    return candidate


# --- 執行備份 ------------------------------------------------------------------------------


def _create_backup(config: BackupConfig, started: datetime) -> Path:
    recipient = _check_recipient(config.recipient)
    age = _find_age(config.age_bin)
    data = snapshot(config.database_path)
    compressed = gzip.compress(data, mtime=0)
    config.backup_dir.mkdir(parents=True, exist_ok=True)
    part = config.backup_dir / f".ledger-{started:%Y%m%d-%H%M%S}-{os.getpid()}.part"
    try:
        _run_age([age, "-r", recipient, "-o", str(part)], compressed, recipient)
        final = _final_path(config.backup_dir, started)
        os.replace(part, final)
        return final
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            logger.exception("backup: could not remove temporary file")


def _error_text(exc: BaseException, config: BackupConfig) -> str:
    text = _scrub(f"{type(exc).__name__}: {exc}", config.recipient)
    return text[:ERROR_MAX]


def _commit_run(db: Session, run: BackupRun) -> None:
    try:
        run_repo.add(db, run)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("backup: could not write backup_runs")
        raise BackupRecordError("無法寫入 backup_runs") from exc


def _check_destination(config: BackupConfig) -> None:
    """設定檢查（在產生任何檔案之前）。沒有 --local-only 卻沒設 remote → 設定錯誤，不降級。"""
    if config.local_only:
        return
    if config.remote is None:
        raise BackupStepError(
            "未設定 RCLONE_REMOTE（雲端備份目的地）；只做本機備份請明確加上 --local-only"
        )
    try:
        backup_remote.validate(config.remote)
    except RemoteError as exc:
        raise BackupStepError(str(exc)) from None


def _upload_and_verify(remote: RemoteConfig, final: Path) -> list[backup_remote.RemoteFile]:
    """上傳並驗證：rclone exit 0，且遠端 listing 找得到該檔、大小等於本機檔。回傳遠端 listing。"""
    backup_remote.upload(remote, final.parent, final.name)
    files = backup_remote.list_files(remote)
    match = [f for f in files if f.name == final.name]
    if not match:
        raise RemoteError("上傳後遠端找不到該檔案")
    if match[0].size != final.stat().st_size:
        raise RemoteError("遠端檔案大小與本機不符")
    return files


def cleanup_remote(
    remote: RemoteConfig, files: list[backup_remote.RemoteFile], now: datetime, protect: str
) -> list[str]:
    """依同一套保留規則刪除遠端過期備份（只動合法檔名的單檔）；有失敗最後丟 BackupStepError。"""
    removed, failed = [], 0
    for name in select_deletions([f.name for f in files], now):
        if name == protect:
            continue
        try:
            backup_remote.delete(remote, name)
            removed.append(name)
        except RemoteError:
            failed += 1
    if failed:
        raise BackupStepError(f"{failed} 個遠端舊備份檔無法刪除")
    return removed


def run_backup(db: Session, config: BackupConfig) -> BackupRun:
    """執行一次備份並寫入 backup_runs。失敗丟 BackupFailed 或 BackupRecordError（見模組說明）。"""
    started = clock.now()
    try:
        _check_destination(config)
        final = _create_backup(config, started)
    except Exception as exc:
        logger.exception("backup failed")
        _commit_run(
            db,
            BackupRun(
                started_at=clock.to_iso(started),
                finished_at=clock.now_iso(),
                status="FAILED",
                error=_error_text(exc, config),
            ),
        )
        raise BackupFailed("備份失敗") from exc

    size = final.stat().st_size
    warnings: list[str] = []
    remote_files: list[backup_remote.RemoteFile] = []
    use_remote = not config.local_only and config.remote is not None
    if use_remote:
        try:
            remote_files = _upload_and_verify(config.remote, final)
        except Exception as exc:
            # 本機密文已完成並保留；不做任何清理，直接記錄雲端失敗
            logger.exception("backup upload failed")
            _commit_run(
                db,
                BackupRun(
                    started_at=clock.to_iso(started),
                    finished_at=clock.now_iso(),
                    status="FAILED",
                    file_name=final.name,
                    size_bytes=size,
                    error=UPLOAD_FAILED,
                ),
            )
            raise BackupFailed("雲端上傳失敗") from exc
    else:
        warnings.append(LOCAL_ONLY_WARNING)

    try:
        cleanup(config.backup_dir, started, protect=final.name)
    except Exception:
        logger.exception("backup cleanup failed")
        warnings.append(CLEANUP_WARNING)
    if use_remote:
        try:
            cleanup_remote(config.remote, remote_files, started, protect=final.name)
        except Exception:
            logger.exception("backup remote cleanup failed")
            warnings.append(REMOTE_CLEANUP_WARNING)

    run = BackupRun(
        started_at=clock.to_iso(started),
        finished_at=clock.now_iso(),
        status="SUCCESS",
        file_name=final.name,
        size_bytes=size,
        error=WARNING_SEPARATOR.join(warnings) or None,
    )
    _commit_run(db, run)
    logger.info(
        "backup ok: %s (%d bytes)%s", final.name, size, " [local only]" if not use_remote else ""
    )
    return run


# --- 還原驗證（P3-5A：解密、可開啟、完整性；Docker 與餘額比對留 P3-7）---------------------------


@dataclass(frozen=True)
class RestoreReport:
    integrity_ok: bool
    alembic_version: str | None
    row_counts: dict[str, int]


VERIFY_TABLES = ("accounts", "transactions", "postings", "backup_runs")


def verify_backup(backup_file: Path, identity_file: Path, age_bin: str = "age") -> RestoreReport:
    """解密備份、gunzip，以 deserialize 載入記憶體 SQLite 驗證。明文不寫入磁碟。"""
    age = _find_age(age_bin)
    compressed = _run_age([age, "-d", "-i", str(identity_file), str(backup_file)], None)
    try:
        data = gzip.decompress(compressed)
    except (OSError, EOFError) as exc:
        raise BackupStepError("解密後不是有效的 gzip 資料") from exc
    conn = sqlite3.connect(":memory:")
    try:
        try:
            conn.deserialize(data)
            integrity = conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            version = None
            if "alembic_version" in tables:
                row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
                version = row[0] if row else None
            counts = {
                table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
                for table in VERIFY_TABLES
                if table in tables
            }
        except sqlite3.DatabaseError as exc:
            raise BackupStepError("解密後的資料不是可讀的 SQLite 資料庫") from exc
    finally:
        conn.close()
    return RestoreReport(integrity, version, counts)


def latest_backup_name(names: Iterable[str]) -> str | None:
    """合法備份檔名中時間最新的一個；不符合格式的檔名（例如遠端資料夾裡的其他檔案）一律忽略。"""
    keyed = [(key, name) for name in names if (key := parse_file_name(name)) is not None]
    return max(keyed)[1] if keyed else None


def verify_remote_backup(
    remote: RemoteConfig, which: str, identity_file: Path, age_bin: str = "age"
) -> tuple[str, RestoreReport]:
    """從遠端下載一份備份（which = 'latest' 或檔名）→ 解密 → gunzip → SQLite 驗證。

    只下載密文到暫存目錄（finally 清掉）；明文只在記憶體。不碰任何正式資料庫。
    """
    try:
        backup_remote.validate(remote)
        names = [f.name for f in backup_remote.list_files(remote)]
        if which == "latest":
            name = latest_backup_name(names)
            if name is None:
                raise BackupStepError("遠端沒有任何合法的備份檔")
        else:
            if parse_file_name(which) is None:
                raise BackupStepError("不是合法的備份檔名")
            if which not in names:
                raise BackupStepError("遠端找不到指定的備份檔")
            name = which
        with tempfile.TemporaryDirectory(prefix="ledger-restore-") as tmp:
            downloaded = backup_remote.download(remote, name, Path(tmp))
            return name, verify_backup(downloaded, identity_file, age_bin)
    except RemoteError as exc:
        raise BackupStepError(str(exc)) from None


# --- 首頁備份狀態 --------------------------------------------------------------------------


@dataclass(frozen=True)
class BackupStatus:
    level: str  # ok／warn／alert；template 只依它選樣式
    message: str


def _when_label(moment: datetime, now: datetime) -> str:
    local, today = moment.astimezone(clock.APP_TIMEZONE), now.astimezone(clock.APP_TIMEZONE)
    days = (today.date() - local.date()).days
    clock_text = f"{local:%H:%M}"
    if days == 0:
        return f"今天 {clock_text}"
    if days == 1:
        return f"昨天 {clock_text}"
    return f"{local:%m-%d} {clock_text}"


def backup_status(db: Session, now: datetime | None = None) -> BackupStatus:
    """首頁「最後備份」一行（規格 8.1）。只看最後一筆 backup_runs，不顯示原始 error。

    FAILED → alert（有 file_name 表示本機密文已完成、只是雲端失敗，文字要講清楚）；
    SUCCESS 但超過 36 小時（嚴格大於）→ warn；SUCCESS 但僅本機 → warn；SUCCESS 但清理舊檔
    （本機或雲端）失敗 → warn；沒有任何紀錄 → warn；其餘 → ok。
    """
    now = now or clock.now()
    run = run_repo.latest(db)
    if run is None:
        return BackupStatus("warn", "尚未有備份紀錄")
    finished = datetime.fromisoformat(run.finished_at)
    label = _when_label(finished, now)
    if run.status == "FAILED":
        if run.file_name is not None:
            return BackupStatus("alert", f"本機備份已完成，但雲端上傳失敗（{label}），請檢查 log")
        return BackupStatus("alert", f"最近一次備份失敗（{label}），請檢查 log")
    if now - finished > STALE_AFTER:
        return BackupStatus("warn", f"已超過 36 小時未備份，最後成功：{label}")
    if run.error is not None:
        if LOCAL_ONLY_WARNING in run.error:
            return BackupStatus("warn", "最後備份僅在本機，尚未上傳雲端")
        return BackupStatus("warn", "最後備份成功，但舊備份清理失敗，請檢查 log")
    return BackupStatus("ok", f"最後備份：{label} ✓")
