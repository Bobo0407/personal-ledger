# ADR-009：雲端備份用 rclone 命令列工具；SUCCESS 代表「本機＋雲端」都完成

- 狀態：已採用（P3-5B）
- 相關：規格 F29、7.2；ADR-008；`docs/decisions/p3.md` P3-5B

## 背景（Context）

P3-5A 的備份只存在這台機器上；伺服器壞掉或被入侵時，備份會跟著一起消失。規格要求把加密後的備份
放到異地（Google Drive）。需要決定：怎麼上傳、怎麼判定成功、上傳失敗時 backup_runs 與首頁怎麼表達。

## 決定（Decision）

1. **用 rclone CLI**，由 `app/services/backup_remote.py`（薄轉接層）以 `subprocess`（list 參數、不經 shell）
   呼叫；不引入 Google Drive／OAuth 的 Python 套件；沒有新增 Python dependency。
   rclone 已處理 OAuth 授權與 token 更新、續傳、重試與限流；自己寫要多出數百行 Drive 專用程式與一堆
   第三方套件。rclone 另有 `local` backend，CI 可用真的 rclone 測試而不需要任何 Google 帳號。
   用到的指令僅限 `copy`／`lsjson`／`deletefile`／`copyto`（下載），不使用 `sync`／`purge`／`delete`。
2. **只上傳 age 密文**：`copy --immutable --include /<檔名>` 只送出那一個符合備份格式的 `.age` 檔。
   明文與壓縮檔從未寫到磁碟，因此沒有任何路徑能上傳未加密資料。
3. **成功條件由我們自己定義，不依賴 backend 細節**：rclone exit 0，**且** `lsjson` 列出該檔名、
   遠端大小等於本機大小。rclone 在 Drive 上另外做的 hash 驗證視為額外保障，不是本系統的正確性契約。
   真正的端到端驗證是 `restore_check.py --remote`：下載 → age 解密 → gunzip → `integrity_check` →
   `alembic_version` → 主要表可讀。
4. **同名檔案不覆蓋**：每次備份都會產生新的時間戳檔名，正常情況不會撞名；真的撞到且內容不同時，
   `copy --immutable` 讓 rclone 失敗，我們記為上傳失敗。不做預先列表、不猜內容是否相同、不做補傳或 reconcile。
5. **SUCCESS 的語意改為「本機加密檔＋已設定的雲端上傳與驗證都完成」**，不新增 migration：
   - 雲端失敗（本機檔已完成）→ `FAILED`，`file_name`／`size_bytes` 保留，`error` 是固定字串，exit 1，
     首頁紅色且明講「本機備份已完成，但雲端上傳失敗」。本機檔不刪、這次不做任何清理。
   - 沒設定 `RCLONE_REMOTE` 且沒有 `--local-only` → 設定錯誤，`FAILED`、exit 1，不產生本機檔，不默默降級。
   - 明確 `--local-only`（開發／CI）→ `SUCCESS` ＋ 固定「僅本機」警告，首頁黃色。P3-6 的正式排程不得使用。
   - 清理失敗（本機或雲端）→ 備份本身仍 `SUCCESS`，`error` 存固定警告，首頁黃色。
   - P3-5B 之前的 SUCCESS 一律視為「僅本機」。正式環境的第一次排程備份在 P3-6，所以正式資料庫沒有這種歷史。
6. **遠端保留與本機相同**（每日 30 天＋每月第一份 12 個月），重用 `select_deletions()`；以檔名時間戳為準，
   不看遠端的修改時間；只處理檔名符合格式的檔案，並且只用 `deletefile` 單檔刪除；遠端資料夾內其他檔案、
   子資料夾永遠不碰。

## 實測發現（記錄在案）

rclone 1.75.2（local backend）：`copyto --immutable` 在單檔模式下**不會**擋住覆蓋同名但內容不同的檔案
（exit 0、內容被換掉）；目錄模式的 `copy --immutable` 則會失敗（exit 6，`immutable file modified`）。
因此上傳改用 `copy --immutable --include`。Google Drive 上的實際行為待 Windows 真 Drive 驗收確認，
這份 ADR 不把未實測的 backend 細節當成保證。

## 考慮過的替代方案

| 方案 | 優點 | 缺點 |
|---|---|---|
| **rclone CLI（採用）** | 零 Python dependency；OAuth、重試、限流已解決；local backend 可在 CI 真測 | 需要安裝執行檔；rclone.conf 含 token，要當 secret |
| Google Drive Python SDK | 純 Python | 要自己處理 OAuth 授權流程與 token 保存、續傳與重試；新增多個第三方套件與長期維護成本 |
| 上傳前預先列表＋同名同大小再 `rclone check` | 可辨識「同一檔重傳」 | 這版每次都產生新檔名，用不到；多出分支與測試，違反最小策略 |
| 新增 `upload_events` 資料表或 migration 0006 欄位 | 歷史可完整追溯 | 過度工程；FAILED 帶 `file_name`、固定 `error` 字串已足以表達，正式環境沒有 5A 語意的歷史 |

## 後果（Consequences）

- 正面：離機備份有了；失敗不會被描述成「完全沒有備份」，也不會悄悄降級；沒有新增 schema 或 Python dependency。
- 負面：環境需要 rclone 執行檔（CI：`apt-get install rclone`；缺時備份測試失敗，不 skip）；
  依賴 rclone 的命令列介面，版本差異要靠整合測試發現。
- 上傳失敗的那一天，遠端會有缺口，由紅色警示與 FAILED 紀錄告知；不自動補傳舊檔。
