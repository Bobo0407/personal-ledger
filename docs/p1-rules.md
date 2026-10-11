# P1 規則與測試對照

這份表列出 P1 帳務核心的規則代碼、實作位置與代表性測試，以及規格第 11 節帳務核心案例的對應測試。
測試路徑省略 `tests/` 前綴；`integration/` 用暫存 SQLite 加測試用科目表，`unit/` 是純函式。
`test_tXX_*` 的存在由 `unit/test_architecture.py::test_every_core_spec_case_has_a_test` 自動檢查。

## 1. 規格第 11 節：帳務核心案例

| 編號 | 情境 | 測試 |
|---|---|---|
| T01 | 一般支出（LINE Pay → 郵局） | `integration/test_transactions.py::test_t01_lunch_with_line_pay` |
| T02 | 信用卡消費 | `integration/test_transactions.py::test_t02_online_shopping_on_credit_card` |
| T03 | 信用卡繳款不計入支出 | `integration/test_transactions.py::test_t03_card_payment_is_not_expense` |
| T04 | 轉帳含手續費 | `integration/test_transactions.py::test_t04_transfer_with_fee` |
| T05 | 全額退款 | `integration/test_refunds.py::test_t05_full_refund` |
| T06 | 跨月退款 | `integration/test_refunds.py::test_t06_cross_month_refund` |
| T07 | 退款超額 | `integration/test_refunds.py::test_t07_over_refund_blocked_and_nothing_written` |
| T08 | 外幣刷卡 + 手續費 | `integration/test_transactions.py::test_t08_foreign_card_purchase_with_fee` |
| T09 | 分期全額入帳 + 利息 | `integration/test_transactions.py::test_t09_installment_full_amount_plus_interest` |
| T10 | 薪資單 | `integration/test_special_transactions.py::test_t10_payroll` |
| T11 | 借貸不平衡 | `integration/test_ledger_post.py::test_t11_unbalanced_reports_difference` |
| T12 | 記在非末層科目 | `integration/test_ledger_post.py::test_t12_non_leaf_account_rejected` |
| T13 | 期初餘額 | `integration/test_special_transactions.py::test_t13_opening_balances` |
| T14 | 帳平檢查 | `integration/test_integrity.py::test_t14_unbalanced_transaction_written_directly` |
| T33 | 帳務調整 | `integration/test_special_transactions.py::test_t33_cash_count_shortage`、`test_t33_missing_note_is_v15` |
| T34 | 多分錄交易退款 | `integration/test_refunds.py::test_t34_refund_only_reverses_main_of_multi_line_expense` |

P1 也先在 service 層完成了 T24（修改與稽核）、T25（刪除與還原）：
`integration/test_transaction_lifecycle.py::test_t24_update_with_audit`、`test_t25_soft_delete_and_restore`。
這兩個在規格裡是整合測試，P2 有介面後還要補介面層的驗收。

## 2. 交易驗證（規格第 9.1 節與 P1 補充代碼）

嚴重程度：**擋下** = `ValidationFailed`；**確認** = `ConfirmationRequired`；**提醒** = 寫入後的 notices。

| 代碼 | 規則 | 嚴重程度 | 實作 | 代表性測試 |
|---|---|---|---|---|
| V01 | 使用者輸入的金額 > 0 的整數 | 擋下 | `entries.py` 各 `check()` | `unit/test_entry_builders.py::test_expense_v01_amount_must_be_positive_int`、`integration/test_transactions.py::test_form_errors_stop_before_building` |
| V02 | 至少 2 筆分錄，且加總為 0 | 擋下 | `validation.py`；還原時 `validate_restore` | `integration/test_ledger_post.py::test_t11_*`、`test_needs_at_least_two_postings` |
| V03 | 只能記在最末層科目 | 擋下 | `validation.py` | `integration/test_ledger_post.py::test_t12_*`、`integration/test_special_transactions.py::test_opening_leaf_only` |
| V04 | 科目、付款方式、商家必須啟用中（修改時只看被改動的部分，D14） | 擋下 | `validation.py` | `integration/test_ledger_post.py::test_inactive_account_rejected`、`integration/test_transaction_lifecycle.py::test_d14_*` |
| V05 | 必填欄位；科目、付款方式必須存在 | 擋下 | `entries.py` `check()` + `validation.py` | `integration/test_ledger_post.py::test_missing_date`、`test_missing_account_points_to_line` |
| V06 | 轉帳的轉出、轉入不能相同 | 擋下 | `entries.py` `TransferEntry.check()` | `unit/test_entry_builders.py::test_transfer_v06_same_account`、`integration/test_transactions.py::test_v06_transfer_same_account` |
| V07 | 退款加總不超過原交易 MAIN | 擋下 | `validation.py` `_check_refund` | `integration/test_refunds.py::test_t07_*`、`test_t34_*`、`test_partial_refunds_accumulate` |
| V08 | 發票號碼不重複（**P1 只做部分**：資料庫索引 + 還原時檢查；完整版在 P2） | 擋下 | DB 部分唯一索引；`validate_restore` | `integration/test_ledger_schema.py::test_invoice_no_unique_among_active_transactions`、`integration/test_transaction_lifecycle.py::test_restore_checks_invoice_in_use` |
| V09 | 原幣別與原幣金額同時填寫或同時空白；幣別存在 | 擋下 | `validation.py` + DB CHECK | `integration/test_ledger_post.py::test_v09_original_currency` |
| V10 | 交易金額 > 門檻（OPENING、PAYROLL 除外；修改時只在金額改變時） | 確認 | `validation.py` | `integration/test_ledger_post.py::test_v10_*`、`integration/test_transaction_lifecycle.py::test_v10_only_when_magnitude_changes` |
| V11 | 日期晚於今天（修改時只在日期改變時） | 確認 | `validation.py` | `integration/test_ledger_post.py::test_v11_future_date_requires_confirmation`、`integration/test_transaction_lifecycle.py::test_v11_only_when_date_changes_to_future` |
| V12 | 同日同商家同金額 | 確認 | **P2** | — |
| V13 | 現金／銀行截至今天的餘額因這筆變少且 < 0 | 提醒 | `validation.py` `negative_balance_notices` | `integration/test_ledger_post.py::test_v13_*`、`integration/test_transaction_lifecycle.py::test_v13_*` |
| V14 | 預算使用率跨過 80%／100% | 提醒 | **P3** | — |
| V15 | ADJUSTMENT 備註必填、只調整一個資產或負債 | 擋下 | `validation.py` | `integration/test_special_transactions.py::test_t33_missing_note_is_v15`、`test_v15_handwritten_drafts` |
| POSTING_AMOUNT | 分錄金額是不為 0 的整數 | 擋下 | `validation.py` | `integration/test_ledger_post.py::test_posting_amount_invariant` |
| TXN_TYPE／LINE_ROLE | 不認識的交易類型或分錄角色 | 擋下 | `validation.py` | `integration/test_ledger_post.py::test_unknown_txn_type_and_line_role` |
| SYSTEM_ACCOUNT | 系統科目只能用在對應的交易類型 | 擋下 | `validation.py` | `integration/test_ledger_post.py::test_system_accounts_only_in_their_txn_type` |
| TXN_STRUCTURE | 依交易類型的分錄結構（第二道防線） | 擋下 | `txn_structure.py` | `integration/test_txn_structure.py`、`integration/test_refunds.py::test_refund_structure*` |
| OPENING_BALANCE | 期初餘額必須是整數 | 擋下 | `entries.py` `OpeningEntry.check()` | `unit/test_special_builders.py::test_opening_check` |
| OPENING_EXISTS | 同一帳戶只能有一筆有效期初（D12） | 擋下 | `validation.py` | `integration/test_special_transactions.py::test_d12_*`、`integration/test_transaction_lifecycle.py::test_restore_opening_rechecks_d12` |
| ADJUSTMENT_DELTA | 調整差額是不為 0 的整數 | 擋下 | `entries.py` `AdjustmentEntry.check()` | `unit/test_special_builders.py::test_adjustment_delta` |
| REFUND_ORIGINAL | 原交易必須是有效支出；必須沖回原 MAIN 科目 | 擋下 | `validation.py` | `integration/test_refunds.py::test_refund_original_must_be_active_expense`、`test_handwritten_refund_*` |
| REFUND_DATE | 退款日期不能早於原交易 | 擋下 | `validation.py` | `integration/test_refunds.py::test_refund_date_cannot_precede_original`、`test_restore_refund_rechecks_date` |
| REFUND_EXISTS | 有有效退款的支出不能刪除 | 擋下 | `ledger.py` `soft_delete` | `integration/test_refunds.py::test_original_with_refund_cannot_be_deleted` |
| REFUND_LOCKED | 原支出有退款時的鎖定欄位；退款不能換原交易 | 擋下 | `validation.py` `_check_refund_locks` | `integration/test_refunds.py::test_original_with_refund_locked_fields`、`test_update_refund_cannot_switch_original` |
| TXN_TYPE_IMMUTABLE | 交易類型不能修改 | 擋下 | `ledger.py` `update` | `integration/test_transaction_lifecycle.py::test_txn_type_is_immutable` |
| TXN_DELETED | 已刪除的交易不能修改或再刪除 | 擋下 | `ledger.py` | `integration/test_transaction_lifecycle.py::test_deleted_transaction_must_be_restored_before_update` |
| TXN_NOT_DELETED | 未刪除的交易不能還原 | 擋下 | `ledger.py` `restore` | `integration/test_transaction_lifecycle.py::test_restore_active_transaction` |

## 3. 系統例外

| 例外 | 意義 | 測試 |
|---|---|---|
| `NotFound` | 找不到交易或主資料 | `integration/test_transaction_lifecycle.py::test_missing_transaction` |
| `InvalidSetting` | `large_amount_threshold` 存在但不是正整數 | `integration/test_ledger_post.py::test_v10_invalid_setting_fails_loudly` |
| `MissingSystemAccount` | 找不到期初權益或帳務調整系統科目 | `integration/test_special_transactions.py::test_missing_system_account_is_system_error` |
| `SessionNotClean` | 呼叫寫入時 Session 有未提交的變更 | `integration/test_ledger_post.py::test_refuses_session_with_pending_changes`、`integration/test_transaction_lifecycle.py::test_lifecycle_refuses_dirty_session` |

## 4. 主資料規則（規格第 9.2 節，P1 適用部分）

| 代碼 | 規則 | 測試 |
|---|---|---|
| M01 | 已有分錄（含草稿、已刪除）的科目不能刪除 | `integration/test_accounts_service.py::test_cannot_delete_account_with_postings`、`test_postings_on_deleted_transactions_still_block_delete` |
| M02 | 子科目的五大類必須與上層相同 | `integration/test_accounts_service.py::test_child_class_must_match_parent` |
| M03 | 已有分錄的最末層科目不能新增子科目 | `integration/test_accounts_service.py::test_cannot_add_child_to_account_with_postings` |
| M04 | 科目階層不能形成迴圈 | V1 不提供科目搬移（S8），新增時一定有既有上層，不會形成迴圈；DB 另有不可自我參照的限制 |
| M05 | 預算只能設在第一層費用科目 | **P3** |
| M06 | 商家別名正規化後不能重複 | DB 唯一限制（`integration/test_ledger_schema.py::test_alias_normalized_is_unique_across_merchants`）；別名功能在 P2 |
| M07 | 信用卡必須設定結帳日與繳款日；扣繳帳戶為啟用中的銀行 | `integration/test_credit_cards_service.py::test_m07_*` |
| M08 | 預設付款方式只能有一個 | `integration/test_ledger_schema.py::test_only_one_default_payment_method`、`integration/test_payment_methods_service.py::test_cannot_deactivate_default` |
| S9 | 同一上層下名稱不可重複 | `integration/test_accounts_service.py::test_sibling_names_must_be_unique` |
| S10 | tax_category 只允許最末層 | `integration/test_accounts_service.py::test_tax_category_only_on_leaf` |

## 5. 帳平檢查（integrity.check_all）

檢查全部交易（含已刪除與草稿），因為它們隨時可能被還原或確認入帳。只檢查帳務結構，不複製業務驗證。

| 代碼 | 規則 | 測試 |
|---|---|---|
| UNBALANCED | 分錄加總不為 0 | `integration/test_integrity.py::test_t14_*`、`test_checks_draft_and_deleted_transactions` |
| TOO_FEW_POSTINGS | 分錄少於 2 筆 | `integration/test_integrity.py::test_single_posting`、`test_transaction_without_postings` |
| NON_LEAF_ACCOUNT | 分錄記在非最末層科目 | `integration/test_integrity.py::test_posting_on_non_leaf_account` |

## 6. 功能範圍：F01–F14、F30

| 功能 | 內容 | P1 狀態 |
|---|---|---|
| F01 | 複式記帳引擎 | 完成（ADR-001、ADR-003） |
| F02 | 科目表與階層 | 完成（seed + 科目樹） |
| F03 | 科目管理：新增、改名、停用；有交易不能刪除 | 完成（service；介面在 P2） |
| F04 | 期初餘額 | 完成（T13、D12） |
| F05 | 帳戶：現金、郵局、聯邦活存、聯邦信用卡 | 完成（正式科目表 seed） |
| F06 | 付款管道對應帳戶 | 完成（ADR-004、T01） |
| F07 | 信用卡結帳日、繳款日、扣繳帳戶 | 規格階段表列在 P3；P1 已完成設定資料與 M07，週期計算在 P3 |
| F08 | 一般收入、支出 | 完成（T01、T02） |
| F09 | 帳戶間轉帳 | 完成（T04） |
| F10 | 信用卡繳款 | 完成（T03、D9） |
| F11 | 退款 | 完成（T05–T07、T34、ADR-006） |
| F12 | 分期付款全額入帳 + 利息 | 完成（T09；期數在 V1.5） |
| F13 | 多分錄交易 | 完成（ManualEntry、T08、T10） |
| F14 | 應收／應付 | 完成（最末層科目 + 轉帳） |
| F30 | 資料驗證規則 | P1 適用部分完成：V01–V07、V09–V11、V13、V15；V08 部分；V12 在 P2、V14 在 P3 |

## 7. 架構守門（`unit/test_architecture.py`）

| 規則 | 測試 |
|---|---|
| 只有 `services/ledger.py` 建立 `Posting` | `test_only_ledger_service_creates_postings` |
| 只有 `services/ledger.py` 建立 `Transaction`、改 `deleted_at` | `test_only_ledger_service_creates_transactions_and_changes_deleted_at` |
| LedgerService 寫入只由 `services/transactions.py` 呼叫 | `test_ledger_writes_only_from_transactions_service` |
| 排除條件只寫在 `repositories/ledger.py` | `test_active_filter_lives_only_in_ledger_repository`、`test_transaction_status_and_deleted_at_queried_only_in_ledger_repository` |
| routers 不碰 repositories、LedgerService、Posting，不 add／commit | `test_routers_do_not_touch_ledger_internals` |
| repositories 不 commit、不 import services | `test_repositories_do_not_commit_or_import_services` |
| T01–T14、T33、T34 都有 `test_tXX_*` | `test_every_core_spec_case_has_a_test` |
| 金額欄位是 INTEGER | `integration/test_ledger_schema.py::test_money_columns_are_integer` |
