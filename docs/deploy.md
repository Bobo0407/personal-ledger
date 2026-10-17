# 部署與維運手冊（P3-6）

單人、單台 Linux VM，手動部署。規格 4.2：Uvicorn（systemd 管理）＋ Caddy 自動 HTTPS。
架構取捨見 [ADR-010](adr/ADR-010-deployment.md)。

**狀態：repo 端已完成，真 Linux VM 驗收尚未完成。** 本文件的指令在 VM 實測前都視為「待實測」：
照做時若與實際不符，以實際為準，並把差異記在最後的〈驗收紀錄〉，再回頭修這份文件。
文件只保留實際跑通的做法，沒有實測過的行為不寫成保證。

## 0. Scope decision 與先看這幾條

**Scope decision（使用者於 P3-6 決定）：** production 備份不列入目前版本需求。既有的備份實作
（`scripts/backup.py`、`restore_check.py`、`ledger-backup.service`／`.timer`、ADR-008／ADR-009）保留在 repo，
但 **production 不安裝、不啟用**。資料遺失的風險由使用者接受。未來若恢復需求，再重新啟用（見附錄 A）。

- Google Drive／rclone／OAuth 的 production 設定已取消，從未在 production 驗收，也不是 P3-6 或 launch gate 的條件。
- **不要啟用 `ledger-backup.timer`**，也不要把兩個 backup unit 安裝到 `/etc/systemd/system/`。
- 網站不讀任何備份設定：`ledger.env` 不需要 `BACKUP_*`／`RCLONE_*`，也不需要 `rclone.conf`。
- 首頁「系統狀態」會一直顯示黃色的「尚未有備份紀錄」。這是功能未啟用的提示，不是故障，不影響使用。
- **所有 secret 不進 Git**：`ledger.env`、`ledger.db`。
- **只在 app 已停止後才複製 SQLite 檔案**（見 §6）。

## 1. 架構、目錄與權限

```
/srv/ledger/
├── app/        git checkout；owner = 管理員帳號，ledger 只讀
├── venv/       管理員建立，ledger 只讀
├── config/     ledger.env、freeze-*.txt
├── data/       ledger.db、pre-deploy/（遷移前快照，用完刪）
└── logs/       app.log（每天輪替、保留 14 天）
/etc/systemd/system/   ledger.service
/etc/caddy/Caddyfile
```

| 路徑 | owner:group | 權限 |
|---|---|---|
| `/srv/ledger/app`、`/srv/ledger/venv` | 管理員 | 目錄 755、檔案 644（ledger 只讀） |
| `/srv/ledger/config`、`data`、`data/pre-deploy`、`logs` | ledger:ledger | **0700** |
| `ledger.db`、`ledger.env`、`app.log` | ledger:ledger | **0600** |

- 網站與 migration 都以非 root 的 `ledger` 使用者執行。
- 權限靠 `umask 077`（互動操作）與 `UMask=0077`（service）達成，不使用 ACL。
- 沒有任何東西以 root 執行 web app。
- 目前沒有附件功能，不建 `attachments/`。

## 2. 前置條件

- Ubuntu 24.04 LTS VM（系統 Python 3.12）。記錄實際的 CPU 架構、供應商與 OS 版本。
- 網域，DNS A record 已指向 VM 的公網 IP。
- 供應商層（security group／NSG）只開 22、80、443；**不要開 8000**。SSH 只允許金鑰登入。不裝 fail2ban。
  主機層 `ufw` 是否要開視映像預設規則而定（某些映像的 iptables 預設規則會與 ufw 衝突），先看清楚再決定。

## 3. 一次性準備

以下用管理員帳號（可 sudo）操作。

### 3.1 套件

```bash
sudo apt update
sudo apt install -y python3-venv git
python3 --version          # 預期 3.12.x
```

不安裝：sqlite3 CLI、gunicorn、nginx、Docker、fail2ban。不需要 age、rclone。

### 3.2 Caddy（官方 apt repo）

照 Caddy 官方文件的 Debian／Ubuntu 安裝步驟新增官方 repo 並安裝（指令以官方頁面當下內容為準，
這裡不重抄以免過期）。安裝後 Caddy 會有自己的 systemd unit（`caddy.service`）與 `/etc/caddy/Caddyfile`。

### 3.3 使用者與目錄

```bash
sudo useradd --system --home-dir /srv/ledger --shell /usr/sbin/nologin ledger
sudo install -d -o ledger -g ledger -m 0700 \
  /srv/ledger/config /srv/ledger/data /srv/ledger/data/pre-deploy /srv/ledger/logs
sudo install -d -o "$USER" -g "$USER" -m 0755 /srv/ledger/app /srv/ledger/venv
```

`/srv/ledger` 本身若不存在由 `install -d` 一併建立；若 `data/` 要放在獨立的 persistent disk，
先掛載到 `/srv/ledger/data` 再執行上面的 `install`。

### 3.4 程式碼與 venv

```bash
git clone <repo URL> /srv/ledger/app          # 目錄已存在且為空
cd /srv/ledger/app
git checkout <要部署的 commit>                 # 只部署 CI 為綠的 commit
python3 -m venv /srv/ledger/venv
/srv/ledger/venv/bin/pip install -e .
/srv/ledger/venv/bin/pip freeze | sudo tee /srv/ledger/config/freeze-$(date +%Y%m%d).txt >/dev/null
```

**不要在 `/srv/ledger/app` 放 `.env`**：程式會把 cwd 的 `.env` 當後備設定，正式設定只放在 `ledger.env`。

### 3.5 `ledger.env`

```bash
sudo install -o ledger -g ledger -m 0600 deploy/ledger.env.example /srv/ledger/config/ledger.env
sudoedit /srv/ledger/config/ledger.env       # 只需要填 SECRET_KEY
```

`SECRET_KEY` 在 VM 上現場產生（範本內有指令），`APP_ENV=prod` 時程式會強制長度與 `SESSION_COOKIE_SECURE=true`，
不符就拒絕啟動。範本裡的備份設定是註解掉的，**維持原樣即可**，不要填。

### 3.6 `lrun`：以 ledger 身分、載入 `ledger.env`、umask 077 執行指令

首次初始化與 migration 都用它（貼進你的 shell 設定或在當次 session 定義）：

```bash
lrun() {
  sudo -u ledger bash -c 'umask 077 && cd /srv/ledger/app && set -a && . /srv/ledger/config/ledger.env \
    && set +a && export PATH=/srv/ledger/venv/bin:$PATH && exec "$@"' _ "$@"
}
```

這樣建立的檔案（例如第一次 migration 建出的 `ledger.db`）是 0600。（`lrun` 待 VM 實測。）

## 4. 資料庫初始化（第一次部署）

不手動建立 DB 檔案：以 `ledger` 身分執行 migration，SQLite 在這一步建立 `ledger.db`（權限 0600，因為 `lrun` 有 umask 077）。

```bash
lrun alembic upgrade head
lrun alembic current                    # 必須印出 0005 (head)
lrun python scripts/seed_master_data.py
lrun python scripts/create_user.py      # 互動輸入密碼；終端機無法輸入時見 README 的一次性環境變數做法
```

**這一步只載入主資料（科目表、付款方式）與建立唯一帳號。不輸入任何 opening balance**——那是 P3-7。
migration 失敗就停在這裡，不要往下安裝與啟動 app。

## 5. 安裝服務並啟動

只安裝 `ledger.service`。**不要安裝 `ledger-backup.service`／`ledger-backup.timer`。**

```bash
cd /srv/ledger/app
sudo install -m 0644 deploy/ledger.service -t /etc/systemd/system/    # unit 檔不可有執行權限，否則 systemd 會警告
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/ledger.service
sudo systemctl enable --now ledger.service
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/login      # 預期 200
sudo ss -ltnp | grep ':8000'                                                # 只應看到 127.0.0.1:8000
```

Caddy（確認 DNS 已指向這台 VM 之後）：

```bash
sudo sed 's/YOUR_DOMAIN_HERE/<你的網域>/' deploy/Caddyfile | sudo tee /etc/caddy/Caddyfile >/dev/null
grep -c YOUR_DOMAIN_HERE /etc/caddy/Caddyfile            # 必須是 0
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy       # 或 restart
curl -sSI http://<你的網域>/login | head -3              # 預期導向 https
curl -fsS -o /dev/null -w '%{http_code}\n' https://<你的網域>/login        # 預期 200
```

health check 就是 `GET /login`（未登入回 200）加上 `alembic current` 與人工登入首頁；沒有另外的 `/health`。

## 6. 更新部署

**沒有 migration 的更新：**

```bash
cd /srv/ledger/app && git fetch && git checkout <新 commit>
/srv/ledger/venv/bin/pip install -e .
sudo systemctl restart ledger
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/login
```

**有 migration 的更新**（先用 `git diff --stat HEAD..<新 commit> -- migrations` 確認）。順序不可調換。
production 沒有備份，所以 migration 前的本機快照是唯一的後路：

1. `sudo systemctl stop ledger`，確認 app **已完全停止**：
   `systemctl is-active ledger`（要是 `inactive`）、`sudo ss -ltn 'sport = :8000'`（要沒有輸出）。
2. **只在 app 已停止後**，做本機 pre-deploy SQLite 快照（這時沒有程序在寫 DB，單純複製檔案是安全的）：
   ```bash
   ls /srv/ledger/data/ledger.db-* 2>/dev/null     # 必須沒有輸出；若有 -journal 檔，先停下來查原因
   lrun install -m 0600 /srv/ledger/data/ledger.db \
     /srv/ledger/data/pre-deploy/ledger-before-$(date +%Y%m%d-%H%M%S).db
   cmp /srv/ledger/data/ledger.db /srv/ledger/data/pre-deploy/ledger-before-*.db   # 最新那份應無差異
   ```
   快照放 `/srv/ledger/data/pre-deploy/`，權限 0600。它是明文 DB，不是備份機制，確認新版穩定後手動刪除。
3. 取得新程式碼：`git fetch && git checkout <新 commit>`，再 `pip install -e .`。
4. `lrun alembic upgrade head`。**失敗就停止，不要啟動 app**（見 §7）。不自動 downgrade。
5. `lrun alembic current` 必須印出 `<rev> (head)`。
6. `sudo systemctl start ledger`，驗證 `/login` 回 200、登入後首頁正常。

## 7. Rollback（人工、最小）

- **程式碼問題（沒有套用 migration）**：`git checkout <上一個 commit>` → `pip install -e .` → `sudo systemctl restart ledger`。
- **migration 失敗**：app 保持停止。先看錯誤；DB 若被部分修改，用 pre-deploy 快照還原（下一項）。
- **migration 已套用、新版有問題**：**優先 fix-forward**（修正後再部署）。必要時人工還原：
  1. `sudo systemctl stop ledger`
  2. 把壞掉的 `ledger.db` 改名保留，再把 pre-deploy 快照複製回 `ledger.db`（owner ledger、權限 0600）
  3. `git checkout <上一個 commit>`、`pip install -e .`
  4. `lrun alembic current` 確認版本對應舊 commit
  5. `sudo systemctl start ledger`
- **不做**自動 alembic downgrade，不做 rollback framework。快照確認不再需要後手動刪除。

## 8. 日誌

| 來源 | 去向 | 看法 |
|---|---|---|
| app log | `/srv/ledger/logs/app.log`（每天輪替、保留 14 天）；同時輸出到 stderr 進 journal | `tail`、`journalctl -u ledger` |
| Caddy | journal（預設不開 access log） | `journalctl -u caddy` |

- 不導入 ELK／Loki／Sentry。log 不應含密碼、token。
- 確認 journal 是持久的（`/var/log/journal` 存在、`journalctl --disk-usage`），否則重開機後診斷訊息會消失（待 VM 確認）。

## 9. VM 驗收順序（P3-6 acceptance）

全部通過前，P3-6 的狀態只能是「implementation complete / acceptance pending」，**不得 CLOSED**。
每一項都把實際結果記到〈驗收紀錄〉。

**A. 主機與套件**
- [ ] OS 為 Ubuntu 24.04、`python3 --version` 為 3.12.x；記錄 CPU 架構
- [ ] `caddy version` 已記錄

**B. 使用者、目錄、權限**
- [ ] 沒有任何 web 程序以 root 執行（`ps -eo user,cmd | grep '[u]vicorn'` 的使用者是 `ledger`）
- [ ] 目錄 owner 與權限符合 §1 表格
- [ ] 檔案權限不得有 group／other 的讀寫權限（以下指令不得輸出任何內容）：
  ```bash
  sudo find /srv/ledger/data /srv/ledger/config /srv/ledger/logs -perm /077 -ls
  ```
- [ ] 用 `stat` 確認，結果必須是 `600 ledger:ledger`：
  ```bash
  sudo stat -c '%a %U:%G %n' /srv/ledger/data/ledger.db /srv/ledger/config/ledger.env
  ```

**C. 設定與 secrets**
- [ ] repo 乾淨：`git -C /srv/ledger/app status --short` 無輸出
- [ ] `/srv/ledger/app` 內沒有 `.env`
- [ ] `ledger.env` 的 `APP_ENV=prod`、`SESSION_COOKIE_SECURE=true`，`SECRET_KEY` 非空

**D. 資料庫**
- [ ] `lrun alembic current` 印出 `0005 (head)`
- [ ] `ledger.db` 位於 `/srv/ledger/data/`（`DATABASE_URL` 為 `sqlite:////srv/ledger/data/ledger.db`）
- [ ] 已 seed 主資料並建立帳號；**沒有輸入任何 opening balance**

**E. 網站服務**
- [ ] `systemctl is-enabled ledger` 為 enabled、`is-active` 為 active
- [ ] `curl http://127.0.0.1:8000/login` 回 200
- [ ] `sudo ss -ltnp | grep 8000` 只顯示 `127.0.0.1:8000`；從外部 `curl --connect-timeout 5 http://<公網IP>:8000/` 失敗
- [ ] `systemctl is-enabled ledger-backup.timer` 不是 enabled（預期 `not-found` 或 `disabled`）

**F. DNS 與 HTTPS**
- [ ] `dig +short <網域>` 為 VM 公網 IP
- [ ] `/etc/caddy/Caddyfile` 沒有殘留 `YOUR_DOMAIN_HERE`，`caddy validate` 通過
- [ ] HTTP 導向 HTTPS；`https://<網域>/login` 回 200，憑證有效
- [ ] 供應商層只開 22／80／443

**G. 持久性與重開機**
- [ ] `sudo systemctl restart ledger` 後可登入，資料仍在
- [ ] `sudo reboot` 後 `ledger`、`caddy` 都自動恢復，`/login` 回 200，app 程序仍不是 root

**H. 日誌**
- [ ] `journalctl -u ledger`、`-u caddy` 可查；`/var/log/journal` 存在（持久）
- [ ] `app.log` 有寫入；log 中沒有 token

## 10. 驗收紀錄（驗收時填寫）

| 項目 | 實際結果 | 日期 |
|---|---|---|
| VM 供應商／架構／OS | | |
| Python／caddy 版本 | | |
| `lrun`、`systemd-analyze verify`、硬化選項是否如預期 | | |
| journal 是否持久 | | |

## 11. P3-6 不做／P3-7 保留

P3-6 **不做**：production 備份（含 Google Drive／rclone／OAuth，已取消）、opening balances、正式帳戶對帳、
手機 375px 最終驗收、launch gate、V1.5、Docker production、自動 CI/CD、`/health`、`deploy.sh`、
03:30 integrity job（沿用 P3-1 首頁即時檢查的決策）、多 worker。

P3-7 **保留**：opening balances、正式帳戶對帳、375px 手機最終驗收、launch gate、信用卡首次初始化策略。
備份相關項目（正式 restore drill、Docker restore demo、「至少一次自動備份成功」）已因 scope decision 移出 launch gate。

## 附錄 A：備份功能（保留但 production 不啟用）

不是驗收項目，也不是待辦。僅供未來恢復需求時參考：

- 實作：`scripts/backup.py`（SQLite backup API → gzip → age 加密 → 可選 rclone 上傳）、`scripts/restore_check.py`、
  `deploy/ledger-backup.service`／`.timer`（保留在 repo、目前不安裝）。設計見 ADR-008、ADR-009 與 `docs/decisions/p3.md`。
- 若要重新啟用，需要重新規劃：備份目的地、金鑰管理、`ledger.env` 的備份設定、安裝並啟用 timer，以及一次真實的還原測試。
  這些在 production 都**從未驗收過**。
- Google Drive 這個目的地的 production 設定（自建 OAuth client、rclone.conf）已取消；P3-5B 當時只在 Windows
  開發機驗收過，沒有任何 production 驗收結果。
