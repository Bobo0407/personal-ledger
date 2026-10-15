# ADR-008：備份加密使用 age 命令列工具

- 狀態：已採用（P3-5A）
- 相關：規格 F29、7.2、「加密異地備份」；`docs/decisions/p3.md` P3-5A

## 背景（Context）

規格要求每日備份「SQLite backup API → gzip → age 加密 → 上傳」，而且「沒還原過的備份不算備份」。
專案偏好不增加 dependency。Python 標準庫沒有 authenticated encryption（沒有 AES-GCM 之類），
自己用 HMAC／hashlib 拼加密是不可接受的做法。所以「只用標準庫完成加密」做不到，必須選一個加密方案。

威脅模型：備份檔會離開這台機器（P3-5B 上傳雲端）；伺服器可能被入侵；備份要能在幾年後、
在另一台乾淨機器上還原，而且不依賴我們的程式碼。

## 決定（Decision）

用 **age 命令列工具**（`age -r <公鑰>`），由 Python 以 `subprocess`（list 參數、不經 shell）呼叫。

1. 公鑰加密：伺服器只有公鑰（`BACKUP_AGE_RECIPIENT`），被入侵也解不開已存在的備份；私鑰存在密碼管理員，
   只在還原時傳入。
2. 輸出是標準 age 格式：任何機器用標準 `age -d -i <私鑰>` 都能還原，不需要我們的程式。
3. 沒有新增 Python dependency。代價是環境需要 age 執行檔（Windows：`winget install FiloSottile.age`；
   CI：`apt-get install age`）。
4. 快照與壓縮都在記憶體，以 stdin 交給 age，磁碟上只有密文。

## 考慮過的替代方案

| 方案 | 優點 | 缺點 |
|---|---|---|
| **age CLI（採用）** | 零 Python dependency；公鑰加密；標準格式；規格原案 | 需要安裝執行檔；測試要真的 age |
| `pyrage`（age 的 Python 綁定） | pip 即可安裝，同為 age 格式 | 新增第三方 dependency；wheel 對 3.12／3.13 與 Windows 的相容性要自行驗證；少一個可替換的獨立工具 |
| `cryptography` AES-GCM（對稱金鑰） | pip 即可安裝 | 偏離規格；金鑰必須和備份在同一台機器上，等於伺服器被入侵就能解全部；自訂檔案格式，還原要靠自己寫的程式 |
| 標準庫自行實作 | 無 dependency | 不安全，不考慮 |

## 後果（Consequences）

- 正面：金鑰管理簡單且安全；備份格式不綁定本專案；還原路徑可獨立驗證。
- 負面：CI 與開發機都要安裝 age；CI 缺 age 時備份測試會失敗（不 skip），本機缺 age 則 skip。
- 之後若要改用 `pyrage`，輸出格式相容，只需替換 `_run_age()`，不影響已存在的備份。
