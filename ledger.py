"""多币种应收子账：外币未清项核销与期末汇兑重估。

本位币 USD，外币 EUR/GBP/CAD。金额与汇率一律使用 Decimal，
总账余额只由凭证行汇总，未清项状态单独维护。
"""

from __future__ import annotations

import sqlite3
from datetime import date as _date
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
RATE_QUANT = Decimal("0.000001")

BASE_CURRENCY = "USD"
FOREIGN_CURRENCIES = ("EUR", "GBP", "CAD")

REVENUE_ACCOUNT = "REVENUE"
FX_REALIZED_ACCOUNT = "FX_REALIZED"
FX_UNREALIZED_ACCOUNT = "FX_UNREALIZED"


def ar_account(currency: str) -> str:
    return f"AR:{currency}"


def bank_account(currency: str) -> str:
    return f"BANK:{currency}"


ALL_ACCOUNTS = tuple(
    [ar_account(c) for c in FOREIGN_CURRENCIES]
    + [bank_account(c) for c in FOREIGN_CURRENCIES]
    + [REVENUE_ACCOUNT, FX_REALIZED_ACCOUNT, FX_UNREALIZED_ACCOUNT]
)


class LedgerError(Exception):
    """所有被拒绝的操作抛出此异常，且不留下任何状态变化。"""


class Ledger:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    # -- 基础设施 ---------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS rates (
                    currency       TEXT NOT NULL,
                    effective_date TEXT NOT NULL,
                    rate           TEXT NOT NULL,
                    PRIMARY KEY (currency, effective_date)
                );
                CREATE TABLE IF NOT EXISTS entries (
                    id   INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    memo TEXT
                );
                CREATE TABLE IF NOT EXISTS lines (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_id       INTEGER NOT NULL REFERENCES entries(id),
                    account        TEXT NOT NULL,
                    debit_cents    INTEGER NOT NULL DEFAULT 0,
                    credit_cents   INTEGER NOT NULL DEFAULT 0,
                    foreign_amount TEXT
                );
                CREATE TABLE IF NOT EXISTS items (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    customer        TEXT NOT NULL,
                    currency        TEXT NOT NULL,
                    kind            TEXT NOT NULL,
                    date            TEXT NOT NULL,
                    foreign_orig    TEXT NOT NULL,
                    foreign_balance TEXT NOT NULL,
                    book_orig_cents INTEGER NOT NULL,
                    book_cents      INTEGER NOT NULL,
                    entry_id        INTEGER NOT NULL REFERENCES entries(id),
                    closed          INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS settlements (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_id       INTEGER NOT NULL REFERENCES entries(id),
                    customer       TEXT NOT NULL,
                    currency       TEXT NOT NULL,
                    date           TEXT NOT NULL,
                    cash_foreign   TEXT NOT NULL,
                    prepay_item_id INTEGER REFERENCES items(id),
                    reversed       INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS settlement_allocs (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    settlement_id   INTEGER NOT NULL REFERENCES settlements(id),
                    item_id         INTEGER NOT NULL REFERENCES items(id),
                    foreign_amount  TEXT NOT NULL,
                    book_delta_cents INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )

    # -- 校验与换算辅助 ---------------------------------------------------

    @staticmethod
    def _decimal(value, what: str) -> Decimal:
        if isinstance(value, (bool, float)) or value is None:
            raise LedgerError(f"{what}: 不允许的类型 {type(value).__name__}")
        try:
            dec = Decimal(value)
        except Exception:
            raise LedgerError(f"{what}: 无法解析为十进制数: {value!r}") from None
        if not dec.is_finite():
            raise LedgerError(f"{what}: 数值必须有限")
        return dec

    @classmethod
    def _money(cls, value, what: str) -> Decimal:
        dec = cls._decimal(value, what)
        quantized = dec.quantize(CENT)
        if quantized != dec:
            raise LedgerError(f"{what}: 金额最多两位小数")
        return quantized

    @classmethod
    def _rate_value(cls, value) -> Decimal:
        dec = cls._decimal(value, "rate")
        quantized = dec.quantize(RATE_QUANT)
        if quantized != dec:
            raise LedgerError("rate: 汇率最多 6 位小数")
        if dec <= 0:
            raise LedgerError("rate: 汇率必须为正")
        return quantized

    @staticmethod
    def _check_date(value: str) -> str:
        if not isinstance(value, str):
            raise LedgerError("日期必须是 YYYY-MM-DD 字符串")
        try:
            _date.fromisoformat(value)
        except ValueError:
            raise LedgerError(f"非法日期: {value!r}") from None
        return value

    @staticmethod
    def _check_currency(currency: str) -> str:
        if currency not in FOREIGN_CURRENCIES:
            raise LedgerError(f"不支持的币种: {currency!r}")
        return currency

    @staticmethod
    def _check_customer(customer: str) -> str:
        if not isinstance(customer, str) or not customer:
            raise LedgerError("customer 必须是非空字符串")
        return customer

    @staticmethod
    def _cents(usd: Decimal) -> int:
        return int(usd.quantize(CENT, rounding=ROUND_HALF_UP) * 100)

    @staticmethod
    def _round_cent(value: Decimal) -> Decimal:
        return value.quantize(CENT, rounding=ROUND_HALF_UP)

    def _rate_at(self, currency: str, day: str) -> Decimal:
        row = self._conn.execute(
            "SELECT rate FROM rates WHERE currency = ? AND effective_date <= ?"
            " ORDER BY effective_date DESC LIMIT 1",
            (currency, day),
        ).fetchone()
        if row is None:
            raise LedgerError(f"{currency} 在 {day} 没有可用汇率")
        return Decimal(row["rate"])

    def _revalued_through(self) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'revalued_through'"
        ).fetchone()
        return row["value"] if row else None

    def _check_posting_date(self, day: str) -> None:
        through = self._revalued_through()
        if through is not None and day <= through:
            raise LedgerError(f"日期 {day} 不晚于已重估截止日 {through}，拒绝过账")

    def _insert_entry(self, date: str, kind: str, memo: str, lines: list[tuple]) -> int:
        """lines: (account, debit_cents, credit_cents, foreign_amount|None)"""
        total_dr = sum(line[1] for line in lines)
        total_cr = sum(line[2] for line in lines)
        if total_dr != total_cr:
            raise AssertionError(f"凭证不平衡: 借 {total_dr} 贷 {total_cr}")
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO entries(date, kind, memo) VALUES (?, ?, ?)",
                (date, kind, memo),
            )
            entry_id = cur.lastrowid
            self._conn.executemany(
                "INSERT INTO lines(entry_id, account, debit_cents, credit_cents,"
                " foreign_amount) VALUES (?, ?, ?, ?, ?)",
                [(entry_id, acc, dr, cr, fx) for acc, dr, cr, fx in lines],
            )
        return entry_id

    # -- 公共 API ---------------------------------------------------------

    def set_rate(self, currency: str, effective_date: str, rate) -> None:
        currency = self._check_currency(currency)
        effective_date = self._check_date(effective_date)
        rate = self._rate_value(rate)
        with self._conn:
            self._conn.execute(
                "INSERT INTO rates(currency, effective_date, rate) VALUES (?, ?, ?)"
                " ON CONFLICT(currency, effective_date) DO UPDATE SET rate = excluded.rate",
                (currency, effective_date, str(rate)),
            )

    def post_document(self, customer: str, kind: str, currency: str, amount, date: str) -> int:
        customer = self._check_customer(customer)
        currency = self._check_currency(currency)
        date = self._check_date(date)
        amount = self._money(amount, "amount")
        if amount <= 0:
            raise LedgerError("amount: 凭证金额必须为正")
        if kind not in ("invoice", "credit_note"):
            raise LedgerError(f"未知凭证类型: {kind!r}")
        self._check_posting_date(date)
        rate = self._rate_at(currency, date)
        usd_cents = self._cents(amount * rate)
        ar = ar_account(currency)
        if kind == "invoice":
            foreign, book = amount, usd_cents
            lines = [(ar, usd_cents, 0, str(amount)), (REVENUE_ACCOUNT, 0, usd_cents, None)]
        else:
            foreign, book = -amount, -usd_cents
            lines = [(REVENUE_ACCOUNT, usd_cents, 0, None), (ar, 0, usd_cents, str(-amount))]
        entry_id = self._insert_entry(date, "document", f"{kind} {customer}", lines)
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO items(customer, currency, kind, date, foreign_orig,"
                " foreign_balance, book_orig_cents, book_cents, entry_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (customer, currency, kind, date, str(foreign), str(foreign),
                 book, book, entry_id),
            )
            return cur.lastrowid

    def settle(self, customer: str, currency: str, date: str, cash, allocations) -> int:
        customer = self._check_customer(customer)
        currency = self._check_currency(currency)
        date = self._check_date(date)
        cash = self._money(cash, "cash")
        if cash < 0:
            raise LedgerError("cash: 现金不能为负")
        self._check_posting_date(date)
        rate = self._rate_at(currency, date)

        allocs: dict[int, tuple[Decimal, sqlite3.Row]] = {}
        for item_id, raw in (allocations or {}).items():
            amount = self._money(raw, "allocation")
            if amount == 0:
                raise LedgerError("allocation: 核销额不能为零")
            row = self._conn.execute(
                "SELECT * FROM items WHERE id = ? AND closed = 0", (item_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(f"未清项不存在: {item_id!r}")
            if row["customer"] != customer or row["currency"] != currency:
                raise LedgerError(f"未清项 {item_id} 不属于该客户或币种不符")
            balance = Decimal(row["foreign_balance"])
            if balance == 0:
                raise LedgerError(f"未清项 {item_id} 余额已为零")
            if (amount < 0) != (balance < 0):
                raise LedgerError(f"未清项 {item_id} 核销方向与余额符号不符")
            if abs(amount) > abs(balance):
                raise LedgerError(f"未清项 {item_id} 核销额超过剩余余额")
            if item_id in allocs:
                raise LedgerError(f"未清项 {item_id} 重复分配")
            allocs[item_id] = (amount, row)

        net = sum((amount for amount, _ in allocs.values()), Decimal("0.00"))
        if net < 0:
            raise LedgerError("分配净额不能为负")
        if net > cash:
            raise LedgerError("分配净额不能超过收到现金")
        prepay = cash - net

        bank_cents = self._cents(cash * rate)
        prepay_cents = self._cents(prepay * rate)
        ar = ar_account(currency)

        lines: list[tuple] = []
        if bank_cents or cash:
            lines.append((bank_account(currency), bank_cents, 0, str(cash)))
        deltas: dict[int, int] = {}
        for item_id, (amount, row) in allocs.items():
            balance = Decimal(row["foreign_balance"])
            book = row["book_cents"]
            if amount == balance:
                delta = -book
            else:
                magnitude = (Decimal(book) * amount / balance).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
                delta = -int(magnitude)
            deltas[item_id] = delta
            if delta < 0:
                lines.append((ar, 0, -delta, str(-amount)))
            elif delta > 0:
                lines.append((ar, delta, 0, str(-amount)))
            else:
                lines.append((ar, 0, 0, str(-amount)))
        if prepay > 0:
            lines.append((ar, 0, prepay_cents, str(-prepay)))

        total_dr = sum(line[1] for line in lines)
        total_cr = sum(line[2] for line in lines)
        if total_dr > total_cr:
            lines.append((FX_REALIZED_ACCOUNT, 0, total_dr - total_cr, None))
        elif total_cr > total_dr:
            lines.append((FX_REALIZED_ACCOUNT, total_cr - total_dr, 0, None))

        entry_id = self._insert_entry(date, "settlement", f"settle {customer}", lines)
        with self._conn:
            prepay_item_id = None
            if prepay > 0:
                cur = self._conn.execute(
                    "INSERT INTO items(customer, currency, kind, date, foreign_orig,"
                    " foreign_balance, book_orig_cents, book_cents, entry_id)"
                    " VALUES (?, ?, 'prepayment', ?, ?, ?, ?, ?, ?)",
                    (customer, currency, date, str(-prepay), str(-prepay),
                     -prepay_cents, -prepay_cents, entry_id),
                )
                prepay_item_id = cur.lastrowid
            cur = self._conn.execute(
                "INSERT INTO settlements(entry_id, customer, currency, date,"
                " cash_foreign, prepay_item_id) VALUES (?, ?, ?, ?, ?, ?)",
                (entry_id, customer, currency, date, str(cash), prepay_item_id),
            )
            settlement_id = cur.lastrowid
            for item_id, (amount, row) in allocs.items():
                delta = deltas[item_id]
                new_balance = Decimal(row["foreign_balance"]) - amount
                self._conn.execute(
                    "UPDATE items SET foreign_balance = ?, book_cents = book_cents + ?"
                    " WHERE id = ?",
                    (str(new_balance), delta, item_id),
                )
                self._conn.execute(
                    "INSERT INTO settlement_allocs(settlement_id, item_id,"
                    " foreign_amount, book_delta_cents) VALUES (?, ?, ?, ?)",
                    (settlement_id, item_id, str(amount), delta),
                )
        return settlement_id

    def revalue(self, period_end: str):
        period_end = self._check_date(period_end)
        self._check_posting_date(period_end)
        rows = self._conn.execute(
            "SELECT * FROM items WHERE foreign_balance <> '0.00' AND closed = 0"
            " ORDER BY id"
        ).fetchall()
        plans = []
        for row in rows:
            rate = self._rate_at(row["currency"], period_end)
            target = self._cents(Decimal(row["foreign_balance"]) * rate)
            delta = target - row["book_cents"]
            if delta:
                plans.append((row["id"], row["currency"], target, delta))
        lines: list[tuple] = []
        for _item_id, currency, _target, delta in plans:
            ar = ar_account(currency)
            if delta > 0:
                lines.append((ar, delta, 0, None))
                lines.append((FX_UNREALIZED_ACCOUNT, 0, delta, None))
            else:
                lines.append((ar, 0, -delta, None))
                lines.append((FX_UNREALIZED_ACCOUNT, -delta, 0, None))
        entry_id = None
        if lines:
            entry_id = self._insert_entry(period_end, "revaluation", "revalue", lines)
        with self._conn:
            for item_id, _currency, target, _delta in plans:
                self._conn.execute(
                    "UPDATE items SET book_cents = ? WHERE id = ?", (target, item_id)
                )
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('revalued_through', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (period_end,),
            )
        return entry_id

    def reverse_settlement(self, settlement_id: int, date: str) -> int:
        date = self._check_date(date)
        self._check_posting_date(date)
        st = self._conn.execute(
            "SELECT * FROM settlements WHERE id = ?", (settlement_id,)
        ).fetchone()
        if st is None:
            raise LedgerError(f"核销单不存在: {settlement_id!r}")
        if st["reversed"]:
            raise LedgerError(f"核销单 {settlement_id} 已被撤销")
        prepay_item = None
        if st["prepay_item_id"] is not None:
            prepay_item = self._conn.execute(
                "SELECT * FROM items WHERE id = ?", (st["prepay_item_id"],)
            ).fetchone()
            if prepay_item["foreign_balance"] != prepay_item["foreign_orig"]:
                raise LedgerError("该核销产生的预收已被后续核销使用，无法撤销")

        orig_lines = self._conn.execute(
            "SELECT account, debit_cents, credit_cents, foreign_amount"
            " FROM lines WHERE entry_id = ?",
            (st["entry_id"],),
        ).fetchall()
        lines: list[tuple] = []
        for row in orig_lines:
            foreign = row["foreign_amount"]
            lines.append((
                row["account"], row["credit_cents"], row["debit_cents"],
                str(-Decimal(foreign)) if foreign is not None else None,
            ))
        if prepay_item is not None:
            unwind = prepay_item["book_cents"] - prepay_item["book_orig_cents"]
            if unwind:
                ar = ar_account(st["currency"])
                if unwind > 0:
                    lines.append((ar, 0, unwind, None))
                    lines.append((FX_UNREALIZED_ACCOUNT, unwind, 0, None))
                else:
                    lines.append((ar, -unwind, 0, None))
                    lines.append((FX_UNREALIZED_ACCOUNT, 0, -unwind, None))

        entry_id = self._insert_entry(
            date, "reversal", f"reverse settlement {settlement_id}", lines
        )
        allocs = self._conn.execute(
            "SELECT * FROM settlement_allocs WHERE settlement_id = ?",
            (settlement_id,),
        ).fetchall()
        with self._conn:
            for alloc in allocs:
                item = self._conn.execute(
                    "SELECT foreign_balance FROM items WHERE id = ?",
                    (alloc["item_id"],),
                ).fetchone()
                restored = Decimal(item["foreign_balance"]) + Decimal(alloc["foreign_amount"])
                self._conn.execute(
                    "UPDATE items SET foreign_balance = ?, book_cents = book_cents - ?"
                    " WHERE id = ?",
                    (str(restored), alloc["book_delta_cents"], alloc["item_id"]),
                )
            if prepay_item is not None:
                self._conn.execute(
                    "UPDATE items SET foreign_balance = '0.00', book_cents = 0,"
                    " closed = 1 WHERE id = ?",
                    (prepay_item["id"],),
                )
            self._conn.execute(
                "UPDATE settlements SET reversed = 1 WHERE id = ?", (settlement_id,)
            )
        return entry_id

    def open_items(self, customer: str | None = None) -> list[dict]:
        sql = ("SELECT id, customer, currency, kind, date, foreign_balance, book_cents"
               " FROM items WHERE foreign_balance <> '0.00' AND closed = 0")
        params: tuple = ()
        if customer is not None:
            sql += " AND customer = ?"
            params = (customer,)
        sql += " ORDER BY id"
        return [
            {
                "id": row["id"],
                "customer": row["customer"],
                "currency": row["currency"],
                "kind": row["kind"],
                "date": row["date"],
                "foreign_balance": Decimal(row["foreign_balance"]),
                "book_value": Decimal(row["book_cents"]) / 100,
            }
            for row in self._conn.execute(sql, params)
        ]

    def trial_balance(self) -> dict[str, Decimal]:
        result = {account: Decimal("0.00") for account in ALL_ACCOUNTS}
        for row in self._conn.execute(
            "SELECT account, SUM(debit_cents) AS dr, SUM(credit_cents) AS cr"
            " FROM lines GROUP BY account"
        ):
            result[row["account"]] = (Decimal(row["dr"]) - Decimal(row["cr"])) / 100
        return result
