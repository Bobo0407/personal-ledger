# ADR-007：交易標籤由 LedgerService 在同一個資料庫交易內寫入

- 狀態：已採用（P2 Step 8A）
- 相關：規格 F19、6.8、7.4、「標籤層級」決策；ADR-003（帳務寫入路徑）、ADR-005（交易生命週期）

## 背景（Context）

F19 要求一筆交易可以掛多個標籤。規格 7.4 規定一筆交易的寫入順序是：
transactions、postings、transaction_tags → audit_logs → 提交。

現行的寫入路徑（ADR-003）：

- `ledger.post／update` 是唯一的提交點；進入時要求 Session 沒有未提交的變更（`_ensure_clean`），
  任何錯誤整批 rollback。
- 稽核快照在提交前、寫完分錄之後產生。

標籤掛在交易、不掛在分錄（規格「標籤層級」）：不影響借貸，也不影響任何帳務規則，
但需要「標籤存在、新加上的標籤是啟用中」的檢查，而且應該出現在稽核紀錄裡。

## 決定（Decision）

1. `EntryDraft` 增加 `tag_ids: tuple[int, ...] | None = None`：
   - `None` = 這次不處理標籤（新增時就是沒有標籤；修改時保留原本的標籤）。
   - `()` = 沒有標籤／移除全部。
   - 非空 tuple = 整組替換。順序沒有語意；由 transactions 去重並排序。
   Builder 與各 Form 不知道標籤，由 `transactions` 用 `replace(draft, tag_ids=...)` 補上，
   和 `source_ref` 的做法相同。
2. `ledger.post／update` 在分錄 flush 之後、寫稽核之前，寫入或整組替換 `transaction_tags`，
   和交易共用同一個 commit／rollback。LedgerService 是唯一寫入 `transaction_tags` 的地方（架構測試守住）。
3. `validate._check_header` 檢查標籤：找不到 → V05；新加上的停用標籤 → V04；
   原本就有、這次沒動的停用標籤可以保留（D14 的精神，和商家相同）。修改前的標籤在修改前從資料庫查好，
   放進 `ExistingTransaction.tag_ids`。還原不重跑這項檢查（和還原不重跑 V04 一致）。
4. 稽核快照增加 `tag_ids`（排序後的 id），只記 id 不記名稱。快照一律用查詢讀資料庫目前的
   `transaction_tags`；`Transaction` 刻意不建標籤 relationship，寫入標籤後明確 flush 再取快照，
   不依賴 ORM 集合或 autoflush。異動摘要顯示「標籤：A、B → A、C」，用現在的名稱，查不到顯示 #id；
   8A 之前的舊紀錄沒有 `tag_ids`，視為未知、不比較。
5. 軟刪除、還原不動關聯。外鍵都不 cascade：交易只軟刪除，有交易引用的標籤不能硬刪除。

## 替代方案（Alternatives）

- **在 orchestration 層於 `ledger.post` 前後補標籤**：post 內部自己提交、而且要求乾淨的 Session，
  只能做成兩次提交（不是原子），不然就要把提交責任移出 LedgerService（加 `commit=False`）；
  CREATE 稽核也會漏掉標籤。
- **在 LedgerService 加 after-flush callback／hook**：可以原子，但為單一用途增加通用擴充點，
  標籤驗證與「callback 必須在稽核前執行」都只能靠慣例維持，測試成本也較高。
- **把標籤放進各 Form 與 builder**：builder 會知道與借貸無關的事，而且要修改所有 builder。

## 取捨（Trade-offs）

- P1 core 的資料結構（`EntryDraft`、`ExistingTransaction`）與寫入步驟各多了一項。這是刻意的：
  讓規格 7.4 的寫入順序由程式本身保證，而不是靠呼叫端配合。
- `None` 與 `()` 的區別必須寫清楚並由測試守住：既有的修改流程沒有傳標籤時，不能把標籤清掉。
- 稽核只記 id：標籤改名後，歷史摘要顯示新名稱（和科目、商家的決策一致）；
  好處是改名後仍能對回同一個標籤。
- 不建 ORM relationship，讀取多一次查詢；換來快照與驗證一定讀到資料庫的真實狀態。

## 為什麼適合單人系統（Why this is appropriate for a single-user app）

只增加一個欄位與一個固定的寫入步驟，沒有引入 hook、事件、佇列或另一套交易邊界。
原子性由 SQLite 的單一資料庫交易保證；測試沿用既有的「稽核失敗整批回滾」寫法即可涵蓋。
