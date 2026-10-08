"""accounts：科目表（規格 6.1）。

帳戶（現金、郵局、信用卡）和分類（餐飲、薪資）都是這張表的一筆資料。
- 五大類只是 account_class 的值，不是資料列（決策 D1）；parent_id 為 NULL 的就是第一層科目。
- 「最末層」不存欄位，定義為「沒有任何子科目」（決策 D3），由服務層推導。
- 不存餘額欄位，餘額一律由 postings 加總（規格 5.3）。
"""

from sqlalchemy import CheckConstraint, ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import (
    AccountClass,
    AccountSubtype,
    SystemAccountKey,
    TaxCategory,
    sql_in,
)


class Account(TimestampMixin, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint(sql_in("account_class", AccountClass), name="account_class"),
        CheckConstraint(f"subtype IS NULL OR {sql_in('subtype', AccountSubtype)}", name="subtype"),
        # 子類型只用於資產、負債
        CheckConstraint(
            "subtype IS NULL OR account_class IN ('ASSET', 'LIABILITY')",
            name="subtype_only_asset_liability",
        ),
        CheckConstraint(
            f"tax_category IS NULL OR {sql_in('tax_category', TaxCategory)}",
            name="tax_category",
        ),
        CheckConstraint(
            f"system_key IS NULL OR {sql_in('system_key', SystemAccountKey)}",
            name="system_key",
        ),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_own_parent"),
        CheckConstraint("is_active IN (0, 1)", name="is_active_bool"),
        CheckConstraint("show_on_home IN (0, 1)", name="show_on_home_bool"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(unique=True)  # 例如 5101，用於排序
    name: Mapped[str]
    account_class: Mapped[str]
    subtype: Mapped[str | None]
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), index=True)
    system_key: Mapped[str | None] = mapped_column(unique=True)
    tax_category: Mapped[str | None]
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("1"))
    show_on_home: Mapped[bool] = mapped_column(default=False, server_default=text("0"))
    sort_order: Mapped[int] = mapped_column(default=0, server_default=text("0"))

    parent: Mapped["Account | None"] = relationship(
        back_populates="children", remote_side=lambda: [Account.id]
    )
    children: Mapped[list["Account"]] = relationship(back_populates="parent")

    def __repr__(self) -> str:
        return f"<Account id={self.id} code={self.code!r} name={self.name!r}>"
