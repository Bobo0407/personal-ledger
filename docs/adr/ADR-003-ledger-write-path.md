# ADR-003：帳務寫入路徑（check → build → validate → LedgerService）

- 狀態：已採用（P1）
- 相關：規格第 7、9 節；決策 Q2、Q3、P1–P4、D6

## 背景

每種交易（支出、收入、轉帳、信用卡繳款、期初、調整、薪資、退款）都有自己的表單和分錄形狀，
但帳務不變條件（平衡、最末層、啟用中、系統科目用途……）是共通的。
規則如果散在 router、builder、service 各處，同一條規則會有兩份實作，最後行為不一致。

## 決定

寫入一律走同一條路徑，每一層只做一件事：

| 層 | 位置 | 責任 |
|---|---|---|
| `Builder.check(form)` | `services/entries.py` | 表單語意：V01 金額 > 0、V06、必填欄位。不查資料庫 |
| 主資料解析 | `services/transactions.py` | 付款方式 → 實際帳戶、系統科目、退款的原交易 |
| `Builder.build(form, ...)` | `services/entries.py` | 純結構轉換成 `EntryDraft`（平衡的分錄草稿）。不查資料庫、不驗證 |
| `validate(db, draft)` | `services/validation.py` | 所有草稿層的帳務不變條件；`TXN_STRUCTURE` 是依交易類型的第二道防線 |
| `ledger.post/update/soft_delete/restore` | `services/ledger.py` | 唯一的寫入入口：驗證 → 寫入交易與分錄 → 稽核 → commit |

1. **LedgerService 是唯一寫入入口。** 只有 `services/ledger.py` 能建立 `Transaction`、`Posting`、改 `deleted_at`；
   LedgerService 的寫入函式只由 `services/transactions.py` 呼叫。router 只呼叫 `transactions.*`。
   以上都由架構守門測試把關。
2. **驗證結果分三種**：errors（擋下，`ValidationFailed`）、confirmations（要使用者確認後帶 `confirmed` 重送，
   `ConfirmationRequired`）、notices（儲存後提醒）。規則全部跑完才回傳，多個錯誤一次列出。
3. **原子性。** 交易、分錄、稽核紀錄（`audit_logs`）在同一個資料庫交易內一起 commit；任何例外整批 rollback。
   `audit.record()` 只把紀錄加進 Session，不自己 commit。
4. **乾淨的 Session。** 呼叫時 Session 不能有尚未提交的變更（`SessionNotClean`），避免把別人的變更一起 commit；
   不另外建立 Unit of Work 抽象。

## 考慮過的替代方案

- **builder 直接驗證並寫入**：少一層，但新增交易類型時每個 builder 都要重寫共通規則。
- **在 router 組分錄**：最直覺，但借貸邏輯會跑到畫面層，也無法在沒有 UI 的 P1 完整測試。
- **Unit of Work／Repository 抽象層**：對單一使用者 SQLite 是過度設計；`_ensure_clean` + 單一 commit 點已足夠。

## 後果

- 正面：規則只有一份；P1 完全沒有 UI 也能用 pytest 驗證所有帳務行為。
- 正面：就算有人手寫 `EntryDraft` 繞過 builder，`validate()` 與 `TXN_STRUCTURE` 仍會擋下不合理的分錄。
- 代價：新增一種交易類型要動 entries、transactions、txn_structure 三處；這是刻意的分工，不是重複。
