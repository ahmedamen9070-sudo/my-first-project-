"""A refund correction releases source capacity on its date, never retroactively."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import payables
import supplier_adjustments as adjustments
import supplier_payments as payments
import supplier_reconciliation as reconciliation
import supplier_settlements as settlements
import test_supplier_payments as fixtures


class RefundReversalIntegration(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SupplierPayments('runTest')
        self.fixture.setUp()
        self.db = self.fixture.db
        settlements.initialize(self.db)
        adjustments.initialize(self.db)
        self.counter = 0

    def tearDown(self):
        self.fixture.tearDown()

    def key(self, prefix):
        self.counter += 1
        return prefix + str(self.counter)

    def advance(self, amount='100'):
        return self.fixture.post(self.fixture.payload(amount=amount, allow_advance=True))

    def refund(self, source_id, source_type='advance', amount='30', day='2026-10-22'):
        draft = adjustments.save_refund(self.db, {
            'request_key': self.key('refund'), 'supplier_id': 1,
            'source_type': source_type, 'source_id': source_id,
            'business_date': day, 'reference': self.key('RF-'),
            'amount': amount, 'reason': 'استرداد رصيد موثق', 'method': 'cash',
            'funding_account_id': self.fixture.cash,
            'details': {'received_by': 'أمين الخزينة', 'receipt_reference': self.key('RC-')},
        })
        return adjustments.approve_refund(self.db, draft['id'], {'revision': draft['revision']})

    def reverse(self, refund, day='2026-10-25'):
        return adjustments.reverse_refund(self.db, refund['id'], {
            'request_key': self.key('reverse-refund'), 'revision': refund['revision'],
            'event_revision': 0, 'business_date': day, 'reference': self.key('COR-'),
            'reason': 'تصحيح استرداد مسجل بطريق الخطأ',
        })

    def sources(self, source_id, kind='advance', day='2026-12-31'):
        return next(item for item in settlements.source_balances(self.db, 1, day)['sources']
                    if item['source_id'] == source_id and item['source_type'] == kind)

    def assert_matched(self, day):
        result = reconciliation.report(self.db, day)
        self.assertTrue(result['matched'], result)
        self.assertTrue(all(item['matched'] for item in result['accounts']))
        self.assertEqual(result['exceptions'], [])
        return result

    def settlement_data(self, source_id, invoice, amount='100', day='2026-10-25'):
        return {'request_key': self.key('settle'), 'supplier_id': 1,
                'source_type': 'advance', 'source_id': source_id,
                'business_date': day, 'reference': self.key('ST-'),
                'allocations': [{'invoice_id': invoice['id'], 'amount': amount}]}

    def post_settlement(self, data):
        draft = settlements.save(self.db, data)
        return settlements.approve(self.db, draft['id'], {'revision': draft['revision']})

    def test_advance_source_and_reconciliation_follow_reversal_date(self):
        advance = self.advance()
        refund = self.refund(advance['id'])
        original = dict(self.db.execute('SELECT * FROM supplier_refunds WHERE id=?', (refund['id'],)).fetchone())
        result = self.reverse(refund)
        self.assertEqual(result['effective_status'], 'reversed')
        self.assertEqual(self.sources(advance['id'], day='2026-10-21')['available_cents'], 10000)
        before = self.sources(advance['id'], day='2026-10-24')
        self.assertEqual((before['available_cents'], before['refunded_cents']), (7000, 3000))
        after = self.sources(advance['id'], day='2026-10-25')
        self.assertEqual((after['available_cents'], after['refunded_cents']), (10000, 0))
        self.assertEqual(payables.supplier_balances(self.db, 1, '2026-10-24')['advance_cents'], 7000)
        self.assertEqual(payables.supplier_balances(self.db, 1, '2026-10-25')['advance_cents'], 10000)
        self.assertEqual(payments.account_balance(self.db, self.fixture.cash, '2026-10-24'), -7000)
        self.assertEqual(payments.account_balance(self.db, self.fixture.cash, '2026-10-25'), -10000)
        for day in ['2026-10-15', '2026-10-16', '2026-10-21', '2026-10-22', '2026-10-24', '2026-10-25']:
            self.assert_matched(day)
        self.assertEqual(dict(self.db.execute('SELECT * FROM supplier_refunds WHERE id=?', (refund['id'],)).fetchone()), original)

    def test_return_credit_reversal_restores_aging_without_repricing(self):
        invoice = self.fixture.invoice()
        self.fixture.post(self.fixture.payload(amount='100', allocation_mode='invoice', target_invoice_id=invoice['id']))
        self.fixture.returned(invoice, quantity='0.5')
        refund = self.refund(invoice['id'], 'return_credit', '20')
        self.reverse(refund)
        before = payables.open_invoices(self.db, 1, '2026-10-24')[0]
        after = payables.open_invoices(self.db, 1, '2026-10-25')[0]
        self.assertEqual((before['outstanding_cents'], before['return_credit_cents'], before['credit_refunded_cents']), (0, 3000, 2000))
        self.assertEqual((after['outstanding_cents'], after['return_credit_cents'], after['credit_refunded_cents']), (0, 5000, 0))
        self.assertEqual(payables.aging(self.db, '2026-10-24')['totals']['credit_cents'], 3000)
        self.assertEqual(payables.aging(self.db, '2026-10-25')['totals']['credit_cents'], 5000)
        source = self.sources(invoice['id'], 'return_credit', '2026-10-25')
        self.assertEqual(source['available_cents'], 5000)
        self.assertEqual(source['original_cents'], 5000)
        self.assertEqual(payables.enrich_product(self.db, dict(self.db.execute('SELECT * FROM products WHERE id=1').fetchone()))['last_purchase_price_cents'], 10000)
        self.assert_matched('2026-10-24')
        self.assert_matched('2026-10-25')

    def test_summary_separates_original_and_effective_refunds_and_statement_labels(self):
        advance = self.advance()
        refund = self.refund(advance['id'])
        reversed_refund = self.reverse(refund)
        before = payables.ledger_summary(self.db, '2026-10-24')
        after = payables.ledger_summary(self.db, '2026-10-25')
        self.assertEqual((before['gross_refunds_cents'], before['refunds_cents'], before['reversed_refunds_cents']), (3000, 3000, 0))
        self.assertEqual((after['gross_refunds_cents'], after['refunds_cents'], after['reversed_refunds_cents']), (3000, 0, 3000))
        before_statement = payables.statement(self.db, 1, '2026-10-24')
        self.assertFalse(any(row['type'] == 'supplier_refund_reversal' for row in before_statement['rows']))
        after_statement = payables.statement(self.db, 1, '2026-10-25')
        line = next(row for row in after_statement['rows'] if row['type'] == 'supplier_refund_reversal')
        self.assertEqual(line['code'], reversed_refund['reversal']['code'])
        self.assertEqual(line['description'], 'عكس استرداد من مورد')
        self.assertEqual(line['advance_cents'], 3000)

    def test_restored_advance_cannot_be_settled_before_its_reversal(self):
        advance = self.advance()
        invoice = self.fixture.invoice()
        refund = self.refund(advance['id'], amount='100')
        self.reverse(refund)
        with self.assertRaises(ValueError):
            settlements.save(self.db, self.settlement_data(advance['id'], invoice, day='2026-10-24'))
        applied = self.post_settlement(self.settlement_data(advance['id'], invoice))
        self.assertEqual(applied['status'], 'posted')
        self.assertEqual(payables.invoice_outstanding(self.db, invoice['id'], '2026-10-25'), 0)
        self.assertEqual(self.sources(advance['id'])['available_cents'], 0)
        self.assert_matched('2026-10-24')
        self.assert_matched('2026-10-25')

    def test_later_reversal_does_not_allow_historical_double_refund(self):
        advance = self.advance()
        refund = self.refund(advance['id'], amount='100')
        self.reverse(refund, '2026-11-01')
        with self.assertRaises(ValueError):
            self.refund(advance['id'], amount='1', day='2026-10-25')
        corrected = self.refund(advance['id'], amount='100', day='2026-11-01')
        self.assertEqual(corrected['status'], 'posted')
        self.assertEqual(self.sources(advance['id'], day='2026-11-01')['available_cents'], 0)
        self.assert_matched('2026-10-25')
        self.assert_matched('2026-11-01')

    def test_original_advance_account_is_retained_after_mapping_change(self):
        advance = self.advance()
        refund = self.refund(advance['id'])
        new_account = self.fixture.account('115003')
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الموردون' AND role='supplier_advances'", (new_account,))
        self.reverse(refund)
        report = self.assert_matched('2026-10-25')
        old = next(row for row in report['accounts'] if row['id'] == self.fixture.advance)
        self.assertEqual(old['balance_cents'], 10000)
        self.assertEqual(old['subledger_balance_cents'], 10000)
        self.assertEqual(self.sources(advance['id'])['account_id'], self.fixture.advance)
        self.assertEqual(payments.account_balance(self.db, new_account), 0)

    def test_settlement_reverse_must_follow_related_refund_reversal(self):
        advance = self.advance()
        invoice = self.fixture.invoice()
        applied = self.post_settlement(self.settlement_data(advance['id'], invoice, day='2026-10-18'))
        self.fixture.returned(invoice, quantity='0.5', day='2026-10-20')
        refund = self.refund(invoice['id'], 'return_credit', '20')
        self.reverse(refund, '2026-10-24')
        data = {'revision': applied['revision'], 'business_date': '2026-10-23',
                'reference': 'SET-REV-1', 'reason': 'تصحيح تسوية مرتبطة باسترداد معكوس'}
        with self.assertRaises(ValueError):
            settlements.reverse(self.db, applied['id'], data)
        reversed_settlement = settlements.reverse(self.db, applied['id'], {**data, 'business_date': '2026-10-24'})
        self.assertEqual(reversed_settlement['status'], 'reversed')
        self.assertEqual(payables.invoice_outstanding(self.db, invoice['id'], '2026-10-24'), 5000)
        for day in ['2026-10-22', '2026-10-23', '2026-10-24']:
            self.assert_matched(day)


if __name__ == '__main__':
    unittest.main()
