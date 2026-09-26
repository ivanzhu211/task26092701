import unittest


class TestSchedule(unittest.TestCase):
    pass


class TestRandomOps(unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
import os
import random
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal

from ledger import FOREIGN_CURRENCIES, Ledger, LedgerError, ar_account


def check_invariants(testcase, ledger):
    """校验性质 1-3：凭证平衡、应收科目与未清项一致、零余额零账面。"""
    conn = ledger.connection
    for row in conn.execute(
        "SELECT entry_id, SUM(debit_cents) AS dr, SUM(credit_cents) AS cr"
        " FROM lines GROUP BY entry_id"
    ):
        testcase.assertEqual(row["dr"], row["cr"], f"凭证 {row['entry_id']} 借贷不平衡")
    tb = ledger.trial_balance()
    testcase.assertEqual(sum(tb.values()), Decimal("0.00"), "试算平衡表合计不为零")
    items = ledger.open_items()
    for currency in FOREIGN_CURRENCIES:
        book_sum = sum(
            (it["book_value"] for it in items if it["currency"] == currency),
            Decimal("0.00"),
        )
        testcase.assertEqual(
            tb[ar_account(currency)], book_sum,
            f"{currency} 应收科目本位币余额与未清项账面之和不等",
        )
        foreign_sum = sum(
            (it["foreign_balance"] for it in items if it["currency"] == currency),
            Decimal("0.00"),
        )
        ledger_foreign = sum(
            (Decimal(r[0]) for r in conn.execute(
                "SELECT foreign_amount FROM lines WHERE account = ?"
                " AND foreign_amount IS NOT NULL",
                (ar_account(currency),),
            )),
            Decimal("0.00"),
        )
        testcase.assertEqual(
            ledger_foreign, foreign_sum,
            f"{currency} 应收科目原币余额与未清项原币之和不等",
        )
    for row in conn.execute(
        "SELECT id, book_cents FROM items WHERE foreign_balance = '0.00'"
    ):
        testcase.assertEqual(row["book_cents"], 0,
                             f"未清项 {row['id']} 原币归零但账面不为零")


class TestSchedule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "ledger.db")
        self.ledger = Ledger(self.db_path)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_invoice_revalue_three_payments_then_reversal(self):
        lg = self.ledger
        lg.set_rate("EUR", "2025-01-01", "1.10")
        lg.set_rate("EUR", "2025-01-31", "1.20")
        lg.set_rate("EUR", "2025-02-01", "1.15")
        lg.set_rate("EUR", "2025-02-28", "1.18")

        item = lg.post_document("ACME", "invoice", "EUR", "1000.00", "2025-01-10")
        check_invariants(self, lg)
        lg.revalue("2025-01-31")
        check_invariants(self, lg)
        self.assertEqual(lg.open_items()[0]["book_value"], Decimal("1200.00"))

        # 重新打开数据库，验证状态可恢复
        lg.close()
        self.ledger = lg = Ledger(self.db_path)
        self.assertEqual(lg.open_items()[0]["foreign_balance"], Decimal("1000.00"))

        settlements = [
            lg.settle("ACME", "EUR", "2025-02-05", "333.33", {item: "333.33"}),
            lg.settle("ACME", "EUR", "2025-02-10", "333.33", {item: "333.33"}),
            lg.settle("ACME", "EUR", "2025-02-15", "333.34", {item: "333.34"}),
        ]
        check_invariants(self, lg)
        tb = lg.trial_balance()
        realized_pnl = -tb["FX_REALIZED"]
        self.assertEqual(realized_pnl, Decimal("-50.00"))
        self.assertEqual(lg.open_items(), [])
        self.assertEqual(tb["AR:EUR"], Decimal("0.00"))

        lg.revalue("2025-02-28")
        check_invariants(self, lg)
        lg.reverse_settlement(settlements[1], "2025-03-01")
        check_invariants(self, lg)

        items = lg.open_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["foreign_balance"], Decimal("333.33"))
        tb = lg.trial_balance()
        self.assertEqual(tb["AR:EUR"], items[0]["book_value"])
        self.assertEqual(tb["AR:EUR"], Decimal("399.99"))
        self.assertEqual(tb["BANK:EUR"], Decimal("766.67"))
        self.assertEqual(tb["REVENUE"], Decimal("-1100.00"))
        self.assertEqual(tb["FX_UNREALIZED"], Decimal("-100.00"))
        self.assertEqual(-tb["FX_REALIZED"], Decimal("-33.34"))
        self.assertEqual(sum(tb.values()), Decimal("0.00"))

    def test_consumed_prepayment_blocks_reversal(self):
        lg = self.ledger
        lg.set_rate("EUR", "2025-01-01", "1.10")
        item = lg.post_document("ACME", "invoice", "EUR", "100.00", "2025-01-10")
        sid = lg.settle("ACME", "EUR", "2025-01-11", "150.00", {item: "100.00"})
        prepay = [it for it in lg.open_items() if it["kind"] == "prepayment"][0]
        inv2 = lg.post_document("ACME", "invoice", "EUR", "50.00", "2025-01-12")
        lg.settle("ACME", "EUR", "2025-01-13", "0.00",
                  {inv2: "50.00", prepay["id"]: "-50.00"})
        check_invariants(self, lg)
        with self.assertRaises(LedgerError):
            lg.reverse_settlement(sid, "2025-01-14")
        check_invariants(self, lg)

    def test_reversal_unwinds_prepayment_revaluation(self):
        lg = self.ledger
        lg.set_rate("EUR", "2025-01-01", "1.10")
        lg.set_rate("EUR", "2025-01-31", "1.20")
        item = lg.post_document("ACME", "invoice", "EUR", "100.00", "2025-01-10")
        sid = lg.settle("ACME", "EUR", "2025-01-15", "150.00", {item: "100.00"})
        lg.revalue("2025-01-31")
        self.assertNotEqual(lg.trial_balance()["FX_UNREALIZED"], Decimal("0.00"))
        lg.reverse_settlement(sid, "2025-02-01")
        check_invariants(self, lg)
        tb = lg.trial_balance()
        items = lg.open_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["foreign_balance"], Decimal("100.00"))
        self.assertEqual(items[0]["book_value"], Decimal("110.00"))
        self.assertEqual(tb["FX_UNREALIZED"], Decimal("0.00"))

    def test_backfilled_rates_do_not_change_posted_documents(self):
        lg = self.ledger
        lg.set_rate("GBP", "2025-01-01", "1.25")
        lg.post_document("ACME", "invoice", "GBP", "100.00", "2025-01-10")
        tb_before = lg.trial_balance()
        lg.set_rate("GBP", "2025-01-05", "9.99")
        lg.set_rate("GBP", "2025-01-01", "9.99")
        self.assertEqual(lg.trial_balance(), tb_before)
        self.assertEqual(lg.open_items()[0]["book_value"], Decimal("125.00"))


class TestRandomOps(unittest.TestCase):
    def test_random_sequence(self):
        rng = random.Random(20260927)
        customers = ["ALPHA", "BETA", "GAMMA"]
        start = date(2025, 1, 1)
        days = [start + timedelta(days=i) for i in range(90)]
        month_ends = {"2025-01-31", "2025-02-28", "2025-03-31"}

        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(os.path.join(tmp, "random.db"))
            rates = {c: Decimal("0.9") + Decimal(rng.randrange(0, 50)) / 100
                     for c in FOREIGN_CURRENCIES}
            settlements = []  # 未撤销的核销单 id
            reversed_ids = set()
            reopened = False

            def entry_count():
                return ledger.connection.execute(
                    "SELECT COUNT(*) FROM entries").fetchone()[0]

            def random_amount():
                return Decimal(rng.randrange(100, 200000)) / 100

            def do_post():
                ledger.post_document(
                    rng.choice(customers),
                    rng.choice(["invoice", "invoice", "credit_note"]),
                    rng.choice(FOREIGN_CURRENCIES),
                    random_amount(), day.isoformat())

            def do_settle():
                items = ledger.open_items()
                if not items:
                    return
                cust = rng.choice(customers)
                ccy = rng.choice(FOREIGN_CURRENCIES)
                pool = [it for it in items
                        if it["customer"] == cust and it["currency"] == ccy]
                if not pool:
                    return
                rng.shuffle(pool)
                allocs = {}
                net = Decimal("0.00")
                for it in pool[: rng.randrange(1, len(pool) + 1)]:
                    bal = it["foreign_balance"]
                    if rng.random() < 0.4:
                        alloc = bal
                    else:
                        alloc = (bal * rng.randrange(1, 100) / 100).quantize(
                            Decimal("0.01"))
                        if alloc == 0:
                            alloc = Decimal("0.01") if bal > 0 else Decimal("-0.01")
                    if net + alloc < 0:
                        continue
                    allocs[it["id"]] = alloc
                    net += alloc
                extra = Decimal("0.00") if rng.random() < 0.5 else random_amount()
                ledger.settle(cust, ccy, day.isoformat(), net + extra, allocs)

            def do_reverse():
                rows = ledger.connection.execute(
                    "SELECT id FROM settlements WHERE reversed = 0").fetchall()
                if not rows:
                    return
                blocked = ledger.connection.execute(
                    "SELECT s.id FROM settlements s JOIN items i"
                    " ON i.id = s.prepay_item_id"
                    " WHERE s.reversed = 0 AND i.foreign_balance <> i.foreign_orig"
                ).fetchall()
                if blocked and rng.random() < 0.5:
                    sid = rng.choice(blocked)[0]
                    before = entry_count()
                    with self.assertRaises(LedgerError):
                        ledger.reverse_settlement(sid, day.isoformat())
                    self.assertEqual(entry_count(), before)
                    return
                sid = rng.choice(rows)[0]
                try:
                    ledger.reverse_settlement(sid, day.isoformat())
                    reversed_ids.add(sid)
                except LedgerError:
                    pass  # 预收已被用掉，拒绝是合法行为
                if sid in settlements:
                    settlements.remove(sid)

            def do_invalid():
                before = entry_count()
                tb_before = ledger.trial_balance()
                choice = rng.randrange(8)
                with self.assertRaises(LedgerError):
                    if choice == 0:
                        ledger.post_document("ALPHA", "invoice", "JPY",
                                             "10.00", day.isoformat())
                    elif choice == 1:
                        ledger.post_document("ALPHA", "invoice",
                                             rng.choice(FOREIGN_CURRENCIES),
                                             "10.001", day.isoformat())
                    elif choice == 2:
                        ledger.post_document("ALPHA", "invoice",
                                             rng.choice(FOREIGN_CURRENCIES),
                                             "10.00", "2020-01-01")
                    elif choice == 3:
                        items = ledger.open_items()
                        if not items:
                            raise LedgerError("skip")
                        it = rng.choice(items)
                        bal = it["foreign_balance"]
                        over = bal + (Decimal("0.01") if bal > 0
                                      else Decimal("-0.01"))
                        ledger.settle(it["customer"], it["currency"],
                                      day.isoformat(), "999999.00",
                                      {it["id"]: over})
                    elif choice == 4:
                        items = ledger.open_items()
                        if not items:
                            raise LedgerError("skip")
                        it = rng.choice(items)
                        ledger.settle(it["customer"], it["currency"],
                                      day.isoformat(), "0.00",
                                      {it["id"]: it["foreign_balance"]})
                    elif choice == 5:
                        ledger.reverse_settlement(10 ** 9, day.isoformat())
                    elif choice == 6:
                        if not reversed_ids:
                            raise LedgerError("skip")
                        ledger.reverse_settlement(
                            rng.choice(sorted(reversed_ids)), day.isoformat())
                    else:
                        ledger.set_rate(rng.choice(FOREIGN_CURRENCIES),
                                        day.isoformat(), "1.1234567")
                self.assertEqual(entry_count(), before, "被拒绝的操作留下了凭证")
                self.assertEqual(ledger.trial_balance(), tb_before,
                                 "被拒绝的操作改变了状态")

            for day in days:
                iso = day.isoformat()
                for ccy in FOREIGN_CURRENCIES:
                    drift = Decimal(rng.randrange(-200, 201)) / 10000
                    rates[ccy] = (rates[ccy] + drift).quantize(
                        Decimal("0.000001"))
                    if rates[ccy] < Decimal("0.5"):
                        rates[ccy] = Decimal("0.5")
                    ledger.set_rate(ccy, iso, rates[ccy])
                for _ in range(rng.randrange(0, 4)):
                    action = rng.random()
                    if action < 0.45:
                        do_post()
                    elif action < 0.75:
                        do_settle()
                    elif action < 0.85:
                        do_reverse()
                    else:
                        do_invalid()
                    check_invariants(self, ledger)
                if iso in month_ends:
                    ledger.revalue(iso)
                    check_invariants(self, ledger)
                if not reopened and iso == "2025-02-10":
                    ledger.close()
                    ledger = Ledger(os.path.join(tmp, "random.db"))
                    reopened = True
                    check_invariants(self, ledger)

            ledger.close()
