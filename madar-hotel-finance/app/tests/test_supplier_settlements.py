"""Supplier settlement accounting: real dated sources, guarded use and reversals."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting
import payables
import supplier_payments
import supplier_settlements as settlements
import test_payables


class SupplierSettlements(unittest.TestCase):
    def setUp(self):
        self.fixture = test_payables.PayablesAccounting()
        self.fixture.setUp()
        self.db = self.fixture.db
        supplier_payments.initialize(self.db)
        settlements.initialize(self.db)
        self.db.execute("UPDATE account_mappings SET status='confirmed' WHERE module='الموردون'")
        self.db.commit()
        self.n = 0

    def tearDown(self):
        self.fixture.tearDown()

    def invoice(self, amount=10000, day='2026-02-01', product=1):
        return self.fixture.make_invoice(price=amount, qty='1', business_date=day, due=day, product=product)

    def advance(self, amount=10000, day='2026-01-01'):
        self.n += 1
        draft = supplier_payments.save_payment(self.db, {
            'request_key': f'payment-{self.n}', 'supplier_id': 1, 'payment_date': day,
            'amount_cents': amount, 'method': 'cash', 'funding_account_id': self.fixture.funding,
            'allocation_mode': 'manual', 'allow_advance': True, 'details': {'received_by': 'المورد'},
        })
        return supplier_payments.approve_payment(self.db, draft['id'], {'revision': draft['revision']})

    def paid_invoice(self, amount=10000, day='2026-01-01'):
        invoice = self.invoice(amount, day)
        self.n += 1
        draft = supplier_payments.save_payment(self.db, {
            'request_key': f'payment-{self.n}', 'supplier_id': 1, 'payment_date': day,
            'amount_cents': amount, 'method': 'cash', 'funding_account_id': self.fixture.funding,
            'allocation_mode': 'invoice', 'target_invoice_id': invoice['id'],
            'details': {'received_by': 'المورد'},
        })
        supplier_payments.approve_payment(self.db, draft['id'], {'revision': draft['revision']})
        return invoice

    def payload(self, source_type, source_id, allocations, day='2026-03-01'):
        self.n += 1
        return {'request_key': f'settlement-{self.n}', 'reference': f'SET-{self.n}',
                'supplier_id': 1, 'source_type': source_type, 'source_id': source_id,
                'business_date': day, 'notes': 'تسوية',
                'allocations': [{'invoice_id': invoice['id'], 'amount_cents': amount} for invoice, amount in allocations]}

    def post(self, payload):
        draft = settlements.save(self.db, payload)
        return settlements.approve(self.db, draft['id'], {'revision': draft['revision']})

    def source(self, kind, ident, as_of='9999-12-31'):
        return next(s for s in settlements.source_balances(self.db, 1, as_of)['sources']
                    if s['source_type'] == kind and s['source_id'] == ident)

    def assert_balanced(self):
        for row in self.db.execute('''SELECT journal_id,SUM(debit_cents) debit,SUM(credit_cents) credit
            FROM journal_lines GROUP BY journal_id'''):
            self.assertEqual(row['debit'], row['credit'])

    def test_advance_applies_partially_across_invoices_original_accounts_and_balances(self):
        with self.db:
            advance = self.advance()
            one, two = self.invoice(6000), self.invoice(8000)
            self.db.execute("UPDATE account_mappings SET account_id=(SELECT id FROM accounts WHERE code='115001') WHERE module='الموردون' AND role='supplier_advances'")
            result = self.post(self.payload('advance', advance['id'], [(one, 2000), (two, 3000)]))
        self.assertEqual(result['amount_cents'], 5000)
        self.assertEqual(result['source_account_id'], self.fixture.advance)
        self.assertEqual(self.source('advance', advance['id'])['available_cents'], 5000)
        balances = payables.supplier_balances(self.db, 1, '2026-03-01')
        self.assertEqual((balances['payable_balance_cents'], balances['advance_cents'], balances['net_payable_cents']), (9000, 5000, 4000))
        rows = {row['id']: row for row in payables.open_invoices(self.db, 1, '2026-03-01')}
        self.assertEqual((rows[one['id']]['outstanding_cents'], rows[two['id']]['outstanding_cents']), (4000, 5000))
        self.assert_balanced()

    def test_saved_draft_is_not_reservation_and_approval_rechecks_source(self):
        with self.db:
            advance = self.advance(10000)
            one, two = self.invoice(10000), self.invoice(10000)
            first = settlements.save(self.db, self.payload('advance', advance['id'], [(one, 8000)]))
            stale = settlements.save(self.db, self.payload('advance', advance['id'], [(two, 8000)]))
            self.assertEqual(self.source('advance', advance['id'])['available_cents'], 10000)
            settlements.approve(self.db, first['id'], {'revision': first['revision']})
        count = self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0]
        with self.assertRaises(ValueError):
            with self.db:
                settlements.approve(self.db, stale['id'], {'revision': stale['revision']})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], count)
        self.assertEqual(settlements.detail(self.db, stale['id'])['status'], 'draft')

    def test_return_credit_moves_between_invoices_without_changing_net_ap(self):
        with self.db:
            original = self.paid_invoice(10000)
            self.fixture.make_return(original, '.4', business_date='2026-02-01')
            target = self.invoice(10000, '2026-02-10')
            before = payables.supplier_balances(self.db, 1, '2026-03-01')
            settlement = self.post(self.payload('return_credit', original['id'], [(target, 2500)]))
        self.assertEqual(self.source('return_credit', original['id'])['available_cents'], 1500)
        rows = {r['id']: r for r in payables.open_invoices(self.db, 1, '2026-03-01')}
        self.assertEqual(rows[target['id']]['outstanding_cents'], 7500)
        after = payables.supplier_balances(self.db, 1, '2026-03-01')
        self.assertEqual(after['net_payable_cents'], before['net_payable_cents'])
        journal = payables.journal_detail(self.db, settlement['journal_id'])
        self.assertEqual((journal['debit_cents'], journal['credit_cents']), (2500, 2500))
        self.assert_balanced()

    def test_dated_reversal_restores_original_sources_and_invoice_liabilities(self):
        with self.db:
            advance = self.advance()
            target = self.invoice()
            result = self.post(self.payload('advance', advance['id'], [(target, 6000)]))
            args = {'revision': result['revision'], 'business_date': '2026-04-01', 'reference': 'REV-1', 'reason': 'تصحيح التوزيع'}
            reversed_doc = settlements.reverse(self.db, result['id'], args)
            retry = settlements.reverse(self.db, result['id'], args)
        self.assertEqual(reversed_doc['status'], 'reversed')
        self.assertEqual(reversed_doc['reversal']['journal_id'], retry['reversal']['journal_id'])
        self.assertEqual(self.source('advance', advance['id'], '2026-03-31')['available_cents'], 4000)
        self.assertEqual(self.source('advance', advance['id'], '2026-04-01')['available_cents'], 10000)
        self.assertEqual(payables.invoice_outstanding(self.db, target['id'], '2026-03-31'), 4000)
        self.assertEqual(payables.invoice_outstanding(self.db, target['id'], '2026-04-01'), 10000)
        with self.assertRaises(accounting.ConflictError):
            settlements.reverse(self.db, result['id'], {**args, 'reason': 'سبب آخر'})
        self.assert_balanced()

    def test_historical_and_future_posted_usage_reserve_source_and_target(self):
        with self.db:
            advance = self.advance()
            one, two = self.invoice(), self.invoice()
            self.post(self.payload('advance', advance['id'], [(one, 7000)], '2026-06-01'))
        with self.assertRaises(ValueError):
            settlements.preview(self.db, self.payload('advance', advance['id'], [(two, 4000)], '2026-03-01'))
        self.assertEqual(self.source('advance', advance['id'], '2026-03-01')['available_cents'], 10000)
        with self.assertRaises(ValueError):
            settlements.preview(self.db, self.payload('advance', advance['id'], [(two, 1000)], '2025-12-31'))

    def test_intermediate_future_usage_then_reversal_still_limits_backdated_settlement(self):
        with self.db:
            advance = self.advance()
            one, two = self.invoice(), self.invoice()
            later = self.post(self.payload('advance', advance['id'], [(one, 8000)], '2026-03-01'))
            settlements.reverse(self.db, later['id'], {'revision': later['revision'],
                'business_date': '2026-04-01', 'reference': 'REV-LATER', 'reason': 'تصحيح'})
        self.assertEqual(self.source('advance', advance['id'], '2026-02-01')['available_cents'], 10000)
        self.assertEqual(self.source('advance', advance['id'])['available_cents'], 10000)
        with self.assertRaises(ValueError):
            settlements.preview(self.db, self.payload('advance', advance['id'], [(two, 8000)], '2026-02-01'))
        allowed = settlements.preview(self.db, self.payload('advance', advance['id'], [(two, 2000)], '2026-02-01'))
        self.assertEqual(allowed['available_cents'], 2000)

    def test_intermediate_future_invoice_payment_then_reversal_limits_target_capacity(self):
        import supplier_adjustments
        with self.db:
            supplier_adjustments.initialize(self.db)
            target = self.invoice()
            draft = supplier_payments.save_payment(self.db, {
                'request_key': 'future-target-payment', 'supplier_id': 1, 'payment_date': '2026-03-01',
                'amount_cents': 8000, 'method': 'cash', 'funding_account_id': self.fixture.funding,
                'allocation_mode': 'invoice', 'target_invoice_id': target['id'],
                'details': {'received_by': 'المورد'}})
            paid = supplier_payments.approve_payment(self.db, draft['id'], {'revision': draft['revision']})
            supplier_adjustments.reverse_payment(self.db, paid['id'], {
                'request_key': 'reverse-future-target-payment', 'revision': paid['revision'],
                'business_date': '2026-04-01', 'reference': 'BANK-REV', 'reason': 'تصحيح'})
            advance = self.advance()
        self.assertEqual(payables.invoice_outstanding(self.db, target['id'], '2026-02-01'), 10000)
        self.assertEqual(payables.invoice_outstanding(self.db, target['id'], '9999-12-31'), 10000)
        with self.assertRaises(ValueError):
            settlements.preview(self.db, self.payload('advance', advance['id'], [(target, 8000)], '2026-02-01'))
        report = settlements.preview(self.db, self.payload('advance', advance['id'], [(target, 2000)], '2026-02-01'))
        self.assertEqual(report['allocations'][0]['outstanding_cents'], 2000)

    def test_return_after_advance_application_produces_credit_usable_on_another_invoice(self):
        with self.db:
            advance = self.advance()
            original = self.invoice()
            self.post(self.payload('advance', advance['id'], [(original, 10000)]))
            self.fixture.make_return(original, '.25', business_date='2026-04-01')
            target = self.invoice(5000, '2026-04-02')
            source = self.source('return_credit', original['id'])
            self.assertEqual(source['available_cents'], 2500)
            self.post(self.payload('return_credit', original['id'], [(target, 2500)], '2026-04-03'))
        self.assertEqual(self.source('advance', advance['id'])['available_cents'], 0)
        self.assertEqual(self.source('return_credit', original['id'])['available_cents'], 0)
        self.assertEqual(payables.invoice_outstanding(self.db, target['id'], '2026-04-03'), 2500)
        self.assert_balanced()

    def test_upstream_settlement_reversal_requires_downstream_credit_uses_reversed_first(self):
        with self.db:
            advance = self.advance()
            original = self.invoice()
            applied = self.post(self.payload('advance', advance['id'], [(original, 10000)]))
            self.fixture.make_return(original, '.25', business_date='2026-04-01')
            target = self.invoice(5000, '2026-04-02')
            downstream = self.post(self.payload('return_credit', original['id'], [(target, 2500)], '2026-04-03'))
        args = {'revision': applied['revision'], 'business_date': '2026-05-01', 'reference': 'REV-UP', 'reason': 'تصحيح'}
        with self.assertRaises(ValueError):
            settlements.reverse(self.db, applied['id'], args)
        with self.db:
            settlements.reverse(self.db, downstream['id'], {'revision': downstream['revision'],
                'business_date': '2026-06-01', 'reference': 'REV-DOWN', 'reason': 'تصحيح'})
        with self.assertRaises(ValueError):
            settlements.reverse(self.db, applied['id'], args)
        with self.db:
            reversed_doc = settlements.reverse(self.db, applied['id'], {**args, 'business_date': '2026-06-01'})
        self.assertEqual(reversed_doc['status'], 'reversed')
        self.assertEqual(self.source('advance', advance['id'])['available_cents'], 10000)
        self.assert_balanced()

    def test_payment_and_credit_source_refunds_cannot_be_spent_twice(self):
        with self.db:
            advance = self.advance()
            target = self.invoice()
            self.db.execute('CREATE TABLE IF NOT EXISTS supplier_refunds(id INTEGER PRIMARY KEY,supplier_id INTEGER,source_type TEXT,source_id INTEGER,business_date TEXT,amount_cents INTEGER,status TEXT)')
            self.db.execute("INSERT INTO supplier_refunds(supplier_id,source_type,source_id,business_date,amount_cents,status) VALUES(1,'advance',?,'2026-02-20',8000,'posted')", (advance['id'],))
        self.assertEqual(self.source('advance', advance['id'])['available_cents'], 2000)
        with self.assertRaises(ValueError):
            settlements.preview(self.db, self.payload('advance', advance['id'], [(target, 2500)]))

    def test_wrong_supplier_duplicate_target_amount_dates_and_revisions_rejected(self):
        with self.db:
            advance = self.advance()
            target = self.invoice()
            data = self.payload('advance', advance['id'], [(target, 1000)])
            draft = settlements.save(self.db, data)
        invalid = [dict(data, supplier_id=2), dict(data, allocations=data['allocations'] * 2),
                   dict(data, allocations=[{'invoice_id': target['id'], 'amount': '-1'}]),
                   dict(data, allocations=[{'invoice_id': target['id'], 'amount': 'NaN'}]),
                   dict(data, business_date='2026-01-10'), dict(data, source_type='unknown')]
        for payload in invalid:
            with self.assertRaises(ValueError):
                settlements.preview(self.db, payload)
        for revision in (True, 0, draft['revision'] + 1):
            with self.assertRaises(accounting.ConflictError):
                settlements.approve(self.db, draft['id'], {'revision': revision})

    def test_idempotent_save_post_and_immutable_accounting_snapshots(self):
        with self.db:
            advance = self.advance()
            target = self.invoice()
            data = self.payload('advance', advance['id'], [(target, 1000)])
            draft = settlements.save(self.db, data)
            self.assertEqual(settlements.save(self.db, data)['id'], draft['id'])
            posted = settlements.approve(self.db, draft['id'], {'revision': draft['revision']})
            self.assertEqual(settlements.approve(self.db, draft['id'], {'revision': draft['revision']})['journal_id'], posted['journal_id'])
        with self.assertRaises(accounting.ConflictError):
            settlements.save(self.db, {**data, 'notes': 'تغيير'})
        for sql in ['UPDATE supplier_settlements SET amount_cents=1 WHERE id=?',
                    'DELETE FROM settlement_allocations WHERE settlement_id=?']:
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql, (posted['id'],))
        with self.assertRaises(ValueError):
            settlements.save(self.db, {**data, 'revision': posted['revision']}, posted['id'])

    def test_cancelled_draft_can_neither_post_nor_affect_available_balance(self):
        with self.db:
            advance = self.advance()
            target = self.invoice()
            draft = settlements.save(self.db, self.payload('advance', advance['id'], [(target, 1000)]))
            cancelled = settlements.save(self.db, {'revision': draft['revision'], 'action': 'cancel'}, draft['id'])
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertEqual(self.source('advance', advance['id'])['available_cents'], 10000)
        with self.assertRaises(ValueError):
            settlements.approve(self.db, draft['id'], {'revision': cancelled['revision']})


if __name__ == '__main__':
    unittest.main(verbosity=2)
