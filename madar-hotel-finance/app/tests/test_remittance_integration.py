"""Tax remittance periods, account ledgers and supplier AP stay consistent."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting_periods as periods
import ledger_reports
import payables
import supplier_adjustments as adjustments
import supplier_reconciliation
import withholding
import withholding_balances as balances
import withholding_remittances as remittances
import test_supplier_payments as fixtures


class RemittanceIntegration(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SupplierPayments('runTest')
        self.fixture.setUp()
        self.db = self.fixture.db
        adjustments.initialize(self.db)
        periods.initialize(self.db)
        remittances.initialize(self.db)
        self.serial = 0
        self.wht = self.fixture.account('213004')
        self.code = withholding.create_code(self.db, {
            'name': 'كود اختبار توريد', 'rate': '3', 'active': True, 'confirmed': True})
        self.invoice = self.fixture.invoice()
        self.payment = self.fixture.post(self.fixture.payload(
            amount='100', allocation_mode='invoice', target_invoice_id=self.invoice['id'],
            withholding_code_id=self.code['id']))

    def tearDown(self):
        self.fixture.tearDown()

    def key(self, kind):
        self.serial += 1
        return kind + '-' + str(self.serial)

    def data(self, amount='1', day='2026-10-20', **extra):
        return {'request_key': self.key('remit'), 'business_date': day,
                'period_from': '2026-10-01', 'period_to': '2026-10-16',
                'authority_name': 'الجهة الضريبية في بيانات الاختبار',
                'reference': self.key('RECEIPT'), 'method': 'cash',
                'funding_account_id': self.fixture.cash,
                'details': {'received_by': 'مستلم الجهة', 'receipt_reference': self.key('RCPT')},
                'allocations': [{'payment_id': self.payment['id'], 'amount': amount}], **extra}

    def posted(self, amount='1', day='2026-10-20'):
        saved = remittances.save(self.db, self.data(amount, day))
        return remittances.approve(self.db, saved['id'], {'revision': saved['revision']})

    def reversal(self, doc, day='2026-10-25'):
        return {'request_key': self.key('reverse-remit'), 'revision': doc['revision'],
                'event_revision': 0, 'business_date': day, 'reference': self.key('REV'),
                'reason': 'تصحيح مستند توريد سجل بالخطأ'}

    def event(self, revision=1):
        return {'request_key': self.key('period'), 'revision': revision,
                'reference': self.key('REVIEW'), 'reason': 'مراجعة القيود والموردين والخصم والتوريدات'}

    def force_closed(self):
        self.db.execute("INSERT INTO accounting_periods(month,status,updated_at) VALUES('2026-10','closed','test')")

    def test_remittance_draft_blocks_its_month_and_later_month_until_cancelled(self):
        doc = remittances.save(self.db, self.data())
        october = periods.preview(self.db, '2026-10')
        self.assertFalse(october['can_close'])
        self.assertEqual(october['draft_counts']['withholding_remittance'], 1)
        own = next(item for item in october['pending_drafts'] if item['type'] == 'withholding_remittance')
        self.assertEqual((own['id'], own['label']), (doc['id'], 'توريد خصم ضريبي'))
        november = periods.preview(self.db, '2026-11')
        self.assertFalse(november['can_close'])
        self.assertTrue(any(item['id'] == doc['id'] and item['type'] == 'withholding_remittance'
                            for item in november['earlier_pending_drafts']))
        with self.assertRaises(ValueError):
            periods.close(self.db, '2026-10', self.event())
        remittances.save(self.db, {'revision': doc['revision'], 'action': 'cancel'}, doc['id'])
        self.assertTrue(periods.preview(self.db, '2026-10')['can_close'])
        self.assertTrue(periods.preview(self.db, '2026-11')['can_close'])

    def test_locked_month_blocks_draft_edit_move_cancel_and_post(self):
        data = self.data()
        doc = remittances.save(self.db, data)
        self.force_closed()
        for changes in ({'notes': 'تعديل'}, {'business_date': '2026-11-01'}, {'action': 'cancel'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'مقفولة'):
                remittances.save(self.db, {**data, **changes, 'revision': doc['revision']}, doc['id'])
        with self.assertRaisesRegex(ValueError, 'مقفولة'):
            remittances.approve(self.db, doc['id'], {'revision': doc['revision']})
        self.assertEqual(remittances.detail(self.db, doc['id'])['revision'], doc['revision'])

    def test_posted_remittance_and_its_reversal_are_readable_from_general_ledger(self):
        doc = self.posted()
        reversed_doc = remittances.reverse(self.db, doc['id'], self.reversal(doc))
        reversal = reversed_doc['reversal']
        ledger = ledger_reports.account_ledger(self.db, self.wht, '2026-10-01', '2026-10-31', 1)
        rows = {row['source_type']: row for row in ledger['rows']}
        remit = rows['withholding_remittance']
        rev = rows['withholding_remittance_reversal']
        self.assertEqual((remit['document_code'], remit['reference']), (doc['code'], doc['reference']))
        self.assertEqual(rev['document_code'], reversal['code'])
        self.assertEqual(rev['original_document_code'], doc['code'])
        self.assertEqual(rev['reference'], reversal['reference'])
        self.assertEqual((remit['debit_cents'], rev['credit_cents']), (100, 100))
        self.assertEqual(ledger['closing_balance_cents'], -300)
        self.assertTrue(ledger['complete'])
        self.assertTrue(ledger_reports.trial_balance(self.db, '2026-10-01', '2026-10-31')['balanced'])

    def test_tax_remittance_supplier_trace_does_not_change_ap_or_aging(self):
        before = payables.supplier_balances(self.db, 1, '2026-10-31')
        doc = self.posted()
        after = payables.supplier_balances(self.db, 1, '2026-10-31')
        self.assertEqual(after, before)
        self.assertEqual(after['net_payable_cents'], 0)
        statement = payables.statement(self.db, 1, '2026-10-31')
        row = next(row for row in statement['rows'] if row['type'] == 'withholding_remittance')
        self.assertEqual(row['description'], 'توريد خصم ضريبي')
        self.assertEqual(row['code'], doc['code'])
        self.assertEqual((row['debit_cents'], row['credit_cents'], row['balance_cents']), (0, 0, 0))
        self.assertTrue(supplier_reconciliation.report(self.db, '2026-10-31')['matched'])
        self.assertEqual(payables.aging(self.db, '2026-10-31')['totals']['total_cents'], 0)

    def test_closed_original_month_allows_open_dated_correction_and_safe_retry(self):
        doc = self.posted()
        october = periods.preview(self.db, '2026-10')
        self.assertTrue(october['withholding_reconciliation']['matched'])
        closed = periods.close(self.db, '2026-10', self.event())
        self.assertEqual(closed['period']['status'], 'closed')
        with self.assertRaisesRegex(ValueError, 'مقفولة'):
            remittances.reverse(self.db, doc['id'], self.reversal(doc))
        correction = self.reversal(doc, '2026-11-01')
        result = remittances.reverse(self.db, doc['id'], correction)
        november = periods.preview(self.db, '2026-11')
        self.assertTrue(november['can_close'], november['blockers'])
        periods.close(self.db, '2026-11', self.event())
        retry = remittances.reverse(self.db, doc['id'], correction)
        self.assertEqual(retry['reversal']['id'], result['reversal']['id'])
        ledger = ledger_reports.account_ledger(self.db, self.wht, '2026-10-01', '2026-10-31')
        self.assertEqual(ledger['closing_balance_cents'], -200)
        self.assertEqual(remittances.detail(self.db, doc['id'])['reversal']['business_date'], '2026-11-01')

    def test_source_payment_reversal_after_remittance_retains_valid_tax_credit(self):
        self.posted('3')
        adjustments.reverse_payment(self.db, self.payment['id'], {
            'request_key': self.key('reverse-payment'), 'revision': self.payment['revision'],
            'event_revision': 0, 'business_date': '2026-10-22',
            'reference': self.key('PAY-REV'), 'reason': 'تصحيح سداد بعد تسجيل توريد الضريبة'})
        review = periods.preview(self.db, '2026-10')
        self.assertTrue(review['withholding_reconciliation']['matched'])
        self.assertTrue(review['can_close'], review['blockers'])
        self.assertEqual(payables.supplier_balances(self.db, 1, '2026-10-31')['payable_balance_cents'], 10000)
        ledger = ledger_reports.account_ledger(self.db, self.wht, '2026-10-01', '2026-10-31')
        self.assertEqual(ledger['closing_balance_cents'], 300)
        self.assertTrue(supplier_reconciliation.report(self.db, '2026-10-31')['matched'])
        self.assertTrue(balances.balance_report(self.db, '2026-10-31')['matched'])

    def test_balanced_orphan_wh_journal_blocks_close_even_when_supplier_ap_matches(self):
        payables.create_journal(self.db, 'test_orphan_tax', 1, '2026-10-23', [
            {'account_id': self.fixture.cash, 'debit_cents': 100},
            {'account_id': self.wht, 'credit_cents': 100}])
        self.assertTrue(supplier_reconciliation.report(self.db, '2026-10-31')['matched'])
        self.assertTrue(ledger_reports.trial_balance(self.db, '2026-10-01', '2026-10-31')['balanced'])
        review = periods.preview(self.db, '2026-10')
        self.assertFalse(review['withholding_reconciliation']['matched'])
        self.assertIn('withholding_reconciliation', {item['code'] for item in review['blockers']})
        with self.assertRaises(ValueError):
            periods.close(self.db, '2026-10', self.event())


if __name__ == '__main__':
    unittest.main(verbosity=2)
