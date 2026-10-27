"""Input-tax financial invariants with operator-supplied test rates only."""
import sqlite3
import unittest
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting
import payables
import taxation
import test_payables as fixture


class InputTaxes(unittest.TestCase):
    def setUp(self):
        fixture.PayablesAccounting.setUp(self)
        taxation.initialize(self.db)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def registered(self):
        s = taxation.settings(self.db)
        return taxation.update_settings(self.db, {'revision': s['revision'], 'registration_status': 'registered', 'tax_number': 'TEST-NUMBER'})

    def code(self, rate='14', recovery='100', **extra):
        return taxation.create_code(self.db, {'name': 'كود اختبار ' + str(self.db.execute('SELECT COUNT(*) FROM tax_codes').fetchone()[0]), 'kind': 'vat', 'rate': rate, 'recoverable_percent': recovery, 'active': True, 'confirmed': True, **extra})

    def draft(self, code=None, price=10000, quantity='1', business_date='2026-01-01'):
        invoice = fixture.PayablesAccounting.make_invoice(self, price=price, qty=quantity, business_date=business_date, approve=False)
        values = tuple(invoice[key] for key in ('supplier_id', 'reference', 'business_date', 'department_id', 'notes', 'subtotal_cents', 'tax_basis', 'tax_cents', 'total_cents'))
        lines = [tuple(line[key] for key in ('product_id', 'name', 'unit', 'quantity', 'price_cents', 'total_cents')) for line in invoice['lines']]
        updated, plan = taxation.prepare_invoice(self.db, {'tax_code_id': code['id'] if code else None, 'tax_rate': '0'}, values, lines)
        self.db.execute('UPDATE invoices SET subtotal_cents=?,tax_basis=?,tax_cents=?,total_cents=? WHERE id=?', (*updated[5:], invoice['id']))
        taxation.save_invoice_tax(self.db, invoice['id'], plan)
        return payables.enrich_invoice(self.db, dict(self.db.execute('SELECT * FROM invoices WHERE id=?', (invoice['id'],)).fetchone()))

    def approve(self, invoice):
        return payables.post_invoice(self.db, invoice['id'], {'revision': invoice['revision']})

    def confirm_vat(self):
        self.db.execute("UPDATE account_mappings SET status='confirmed' WHERE module='الضرائب' AND role='recoverable_input_vat'")
        return self.db.execute("SELECT account_id FROM account_mappings WHERE module='الضرائب' AND role='recoverable_input_vat'").fetchone()[0]

    def returned(self, invoice, quantity, reference='RET', date='2026-02-01', approve=True):
        draft = payables.save_return(self.db, {'request_key': reference, 'invoice_id': invoice['id'], 'reference': reference, 'business_date': date, 'lines': [{'invoice_line_id': invoice['lines'][0]['id'], 'quantity': quantity}]})
        return payables.approve_return(self.db, draft['id'], {'revision': draft['revision']}) if approve else draft

    def test_initialization_does_not_infer_registration_rate_or_tax_codes(self):
        self.assertEqual(taxation.settings(self.db)['registration_status'], 'unconfigured')
        self.assertEqual(taxation.snapshot(self.db)['codes'], [])
        plain = self.draft()
        self.assertEqual(plain['total_cents'], 10000)
        self.assertIsNone(plain['tax_code_id'])
        with self.assertRaises(ValueError):
            taxation.prepare_invoice(self.db, {'tax_rate': '14'}, (1, '', '', 1, '', 100, 0, 0, 100), [(1, '', '', '1', 100, 100)])
        taxation.initialize(self.db)
        self.assertEqual(taxation.snapshot(self.db)['codes'], [])

    def test_registration_and_explicit_code_validation(self):
        with self.assertRaises(ValueError):
            taxation.update_settings(self.db, {'revision': 1, 'registration_status': 'registered'})
        with self.assertRaises(ValueError):
            self.code(rate='14.001')
        with self.assertRaises(ValueError):
            taxation.create_code(self.db, {'name': 'مفقود', 'kind': 'vat', 'rate': '14'})
        with self.assertRaises(ValueError):
            self.code(rate='0')
        code = self.code()
        with self.assertRaisesRegex(ValueError, 'التسجيل'):
            self.draft(code)
        self.registered()
        inactive = taxation.update_code(self.db, code['id'], {'revision': code['revision'], 'active': False})
        with self.assertRaises(ValueError):
            self.draft(inactive)
        active = taxation.update_code(self.db, code['id'], {'revision': inactive['revision'], 'active': True, 'confirmed': False})
        with self.assertRaises(ValueError):
            self.draft(active)

    def test_recoverable_vat_posts_separately_and_preserves_net_purchase_price(self):
        self.registered(); vat = self.confirm_vat(); code = self.code()
        invoice = self.approve(self.draft(code, quantity='2'))
        self.assertEqual((invoice['subtotal_cents'], invoice['tax_cents'], invoice['total_cents']), (20000, 2800, 22800))
        self.assertEqual(invoice['tax_recoverable_cents'], 2800)
        rows = self.db.execute('SELECT * FROM journal_lines WHERE journal_id=?', (invoice['journal_id'],)).fetchall()
        self.assertEqual(sum(r['debit_cents'] for r in rows), sum(r['credit_cents'] for r in rows))
        self.assertEqual(sum(r['debit_cents'] for r in rows if r['account_id'] == vat), 2800)
        self.assertEqual(sum(r['debit_cents'] for r in rows if r['account_id'] == self.expense), 20000)
        self.assertEqual(self.db.execute('SELECT price_cents FROM products WHERE id=1').fetchone()[0], 10000)
        self.assertEqual(payables.supplier_balances(self.db, 1, '2026-01-31')['payable_balance_cents'], 22800)
        self.assertEqual(invoice['tax_registration_number'], 'TEST-NUMBER')

    def test_partial_recovery_and_nonrecoverable_vat_capitalize_original_expense(self):
        self.registered(); vat = self.confirm_vat(); code = self.code(recovery='50')
        invoice = self.approve(self.draft(code))
        rows = self.db.execute('SELECT * FROM journal_lines WHERE journal_id=?', (invoice['journal_id'],)).fetchall()
        self.assertEqual(sum(r['debit_cents'] for r in rows if r['account_id'] == vat), 700)
        self.assertEqual(sum(r['debit_cents'] for r in rows if r['account_id'] == self.expense), 10700)
        returned = self.returned(invoice, '0.5')
        self.assertEqual((returned['subtotal_cents'], returned['tax_cents'], returned['total_cents']), (5000, 700, 5700))
        self.assertEqual((returned['tax_recoverable_cents'], returned['tax_nonrecoverable_cents']), (350, 350))
        self.assertEqual(self.db.execute('SELECT SUM(credit_cents) FROM journal_lines WHERE journal_id=? AND account_id=?', (returned['journal_id'], self.expense)).fetchone()[0], 5350)

    def test_not_registered_requires_no_recovery_but_keeps_supplier_vat_cost(self):
        taxation.update_settings(self.db, {'revision': 1, 'registration_status': 'not_registered'})
        recoverable = self.code(recovery='100')
        with self.assertRaisesRegex(ValueError, 'القابلة للخصم'):
            self.draft(recoverable)
        code = self.code(recovery='0')
        invoice = self.approve(self.draft(code))
        self.assertEqual(invoice['tax_recoverable_cents'], 0)
        self.assertEqual(invoice['tax_nonrecoverable_cents'], 1400)
        rows = self.db.execute('SELECT * FROM journal_lines WHERE journal_id=?', (invoice['journal_id'],)).fetchall()
        self.assertEqual(sum(r['debit_cents'] for r in rows if r['account_id'] == self.expense), 11400)

    def test_zero_rate_exempt_and_out_of_scope_keep_distinct_classification(self):
        self.registered()
        for kind in ('zero_rated', 'exempt', 'out_of_scope'):
            code = taxation.create_code(self.db, {'name': kind, 'kind': kind, 'confirmed': True})
            invoice = self.approve(self.draft(code))
            self.assertEqual(invoice['tax_kind'], kind)
            self.assertEqual(invoice['tax_cents'], 0)
            self.assertEqual(invoice['total_cents'], 10000)
            with self.assertRaises(ValueError):
                taxation.update_code(self.db, code['id'], {'revision': 1, 'rate': '1'})

    def test_stale_code_settings_and_mapping_require_resave_before_posting(self):
        self.registered(); self.confirm_vat(); code = self.code()
        draft = self.draft(code)
        code = taxation.update_code(self.db, code['id'], {'revision': code['revision'], 'rate': '10'})
        with self.assertRaises(accounting.ConflictError):
            self.approve(draft)
        fresh = self.draft(code)
        status = taxation.settings(self.db)
        taxation.update_settings(self.db, {'revision': status['revision'], 'notes': 'مراجعة جديدة'})
        with self.assertRaises(accounting.ConflictError):
            self.approve(fresh)
        last = self.draft(code)
        mapping = self.db.execute("SELECT * FROM account_mappings WHERE module='الضرائب' AND role='recoverable_input_vat'").fetchone()
        other = self.db.execute("SELECT id FROM accounts WHERE code='117005'").fetchone()[0]
        accounting.update_mapping(self.db, mapping['id'], {'revision': mapping['revision'], 'account_id': other, 'status': 'confirmed'})
        with self.assertRaises(accounting.ConflictError):
            self.approve(last)

    def test_unconfirmed_input_vat_account_blocks_recovery_approval_not_draft(self):
        self.registered(); code = self.code()
        draft = self.draft(code)
        self.assertEqual(draft['tax_cents'], 1400)
        with self.assertRaisesRegex(ValueError, 'ربط حساب'):
            self.approve(draft)
        self.confirm_vat()
        self.approve(draft)

    def test_tax_snapshots_immutable_and_returns_use_original_rates_accounts(self):
        self.registered(); vat = self.confirm_vat(); code = self.code(recovery='50')
        invoice = self.approve(self.draft(code))
        taxation.update_code(self.db, code['id'], {'revision': code['revision'], 'rate': '5', 'recoverable_percent': '0'})
        status = taxation.settings(self.db)
        taxation.update_settings(self.db, {'revision': status['revision'], 'tax_number': 'CHANGED-NUMBER'})
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE invoice_tax_lines SET tax_cents=0 WHERE invoice_id=?', (invoice['id'],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM invoice_tax_settings WHERE invoice_id=?', (invoice['id'],))
        returned = self.returned(invoice, '0.5')
        self.assertEqual((returned['tax_cents'], returned['total_cents']), (700, 5700))
        rows = self.db.execute('SELECT * FROM journal_lines WHERE journal_id=?', (returned['journal_id'],)).fetchall()
        self.assertEqual(sum(r['credit_cents'] for r in rows if r['account_id'] == vat), 350)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE supplier_return_tax_lines SET net_cents=1 WHERE return_id=?', (returned['id'],))
        original = payables.enrich_invoice(self.db, dict(self.db.execute('SELECT * FROM invoices WHERE id=?', (invoice['id'],)).fetchone()))
        self.assertEqual(original['tax_registration_number'], 'TEST-NUMBER')

    def test_repeated_fractional_returns_reconcile_every_original_tax_component(self):
        self.registered(); self.confirm_vat()
        for price, quantity, rate, recovery in [(1, '3', '100', '50'), (1, '1.5', '100', '66.67'), (3, '3', '14', '50'), (7, '1.5', '33.33', '33.33')]:
            code = self.code(rate=rate, recovery=recovery)
            invoice = self.approve(self.draft(code, price=price, quantity=quantity))
            count = int(Decimal(quantity) / Decimal('.1'))
            returns = [self.returned(invoice, '.1', reference=f'ROUND-{invoice["id"]}-{i}') for i in range(count)]
            self.assertEqual(sum(r['subtotal_cents'] for r in returns), invoice['subtotal_cents'])
            self.assertEqual(sum(r['tax_cents'] for r in returns), invoice['tax_cents'])
            self.assertEqual(sum(r['tax_recoverable_cents'] for r in returns), invoice['tax_recoverable_cents'])
            self.assertEqual(sum(r['tax_nonrecoverable_cents'] for r in returns), invoice['tax_nonrecoverable_cents'])
            self.assertEqual(sum(r['total_cents'] for r in returns), invoice['total_cents'])
            self.assertTrue(all(r['tax_recoverable_cents'] >= 0 and r['tax_nonrecoverable_cents'] >= 0 for r in returns))
            with self.assertRaises(ValueError):
                taxation.return_line_amounts(self.db, invoice['lines'][0]['id'], '.1')

    def test_draft_tax_snapshots_cannot_be_moved_onto_posted_document(self):
        self.registered(); self.confirm_vat(); code = self.code()
        posted_plain = self.approve(self.draft())
        draft = self.draft(code)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE invoice_tax_settings SET invoice_id=? WHERE invoice_id=?', (posted_plain['id'], draft['id']))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE invoice_tax_lines SET invoice_id=? WHERE invoice_id=?', (posted_plain['id'], draft['id']))

    def test_tax_report_is_signed_historical_and_uses_saved_values(self):
        self.registered(); self.confirm_vat(); code = self.code()
        invoice = self.approve(self.draft(code, quantity='2'))
        self.returned(invoice, '0.5', date='2026-02-01')
        january = taxation.report(self.db, '2026-01-01', '2026-01-31')
        through_february = taxation.report(self.db, '2026-01-01', '2026-02-28')
        self.assertEqual(january['totals']['tax_cents'], 2800)
        self.assertEqual(through_february['totals'], {'net_cents': 15000, 'tax_cents': 2100, 'recoverable_cents': 2100, 'nonrecoverable_cents': 0, 'gross_cents': 17100})
        self.assertEqual(through_february['rows'][1]['source_type'], 'supplier_return')
        with self.assertRaises(ValueError):
            taxation.report(self.db, '2026-02-01', '2026-01-01')

    def test_preview_rounds_each_line_and_validates_decimal_inputs_without_writes(self):
        self.registered(); self.confirm_vat(); code = self.code(rate='50', recovery='50')
        result = taxation.preview(self.db, {'tax_code_id': code['id'], 'lines': [{'price': '.01', 'quantity': '1'}, {'price': '.01', 'quantity': '1'}]})
        self.assertEqual((result['subtotal_cents'], result['tax_cents'], result['total_cents']), (2, 2, 4))
        self.assertEqual(result['tax_recoverable_cents'], 2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM invoice_tax_settings').fetchone()[0], 0)
        for bad in ['NaN', '-1', '0.001', True]:
            with self.assertRaises(ValueError):
                taxation.preview(self.db, {'tax_code_id': code['id'], 'lines': [{'price': bad, 'quantity': '1'}]})


if __name__ == '__main__':
    unittest.main(verbosity=2)
