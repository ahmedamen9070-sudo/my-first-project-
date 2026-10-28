"""Gross supplier settlement, net disbursement and withholding remain distinct."""
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting
import accounting_periods
import payables
import supplier_adjustments as adjustments
import supplier_payments as payments
import supplier_reconciliation as reconciliation
import taxation
import withholding
import test_supplier_payments as fixtures


class WithholdingPayments(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SupplierPayments('runTest')
        self.fixture.setUp()
        self.db = self.fixture.db
        adjustments.initialize(self.db)
        self.wht = self.fixture.account('213004')
        self.code = withholding.create_code(self.db, {
            'name': 'خصم اختبار يحدده المشغل', 'rate': '1', 'active': True, 'confirmed': True})

    def tearDown(self):
        self.fixture.tearDown()

    def data(self, invoice, method='cash', **extra):
        return self.fixture.payload(method, allocation_mode='invoice',
            target_invoice_id=invoice['id'], withholding_code_id=self.code['id'], **extra)

    def posted(self, data):
        return self.fixture.post(data)

    def vat_invoice(self, net=10000, rate='14'):
        """A real VAT snapshot and approved invoice, without a fabricated journal."""
        taxation.initialize(self.db)
        current = taxation.settings(self.db)
        if current['registration_status'] == 'unconfigured':
            taxation.update_settings(self.db, {'revision': current['revision'],
                                               'registration_status': 'not_registered'})
        code = taxation.create_code(self.db, {'name': 'VAT TEST ' + str(self.fixture.counter),
            'kind': 'vat', 'rate': rate, 'recoverable_percent': '0', 'active': True, 'confirmed': True})
        self.fixture.counter += 1
        stamp = str(self.fixture.counter)
        invoice_id = self.db.execute('''INSERT INTO invoices(code,request_key,supplier_id,reference,
            business_date,department_id,status,subtotal_cents,total_cents,created_at,updated_at)
            VALUES(?,?,1,?,'2026-10-01',1,'draft',?,?,'now','now')''',
            ('VAT-' + stamp, 'VAT-key-' + stamp, 'VAT-ref-' + stamp, net, net)).lastrowid
        self.db.execute('''INSERT INTO invoice_lines(invoice_id,product_id,name,unit,quantity,price_cents,total_cents)
            VALUES(?,1,'خدمة ضريبية','خدمة','1',?,?)''', (invoice_id, net, net))
        row = dict(self.db.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone())
        values = tuple(row[k] for k in ('supplier_id', 'reference', 'business_date', 'department_id', 'notes',
                                        'subtotal_cents', 'tax_basis', 'tax_cents', 'total_cents'))
        updated, plan = taxation.prepare_invoice(self.db, {'tax_code_id': code['id']}, values,
                                                 [(1, 'خدمة ضريبية', 'خدمة', '1', net, net)])
        self.db.execute('UPDATE invoices SET subtotal_cents=?,tax_basis=?,tax_cents=?,total_cents=? WHERE id=?',
                        (*updated[5:], invoice_id))
        taxation.save_invoice_tax(self.db, invoice_id, plan)
        return payables.post_invoice(self.db, invoice_id, {'revision': 1, 'due_date': '2026-10-15'})

    def test_all_six_methods_settle_gross_and_disburse_net(self):
        for method in ('cash', 'cheque', 'bank_transfer', 'bank_deposit', 'instapay', 'bank_card'):
            with self.subTest(method=method):
                invoice = self.fixture.invoice()
                data = self.data(invoice, method)
                preview = payments.allocation_preview(self.db, data)
                self.assertEqual((preview['allocated_cents'], preview['withholding_base_cents'],
                                  preview['withholding_cents'], preview['net_cash_cents']), (3000, 3000, 30, 2970))
                self.assertEqual(preview['funding_balance_after_cents'],
                                 preview['funding_balance_before_cents'] - (0 if method == 'cheque' else 2970))
                saved = self.posted(data)
                self.assertEqual(saved['amount_cents'], 3000)
                self.assertEqual(saved['allocations'][0]['amount_cents'], 3000)
                self.assertEqual(saved['net_cash_cents'], 2970)
                self.assertEqual(saved['withholding_cents'], 30)
                self.assertEqual(saved['advance_cents'], 0)
                lines = saved['journal']['lines']
                self.assertEqual(sum(l['debit_cents'] for l in lines), 3000)
                self.assertEqual(sum(l['credit_cents'] for l in lines), 3000)
                self.assertEqual(next(l['credit_cents'] for l in lines if l['account_id'] == self.wht), 30)
                funding = self.fixture.cheques if method == 'cheque' else data['funding_account_id']
                self.assertEqual(next(l['credit_cents'] for l in lines if l['account_id'] == funding), 2970)
                self.assertEqual(payables.invoice_outstanding(self.db, invoice['id']), 7000)
                self.assertTrue(reconciliation.report(self.db, '2026-10-31')['matched'])

    def test_vat_is_excluded_from_partial_payment_basis(self):
        invoice = self.vat_invoice()
        self.assertEqual(invoice['total_cents'], 11400)
        code = withholding.update_code(self.db, self.code['id'], {'revision': self.code['revision'], 'rate': '2'})
        first = self.posted(self.data(invoice, amount='57'))
        second = self.posted(self.data(invoice, amount='57'))
        for doc in (first, second):
            self.assertEqual((doc['amount_cents'], doc['withholding_base_cents'],
                              doc['withholding_cents'], doc['net_cash_cents']), (5700, 5000, 100, 5600))
            self.assertEqual(doc['withholding']['rate_basis'], 200)
        self.assertEqual(payables.invoice_outstanding(self.db, invoice['id']), 0)
        self.assertEqual(payments.account_balance(self.db, self.fixture.cash), -11200)
        self.assertEqual(payments.account_balance(self.db, self.wht), -200)
        self.assertEqual(payables.supplier_balances(self.db, 1)['advance_cents'], 0)
        self.assertEqual(code['rate_basis'], 200)

    def test_changed_code_requires_resave_but_preserves_saved_draft_until_then(self):
        invoice = self.fixture.invoice()
        data = self.data(invoice, amount='100')
        draft = payments.save_payment(self.db, data)
        withholding.update_code(self.db, self.code['id'], {'revision': self.code['revision'], 'rate': '3'})
        self.assertEqual(payments.get_payment(self.db, draft['id'])['withholding_cents'], 100)
        with self.assertRaises(accounting.ConflictError):
            payments.approve_payment(self.db, draft['id'], {'revision': draft['revision']})
        updated = payments.update_payment(self.db, draft['id'], {**data, 'revision': draft['revision']})
        self.assertEqual(updated['withholding_cents'], 300)
        posted = payments.approve_payment(self.db, draft['id'], {'revision': updated['revision']})
        self.assertEqual(posted['net_cash_cents'], 9700)

    def test_changed_fifo_invoice_allocation_requires_resave(self):
        first = self.fixture.invoice()
        second = self.fixture.invoice()
        data = self.fixture.payload(amount='50', allocation_mode='fifo', withholding_code_id=self.code['id'])
        draft = payments.save_payment(self.db, data)
        self.posted(self.fixture.payload(amount='100', allocation_mode='invoice', target_invoice_id=first['id']))
        with self.assertRaises(accounting.ConflictError):
            payments.approve_payment(self.db, draft['id'], {'revision': draft['revision']})
        updated = payments.update_payment(self.db, draft['id'], {**data, 'revision': draft['revision']})
        posted = payments.approve_payment(self.db, draft['id'], {'revision': updated['revision']})
        self.assertEqual(posted['allocations'][0]['invoice_id'], second['id'])
        self.assertEqual(posted['withholding']['lines'][0]['invoice_id'], second['id'])

    def test_explicit_null_preserves_legacy_fingerprints_and_idempotent_retries(self):
        invoice = self.fixture.invoice()
        data = self.fixture.payload(allocation_mode='invoice', target_invoice_id=invoice['id'])
        normalized = payments._payload(self.db, data)
        self.assertNotIn('withholding_code_id', normalized)
        digest = hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                                           separators=(',', ':')).encode()).hexdigest()
        draft = payments.save_payment(self.db, data)
        self.assertEqual(self.db.execute('SELECT request_fingerprint FROM supplier_payments WHERE id=?',
                                        (draft['id'],)).fetchone()[0], digest)
        self.assertEqual(payments.save_payment(self.db, {**data, 'withholding_code_id': None})['id'], draft['id'])
        posted = payments.approve_payment(self.db, draft['id'], {'revision': draft['revision']})
        self.assertEqual(posted['withholding_cents'], 0)
        self.assertEqual(posted['net_cash_cents'], posted['amount_cents'])
        self.assertEqual(len(posted['journal']['lines']), 2)
        self.assertEqual(payments.save_payment(self.db, data)['id'], posted['id'])
        self.assertEqual(payments.approve_payment(self.db, posted['id'], {'revision': 1})['journal_entry_id'], posted['journal_entry_id'])

    def test_withholding_never_taxes_advance_or_mixed_excess(self):
        for data in (self.fixture.payload(amount='10', allow_advance=True, withholding_code_id=self.code['id']),
                     self.data(self.fixture.invoice(), amount='110', allow_advance=True)):
            with self.assertRaisesRegex(ValueError, 'دفعة|مقدمة|الفواتير'):
                payments.save_payment(self.db, data)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM supplier_payments').fetchone()[0], 0)

    def test_draft_can_remove_withholding_without_leaving_stale_tax_lines(self):
        invoice = self.fixture.invoice()
        data = self.data(invoice, amount='100')
        draft = payments.save_payment(self.db, data)
        no_tax = payments.update_payment(self.db, draft['id'], {**data, 'revision': draft['revision'],
                                                               'withholding_code_id': None})
        self.assertEqual(no_tax['withholding_cents'], 0)
        self.assertIsNone(no_tax['withholding'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM payment_withholding_lines').fetchone()[0], 0)
        posted = payments.approve_payment(self.db, no_tax['id'], {'revision': no_tax['revision']})
        self.assertEqual(posted['net_cash_cents'], 10000)
        self.assertEqual(payments.account_balance(self.db, self.wht), 0)

    def test_cheque_clears_original_net_liability_after_mapping_changes_then_bounces(self):
        invoice = self.fixture.invoice()
        posted = self.posted(self.data(invoice, 'cheque', amount='100'))
        self.assertEqual(payments.account_balance(self.db, self.fixture.bank), 0)
        self.assertEqual(payments.account_balance(self.db, self.fixture.cheques), -9900)
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الموردون' AND role='issued_cheques'",
                        (self.fixture.account('217003'),))
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الضرائب' AND role='withholding_payable'",
                        (self.fixture.account('213005'),))
        cleared = payments.clear_cheque(self.db, posted['id'], {'revision': posted['revision'],
            'clearance_date': '2026-10-21', 'reference': 'BANK-CLEAR-NET', 'bank_account_id': self.fixture.bank})
        self.assertEqual(payments.account_balance(self.db, self.fixture.bank), -9900)
        self.assertEqual(payments.account_balance(self.db, self.fixture.cheques), 0)
        self.assertEqual(payments.account_balance(self.db, self.wht), -100)
        self.assertEqual(sum(l['debit_cents'] for l in cleared['clearance_journal']['lines']), 9900)
        adjustments.cheque_event(self.db, posted['id'], {'request_key': 'BOUNCE-WHT', 'action': 'bounce',
            'revision': cleared['revision'], 'event_revision': 0, 'business_date': '2026-10-23',
            'reference': 'BANK-BOUNCE', 'reason': 'ارتداد شيك الاختبار'})
        self.assertEqual(payments.account_balance(self.db, self.fixture.bank), 0)
        self.assertEqual(payments.account_balance(self.db, self.fixture.cheques), 0)
        self.assertEqual(payments.account_balance(self.db, self.wht), 0)
        self.assertEqual(payables.invoice_outstanding(self.db, invoice['id']), 10000)
        self.assertTrue(reconciliation.report(self.db, '2026-10-23')['matched'])

    def test_cash_reversal_restores_gross_payable_and_net_cash_by_date(self):
        invoice = self.fixture.invoice()
        posted = self.posted(self.data(invoice, amount='100'))
        old_tax_snapshot = posted['withholding']
        self.db.execute("UPDATE suppliers SET name='اسم جديد',tax_number='NEW-ID' WHERE id=1")
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الضرائب' AND role='withholding_payable'",
                        (self.fixture.account('213005'),))
        withholding.update_code(self.db, self.code['id'], {'revision': self.code['revision'], 'active': False})
        adjustments.reverse_payment(self.db, posted['id'], {'request_key': 'REVERSE-WHT',
            'revision': posted['revision'], 'event_revision': 0, 'business_date': '2026-10-25',
            'reference': 'CORRECT-CASH', 'reason': 'تصحيح سند صرف مكرر'})
        self.assertEqual(payments.account_balance(self.db, self.fixture.cash, '2026-10-24'), -9900)
        self.assertEqual(payments.account_balance(self.db, self.fixture.cash, '2026-10-25'), 0)
        self.assertEqual(payments.account_balance(self.db, self.wht, '2026-10-24'), -100)
        self.assertEqual(payments.account_balance(self.db, self.wht, '2026-10-25'), 0)
        self.assertEqual(payables.invoice_outstanding(self.db, invoice['id'], '2026-10-24'), 0)
        self.assertEqual(payables.invoice_outstanding(self.db, invoice['id'], '2026-10-25'), 10000)
        self.assertEqual(payments.get_payment(self.db, posted['id'])['withholding'], old_tax_snapshot)

    def test_post_retry_after_period_close_does_not_revalidate_current_code(self):
        invoice = self.fixture.invoice()
        data = self.data(invoice, amount='100')
        posted = self.posted(data)
        accounting_periods.initialize(self.db)
        closed = accounting_periods.close(self.db, '2026-10', {'request_key': 'CLOSE-WHT',
            'revision': 1, 'reference': 'REVIEW-WHT', 'reason': 'مراجعة الأرصدة الضريبية'})
        self.assertEqual(closed['period']['status'], 'closed')
        withholding.update_code(self.db, self.code['id'], {'revision': self.code['revision'], 'active': False})
        retry = payments.save_payment(self.db, data)
        self.assertEqual(retry['id'], posted['id'])
        self.assertEqual(payments.approve_payment(self.db, posted['id'], {'revision': 1})['journal_entry_id'],
                         posted['journal_entry_id'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
