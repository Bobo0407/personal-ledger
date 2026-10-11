# 個人記帳系統（ledger）

以複式記帳為核心的個人財務 Web 系統。FastAPI + SQLite + SQLAlchemy/Alembic + Jinja2/HTMX。

> 目前進度：P0 地基。完整 README 於 P3 補齊。

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

## 檢查

```bash
ruff check .
ruff format --check .
pytest
```

每次 push 與 pull request 都會由 GitHub Actions（`.github/workflows/ci.yml`）在 Python 3.12 與 3.13 上執行同樣的三項檢查。
