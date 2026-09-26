import os
import random
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal

from ledger import (
    ACC_FX_REALIZED,
    ACC_FX_UNREALIZED,
    ACC_REVENUE,
    CURRENCIES,
    Ledger,
    LedgerError,
    acc_ar,
    acc_bank,
)


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "ledger.db")
        self.led = Ledger(self.db_path)

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    # ---- invariant helpers ----

    def assert_entries_balanced(self):
        """Property 1: every journal entry balances to the cent in USD."""
        rows = self.led._conn.execute(
            "SELECT entry_id, SUM(debit_cents) - SUM(credit_cents)"
            " FROM lines GROUP BY entry_id"
        ).fetchall()
        for entry_id, diff in rows:
            self.assertEqual(diff, 0, f"entry {entry_id} is unbalanced")

    def assert_ar_matches_subledger(self):
        """Property 2: AR control accounts equal open-item sums, in USD and FC."""
        tb = self.led.trial_balance()
        for currency in CURRENCIES:
            book_sum = self.led._conn.execute(
                "SELECT COALESCE(SUM(book_cents), 0) FROM open_items"
                " WHERE currency = ?",
                (currency,),
            ).fetchone()[0]
            self.assertEqual(
                tb[acc_ar(currency)],
                Decimal(book_sum) / 100,
                f"AR:{currency} USD mismatch",
            )
            fc_lines = self.led._conn.execute(
                "SELECT COALESCE(SUM(fc_cents), 0) FROM lines"
                " WHERE account = ? AND fc_currency = ?",
                (acc_ar(currency), currency),
            ).fetchone()[0]
            fc_items = self.led._conn.execute(
                "SELECT COALESCE(SUM(fc_cents), 0) FROM open_items"
                " WHERE currency = ?",
                (currency,),
            ).fetchone()[0]
            self.assertEqual(
                fc_lines, fc_items, f"AR:{currency} FC mismatch"
            )

    def assert_zero_fc_zero_book(self):
        """Property 3: fully settled items carry exactly zero book value."""
        rows = self.led._conn.execute(
            "SELECT id, book_cents FROM open_items WHERE fc_cents = 0"
        ).fetchall()
        for item_id, book_cents in rows:
            self.assertEqual(book_cents, 0, f"item {item_id} has residual book")

    def assert_invariants(self):
        self.assert_entries_balanced()
        self.assert_ar_matches_subledger()
        self.assert_zero_fc_zero_book()
        tb = self.led.trial_balance()
        self.assertEqual(sum(tb.values()), Decimal(0), "trial balance not zero-sum")

    def realized_gain(self, settlement_id):
        """Credit-positive realized FX gain/loss booked by a settlement."""
        row = self.led._conn.execute(
            "SELECT SUM(l.credit_cents) - SUM(l.debit_cents)"
            " FROM lines l JOIN settlements s ON l.entry_id = s.entry_id"
            " WHERE s.id = ? AND l.account = ?",
            (settlement_id, ACC_FX_REALIZED),
        ).fetchone()
        return Decimal(row[0] or 0) / 100


class DeterministicScenarioTest(LedgerTestCase):
    def test_invoice_revalue_three_payments_then_reversal(self):
        led = self.led
        led.set_rate("EUR", "2026-01-01", "1.10")
        item = led.post_document("Acme", "invoice", "EUR", "1000.00", "2026-01-10")
        self.assertEqual(led.open_items()[0]["book_value"], Decimal("1100.00"))
        self.assert_invariants()

        led.set_rate("EUR", "2026-01-31", "1.20")
        led.revalue("2026-01-31")
        self.assertEqual(led.open_items()[0]["book_value"], Decimal("1200.00"))
        self.assertEqual(led.trial_balance()[ACC_FX_UNREALIZED], Decimal("-100.00"))
        self.assert_invariants()

        # backdated posting must now be rejected
        with self.assertRaises(LedgerError):
            led.post_document("Acme", "invoice", "EUR", "10.00", "2026-01-31")
        with self.assertRaises(LedgerError):
            led.settle("Acme", "EUR", "2026-01-15", "1.00", {})
        self.assert_invariants()

        led.set_rate("EUR", "2026-02-01", "1.15")
        settlements = []
        for day, amount in (("2026-02-05", "333.33"),
                            ("2026-02-10", "333.33"),
                            ("2026-02-15", "333.34")):
            settlements.append(
                led.settle("Acme", "EUR", day, amount, {item: amount})
            )
            self.assert_invariants()

        realized = [self.realized_gain(s) for s in settlements]
        self.assertEqual(realized[0], Decimal("-16.67"))
        self.assertEqual(realized[1], Decimal("-16.66"))
        self.assertEqual(realized[2], Decimal("-16.67"))
        self.assertEqual(sum(realized), Decimal("-50.00"))

        # item fully settled: zero FC, exactly zero book value
        self.assertEqual(led.open_items(), [])
        self.assertEqual(led.trial_balance()[acc_ar("EUR")], Decimal("0.00"))

        # next revaluation (item already closed), then reverse settlement 2
        led.set_rate("EUR", "2026-02-28", "1.18")
        led.revalue("2026-02-28")
        self.assert_invariants()
        led.reverse_settlement(settlements[1], "2026-03-01")
        self.assert_invariants()

        items = led.open_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], item)
        self.assertEqual(items[0]["fc_balance"], Decimal("333.33"))
        self.assertEqual(items[0]["book_value"], Decimal("399.99"))

        tb = led.trial_balance()
        self.assertEqual(tb[acc_ar("EUR")], Decimal("399.99"))
        self.assertEqual(tb[acc_bank("EUR")], Decimal("766.67"))
        # realized loss of 50.00 less the reversed 16.66 -> 33.34 debit balance
        self.assertEqual(tb[ACC_FX_REALIZED], Decimal("33.34"))
        self.assertEqual(tb[ACC_FX_UNREALIZED], Decimal("-100.00"))
        self.assertEqual(tb[ACC_REVENUE], Decimal("-1100.00"))

        # state survives reopening
        led.close()
        self.led = Ledger(self.db_path)
        self.assertEqual(self.led.open_items()[0]["book_value"], Decimal("399.99"))
        self.assertEqual(self.led.trial_balance(), tb)
        self.assert_invariants()

    def test_reverse_settlement_with_revalued_prepayment(self):
        led = self.led
        led.set_rate("EUR", "2026-01-01", "1.10")
        stl = led.settle("Acme", "EUR", "2026-01-10", "100.00", {})
        items = led.open_items()
        self.assertEqual(items[0]["kind"], "prepayment")
        self.assertEqual(items[0]["book_value"], Decimal("-110.00"))

        led.set_rate("EUR", "2026-01-31", "1.20")
        led.revalue("2026-01-31")
        self.assertEqual(led.open_items()[0]["book_value"], Decimal("-120.00"))
        self.assert_invariants()

        # reversal crosses the revaluation; residual goes to realized FX
        led.reverse_settlement(stl, "2026-02-01")
        self.assertEqual(led.open_items(), [])
        tb = led.trial_balance()
        self.assertEqual(tb[acc_ar("EUR")], Decimal("0.00"))
        self.assertEqual(tb[acc_bank("EUR")], Decimal("0.00"))
        self.assertEqual(tb[ACC_FX_UNREALIZED], Decimal("10.00"))
        self.assertEqual(tb[ACC_FX_REALIZED], Decimal("-10.00"))
        self.assert_invariants()


class RandomizedScenarioTest(LedgerTestCase):
    SEED = 20260927

    def _prepay_used(self, settlement_id):
        row = self.led._conn.execute(
            "SELECT prepay_item_id, cash_fc_cents FROM settlements WHERE id = ?",
            (settlement_id,),
        ).fetchone()
        prepay_id, cash_fc = row
        if prepay_id is None:
            return False
        alloc_sum = self.led._conn.execute(
            "SELECT COALESCE(SUM(fc_cents), 0) FROM settlement_allocs"
            " WHERE settlement_id = ?",
            (settlement_id,),
        ).fetchone()[0]
        original_fc = -(cash_fc - alloc_sum)
        cur = self.led._conn.execute(
            "SELECT fc_cents FROM open_items WHERE id = ?", (prepay_id,)
        ).fetchone()
        return cur is None or cur[0] != original_fc

    def _snapshot(self):
        entries = self.led._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(id), 0) FROM entries"
        ).fetchone()
        return entries, self.led.trial_balance()

    def test_random_operations(self):
        rng = random.Random(self.SEED)
        led = self.led
        customers = ["Alpha", "Beta", "Gamma"]
        foreign = ["EUR", "GBP", "CAD"]
        rates = {"EUR": Decimal("1.10"), "GBP": Decimal("1.30"),
                 "CAD": Decimal("0.75")}
        month_ends = {date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)}
        first_rate_day = {"EUR": 0, "GBP": 4, "CAD": 0}  # GBP starts late

        start = date(2026, 1, 1)
        for day_no in range(91):
            today = start + timedelta(days=day_no)
            today_s = today.isoformat()

            # daily rate quotes (random walk, 6dp)
            for ccy in foreign:
                if day_no >= first_rate_day[ccy]:
                    drift = Decimal(rng.randint(-300, 300)) / 10000
                    rates[ccy] = max(
                        Decimal("0.5"),
                        (rates[ccy] * (1 + drift)).quantize(
                            Decimal("0.000001")),
                    )
                    led.set_rate(ccy, today_s, rates[ccy])

            # occasionally rewrite an old rate: posted state must not move
            if rng.random() < 0.1 and day_no > 10:
                before = self._snapshot()
                ccy = rng.choice(foreign)
                old_day = (today - timedelta(days=rng.randint(1, 5))).isoformat()
                led.set_rate(ccy, old_day, rates[ccy])
                self.assertEqual(self._snapshot(), before)

            for _ in range(rng.randint(1, 3)):
                roll = rng.random()
                if roll < 0.40:
                    self._op_post(rng, customers, today_s)
                elif roll < 0.70:
                    self._op_settle(rng, customers, today_s)
                elif roll < 0.80:
                    self._op_reverse(rng, today_s)
                else:
                    self._op_illegal(rng, customers, today, day_no)
                self.assert_invariants()

            # period-end revaluation closes the day
            if today in month_ends or rng.random() < 0.05:
                led.revalue(today_s)
                self.assert_invariants()

    # ---- random operations ----

    def _op_post(self, rng, customers, today_s):
        kind = "invoice" if rng.random() < 0.7 else "credit_note"
        ccy = rng.choice(["EUR", "GBP", "CAD"])
        amount = Decimal(rng.randint(1000, 300000)) / 100
        try:
            self.led.post_document(
                rng.choice(customers), kind, ccy, amount, today_s)
        except LedgerError:
            # only acceptable before the first GBP rate exists
            self.assertEqual(ccy, "GBP")

    def _op_settle(self, rng, customers, today_s):
        customer = rng.choice(customers)
        ccy = rng.choice(["EUR", "GBP", "CAD"])
        items = [it for it in self.led.open_items(customer)
                 if it["currency"] == ccy]
        if not items:
            return
        rng.shuffle(items)
        allocations = {}
        total = Decimal(0)
        for it in items:
            if rng.random() < 0.5:
                continue
            bal = it["fc_balance"]
            cents = abs(bal) * 100
            take = rng.randint(1, int(cents))
            portion = (Decimal(take) / 100).copy_sign(bal)
            if total + portion < 0:
                continue  # keep sum of allocations >= 0
            allocations[it["id"]] = portion
            total += portion
        if not allocations:
            return
        extra = Decimal(rng.choice([0, 0, 0, 500, 5000, 25000])) / 100
        cash = total + extra
        self.led.settle(customer, ccy, today_s, cash, allocations)

    def _op_reverse(self, rng, today_s):
        rows = self.led._conn.execute(
            "SELECT id FROM settlements WHERE reversed = 0"
        ).fetchall()
        if not rows:
            return
        stl_id = rng.choice(rows)[0]
        if self._prepay_used(stl_id):
            with self.assertRaises(LedgerError):
                self.led.reverse_settlement(stl_id, today_s)
        else:
            self.led.reverse_settlement(stl_id, today_s)

    def _op_illegal(self, rng, customers, today, day_no):
        led = self.led
        today_s = today.isoformat()
        before = self._snapshot()
        choice = rng.randrange(8)
        if choice == 0:
            locked = led._locked_until()
            if locked is None:
                return
            with self.assertRaises(LedgerError):
                led.post_document("Alpha", "invoice", "EUR", "10.00", locked)
        elif choice == 1:
            with self.assertRaises(LedgerError):
                led.post_document("Alpha", "invoice", "JPY", "10.00", today_s)
        elif choice == 2:
            with self.assertRaises(LedgerError):
                led.post_document("Alpha", "invoice", "EUR", 10.5, today_s)
        elif choice == 3:
            with self.assertRaises(LedgerError):
                led.post_document("Alpha", "invoice", "EUR", "-5.00", today_s)
        elif choice == 4:
            items = led.open_items()
            if not items:
                return
            it = rng.choice(items)
            over = abs(it["fc_balance"]) + Decimal("0.01")
            with self.assertRaises(LedgerError):
                led.settle(it["customer"], it["currency"], today_s,
                           over, {it["id"]: over.copy_sign(it["fc_balance"])})
        elif choice == 5:
            items = led.open_items()
            if not items:
                return
            it = rng.choice(items)
            part = (abs(it["fc_balance"]) / 2).quantize(Decimal("0.01"))
            if part <= 0:
                return
            alloc = part.copy_sign(it["fc_balance"])
            with self.assertRaises(LedgerError):
                # cash below the allocated sum
                led.settle(it["customer"], it["currency"], today_s,
                           "0.00", {it["id"]: alloc})
        elif choice == 6:
            with self.assertRaises(LedgerError):
                led.settle("Alpha", "EUR", today_s, "1.00", {999999: "1.00"})
        else:
            with self.assertRaises(LedgerError):
                led.reverse_settlement(999999, today_s)
        self.assertEqual(self._snapshot(), before,
                         "rejected operation changed state")


if __name__ == "__main__":
    unittest.main()
