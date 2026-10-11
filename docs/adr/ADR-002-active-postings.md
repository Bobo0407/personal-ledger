# ADR-002：active_postings() 是有效分錄唯一的語意入口

- 狀態：已採用（P1）
- 相關：規格 5.4；決策 Q11；`app/repositories/ledger.py`

## 背景

交易有軟刪除（`deleted_at`）和草稿（`status = DRAFT`，V1.5 才會產生）。
這兩種交易的分錄都還在資料庫裡，但不能計入餘額與報表。
如果每個查詢都自己寫 `deleted_at IS NULL AND status = 'POSTED'`，遲早會有一個地方漏掉，
而且這種錯誤不會報錯，只會讓數字悄悄變錯。

## 決定

1. 有效分錄 = `postings JOIN transactions`，且 `transactions.deleted_at IS NULL AND status = 'POSTED'`。
   這個條件只寫在 `repositories/ledger.active_postings()`。
2. 所有帳務加總都從 `active_postings()` 開始，呼叫端只能**再加條件**或改成加總欄位，
   例如 `active_postings().with_only_columns(func.sum(Posting.amount)).where(...)`。
   日後的 reports／budgets 可以在這個基礎上做 SUM。
3. 例外只有名稱明確的「全部」查詢：帳平檢查（integrity）要看全部交易，因為已刪除與草稿隨時可能被還原或確認；
   發票號碼唯一性用的是資料庫部分唯一索引相同的條件，不是帳務加總。
4. 用架構守門測試把關（`tests/unit/test_architecture.py`）：
   - 排除條件 `deleted_at.is_(None)` 只能出現在 `repositories/ledger.py`（以及發票部分唯一索引的 model 定義）。
   - 查詢用的 `Transaction.status`／`Transaction.deleted_at` 只能出現在 `repositories/ledger.py`。

## 考慮過的替代方案

- **資料庫 view（active_postings view）**：語意集中在資料庫，但 view 也要透過 migration 管理，
  SQLAlchemy 端還是要另外對應；對單一使用者的 SQLite 沒有明顯好處。
- **ORM 全域過濾（例如 `with_loader_criteria`）**：隱性生效，讀已刪除資料（稽核、還原、帳平檢查）時反而要繞過，
  而且不容易從程式碼看出某個查詢到底有沒有被過濾。
- **禁止 `func.sum(Posting.amount)` 出現在其他檔案**：過度限制，報表與預算本來就需要在有效分錄上加總，
  容易產生誤判，所以沒有採用；守的是「不能重寫排除條件」，不是「不能加總」。

## 後果

- 正面：刪除與還原後所有數字自動正確，沒有第二份狀態要同步。
- 正面：新增報表只要從 `active_postings()` 開始，不用記得排除條件。
- 代價：source scan 只能抓到明顯的重寫；最後一道防線仍是功能測試（刪除、草稿不計入的測試）。
