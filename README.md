# 個人記帳系統（ledger）

以複式記帳為核心的個人財務 Web 系統。FastAPI + SQLite + SQLAlchemy/Alembic + Jinja2/HTMX。

> 目前進度：P0 地基、P1 帳務核心（後端，尚無記帳介面）。P2 每日記帳介面開發中。完整 README 於 P3 補齊。

## 架構

```
routers/       只接收請求、驗證登入，呼叫 services；不碰 repositories、LedgerService、Posting
templates/     只負責畫面
services/      帳務規則、驗證、分錄產生、報表邏輯
  transactions.py   交易流程串接（router 唯一要呼叫的帳務入口）
  entries.py        EntryBuilder：表單 → 平衡的分錄草稿（純函式）
  validation.py     所有帳務驗證規則（擋下／確認／提醒）
  txn_structure.py  依交易類型的分錄結構檢查（第二道防線）
  ledger.py         LedgerService：唯一的寫入入口（新增、修改、刪除、還原 + 稽核）
  ledger_queries.py 餘額、期間合計、淨資產
  integrity.py      帳平檢查
repositories/  資料存取，不放商業規則、不 commit
  ledger.py         active_postings()：有效分錄的唯一查詢入口
models/        資料表定義；schema 變更一律透過 Alembic（migrations/）
```

寫入路徑：`transactions.record_*` → `Builder.check(form)` → 解析主資料 → `Builder.build()` →
`ledger.post()`（`validate()` → 寫入交易與分錄 → 稽核 → commit；任何錯誤整批 rollback）。
分層規則由 `tests/unit/test_architecture.py` 自動檢查。

## 帳務核心原則

- **複式記帳**：`transactions` 是表頭，`postings` 是金額的唯一來源；借方為正、貸方為負，每筆交易至少兩筆分錄且加總為 0。
- **金額是整數**（新台幣元）；外幣另存原幣別與原幣最小單位金額。
- **餘額不儲存**：一律由有效分錄即時計算。有效分錄 = `deleted_at IS NULL AND status = 'POSTED'`，
  這個條件只寫在 `repositories/ledger.active_postings()`。
- **只能記在最末層科目**；系統科目（期初權益、帳務調整）用 `system_key` 識別，不依賴固定 id。
- **付款方式只是管道**，對應到實際的資產或負債帳戶；繳信用卡不計入支出。
- **寬鬆修改 + 稽核 + 軟刪除**：交易可修改、刪除、還原，每次異動都寫 `audit_logs`；交易類型不能修改。
- **退款**沖回原交易的 MAIN 費用科目並連結原交易，不是收入。

## 設計文件

- 架構決策（ADR）：
  [001 複式記帳](docs/adr/ADR-001-double-entry-ledger.md)、
  [002 active_postings()](docs/adr/ADR-002-active-postings.md)、
  [003 帳務寫入路徑](docs/adr/ADR-003-ledger-write-path.md)、
  [004 主資料](docs/adr/ADR-004-master-data.md)、
  [005 交易生命週期](docs/adr/ADR-005-transaction-lifecycle.md)、
  [006 退款模型](docs/adr/ADR-006-refund-model.md)、
  [008 備份加密（age）](docs/adr/ADR-008-backup-encryption-age.md)、
  [009 雲端備份（rclone）](docs/adr/ADR-009-backup-remote-rclone.md)、
  [010 部署架構](docs/adr/ADR-010-deployment.md)
- [部署與維運手冊（Linux VM、systemd、Caddy、備份 timer）](docs/deploy.md)
- [P1 決策紀錄](docs/decisions/p1.md)
- [P1 規則與測試對照](docs/p1-rules.md)

## 本機開發

需求：Python 3.12 以上。

```bash
python -m venv .venv
# Windows:   .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # Windows: copy .env.example .env
```

## 資料庫 migration

資料庫位置由 `.env` 的 `DATABASE_URL` 決定，資料表結構一律透過 Alembic 變更。

```bash
alembic upgrade head      # 建立或升級到最新結構
alembic current           # 查看目前版本
alembic downgrade base    # 全部退回（會刪除資料表，只在開發時使用）
```

## 載入主資料（科目表、付款方式）

第一次建立資料庫後執行一次。可以重複執行：只新增缺少的資料，不會覆寫你在介面上改過的名稱、排序，也不會刪除任何資料；
和既有資料的結構衝突（例如同一個科目代碼的五大類不同）時整批中止，不寫入任何資料。

```bash
python scripts/seed_master_data.py --dry-run   # 先看會新增什麼，不寫入
python scripts/seed_master_data.py
```

內容在 `seeds/chart_of_accounts.yaml`。

## 建立帳號

系統只允許一個帳號，第一次使用時建立（只需要 `DATABASE_URL`，不需要 `SECRET_KEY`）：

```bash
python scripts/create_user.py
```

密碼至少 12 個字元（不要求大小寫、數字或符號組合）。輸入時畫面**不會顯示任何字元**，這是正常的，打完直接按 Enter。

### 終端機無法輸入密碼時（一次性環境變數）

部分終端機無法使用隱藏輸入。這時按 `Ctrl+C` 中止，改用一次性環境變數 `LEDGER_INITIAL_PASSWORD`。
以下在 **PowerShell** 執行：用 `Read-Host -AsSecureString` 輸入，密碼不會出現在畫面上，也不會留在指令歷史紀錄裡。

```powershell
$env:LEDGER_INITIAL_PASSWORD = [System.Net.NetworkCredential]::new('', (Read-Host '密碼' -AsSecureString)).Password
python scripts/create_user.py
Remove-Item Env:LEDGER_INITIAL_PASSWORD     # 用完立即清除
```

注意事項：

- 不要寫成 `$env:LEDGER_INITIAL_PASSWORD = "明文密碼"`，那樣密碼會留在 PowerShell 的歷史紀錄檔裡。
- 這個變數只有 `create_user.py` 會讀；不要放進 `.env`，也不會出現在 `.env.example`。
- 資料庫只存 Argon2 雜湊，日誌只記 user_id，兩者都不會出現密碼。
- 清除後可以用 `Test-Path Env:LEDGER_INITIAL_PASSWORD` 確認，應該回傳 `False`；或直接關掉這個終端機視窗。

## 啟動開發伺服器

`.env` 必須有 `SECRET_KEY`，否則啟動時會直接失敗。

```bash
alembic upgrade head
python scripts/create_user.py     # 第一次使用時建立唯一帳號，見上一節
uvicorn app.main:create_app --factory --reload
```

開啟 http://127.0.0.1:8000/ 會被導向登入頁。應用程式日誌寫在 `LOG_DIR/app.log`（預設 `./logs`），每天輪替、保留 14 天。

## 加密備份：本機（P3-5A）＋雲端 rclone（P3-5B）

備份和網站分開執行，網站沒開也能備份。流程：SQLite backup API → 記憶體快照 → gzip → [age](https://github.com/FiloSottile/age) 加密 → 檔案
（磁碟上不會出現未加密的資料庫或壓縮檔）。接著用 [rclone](https://rclone.org/) 把那一個密文檔上傳到雲端並驗證。排程（systemd timer）在 P3-6。

一次性準備：

```powershell
winget install FiloSottile.age          # Linux：sudo apt install age
age-keygen -o ledger-backup-key.txt      # 私鑰檔：存進密碼管理員後，從這台機器刪除
```

`age-keygen` 會印出 `Public key: age1…`。只把**公鑰**放進 `.env`（私鑰絕對不要放在伺服器或專案目錄）：

```
BACKUP_DIR=./data/backups
BACKUP_AGE_RECIPIENT=age1…
```

雲端（rclone）：`rclone.conf` 含 OAuth token，**視為 secret，不進 Git**。先用 `rclone config` 建立一個 remote
（Google Drive 建議用 `drive.file` scope，這點待真 Drive 驗收確認），再在 `.env` 設定：

```
RCLONE_REMOTE=<rclone config 裡的 remote 名稱>
RCLONE_PATH=ledger-backups
# RCLONE_CONFIG=<rclone.conf 路徑>   # 選填；正式排程的服務帳號建議明確指定
```

執行備份與還原驗證（不需要 `SECRET_KEY`）：

```bash
python scripts/backup.py              # 正式用法：本機加密 + 上傳 + 驗證
python scripts/backup.py --local-only # 明確只做本機（開發／CI）；P3-6 的正式排程不得使用
python scripts/restore_check.py --backup data/backups/ledger-YYYYMMDD-HHMMSS.db.gz.age --identity <私鑰檔>
python scripts/restore_check.py --remote latest --identity <私鑰檔>   # 從雲端下載後驗證
```

沒設定 `RCLONE_REMOTE` 又沒加 `--local-only` 時，備份會失敗（FAILED、exit 1），不會默默只做本機。
結束碼：`0` 成功；`1` 備份失敗（含雲端上傳失敗、設定錯誤），已盡力寫入 FAILED 紀錄；`2` 無法保證有紀錄
（來源資料庫不存在、或 backup_runs 寫不進去）。
保留規則（本機與雲端相同）：每日備份 30 天，加上每月第一份 12 個月；雲端只會刪檔名符合備份格式的檔案。
首頁「系統狀態」會顯示最後一次備份的狀態；雲端上傳失敗會顯示紅色「本機備份已完成，但雲端上傳失敗」。

## 檢查

```bash
ruff check .
ruff format --check .
pytest
```

每次 push 與 pull request 都會由 GitHub Actions（`.github/workflows/ci.yml`）在 Python 3.12 與 3.13 上執行同樣的三項檢查。

測試不會碰你的資料庫：每個測試都在 pytest 的暫存目錄建立獨立的 SQLite，跑 migration 後載入測試用科目表
（`tests/fixtures/chart_of_accounts_test.yaml`）。migration 的 upgrade／downgrade 來回也在這裡驗證
（`tests/integration/test_migrations.py`）。

```bash
pytest tests/unit/test_architecture.py     # 只跑分層守門
pytest -k "t05 or t06 or t07 or t34"       # 只跑規格的退款案例
alembic current                            # 確認開發資料庫在最新版本（head）
```

## P2 接手說明（帳務 service 介面）

router 只呼叫 `app/services/transactions.py`：

| 函式 | 用途 |
|---|---|
| `record_expense`／`record_income`／`record_transfer`／`record_card_payment` | 日常交易（轉入信用卡會自動改成信用卡繳款） |
| `record_opening`／`record_adjustment`／`record_payroll`／`record_refund` | 期初、帳務調整、薪資、退款 |
| `update_transaction(db, txn_id, form)` | 修改（表單類型決定交易類型，不能改類型） |
| `delete_transaction`／`restore_transaction` | 軟刪除、還原 |

新增與修改接受 `confirmed`（使用者已確認的規則代碼，例如 `{"V10"}`）與 `user_id`，回傳 `PostResult(transaction_id, notices)`；
刪除只接受 `user_id`、沒有回傳值；還原接受 `user_id`，回傳 `PostResult`（還原不需要確認）。

例外與 HTTP 狀態的對應：

| 例外 | HTTP | 說明 |
|---|---|---|
| `ValidationFailed` | 422 | `result.errors` 是 `Issue(code, message, line, field)`，可以標在對應欄位 |
| `ConfirmationRequired` | 409 | `pending` 是待確認項目；使用者確認後帶 `confirmed` 重送 |
| `NotFound` | 404 | |
| `RuleViolation` | 422 | 主資料規則（M01–M08 等） |
| `InvalidSetting`／`MissingSystemAccount`／`SessionNotClean` | 500 | 系統或程式異常，不是使用者輸入錯誤 |

P2 注意事項：

- router 必須傳入登入使用者的 `user_id`（P1 稽核紀錄的 `user_id` 是 None）。
- 完整的 V08（發票號碼重複）在 P2 實作；目前新增或修改成重複號碼時由資料庫唯一索引擋下（`IntegrityError`，整批 rollback）。
- 草稿交易（DRAFT）與確認入帳在 V1.5；P1 只會產生 POSTED 交易。
- 已知限制：V07、D12 是先檢查再寫入，極端併發下理論上有 race；單一使用者 + SQLite 不額外處理（見 ADR-005）。
