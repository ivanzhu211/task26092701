"""Multi-currency AR subledger with FX revaluation, backed by SQLite.

Base currency USD; foreign currencies EUR, GBP, CAD.
All money is handled with decimal.Decimal (never binary floats);
SQLite stores integer cents. The general ledger is derived purely by
summing journal lines; open-item state is maintained separately.
Journals are append-only: reversals and revaluations are new entries.
"""

from __future__ import annotations

import sqlite3
from datetime import date as _date
from decimal import Decimal, ROUND_HALF_UP

BASE_CURRENCY = "USD"
CURRENCIES = ("USD", "EUR", "GBP", "CAD")

CENT = Decimal("0.01")
ZERO = Decimal("0")

ACC_REVENUE = "Revenue"
ACC_FX_REALIZED = "FX:Realized"
ACC_FX_UNREALIZED = "FX:Unrealized"


def acc_ar(currency: str) -> str:
    return f"AR:{currency}"


def acc_bank(currency: str) -> str:
    return f"Bank:{currency}"


class LedgerError(Exception):
    """Raised when an operation is rejected; no state is changed."""


def _parse_date(value, field: str = "date") -> str:
    if isinstance(value, _date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return _date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    raise LedgerError(f"invalid {field}: {value!r}")


def _parse_money(value, field: str = "amount") -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise LedgerError(f"{field} must not be a float/bool: {value!r}")
    try:
        dec = Decimal(str(value))
    except Exception:
        raise LedgerError(f"invalid {field}: {value!r}") from None
    if not dec.is_finite():
        raise LedgerError(f"invalid {field}: {value!r}")
    if dec != dec.quantize(CENT):
        raise LedgerError(f"{field} must have at most 2 decimal places: {value!r}")
    return dec


def _parse_rate(value) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise LedgerError(f"rate must not be a float/bool: {value!r}")
    try:
        dec = Decimal(str(value))
    except Exception:
        raise LedgerError(f"invalid rate: {value!r}") from None
    if not dec.is_finite() or dec <= 0:
        raise LedgerError(f"rate must be positive: {value!r}")
    if dec != dec.quantize(Decimal("0.000001")):
        raise LedgerError(f"rate must have at most 6 decimal places: {value!r}")
    return dec


def _to_cents(dec: Decimal) -> int:
    return int(dec * 100)


def _to_decimal(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(CENT)


def _round_cent(dec: Decimal) -> Decimal:
    # round half away from zero to cents
    return dec.quantize(CENT, rounding=ROUND_HALF_UP)


class Ledger:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rates (
                currency       TEXT NOT NULL,
                effective_date TEXT NOT NULL,
                rate           TEXT NOT NULL,
                PRIMARY KEY (currency, effective_date)
            );
            CREATE TABLE IF NOT EXISTS entries (
                id   INTEGER PRIMARY KEY,
                date TEXT NOT NULL,
                kind TEXT NOT NULL,
                memo TEXT
            );
            CREATE TABLE IF NOT EXISTS lines (
                entry_id    INTEGER NOT NULL REFERENCES entries(id),
                account     TEXT NOT NULL,
                debit_cents  INTEGER NOT NULL DEFAULT 0,
                credit_cents INTEGER NOT NULL DEFAULT 0,
                fc_currency  TEXT,
                fc_cents     INTEGER
            );
            CREATE TABLE IF NOT EXISTS open_items (
                id           INTEGER PRIMARY KEY,
                customer     TEXT NOT NULL,
                kind         TEXT NOT NULL,
                currency     TEXT NOT NULL,
                doc_date     TEXT NOT NULL,
                fc_cents     INTEGER NOT NULL,
                book_cents   INTEGER NOT NULL,
                origin_entry INTEGER NOT NULL REFERENCES entries(id)
            );
            CREATE TABLE IF NOT EXISTS settlements (
                id             INTEGER PRIMARY KEY,
                entry_id       INTEGER NOT NULL REFERENCES entries(id),
                customer       TEXT NOT NULL,
                currency       TEXT NOT NULL,
                cash_fc_cents  INTEGER NOT NULL,
                prepay_item_id INTEGER,
                reversed       INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settlement_allocs (
                settlement_id INTEGER NOT NULL REFERENCES settlements(id),
                item_id       INTEGER NOT NULL,
                fc_cents      INTEGER NOT NULL,
                book_cents    INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- internal helpers ----

    def _locked_until(self) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'locked_until'"
        ).fetchone()
        return row[0] if row else None

    def _check_not_locked(self, date: str) -> None:
        locked = self._locked_until()
        if locked is not None and date <= locked:
            raise LedgerError(
                f"date {date} is not after revalued period end {locked}"
            )

    def _rate_on(self, currency: str, date: str) -> Decimal:
        if currency == BASE_CURRENCY:
            return Decimal(1)
        row = self._conn.execute(
            "SELECT rate FROM rates WHERE currency = ? AND effective_date <= ?"
            " ORDER BY effective_date DESC LIMIT 1",
            (currency, date),
        ).fetchone()
        if row is None:
            raise LedgerError(f"no {currency} rate effective on or before {date}")
        return Decimal(row[0])

    def _post_entry(
        self,
        date: str,
        kind: str,
        memo: str,
        lines: list[tuple[str, int, str | None, int | None]],
    ) -> int:
        """lines: (account, signed_debit_positive_cents, fc_currency, fc_cents).

        Signed cents: positive -> debit, negative -> credit.
        """
        total = sum(signed for _, signed, _, _ in lines)
        if total != 0:
            raise AssertionError(f"unbalanced entry: {total}")
        cur = self._conn.execute(
            "INSERT INTO entries(date, kind, memo) VALUES (?, ?, ?)",
            (date, kind, memo),
        )
        entry_id = cur.lastrowid
        for account, signed, fc_cur, fc_cents in lines:
            if signed == 0 and not fc_cents:
                continue
            debit = signed if signed > 0 else 0
            credit = -signed if signed < 0 else 0
            self._conn.execute(
                "INSERT INTO lines(entry_id, account, debit_cents, credit_cents,"
                " fc_currency, fc_cents) VALUES (?, ?, ?, ?, ?, ?)",
                (entry_id, account, debit, credit, fc_cur, fc_cents),
            )
        return entry_id

    # ---- public API ----

    def set_rate(self, currency: str, effective_date: str, rate) -> None:
        if currency not in CURRENCIES:
            raise LedgerError(f"unknown currency: {currency!r}")
        if currency == BASE_CURRENCY:
            raise LedgerError("base currency rate is fixed at 1")
        eff = _parse_date(effective_date, "effective_date")
        dec = _parse_rate(rate)
        with self._conn:
            self._conn.execute(
                "INSERT INTO rates(currency, effective_date, rate) VALUES (?, ?, ?)"
                " ON CONFLICT(currency, effective_date)"
                " DO UPDATE SET rate = excluded.rate",
                (currency, eff, str(dec)),
            )

    def post_document(self, customer: str, kind: str, currency: str, amount, date: str) -> int:
        if kind not in ("invoice", "credit_note"):
            raise LedgerError(f"unknown document kind: {kind!r}")
        if currency not in CURRENCIES:
            raise LedgerError(f"unknown currency: {currency!r}")
        if not customer:
            raise LedgerError("customer must be non-empty")
        doc_date = _parse_date(date)
        amt = _parse_money(amount)
        if amt <= 0:
            raise LedgerError("amount must be positive")
        self._check_not_locked(doc_date)
        rate = self._rate_on(currency, doc_date)
        usd = _round_cent(amt * rate)
        amt_cents = _to_cents(amt)
        usd_cents = _to_cents(usd)
        sign = 1 if kind == "invoice" else -1
        with self._conn:
            lines = [
                (acc_ar(currency), sign * usd_cents, currency, sign * amt_cents),
                (ACC_REVENUE, -sign * usd_cents, None, None),
            ]
            entry_id = self._post_entry(
                doc_date, "document", f"{kind} {customer} {currency} {amt}", lines
            )
            cur = self._conn.execute(
                "INSERT INTO open_items(customer, kind, currency, doc_date,"
                " fc_cents, book_cents, origin_entry) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    customer,
                    kind,
                    currency,
                    doc_date,
                    sign * amt_cents,
                    sign * usd_cents,
                    entry_id,
                ),
            )
            return cur.lastrowid

    def settle(self, customer: str, currency: str, date: str, cash, allocations: dict) -> int:
        if currency not in CURRENCIES:
            raise LedgerError(f"unknown currency: {currency!r}")
        if not customer:
            raise LedgerError("customer must be non-empty")
        stl_date = _parse_date(date)
        cash_dec = _parse_money(cash, "cash")
        if cash_dec < 0:
            raise LedgerError("cash must be >= 0")
        if not isinstance(allocations, dict):
            raise LedgerError("allocations must be a dict of item_id -> fc amount")
        self._check_not_locked(stl_date)
        rate = self._rate_on(currency, stl_date)

        # validate allocations before touching any state
        allocs: list[tuple[int, int, int, int]] = []  # item_id, alloc_fc, cur_fc, cur_book
        total_alloc = ZERO
        for item_id, raw in allocations.items():
            alloc = _parse_money(raw, "allocation")
            if alloc == 0:
                raise LedgerError("allocation must be non-zero")
            row = self._conn.execute(
                "SELECT customer, currency, fc_cents, book_cents FROM open_items"
                " WHERE id = ?",
                (item_id,),
            ).fetchone()
            if row is None:
                raise LedgerError(f"unknown open item: {item_id!r}")
            if row[0] != customer or row[1] != currency:
                raise LedgerError(f"open item {item_id} does not belong to"
                                  f" {customer}/{currency}")
            cur_fc, cur_book = row[2], row[3]
            alloc_cents = _to_cents(alloc)
            if cur_fc == 0:
                raise LedgerError(f"open item {item_id} is fully settled")
            if (cur_fc > 0) != (alloc_cents > 0):
                raise LedgerError(f"allocation sign mismatch on item {item_id}")
            if abs(alloc_cents) > abs(cur_fc):
                raise LedgerError(f"allocation exceeds balance of item {item_id}")
            allocs.append((item_id, alloc_cents, cur_fc, cur_book))
            total_alloc += alloc
        if total_alloc < 0 or total_alloc > cash_dec:
            raise LedgerError("sum of allocations must satisfy 0 <= sum <= cash")
        if not allocs and cash_dec == 0:
            raise LedgerError("nothing to settle: cash is 0 and no allocations")

        cash_cents = _to_cents(cash_dec)
        cash_usd = _to_cents(_round_cent(cash_dec * rate))
        prepay_fc = cash_cents - _to_cents(total_alloc)
        prepay_usd = _to_cents(_round_cent((cash_dec - total_alloc) * rate))

        lines: list[tuple[str, int, str | None, int | None]] = []
        if cash_usd or cash_cents:
            lines.append((acc_bank(currency), cash_usd, currency, cash_cents))
        ar_signed = 0
        item_deltas: list[tuple[int, int, int]] = []  # item_id, alloc_fc, book_removed
        for item_id, alloc_cents, cur_fc, cur_book in allocs:
            if alloc_cents == cur_fc:
                book_removed = cur_book
            else:
                book_removed = int(
                    (Decimal(cur_book) * Decimal(alloc_cents) / Decimal(cur_fc))
                    .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                )
            item_deltas.append((item_id, alloc_cents, book_removed))
            ar_signed -= book_removed
        if prepay_fc:
            ar_signed -= prepay_usd
        if ar_signed:
            ar_fc = -(sum(a[1] for a in item_deltas) + prepay_fc)
            lines.append((acc_ar(currency), ar_signed, currency, ar_fc))
        realized = -(cash_usd + ar_signed)
        if realized:
            lines.append((ACC_FX_REALIZED, realized, None, None))

        with self._conn:
            entry_id = self._post_entry(
                stl_date, "settlement",
                f"settle {customer} {currency} cash {cash_dec}", lines,
            )
            cur = self._conn.execute(
                "INSERT INTO settlements(entry_id, customer, currency,"
                " cash_fc_cents) VALUES (?, ?, ?, ?)",
                (entry_id, customer, currency, cash_cents),
            )
            stl_id = cur.lastrowid
            for item_id, alloc_cents, book_removed in item_deltas:
                self._conn.execute(
                    "UPDATE open_items SET fc_cents = fc_cents - ?,"
                    " book_cents = book_cents - ? WHERE id = ?",
                    (alloc_cents, book_removed, item_id),
                )
                self._conn.execute(
                    "INSERT INTO settlement_allocs(settlement_id, item_id,"
                    " fc_cents, book_cents) VALUES (?, ?, ?, ?)",
                    (stl_id, item_id, alloc_cents, book_removed),
                )
            if prepay_fc:
                cur = self._conn.execute(
                    "INSERT INTO open_items(customer, kind, currency, doc_date,"
                    " fc_cents, book_cents, origin_entry)"
                    " VALUES (?, 'prepayment', ?, ?, ?, ?, ?)",
                    (customer, currency, stl_date, -prepay_fc, -prepay_usd, entry_id),
                )
                self._conn.execute(
                    "UPDATE settlements SET prepay_item_id = ? WHERE id = ?",
                    (cur.lastrowid, stl_id),
                )
            return stl_id

    def revalue(self, period_end: str) -> None:
        end = _parse_date(period_end, "period_end")
        locked = self._locked_until()
        if locked is not None and end <= locked:
            raise LedgerError(f"period end {end} must be after {locked}")
        # collect adjustments first; any missing rate rejects the whole operation
        per_currency: dict[str, list[tuple[int, int]]] = {}
        for currency in CURRENCIES:
            if currency == BASE_CURRENCY:
                continue
            rows = self._conn.execute(
                "SELECT id, fc_cents, book_cents FROM open_items"
                " WHERE currency = ? AND fc_cents != 0",
                (currency,),
            ).fetchall()
            if not rows:
                continue
            rate = self._rate_on(currency, end)
            deltas = []
            for item_id, fc_cents, book_cents in rows:
                new_book = _to_cents(_round_cent(_to_decimal(fc_cents) * rate))
                delta = new_book - book_cents
                if delta:
                    deltas.append((item_id, delta))
            per_currency[currency] = deltas
        with self._conn:
            for currency, deltas in per_currency.items():
                if not deltas:
                    continue
                total = sum(d for _, d in deltas)
                entry_id = self._post_entry(
                    end, "revaluation", f"revalue {currency} @ {end}",
                    [(acc_ar(currency), total, None, None),
                     (ACC_FX_UNREALIZED, -total, None, None)],
                )
                for item_id, delta in deltas:
                    self._conn.execute(
                        "UPDATE open_items SET book_cents = book_cents + ?"
                        " WHERE id = ?",
                        (delta, item_id),
                    )
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('locked_until', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (end,),
            )

    def reverse_settlement(self, settlement_id: int, date: str) -> None:
        rev_date = _parse_date(date)
        self._check_not_locked(rev_date)
        row = self._conn.execute(
            "SELECT entry_id, customer, currency, cash_fc_cents, prepay_item_id,"
            " reversed FROM settlements WHERE id = ?",
            (settlement_id,),
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown settlement: {settlement_id!r}")
        entry_id, customer, currency, cash_fc, prepay_item_id, reversed_ = row
        if reversed_:
            raise LedgerError(f"settlement {settlement_id} already reversed")
        allocs = self._conn.execute(
            "SELECT item_id, fc_cents, book_cents FROM settlement_allocs"
            " WHERE settlement_id = ?",
            (settlement_id,),
        ).fetchall()

        prepay_fc = 0
        prepay_book = 0
        if prepay_item_id is not None:
            prow = self._conn.execute(
                "SELECT fc_cents, book_cents FROM open_items WHERE id = ?",
                (prepay_item_id,),
            ).fetchone()
            if prow is None:
                raise LedgerError("prepayment item missing")
            prepay_fc, prepay_book = prow
            # original prepay amount = cash - sum(allocations)
            alloc_sum = sum(a[1] for a in allocs)
            if prepay_fc != -(cash_fc - alloc_sum):
                raise LedgerError(
                    "prepayment from this settlement has been used;"
                    " reversal rejected"
                )

        cash_usd_row = self._conn.execute(
            "SELECT debit_cents FROM lines WHERE entry_id = ? AND account = ?",
            (entry_id, acc_bank(currency)),
        ).fetchone()
        cash_usd = cash_usd_row[0] if cash_usd_row else 0

        lines: list[tuple[str, int, str | None, int | None]] = []
        if cash_usd or cash_fc:
            lines.append((acc_bank(currency), -cash_usd, currency, -cash_fc))
        ar_signed = 0
        for item_id, alloc_fc, alloc_book in allocs:
            ar_signed += alloc_book
        if prepay_item_id is not None:
            ar_signed -= prepay_book  # removing a credit item debits AR
        if ar_signed:
            lines.append((acc_ar(currency), ar_signed, currency, cash_fc))
        realized = -(sum(l[1] for l in lines))
        if realized:
            lines.append((ACC_FX_REALIZED, realized, None, None))

        with self._conn:
            self._post_entry(
                rev_date, "reversal",
                f"reverse settlement {settlement_id}", lines,
            )
            for item_id, alloc_fc, alloc_book in allocs:
                self._conn.execute(
                    "UPDATE open_items SET fc_cents = fc_cents + ?,"
                    " book_cents = book_cents + ? WHERE id = ?",
                    (alloc_fc, alloc_book, item_id),
                )
            if prepay_item_id is not None:
                self._conn.execute(
                    "DELETE FROM open_items WHERE id = ?", (prepay_item_id,)
                )
            self._conn.execute(
                "UPDATE settlements SET reversed = 1 WHERE id = ?",
                (settlement_id,),
            )

    def open_items(self, customer: str | None = None) -> list[dict]:
        sql = ("SELECT id, customer, kind, currency, doc_date, fc_cents,"
               " book_cents FROM open_items WHERE fc_cents != 0")
        params: tuple = ()
        if customer is not None:
            sql += " AND customer = ?"
            params = (customer,)
        sql += " ORDER BY id"
        result = []
        for row in self._conn.execute(sql, params):
            result.append({
                "id": row[0],
                "customer": row[1],
                "kind": row[2],
                "currency": row[3],
                "date": row[4],
                "fc_balance": _to_decimal(row[5]),
                "book_value": _to_decimal(row[6]),
            })
        return result

    def trial_balance(self) -> dict[str, Decimal]:
        balances: dict[str, Decimal] = {}
        for currency in CURRENCIES:
            balances[acc_ar(currency)] = ZERO
            balances[acc_bank(currency)] = ZERO
        balances[ACC_REVENUE] = ZERO
        balances[ACC_FX_REALIZED] = ZERO
        balances[ACC_FX_UNREALIZED] = ZERO
        for account, debits, credits in self._conn.execute(
            "SELECT account, SUM(debit_cents), SUM(credit_cents)"
            " FROM lines GROUP BY account"
        ):
            balances[account] = _to_decimal(debits - credits)
        return balances
