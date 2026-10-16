# 部署與維運手冊（P3-6）

單人、單台 Linux VM，手動部署。規格 4.2：Uvicorn（systemd 管理）＋ Caddy 自動 HTTPS ＋ 每日 systemd timer 備份。
架構取捨見 [ADR-010](adr/ADR-010-deployment.md)；備份設計見 ADR-008／ADR-009 與 `docs/decisions/p3.md`。

**狀態：repo 端已完成，真 Linux VM 驗收尚未進行。** 本文件的指令在 VM 實測前都視為「待實測」：
照做時若與實際不符，以實際為準，並把差異記在最後的〈驗收紀錄〉，再回頭修這份文件。
文件只保留實際跑通的做法；沒有實測過的行為（尤其是 Google／rclone）不寫成保證。

## 0. 先看這幾條

- **不要在正式環境使用本機專用旗標**：`ledger-backup.service` 不得加上它。忘了設定雲端時，備份要失敗並讓首頁變紅，
  而不是默默只做本機備份。
- **age 私鑰不放這台 VM**：只放密碼管理員（加一份手寫備份）。VM 只有公鑰。
- **shared（共用）rclone client_id 不得進 production**：只用自己建立的 Google OAuth client。
- **所有 secret 不進 Git**：`ledger.env`、`rclone.conf`、OAuth token／client_secret、age 私鑰。
- **只在 app 已停止後才複製 SQLite 檔案**（見 §7）。

## 1. 架構、目錄與權限

```
/srv/ledger/
├── app/        git checkout；owner = 管理員帳號，ledger 只讀
├── venv/       管理員建立，ledger 只讀
├── config/     ledger.env、rclone.conf、freeze-*.txt
├── data/       ledger.db、backups/（本機密文）、pre-deploy/（遷移前快照，用完刪）
└── logs/       app.log（每天輪替、保留 14 天）
/etc/systemd/system/   ledger.service  ledger-backup.service  ledger-backup.timer
/etc/caddy/Caddyfile
```

| 路徑 | owner:group | 權限 |
|---|---|---|
| `/srv/ledger/app`、`/srv/ledger/venv` | 管理員 | 目錄 755、檔案 644（ledger 只讀） |
| `/srv/ledger/config`、`data`、`data/backups`、`data/pre-deploy`、`logs` | ledger:ledger | **0700** |
| `ledger.db`、`ledger.env`、`rclone.conf`、備份 `.age`、`app.log` | ledger:ledger | **0600** |

- 網站、備份、migration 都以同一個非 root 的 `ledger` 使用者執行。備份要以唯讀方式讀 SQLite，同一使用者最單純；
  代價是網站程序也能讀 `rclone.conf`。單人單機可接受。
- 權限靠 `umask 077`（互動操作）與 `UMask=0077`（兩個 service）達成，不使用 ACL。
- 沒有任何東西以 root 執行 web app 或備份。
- 目前沒有附件功能，不建 `attachments/`。

## 2. 前置條件

- Ubuntu 24.04 LTS VM（系統 Python 3.12）。記錄實際的 CPU 架構、供應商與 OS 版本。
- 網域，DNS A record 已指向 VM 的公網 IP。
- 供應商層（security group／NSG）只開 22、80、443；**不要開 8000**。SSH 只允許金鑰登入。不裝 fail2ban。
  主機層 `ufw` 是否要開視映像預設規則而定（某些映像的 iptables 預設規則會與 ufw 衝突），先看清楚再決定。
- age 金鑰組：私鑰存密碼管理員，**VM 只拿公鑰**（`age1…`）。
- 自建 Google OAuth client（Desktop app）與 `rclone.conf`（見 §4）。

## 3. 一次性準備

以下用管理員帳號（可 sudo）操作。

### 3.1 套件

```bash
sudo apt update
sudo apt install -y python3-venv git age
python3 --version          # 預期 3.12.x
age --version
```

不安裝：sqlite3 CLI、gunicorn、nginx、Docker、fail2ban。

### 3.2 rclone：與 Windows 驗收相同的 v1.75.1

Windows 驗收用的是 rclone **v1.75.1**，正式環境也裝這個版本（官方版本化下載頁，amd64 或 arm64 依 VM 架構）：

```bash
cd "$(mktemp -d)"
ARCH=$(dpkg --print-architecture)       # amd64 或 arm64
curl -fsSLO "https://downloads.rclone.org/v1.75.1/rclone-v1.75.1-linux-${ARCH}.deb"
curl -fsSLO "https://downloads.rclone.org/v1.75.1/SHA256SUMS"
```

`SHA256SUMS` 是 PGP 簽署過的校驗清單。至少要核對 `.deb` 的 SHA256 與清單一致
（`sha256sum rclone-v1.75.1-linux-${ARCH}.deb` 與 `SHA256SUMS` 內該檔那一行比對），再安裝：

```bash
sudo apt install -y "./rclone-v1.75.1-linux-${ARCH}.deb"
rclone version               # 預期 rclone v1.75.1；把實際輸出記到〈驗收紀錄〉
```

### 3.3 Caddy（官方 apt repo）

照 Caddy 官方文件的 Debian／Ubuntu 安裝步驟新增官方 repo 並安裝（指令以官方頁面當下內容為準，
這裡不重抄以免過期）。安裝後 Caddy 會有自己的 systemd unit（`caddy.service`）與 `/etc/caddy/Caddyfile`。

### 3.4 使用者與目錄

```bash
sudo useradd --system --home-dir /srv/ledger --shell /usr/sbin/nologin ledger
sudo install -d -o ledger -g ledger -m 0700 \
  /srv/ledger/config /srv/ledger/data /srv/ledger/data/backups /srv/ledger/data/pre-deploy /srv/ledger/logs
sudo install -d -o "$USER" -g "$USER" -m 0755 /srv/ledger/app /srv/ledger/venv
```

`/srv/ledger` 本身若不存在由 `install -d` 一併建立；若 `data/` 要放在獨立的 persistent disk，
先掛載到 `/srv/ledger/data` 再執行上面的 `install`。

### 3.5 程式碼與 venv

```bash
git clone <repo URL> /srv/ledger/app          # 目錄已存在且為空
cd /srv/ledger/app
git checkout <要部署的 commit>                 # 只部署 CI 為綠的 commit
python3 -m venv /srv/ledger/venv
/srv/ledger/venv/bin/pip install -e .
/srv/ledger/venv/bin/pip freeze | sudo tee /srv/ledger/config/freeze-$(date +%Y%m%d).txt >/dev/null
```

**不要在 `/srv/ledger/app` 放 `.env`**：程式會把 cwd 的 `.env` 當後備設定，正式設定只放在 `ledger.env`。

### 3.6 `ledger.env`

```bash
sudo install -o ledger -g ledger -m 0600 deploy/ledger.env.example /srv/ledger/config/ledger.env
sudoedit /srv/ledger/config/ledger.env       # 填 SECRET_KEY、BACKUP_AGE_RECIPIENT、RCLONE_REMOTE
```

`SECRET_KEY` 在 VM 上現場產生（範本內有指令），`APP_ENV=prod` 時程式會強制長度與 `SESSION_COOKIE_SECURE=true`，
不符就拒絕啟動。

### 3.7 `lrun`：以 ledger 身分、載入 `ledger.env`、umask 077 執行指令

首次初始化、migration、手動 rclone 檢查都用它（貼進你的 shell 設定或在當次 session 定義）：

```bash
lrun() {
  sudo -u ledger bash -c 'umask 077 && cd /srv/ledger/app && set -a && . /srv/ledger/config/ledger.env \
    && set +a && export PATH=/srv/ledger/venv/bin:$PATH && exec "$@"' _ "$@"
}
```

這樣建立的檔案（例如第一次 migration 建出的 `ledger.db`）是 0600。（`lrun` 待 VM 實測。）

## 4. Google OAuth client 與 `rclone.conf`

**目標：** 一個只用自己 OAuth client 的 `rclone.conf`（scope `drive.file`），複製到 VM。
Windows 驗收用過的共用 client_id 設定**不要**複製上來。

### 4.1 在 Google Cloud Console（一次性，需要你實際操作與確認）

1. 建立專案、啟用 Google Drive API。
2. 設定 OAuth consent screen（External），加入 scope `https://www.googleapis.com/auth/drive.file`。
3. 建立 OAuth client，類型 **Desktop app**，取得 `client_id` 與 `client_secret`，存進密碼管理員。
4. **發布狀態**：Google 對「Testing」狀態的 External app 有 refresh token 到期的限制，細節與數字以 Console
   當下的說明為準。請在 Console 實際確認發布狀態與是否需要驗證，**不要假設**。把看到的狀態與日期記到〈驗收紀錄〉，
   並在 §10 的延後檢查驗證 token 真的沒有過期。

### 4.2 在有瀏覽器的機器產生 `rclone.conf`（優先使用的方案）

在 Windows（或任何有瀏覽器的機器）上，使用與 VM 相同的 rclone v1.75.1，並且用**獨立的設定檔**，
不要和 Windows 驗收用的共用 client 設定混在一起：

```powershell
rclone version                                   # 確認 v1.75.1
rclone config --config "$env:TEMP\rclone-prod.conf"
```

在互動式選單中：新增 remote（名稱建議 `gdrive`，之後填進 `RCLONE_REMOTE`）→ 儲存類型選 Google Drive →
`client_id`、`client_secret` 貼上**你自己建立的** → scope 選擇描述為「只存取 rclone 建立或開啟的檔案」的那一項
（即 `drive.file`；選項編號依 rclone 版本而異，**依描述選，不要依編號**）→ 其餘維持預設 →
自動設定（auto config）用瀏覽器完成授權。選項文字與順序以實際畫面為準；跑通後把實際步驟補回這份文件。

完成後在複製之前檢查（輸出不要貼到任何地方）：

```powershell
rclone config file --config "$env:TEMP\rclone-prod.conf"       # 確認就是這個檔案
rclone lsd gdrive: --config "$env:TEMP\rclone-prod.conf"       # 應該不報錯
```

並用文字編輯器確認設定檔有非空的 `client_id =`、`client_secret =` 兩行，以及 `scope = drive.file`。
沒有 `client_id` 代表在用共用 client，**不得上 production**。

### 4.3 安全地複製到 VM

```powershell
scp "$env:TEMP\rclone-prod.conf" <管理員>@<VM>:~/rclone-prod.conf
```

```bash
sudo install -o ledger -g ledger -m 0600 ~/rclone-prod.conf /srv/ledger/config/rclone.conf
shred -u ~/rclone-prod.conf 2>/dev/null || rm -f ~/rclone-prod.conf
lrun rclone --config /srv/ledger/config/rclone.conf lsd gdrive:          # 以 ledger 身分驗證可連線
```

Windows 端用完後刪除 `$env:TEMP\rclone-prod.conf`（或只留在密碼管理員，不留在磁碟）。
這個檔案含 token 與 client_secret：不進 Git、不貼到聊天或 issue、log 不印。

## 5. 資料庫初始化（第一次部署）

不手動建立 DB 檔案：以 `ledger` 身分執行 migration，SQLite 在這一步建立 `ledger.db`（權限 0600，因為 `lrun` 有 umask 077）。

```bash
lrun alembic upgrade head
lrun alembic current                    # 必須印出 <rev> (head)
lrun python scripts/seed_master_data.py
lrun python scripts/create_user.py      # 互動輸入密碼；終端機無法輸入時見 README 的一次性環境變數做法
```

**這一步只載入主資料（科目表、付款方式）與建立唯一帳號。不輸入任何 opening balance**——那是 P3-7。
migration 失敗就停在這裡，不要往下安裝與啟動 app。

## 6. 安裝服務並啟動

```bash
cd /srv/ledger/app
sudo install -m 0644 deploy/ledger.service deploy/ledger-backup.service deploy/ledger-backup.timer \
  -t /etc/systemd/system/      # unit 檔不可有執行權限，否則 systemd 會警告
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/ledger.service /etc/systemd/system/ledger-backup.service \
  /etc/systemd/system/ledger-backup.timer
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

備份 timer 與第一次備份：

```bash
sudo systemctl enable --now ledger-backup.timer
systemctl list-timers ledger-backup.timer
sudo systemctl start ledger-backup.service     # oneshot，等它跑完
systemctl status ledger-backup.service --no-pager | head -15
```

預期：exit 0、本機 `data/backups/` 出現新的 `.age`、Drive 資料夾出現同名檔、首頁「系統狀態」顯示綠色一行
「最後備份：今天 HH:MM ✓」。

## 7. 更新部署

**沒有 migration 的更新：**

```bash
cd /srv/ledger/app && git fetch && git checkout <新 commit>
/srv/ledger/venv/bin/pip install -e .
sudo systemctl restart ledger
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/login
```

**有 migration 的更新**（先用 `git diff --stat HEAD..<新 commit> -- migrations` 確認）。順序不可調換：

1. **先確認最近一次完整 encrypted remote backup 是 SUCCESS**：此時 app 還在跑。
   執行 `sudo systemctl start ledger-backup.service` 並確認 exit 0，開首頁確認「系統狀態」的備份行是
   **綠色**（`最後備份：今天 … ✓`），不是黃色或紅色。不是綠色就不要繼續。
2. `sudo systemctl stop ledger`，確認 app **已完全停止**：
   `systemctl is-active ledger`（要是 `inactive`）、`sudo ss -ltn 'sport = :8000'`（要沒有輸出）。
3. **只在 app 已停止後**，做本機 pre-deploy SQLite 快照（這時沒有程序在寫 DB，單純複製檔案是安全的）：
   ```bash
   ls /srv/ledger/data/ledger.db-* 2>/dev/null     # 必須沒有輸出；若有 -journal 檔，先停下來查原因
   lrun install -m 0600 /srv/ledger/data/ledger.db \
     /srv/ledger/data/pre-deploy/ledger-before-$(date +%Y%m%d-%H%M%S).db
   cmp /srv/ledger/data/ledger.db /srv/ledger/data/pre-deploy/ledger-before-*.db   # 最新那份應無差異
   ```
   快照放 `/srv/ledger/data/pre-deploy/`，權限 0600。它是明文 DB，確認新版穩定後手動刪除。
4. 取得新程式碼：`git fetch && git checkout <新 commit>`，再 `pip install -e .`。
5. `lrun alembic upgrade head`。**失敗就停止，不要啟動 app**（見 §8）。不自動 downgrade。
6. `lrun alembic current` 必須印出 `<rev> (head)`。
7. `sudo systemctl start ledger`，驗證 `/login` 回 200、登入後首頁正常。

## 8. Rollback（人工、最小）

- **程式碼問題（沒有套用 migration）**：`git checkout <上一個 commit>` → `pip install -e .` → `sudo systemctl restart ledger`。
- **migration 失敗**：app 保持停止。先看錯誤；DB 若被部分修改，用 pre-deploy 快照還原（下一項）。
- **migration 已套用、新版有問題**：**優先 fix-forward**（修正後再部署）。必要時人工還原：
  1. `sudo systemctl stop ledger`
  2. 把壞掉的 `ledger.db` 改名保留，再把 pre-deploy 快照複製回 `ledger.db`（owner ledger、權限 0600）
  3. `git checkout <上一個 commit>`、`pip install -e .`
  4. `lrun alembic current` 確認版本對應舊 commit
  5. `sudo systemctl start ledger`
- **不做**自動 alembic downgrade，不做 rollback framework。快照確認不再需要後手動刪除。

## 9. 日誌

| 來源 | 去向 | 看法 |
|---|---|---|
| app log | `/srv/ledger/logs/app.log`（每天輪替、保留 14 天）；同時輸出到 stderr 進 journal | `tail`、`journalctl -u ledger` |
| 備份 | `scripts/backup.py` 寫 stderr → journal | `journalctl -u ledger-backup` |
| Caddy | journal（預設不開 access log） | `journalctl -u caddy` |
| 使用者可見狀態 | `backup_runs`／首頁 | 首頁「系統狀態」 |

- 不導入 ELK／Loki／Sentry。log 不應含密碼、token、client_secret、recipient。
- 確認 journal 是持久的（`/var/log/journal` 存在、`journalctl --disk-usage`），否則重開機後診斷訊息會消失（待 VM 確認）。
- 備份失敗時 `ledger-backup.service` 會進入 failed（`systemctl --failed`）；對使用者的警示是首頁紅色或 36 小時黃色。

## 10. VM 驗收順序（P3-6 acceptance）

全部通過前，P3-6 的狀態只能是「implementation complete / acceptance pending」，**不得 CLOSED**。
每一項都把實際結果記到〈驗收紀錄〉。

**A. 主機與套件**
- [ ] OS 為 Ubuntu 24.04、`python3 --version` 為 3.12.x；記錄 CPU 架構
- [ ] `age --version`、`rclone version`（預期 v1.75.1）、`caddy version` 已記錄

**B. 使用者、目錄、權限**
- [ ] 沒有任何 web／備份程序以 root 執行（`ps -eo user,cmd | grep '[u]vicorn'` 的使用者是 `ledger`；備份 unit 的 `User=ledger`）
- [ ] 目錄 owner 與權限符合 §1 表格
- [ ] 檔案權限不得有 group／other 的讀寫權限（以下指令不得輸出任何內容）：
  ```bash
  sudo find /srv/ledger/data /srv/ledger/config /srv/ledger/logs -perm /077 -ls
  ```
- [ ] 用 `stat` 逐一確認，結果必須是 `600 ledger:ledger`（備份檔取最新一份 `.age`）：
  ```bash
  sudo stat -c '%a %U:%G %n' /srv/ledger/data/ledger.db /srv/ledger/config/ledger.env \
    /srv/ledger/config/rclone.conf "$(sudo ls -t /srv/ledger/data/backups/*.age | head -1)"
  ```

**C. 設定與 secrets**
- [ ] repo 乾淨：`git -C /srv/ledger/app status --short` 無輸出；`git -C /srv/ledger/app grep -nE 'GOCSPX-|AGE-SECRET-KEY-|ya29\.'` 無輸出
- [ ] `/srv/ledger/app` 內沒有 `.env`
- [ ] `ledger.env` 的 `APP_ENV=prod`、`SESSION_COOKIE_SECURE=true`，`SECRET_KEY` 非空
- [ ] **VM 上沒有 age 私鑰**：`sudo grep -rl 'AGE-SECRET-KEY-' /srv /home /root /tmp 2>/dev/null` 無輸出

**D. 資料庫**
- [ ] `lrun alembic current` 印出 `<rev> (head)`
- [ ] `ledger.db` 位於 `/srv/ledger/data/`（`DATABASE_URL` 為 `sqlite:////srv/ledger/data/ledger.db`）
- [ ] 已 seed 主資料並建立帳號；**沒有輸入任何 opening balance**

**E. 網站服務**
- [ ] `systemctl is-enabled ledger` 為 enabled、`is-active` 為 active
- [ ] `curl http://127.0.0.1:8000/login` 回 200
- [ ] `sudo ss -ltnp | grep 8000` 只顯示 `127.0.0.1:8000`；從外部 `curl --connect-timeout 5 http://<公網IP>:8000/` 失敗

**F. DNS 與 HTTPS**
- [ ] `dig +short <網域>` 為 VM 公網 IP
- [ ] `/etc/caddy/Caddyfile` 沒有殘留 `YOUR_DOMAIN_HERE`，`caddy validate` 通過
- [ ] HTTP 導向 HTTPS；`https://<網域>/login` 回 200，憑證有效
- [ ] 供應商層只開 22／80／443

**G. 備份**
- [ ] `rclone.conf` 有非空的 `client_id`（自建 OAuth client，不是共用 client）與 `scope = drive.file`
- [ ] `systemd-analyze verify` 對三個 unit 無錯誤；`systemd-analyze calendar "*-*-* 03:00:00 Asia/Taipei"` 解析正確
- [ ] `ledger-backup.timer` 為 enabled，`systemctl list-timers` 顯示下次執行時間
- [ ] 手動 `sudo systemctl start ledger-backup.service` 成功（exit 0）
- [ ] 本機 `data/backups/` 有新 `.age`；Google Drive 資料夾有同名 `.age`，`rclone lsl` 大小與本機一致
- [ ] rclone 在 `ledger` 身分下可更新 `rclone.conf`（token 刷新不因權限失敗）
- [ ] 首頁備份行為綠色「最後備份：今天 … ✓」
- [ ] 失敗演練：暫時把 `RCLONE_REMOTE` 設成不存在的名稱再手動觸發 → unit 為 failed、首頁紅色「本機備份已完成，但雲端上傳失敗」、
  本機 `.age` 保留；改回後再跑一次，首頁恢復綠色

**H. 持久性與重開機**
- [ ] `sudo systemctl restart ledger` 後可登入，資料仍在
- [ ] `sudo reboot` 後 `ledger`、`caddy`、`ledger-backup.timer` 都自動恢復，首頁與備份狀態正常

**I. 日誌**
- [ ] `journalctl -u ledger`、`-u ledger-backup`、`-u caddy` 可查；`/var/log/journal` 存在（持久）
- [ ] `app.log` 有寫入；log 中沒有 token、client_secret、recipient

**J. 延後檢查（P3-7 launch gate 前必須完成）**
- [ ] OAuth 發布後經過足夠天數（至少 8 天）備份仍可上傳，確認 token 沒有因為 Testing 狀態到期；
  結果與 Console 狀態一併記錄

## 11. 驗收紀錄（驗收時填寫）

| 項目 | 實際結果 | 日期 |
|---|---|---|
| VM 供應商／架構／OS | | |
| Python／age／rclone／caddy 版本 | | |
| Console 發布狀態（In production／Testing）與是否需驗證 | | |
| `rclone.conf` 產生步驟與實際選項（補回 §4.2） | | |
| `lrun`、`systemd-analyze verify`、硬化選項是否如預期 | | |
| journal 是否持久 | | |
| 第 8 天後 token 檢查 | | |

## 12. P3-6 不做／P3-7 保留

P3-6 **不做**：opening balances、正式帳戶對帳、正式 restore drill、手機 375px 最終驗收、launch gate、V1.5、
Docker production、自動 CI/CD、`/health`、`deploy.sh`、03:30 integrity job（沿用 P3-1 首頁即時檢查的決策）、多 worker。

P3-7 **保留**：opening balances、正式帳戶對帳、正式 restore drill（含從 Drive 網頁下載 `.age`、用密碼管理員私鑰在另一台機器
還原）、Docker restore demo、375px 手機最終驗收、launch gate（≥1 次自動備份成功＋1 次成功的還原測試）、信用卡首次初始化策略。
