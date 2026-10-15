# ruff: noqa: F811  （共用 test_web_txn_new.py 的 fixture：pytest 依參數名稱注入，不是重複定義）
"""/reports/monthly 月報頁（P3-4；規格 8.2、7.3、T16）。時鐘固定在 2026-10-15。

所有數字都由 services/reports.py 算好；這裡驗證畫面「照印」，包含 signed 的不計入預算淨額。
"""

import re
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import PaymentMethod, Setting
from app.services import accounts as account_service
from app.services import budgets, transactions
from app.services.entries import ExpenseForm, IncomeForm, RefundForm
from tests.integration.test_web_txn_new import (  # noqa: F401
    _today,
    anon,
    client,
    db,
    log_dir,
    session_factory,
)
from tests.ledger_helpers import TODAY, acc, post_manual

URL = "/reports/monthly"
OCT, SEP = "2026-10", "2026-09"
ALL = {"V10", "V11", "V13"}
TEMPLATE = Path(__file__).resolve().parents[2] / "app/templates/pages/report_monthly.html"


def pm(db, name="LINE Pay") -> int:
    return db.scalar(select(PaymentMethod.id).where(PaymentMethod.name == name))


def spend(db, day, amount, code="5101", *, exclude=False, method="LINE Pay") -> int:
    form = ExpenseForm(
        txn_date=day,
        amount=amount,
        account_id=acc(db, code),
        payment_method_id=pm(db, method),
        exclude_from_budget=exclude,
    )
    return transactions.record_expense(db, form, confirmed=ALL).transaction_id


def earn(db, day, amount, code="4201", to="1102") -> None:
    form = IncomeForm(day, amount, acc(db, code), acc(db, to))
    transactions.record_income(db, form, confirmed=ALL)


def line(html: str, key: str) -> str:
    """收支總覽／淨資產那幾行 <li data-key="...">…</li> 的純文字。"""
    match = re.search(rf'<li class="[^"]*" data-key="{key}">(.*?)</li>', html, re.S)
    assert match, key
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(1))).strip()


def expense_block(html: str, db, code: str) -> str:
    start = f'<li class="report-expense" data-account-id="{acc(db, code)}">'
    assert start in html, code
    rest = html.split(start, 1)[1]
    return re.split(r'<li class="report-expense"|</section>', rest, maxsplit=1)[0]


# --- 路由、登入、月份參數 ----------------------------------------------------------------------


def test_requires_login(anon):
    resp = anon.get(URL)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Freports%2Fmonthly"


def test_default_is_current_month(client):
    html = client.get(URL).text
    assert "<h1>2026-10 月報</h1>" in html
    assert 'type="month" value="2026-10"' in html


def test_month_query_param(client):
    resp = client.get(URL, params={"month": SEP})
    assert resp.status_code == 200
    assert "<h1>2026-09 月報</h1>" in resp.text


def test_blank_month_is_current_month(client):
    resp = client.get(URL, params={"month": ""})
    assert resp.status_code == 200 and "2026-10 月報" in resp.text


def test_past_future_and_wide_year_months_are_ok(client):
    for month in ("2026-01", "2027-03", "1999-12", "2100-01"):
        assert client.get(URL, params={"month": month}).status_code == 200


@pytest.mark.parametrize(
    "month", ["2026-13", "2026-00", "2026-1", "abc", "2026/10", "0000-05", "9999-12", "0001-01"]
)
def test_invalid_month_is_a_422_page_not_500_and_not_a_silent_fallback(client, month):
    resp = client.get(URL, params={"month": month})
    assert resp.status_code == 422
    html = resp.text
    assert 'data-code="REPORT_MONTH"' in html
    assert 'type="month"' in html  # 選擇器還在，可以直接重選
    assert f'<a href="{URL}">回到本月</a>' in html
    assert "2026-10 月報" not in html  # 不靜默改成本月
    assert "Traceback" not in html and "發生錯誤" not in html


def test_prev_and_next_links(client):
    html = client.get(URL, params={"month": "2026-01"}).text
    assert f'href="{URL}?month=2025-12"' in html
    assert f'href="{URL}?month=2026-02"' in html
    assert "2026-01-01 ～ 2026-01-31" in html


def test_home_has_the_monthly_report_entry(client):
    html = client.get("/").text
    assert re.search(r'<a class="button-link" href="/reports/monthly">月報</a>', html)


# --- 收支總覽 --------------------------------------------------------------------------------


def test_summary_numbers_are_printed_from_the_service(client, db):
    earn(db, TODAY, 50000)
    spend(db, TODAY, 1234)
    html = client.get(URL).text
    assert line(html, "income") == "本月收入 50,000"
    assert line(html, "expense") == "本月支出 1,234"
    assert line(html, "balance") == "本月結餘 48,766"
    assert line(html, "saving-rate") == "儲蓄率 97.5%"
    assert line(html, "adjustment") == "帳務調整合計 0"


def test_negative_saving_rate_is_shown(client, db):
    earn(db, TODAY, 1000)
    spend(db, TODAY, 1500)
    html = client.get(URL).text
    assert line(html, "balance") == "本月結餘 -500"
    assert line(html, "saving-rate") == "儲蓄率 -50.0%"


def test_saving_rate_without_income_shows_dash_not_zero(client, db):
    spend(db, TODAY, 1500)
    html = client.get(URL).text
    assert line(html, "saving-rate") == "儲蓄率 —（本月沒有收入）"
    assert "0.0%" not in html


def test_empty_month_renders_without_error(client):
    resp = client.get(URL, params={"month": "2026-01"})
    assert resp.status_code == 200
    html = resp.text
    assert line(html, "income") == "本月收入 0"
    assert line(html, "expense") == "本月支出 0"
    assert line(html, "saving-rate") == "儲蓄率 —（本月沒有收入）"
    assert "這個月沒有支出" in html
    assert "<td" in html  # 帳戶餘額照常列出
    assert line(html, "net-worth-start") == line(html, "net-worth-end").replace("月底", "月初")


# --- 各科目支出與預算 ------------------------------------------------------------------------


def test_expense_rows_children_and_no_budget_dash(client, db):
    spend(db, TODAY, 1000, "5101")
    spend(db, TODAY, 300, "5102")
    block = expense_block(client.get(URL).text, db, "5100")
    assert "餐飲" in block and "1,300" in block
    assert "正餐" in block and "1,000" in block and "飲料" in block and "300" in block
    assert "預算 —" in block


def test_t16_screen_shows_actual_with_excluded_note_and_unchanged_budget(client, db):
    budgets.set_budget(db, acc(db, "5200"), OCT, 50000)
    spend(db, TODAY, 30000, "5201", exclude=True)
    html = client.get(URL).text
    assert line(html, "expense") == "本月支出 30,000"
    block = expense_block(html, db, "5200")
    assert 'class="num report-actual">30,000<' in block
    assert re.search(r'report-budget-used">0<', block)  # 預算已用 0
    assert 'data-excluded="30000"' in block
    assert "不計入預算淨額" in block and "+30,000" in block


def test_excluded_refund_screen_shows_negative_amount(client, db):
    budgets.set_budget(db, acc(db, "5200"), OCT, 10000)
    original = spend(db, date(2026, 9, 20), 1000, "5201", exclude=True)
    transactions.record_refund(db, RefundForm(original, TODAY, 300, pm(db)), confirmed=ALL)
    block = expense_block(client.get(URL).text, db, "5200")
    assert 'data-excluded="-300"' in block
    assert "不計入預算淨額" in block and "-300" in block


def test_no_excluded_note_when_difference_is_zero(client, db):
    budgets.set_budget(db, acc(db, "5100"), OCT, 5000)
    spend(db, TODAY, 1000, "5101")
    block = expense_block(client.get(URL).text, db, "5100")
    assert "不計入預算淨額" not in block and "data-excluded" not in block
    assert "預算已用" in block


def test_over_budget_state_is_printed(client, db):
    budgets.set_budget(db, acc(db, "5100"), OCT, 1000)
    spend(db, TODAY, 1500, "5101")
    block = expense_block(client.get(URL).text, db, "5100")
    assert 'data-state="over"' in block and "超支" in block and "500" in block


def test_invalid_alert_pct_degrades_only_the_budget_column(client, db):
    budgets.set_budget(db, acc(db, "5100"), OCT, 5000)
    spend(db, TODAY, 1000, "5101")
    db.get(Setting, "budget_alert_pct").value = "abc"
    db.commit()
    resp = client.get(URL)
    assert resp.status_code == 200
    html = resp.text
    assert 'data-code="BUDGET_SETTING"' in html
    assert line(html, "expense") == "本月支出 1,000"
    block = expense_block(html, db, "5100")
    assert "1,000" in block and "預算已用" not in block


def test_first_level_own_postings_row(client, db):
    from tests.ledger_helpers import insert_raw_transaction

    insert_raw_transaction(db, "2026-10-10", ("5100", 70), ("1101", -70))
    spend(db, TODAY, 1000, "5101")
    block = expense_block(client.get(URL).text, db, "5100")
    assert "直接記在此科目" in block and "1,070" in block


def test_inactive_expense_account_is_marked(client, db):
    spend(db, TODAY, 80, "5201")
    account_service.deactivate_account(db, acc(db, "5201"))
    account_service.deactivate_account(db, acc(db, "5200"))
    block = expense_block(client.get(URL).text, db, "5200")
    assert block.count("已停用") == 2  # 第一層與第二層各一個


# --- 帳戶餘額與淨資產 ------------------------------------------------------------------------


def test_account_rows_and_credit_card_debt(client, db):
    earn(db, date(2026, 9, 10), 1000, to="1101")
    spend(db, TODAY, 1000, method="聯邦信用卡")
    html = client.get(URL).text
    cash = re.search(rf'<tr data-account-id="{acc(db, "1101")}">(.*?)</tr>', html, re.S).group(1)
    assert "現金" in cash and cash.count("1,000") == 2
    card = re.search(rf'<tr data-account-id="{acc(db, "2101")}">(.*?)</tr>', html, re.S).group(1)
    assert "聯邦信用卡" in card and "負債" in card
    assert "欠款 1,000" in card
    assert card.count("欠款") == 1  # 月初是 0，不加前綴
    assert "勞退個人專戶" in html  # 不只 show_on_home


def test_net_worth_lines(client, db):
    earn(db, date(2026, 9, 10), 4000, to="1101")
    spend(db, TODAY, 1000, method="聯邦信用卡")
    html = client.get(URL).text
    assert line(html, "net-worth-start") == "月初淨資產 4,000"
    assert line(html, "net-worth-end") == "月底淨資產 3,000"


def test_adjustment_total_is_printed(client, db):
    from app.services.entries import AdjustmentForm

    form = AdjustmentForm(TODAY, acc(db, "1101"), 300, "找到零錢")
    transactions.record_adjustment(db, form, confirmed=ALL)
    assert line(client.get(URL).text, "adjustment") == "帳務調整合計 300"


def test_future_note_only_when_month_ends_after_today(client):
    assert "月底餘額包含本月稍後日期的交易" in client.get(URL).text
    assert "月底餘額包含本月稍後日期的交易" not in client.get(URL, params={"month": SEP}).text


def test_manual_income_and_deleted_transactions_follow_the_service(client, db):
    post_manual(db, TODAY, ("1102", 700), ("4201", -700))
    txn_id = spend(db, TODAY, 400)
    transactions.delete_transaction(db, txn_id)
    html = client.get(URL).text
    assert line(html, "income") == "本月收入 700"
    assert line(html, "expense") == "本月支出 0"


# --- 模板不做運算 ----------------------------------------------------------------------------


def test_template_has_no_arithmetic_filters():
    text = TEMPLATE.read_text(encoding="utf-8")
    for forbidden in ("| abs", "| sum", "| round", "| int", "| float"):
        assert forbidden not in text
    # 沒有在 {{ }} 或 {% %} 裡加減乘除（格式字串 "{:+,}" 與比較不算）
    for expr in re.findall(r"\{\{(.*?)\}\}|\{%(.*?)%\}", text):
        code = "".join(expr)
        code = re.sub(r'"[^"]*"', "", code)  # 去掉字串
        assert not re.search(r"\w\s*[-*/]\s*\w", code), code
        assert " + " not in code, code
