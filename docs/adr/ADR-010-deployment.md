# ADR-010：單台 Linux VM、systemd＋Caddy、手動部署

- 狀態：已採用（P3-6，repo 端）；真 VM 驗收未完成前不算 CLOSED
- **修訂（P3-6，使用者的 scope decision）：production 不做備份。** 決定 7、11 與相關後果已作廢，見下方〈修訂〉。
- 相關：規格 4.2、7.2；ADR-008／ADR-009；`docs/deploy.md`；`docs/decisions/p3.md` P3-6

## 背景（Context）

系統是單人、手機優先的個人記帳，要長期實際使用。規格 4.2 指定：免費雲端 Linux VM、資料在
`/srv/ledger/data` 的 persistent disk、Caddy 自動 HTTPS、Uvicorn 由 systemd 管理、備份由 systemd timer 觸發。
P3-5A／5B 已完成 `scripts/backup.py`（本機加密＋ rclone 上傳）。（備份部分見文末〈修訂〉，production 已決定不啟用。）需要決定 production 怎麼擺、怎麼跑、怎麼更新，
並且不把這個單人系統做成企業級部署。

## 決定（Decision）

1. **單台 Ubuntu 24.04 VM，不用 Docker／Kubernetes。** 系統 Python 3.12 符合 `requires-python`；用 venv。
   規格裡的 Dockerfile／docker-compose 標明「Demo 用」，不是 production。
2. **目錄**：程式 `/srv/ledger/app`、venv、設定 `/srv/ledger/config`（`ledger.env`、`rclone.conf`）、
   資料 `/srv/ledger/data`（含 `backups/`、`pre-deploy/`）、日誌 `/srv/ledger/logs`。secret 與 repo 分開放。
3. **網站與備份用同一個非 root 的 `ledger` 使用者。** 備份以唯讀 URI 讀 SQLite，同一使用者最單純；
   rclone 會把更新後的 OAuth token 寫回 `rclone.conf`，所以它必須放在 `ledger` 可寫入的位置。
   代價是網站程序也讀得到 `rclone.conf`；單人單機可接受，拆成兩個使用者會讓 DB 讀取與 token 寫入的權限變複雜。
4. **權限靠 umask，不用 ACL**：兩個 service 都設 `UMask=0077`，互動操作用 `umask 077`；
   `config/`、`data/`、`logs/` 為 0700，`ledger.db`、`ledger.env`、`rclone.conf`、備份檔為 0600。
5. **單一 worker 的 Uvicorn，只 bind `127.0.0.1:8000`，不用 gunicorn。** 對外由 Caddy 反向代理並自動 HTTPS。
   SQLite 加單人使用，多 worker 沒有好處，只增加鎖定風險。供應商層只開 22／80／443。
6. **migration 只在部署流程中手動執行**（app 停止後），不放 `ExecStartPre`。放進 `ExecStartPre` 的話，migration
   失敗會變成 service 重啟迴圈，而且程式 rollback 後 DB 已前進卻沒人察覺。有 migration 的更新：先確認最近一次完整
   遠端備份 SUCCESS → 停止 app → 確認已停止後才複製 SQLite 檔案做 pre-deploy 快照 → migration → 確認 `current` 為 head →
   啟動。**不自動 downgrade**；rollback 優先 fix-forward，必要時人工還原快照。
7. （**已作廢，見〈修訂〉**）~~備份由 `ledger-backup.timer` 觸發~~：每日 03:00 `Asia/Taipei`（時區寫在 `OnCalendar`，不依賴 VM 時區），
   `Persistent=true`（VM 當時沒開機會補跑）。直接呼叫 `scripts/backup.py`，不重寫備份邏輯；
   **不得使用本機專用旗標**，忘了設定雲端時要得到 FAILED 與首頁紅色警示。exit 1／2 讓 unit 進入 failed。
8. **不部署 03:30 帳平檢查排程。** 規格 7.2 原本寫由 APScheduler 執行，但 P3-1 已決定並完成「每次開啟首頁即時檢查」，
   不加入 APScheduler。P3-6 沿用該決策，不重做，也不為了部署新增 scheduler framework。
9. **不新增 `/health`**：未登入的 `GET /login` 回 200 加上 `alembic current` 與人工登入就足夠。
10. **手動部署，不做 `deploy.sh` 與自動 CD。** 單人、約六個步驟，每一步都該親眼看輸出；GitHub 不持有 production 憑證。
    設定檔放 `deploy/`，操作步驟寫在 `docs/deploy.md`。
11. （**已作廢，見〈修訂〉**）~~Google OAuth 只用自建 Desktop client（scope `drive.file`），共用 client_id 不得進 production。`rclone.conf`
    在有瀏覽器的機器用 `rclone config` 產生後安全複製到 VM（流程與版本 v1.75.1 見 runbook）。Console 的發布狀態與 token
    行為必須實際確認，不寫成保證。~~
12. **用靜態守門測試固定這些約束**（`tests/unit/test_deploy_files.py`）：只 bind localhost、單 worker、`UMask=0077`、
    保留的備份 unit 不含本機專用旗標、timer 時間、範本不含真 secret、Caddyfile 只有 placeholder 網域、
    runbook 不安裝或啟用備份 timer、VM 驗收清單不含備份／Google／rclone 步驟。

## 考慮過的替代方案

| 方案 | 優點 | 缺點 |
|---|---|---|
| Docker／compose 當 production | 環境可重現 | 多一層網路與 volume 權限；SQLite、systemd timer、rclone token 寫回都變複雜；規格明確標 Demo 用 |
| gunicorn＋多 worker | 吞吐量 | 單人用不到；SQLite 寫入鎖競爭 |
| migration 放 `ExecStartPre` | 全自動 | 失敗成重啟迴圈；程式與 DB 版本脫鉤難察覺 |
| web／備份拆兩個使用者 | 權限隔離 | 備份要讀 DB、rclone 要寫 token，權限互相牽制；單機收益低 |
| `deploy.sh`／GitHub Actions SSH deploy | 省手動步驟 | 失敗處理藏起來；GitHub 要持有 production 憑證；為單人系統過度設計 |
| APScheduler 做備份或 03:30 檢查 | 純 Python | 備份必須在網站停止時也能跑；03:30 檢查已被首頁即時檢查取代 |
| nginx 取代 Caddy | 熟悉 | 憑證要另外處理；規格指定 Caddy |

## 後果（Consequences）

- 正面：架構小而清楚；secret 不在 repo；web 不以 root 執行；更新與 rollback 都是人工可讀的短流程。
- 負面：每次部署要人工做幾步（單人可接受）；更新期間網站短暫停止；網站程序能讀 `rclone.conf`；
  依賴系統套件版本（Ubuntu 24.04、固定 rclone v1.75.1、Caddy 官方 repo）。
- 這份決策的 repo 端完成不等於 P3-6 完成：必須在真 Linux VM 依 `docs/deploy.md` 驗收（systemd、Caddy HTTPS、DNS、
  8000 不對外、migration、資料持久、重開機、權限、日誌）。

## 修訂：production 不做備份（P3-6 scope decision）

使用者於 P3-6 決定：**production 備份不列入目前版本需求；既有 backup implementation 保留，但 production 不啟用。
資料遺失風險由使用者接受。未來若恢復需求，再重新啟用。**

- 取消：production 的 Google Drive 備份、自建 OAuth client、`rclone.conf`、`ledger-backup.timer`、備份驗收與延後 token 檢查、
  launch gate 裡的「自動備份成功／還原測試」。不改用其他雲端（Backblaze／Dropbox／OneDrive／SFTP）。
  這些項目**不是已完成**，而是**取消的需求**；production 從未做過任何備份驗收。
- 保留不動：`app/services/backup*.py`、`scripts/backup.py`、`scripts/restore_check.py`、`backup_runs`（migration 0005）、
  ADR-008／ADR-009、`deploy/ledger-backup.service`／`.timer`（檔案保留、不安裝）。不刪除、不重構。
- App 本體不需要修改：網站的 `Settings` 不含任何備份欄位（`extra="ignore"`），prod 在沒有任何備份設定下可正常啟動
  （`/login` 回 200）。首頁「從未備份」本來就是黃色提示（`warn`），不是紅色，也不阻擋使用。
- `ledger.env.example` 的備份區塊改為註解。`BACKUP_AGE_RECIPIENT`、`RCLONE_REMOTE`、`RCLONE_CONFIG` 在 production 留空或不設都可以。
- 風險：VM 壞掉、誤刪或 migration 出錯時，沒有任何異地副本可還原。migration 前的本機 pre-deploy 快照
  只能防止單次升級出錯，不是備份。這個風險已由使用者明確接受。
