"""ManualEntry：只做結構轉換，不查資料庫（Q6、Step 3 修正三）。"""

from datetime import date

from app.services.drafts import PostingDraft
from app.services.entries import ManualEntry, ManualForm, ManualLine


def test_builds_signed_postings_in_order():
    form = ManualForm(
        txn_date=date(2026, 10, 15),
        lines=(ManualLine(1, 1000, line_role="MAIN"), ManualLine(2, 15), ManualLine(3, -1015)),
        description="  海外刷卡  ",
    )
    draft = ManualEntry.build(form)

    assert draft.txn_type == "MANUAL"
    assert draft.txn_date == date(2026, 10, 15)
    assert draft.description == "海外刷卡"
    assert draft.payment_method_id is None
    assert draft.postings == (
        PostingDraft(1, 1000, line_role="MAIN"),
        PostingDraft(2, 15),
        PostingDraft(3, -1015),
    )


def test_zero_lines_are_skipped():
    form = ManualForm(
        date(2026, 10, 15), (ManualLine(1, 100), ManualLine(2, 0), ManualLine(3, -100))
    )
    assert [p.account_id for p in ManualEntry.build(form).postings] == [1, 3]


def test_unselected_account_becomes_draft_line():
    # 修正三：builder 不驗證，account_id=None 照樣轉成草稿，交給 V05
    form = ManualForm(date(2026, 10, 15), (ManualLine(None, 100), ManualLine(3, -100)))
    assert ManualEntry.build(form).postings[0].account_id is None


def test_invalid_amounts_are_not_silently_dropped():
    # 只略過整數 0；bool、小數留給驗證擋下
    form = ManualForm(date(2026, 10, 15), (ManualLine(1, False), ManualLine(2, 0.0)))
    assert len(ManualEntry.build(form).postings) == 2


def test_blank_description_and_memo_become_none():
    form = ManualForm(date(2026, 10, 15), (ManualLine(1, 1, memo="  "),), description="  ")
    draft = ManualEntry.build(form)
    assert draft.description is None
    assert draft.postings[0].memo is None
