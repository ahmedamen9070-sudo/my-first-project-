"""A corrected receipt releases its source on its own immutable journal date."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting
import accounting_periods as periods
import payables
import supplier_adjustments as adjustments
import supplier_payments as payments
import supplier_reconciliation as reconciliation
import supplier_settlements as settlements
import test_supplier_adjustments as fixtures


class SupplierRefundReversals(unittest.TestCase):
    account = fixtures.SupplierAdjustments.account
    invoice = fixtures.SupplierAdjustments.invoice
    details = fixtures.SupplierAdjustments.details
    payload = fixtures.SupplierAdjustments.payload
    post = fixtures.SupplierAdjustments.post
    returned = fixtures.SupplierAdjustments.returned
    advance_payment = fixtures.SupplierAdjustments.advance_payment
    refund_data = fixtures.SupplierAdjustments.refund_data
    refund = fixtures.SupplierAdjustments.refund
    payment_reverse_data = fixtures.SupplierAdjustments.reverse_data

    def setUp(self):
        fixtures.SupplierAdjustments.setUp(self)
        settlements.initialize(self.db)
        periods.initialize(self.db)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def reversal_data(self, refund, **changes):
        self.counter += 1
        data = {'request_key': f'refund-reversal-{self.counter}', 'revision': refund['revision'],
                'event_revision': refund['adjustment_revision'], 'business_date': '2026-10-25',
                'reference': f'CORRECTION-{self.counter}', 'reason': 'تصحيح استرداد سُجل بالخطأ'}
        data.update(changes)
        return data

    def raw_refund(self, ident):
        return dict(self.db.execute('SELECT * FROM supplier_refunds WHERE id=?', (ident,)).fetchone())

    def test_cash_reversal_restores_source_on_correction_date_and_preserves_original(self):
        payment = self.advance_payment()
        refund = self.refund(self.refund_data(payment['id'], amount='30'))
        original = self.raw_refund(refund['id'])
        original_journal = refund['journal']
        result = adjustments.reverse_refund(self.db, refund['id'], self.reversal_data(refund))
        self.assertEqual(result['status'], 'posted')
        self.assertEqual(result['effective_status'], 'reversed')
        self.assertEqual(result['adjustment_revision'], 1)
        self.assertEqual(result['reversal']['code'], 'SRR-000001')
        self.assertEqual(result['reversal']['journal']['source_type'], 'supplier_refund_reversal')
        self.assertEqual(result['reversal']['journal']['source_id'], result['reversal']['id'])
        self.assertEqual(self.raw_refund(refund['id']), original)
        self.assertEqual(result['journal'], original_journal)
        original_lines = [(l['account_id'], l['debit_cents'], l['credit_cents'], l['supplier_id'], l['department_id']) for l in original_journal['lines']]
        opposite = [(l['account_id'], l['credit_cents'], l['debit_cents'], l['supplier_id'], l['department_id']) for l in result['reversal']['journal']['lines']]
        self.assertEqual(original_lines, opposite)
        for day, expected in [('2026-10-21', 10000), ('2026-10-22', 7000), ('2026-10-24', 7000), ('2026-10-25', 10000)]:
            with self.subTest(day=day):
                self.assertEqual(payables.supplier_balances(self.db, 1, day)['advance_cents'], expected)
                self.assertEqual(settlements.source_balances(self.db, 1, day)['sources'][0]['available_cents'], expected)
                self.assertTrue(reconciliation.report(self.db, day)['matched'])
        self.assertEqual(payments.account_balance(self.db, self.cash, '2026-10-24'), -7000)
        self.assertEqual(payments.account_balance(self.db, self.cash, '2026-10-25'), -10000)
        self.assertEqual(adjustments.get_refund(self.db, refund['id'], '2026-10-24')['effective_status'], 'posted')
        self.assertEqual(adjustments.get_refund(self.db, refund['id'], '2026-10-25')['effective_status'], 'reversed')

    def test_bank_reversal_keeps_original_accounts_and_value_date_after_mapping_change(self):
        payment = self.advance_payment(method='bank_transfer')
        data = self.refund_data(payment['id'], method='bank_transfer', details={
            'bank_name': 'بنك الفندق', 'transaction_reference': 'BANK-REFUND', 'value_date': '2026-10-29'})
        refund = self.refund(data)
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الموردون' AND role='supplier_advances'", (self.account('115003'),))
        result = adjustments.reverse_refund(self.db, refund['id'], self.reversal_data(refund))
        lines = result['reversal']['journal']['lines']
        self.assertEqual(next(l['account_id'] for l in lines if l['debit_cents']), self.advance)
        self.assertEqual(next(l['account_id'] for l in lines if l['credit_cents']), self.bank)
        self.assertEqual(result['details']['value_date'], '2026-10-29')
        self.assertEqual(result['reversal']['business_date'], '2026-10-25')
        self.assertEqual(payments.account_balance(self.db, self.bank, '2026-10-25'), -10000)
        self.assertTrue(reconciliation.report(self.db, '2026-10-25')['matched'])

    def test_return_credit_is_restored_and_can_be_applied_after_reversal(self):
        original = self.invoice()
        self.post(self.payload(amount='100', allocation_mode='invoice', target_invoice_id=original['id']))
        self.returned(original, quantity='0.5')
        refund = self.refund(self.refund_data(original['id'], source_type='return_credit', amount='20'))
        adjustments.reverse_refund(self.db, refund['id'], self.reversal_data(refund))
        self.assertEqual(payables.supplier_balances(self.db, 1, '2026-10-24')['unapplied_return_credit_cents'], 3000)
        self.assertEqual(payables.supplier_balances(self.db, 1, '2026-10-25')['unapplied_return_credit_cents'], 5000)
        target = self.invoice(business_date='2026-10-24', due_date='2026-10-30')
        draft = settlements.save(self.db, {'request_key': 'apply-restored-credit', 'supplier_id': 1,
            'source_type': 'return_credit', 'source_id': original['id'], 'business_date': '2026-10-26',
            'reference': 'APPLY-RESTORED', 'allocations': [{'invoice_id': target['id'], 'amount': '40'}]})
        settlements.approve(self.db, draft['id'], {'revision': draft['revision']})
        self.assertEqual(payables.invoice_outstanding(self.db, target['id'], '2026-10-26'), 6000)
        self.assertEqual(payables.supplier_balances(self.db, 1, '2026-10-26')['unapplied_return_credit_cents'], 1000)
        self.assertTrue(reconciliation.report(self.db, '2026-10-26')['matched'])

    def test_restored_source_does_not_allow_a_backdated_refund_to_overconsume_history(self):
        payment = self.advance_payment()
        refund = self.refund(self.refund_data(payment['id'], amount='80'))
        adjustments.reverse_refund(self.db, refund['id'], self.reversal_data(refund))
        with self.assertRaisesRegex(ValueError, 'يتجاوز'):
            adjustments.save_refund(self.db, self.refund_data(payment['id'], amount='40', business_date='2026-10-23'))
        fresh = self.refund(self.refund_data(payment['id'], amount='40', business_date='2026-10-25'))
        self.assertEqual(fresh['amount_cents'], 4000)
        self.assertEqual(payables.supplier_balances(self.db, 1, '2026-10-25')['advance_cents'], 6000)
        self.assertTrue(reconciliation.report(self.db, '2026-10-25')['matched'])

    def test_upstream_payment_reversal_waits_for_refund_correction_date(self):
        payment = self.advance_payment()
        refund = self.refund(self.refund_data(payment['id']))
        adjustments.reverse_refund(self.db, refund['id'], self.reversal_data(refund, business_date='2026-10-28'))
        with self.assertRaisesRegex(ValueError, 'استرداد'):
            adjustments.reverse_payment(self.db, payment['id'], self.payment_reverse_data(payment, business_date='2026-10-27'))
        result = adjustments.reverse_payment(self.db, payment['id'], self.payment_reverse_data(payment, business_date='2026-10-28'))
        self.assertEqual(result['effective_status'], 'reversed')
        self.assertEqual(payables.supplier_balances(self.db, 1, '2026-10-28')['advance_cents'], 0)
        self.assertEqual(payments.account_balance(self.db, self.cash, '2026-10-28'), 0)
        self.assertTrue(reconciliation.report(self.db, '2026-10-28')['matched'])

    def test_changed_requests_revision_types_dates_and_unposted_refunds_are_rejected(self):
        payment = self.advance_payment()
        draft = adjustments.save_refund(self.db, self.refund_data(payment['id']))
        with self.assertRaisesRegex(ValueError, 'المعتمد'):
            adjustments.reverse_refund(self.db, draft['id'], self.reversal_data(draft))
        refund = adjustments.approve_refund(self.db, draft['id'], {'revision': draft['revision']})
        for changes in ({'revision': True}, {'revision': 1}, {'event_revision': True}, {'event_revision': 1}, {'adjustment_revision': 1}):
            with self.subTest(changes=changes), self.assertRaises(accounting.ConflictError):
                adjustments.reverse_refund(self.db, refund['id'], self.reversal_data(refund, **changes))
        for changes in ({'reason': ''}, {'reference': ''}, {'business_date': '2026-10-21'}, {'business_date': '2026-02-30'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                adjustments.reverse_refund(self.db, refund['id'], self.reversal_data(refund, **changes))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM supplier_refund_reversals').fetchone()[0], 0)

    def test_exact_retry_survives_closed_period_and_conflicting_request_keys_fail(self):
        payment = self.advance_payment()
        refund = self.refund(self.refund_data(payment['id']))
        data = self.reversal_data(refund)
        first = adjustments.reverse_refund(self.db, refund['id'], data)
        self.db.execute("INSERT INTO accounting_periods(month,status,revision,updated_at) VALUES('2026-10','closed',2,'now')")
        changes = self.db.total_changes
        retried = adjustments.reverse_refund(self.db, refund['id'], {**data, 'revision': 1})
        self.assertEqual(retried['reversal']['id'], first['reversal']['id'])
        self.assertEqual(self.db.total_changes, changes)
        with self.assertRaises(accounting.ConflictError):
            adjustments.reverse_refund(self.db, refund['id'], {**data, 'reason': 'سبب مختلف'})
        with self.assertRaises(accounting.ConflictError):
            adjustments.reverse_refund(self.db, refund['id'], {**data, 'request_key': 'another-key'})
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE supplier_refund_reversals SET reason=? WHERE refund_id=?', ('تغيير', refund['id']))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM supplier_refund_reversals WHERE refund_id=?', (refund['id'],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM supplier_refunds WHERE id=?', (refund['id'],))

    def test_global_request_key_cannot_reverse_a_second_refund(self):
        payment = self.advance_payment()
        one = self.refund(self.refund_data(payment['id'], amount='20'))
        two = self.refund(self.refund_data(payment['id'], amount='20'))
        data = self.reversal_data(one)
        adjustments.reverse_refund(self.db, one['id'], data)
        with self.assertRaises(accounting.ConflictError):
            adjustments.reverse_refund(self.db, two['id'], {**data, 'revision': two['revision']})
        self.assertEqual(adjustments.get_refund(self.db, two['id'])['effective_status'], 'posted')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM supplier_refund_reversals').fetchone()[0], 1)

    def test_closed_correction_date_is_blocked_but_original_closed_month_can_be_corrected_later(self):
        payment = self.advance_payment()
        refund = self.refund(self.refund_data(payment['id']))
        self.db.execute("INSERT INTO accounting_periods(month,status,revision,updated_at) VALUES('2026-10','closed',2,'now')")
        with self.assertRaisesRegex(ValueError, 'مقفولة'):
            adjustments.reverse_refund(self.db, refund['id'], self.reversal_data(refund))
        result = adjustments.reverse_refund(self.db, refund['id'], self.reversal_data(refund, business_date='2026-11-01'))
        self.assertEqual(result['reversal']['business_date'], '2026-11-01')
        self.assertEqual(adjustments.get_refund(self.db, refund['id'], '2026-10-31')['effective_status'], 'posted')
        self.assertTrue(reconciliation.report(self.db, '2026-10-31')['matched'])
        self.assertTrue(reconciliation.report(self.db, '2026-11-01')['matched'])

    def test_additive_initialization_preserves_existing_receipt_and_list_marks_reversal(self):
        payment = self.advance_payment()
        refund = self.refund(self.refund_data(payment['id']))
        original = self.raw_refund(refund['id'])
        # A database from the prior version has receipts but no correction table.
        self.db.execute('DROP TABLE supplier_refund_reversals')
        adjustments.initialize(self.db)
        adjustments.initialize(self.db)
        self.assertEqual(self.raw_refund(refund['id']), original)
        result = adjustments.reverse_refund(self.db, refund['id'], self.reversal_data(refund))
        listed = adjustments.list_refunds(self.db, 1)
        self.assertEqual(listed[0]['reversal']['code'], result['reversal']['code'])
        self.assertEqual(listed[0]['effective_status'], 'reversed')
        self.assertNotIn('request_fingerprint', listed[0])
        self.assertNotIn('request_fingerprint', listed[0]['reversal'])


if __name__ == '__main__':
    unittest.main()
