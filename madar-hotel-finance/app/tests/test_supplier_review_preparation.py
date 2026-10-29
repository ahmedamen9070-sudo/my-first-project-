"""Current worklists stay complete, date-labelled and independent of posting."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting_periods
import payables
import supplier_adjustments
import supplier_payments
import supplier_review_preparation as preparation
import supplier_settlements
import withholding
import withholding_remittances
import test_supplier_payments as fixtures


class SupplierReviewPreparation(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SupplierPayments('runTest')
        self.fixture.setUp()
        self.db = self.fixture.db
        supplier_settlements.initialize(self.db)
        supplier_adjustments.initialize(self.db)
        accounting_periods.initialize(self.db)
        withholding_remittances.initialize(self.db)
        self.serial = 0

    def tearDown(self):
        self.fixture.tearDown()

    def key(self, prefix):
        self.serial += 1
        return prefix + '-' + str(self.serial)

    def invoice_draft(self, ds='2026-10-10', amount=3211, status='draft'):
        key = self.key('draft-invoice')
        return self.db.execute('''INSERT INTO invoices(code,request_key,supplier_id,reference,
            business_date,department_id,status,subtotal_cents,total_cents,created_at,updated_at)
            VALUES(?,?,1,?,?,1,?,?,?,'test','test')''',
            (key, key, 'REF-' + key, ds, status, amount, amount)).lastrowid

    def work(self, ds='2026-10-19'):
        return preparation.current_work(self.db, ds)

    def mapping(self):
        return self.db.execute("SELECT * FROM account_mappings WHERE module='الموردون' AND role='payable_control'").fetchone()

    def test_real_posted_invoice_legacy_draft_status_is_not_pending(self):
        posted = self.fixture.invoice()
        self.assertEqual(self.db.execute('SELECT status FROM invoices WHERE id=?', (posted['id'],)).fetchone()[0], 'draft')
        wanted = self.invoice_draft()
        self.invoice_draft(status='cancelled')
        result = self.work()
        self.assertEqual([(r['kind'], r['id']) for r in result['drafts']], [('invoice', wanted)])
        self.assertEqual(result['drafts'][0]['supplier_name'], 'مورد أغذية')
        self.assertEqual(result['drafts'][0]['amount_cents'], 3211)
        self.assertEqual(result['counts']['drafts_by_kind']['invoice'], 1)

    def test_six_real_draft_types_are_current_and_read_only(self):
        invoice = self.fixture.invoice()
        advance = self.fixture.post(self.fixture.payload(
            amount='10', payment_date='2026-10-02', allow_advance=True))
        taxable_invoice = self.fixture.invoice()
        code = withholding.create_code(self.db, {'name': 'كود اختبار مركز المراجعة',
            'rate': '3', 'active': True, 'confirmed': True})
        taxed_payment = self.fixture.post(self.fixture.payload(amount='100',
            allocation_mode='invoice', target_invoice_id=taxable_invoice['id'], withholding_code_id=code['id']))
        self.invoice_draft()
        payables.save_return(self.db, {'request_key': self.key('return'), 'invoice_id': invoice['id'],
            'business_date': '2026-10-20', 'reference': self.key('RET'),
            'lines': [{'invoice_line_id': invoice['lines'][0]['id'], 'quantity': '.1'}]})
        payment = supplier_payments.save_payment(self.db, self.fixture.payload(
            amount='1', allocation_mode='invoice', target_invoice_id=invoice['id']))
        supplier_settlements.save(self.db, {'request_key': self.key('settlement'), 'supplier_id': 1,
            'source_type': 'advance', 'source_id': advance['id'], 'business_date': '2026-10-20',
            'reference': self.key('SET'), 'allocations': [{'invoice_id': invoice['id'], 'amount': '1'}]})
        supplier_adjustments.save_refund(self.db, {'request_key': self.key('refund'), 'supplier_id': 1,
            'source_type': 'advance', 'source_id': advance['id'], 'business_date': '2026-10-20',
            'reference': self.key('RF'), 'reason': 'استرداد اختبار', 'amount': '1', 'method': 'cash',
            'funding_account_id': self.fixture.cash, 'details': {'received_by': 'أمين الخزينة'}})
        remittance = withholding_remittances.save(self.db, {'request_key': self.key('remittance'),
            'business_date': '2026-10-21', 'period_from': '2026-10-01', 'period_to': '2026-10-16',
            'authority_name': 'جهة اختبار', 'reference': self.key('TAX'), 'method': 'cash',
            'funding_account_id': self.fixture.cash, 'details': {'received_by': 'مستلم الجهة'},
            'allocations': [{'payment_id': taxed_payment['id'], 'amount': '1'}]})
        changes = self.db.total_changes
        schema = self.db.execute('PRAGMA schema_version').fetchone()[0]
        audit_count = self.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
        result = self.work()
        self.assertEqual({r['kind'] for r in result['drafts']},
                         {'invoice', 'return', 'payment', 'settlement', 'refund', 'remittance'})
        self.assertEqual(result['counts']['drafts'], 6)
        self.assertEqual(result['counts']['future_drafts'], 4)
        self.assertTrue(all(isinstance(r['amount_cents'], int) for r in result['drafts']))
        payment_row = next(r for r in result['drafts'] if r['kind'] == 'payment')
        self.assertEqual((payment_row['id'], payment_row['reference']), (payment['id'], 'ACK-1'))
        tax_row = next(r for r in result['drafts'] if r['kind'] == 'remittance')
        self.assertEqual(tax_row['id'], remittance['id'])
        self.assertIsNone(tax_row['supplier_id'])
        self.assertEqual(tax_row['authority_name'], 'جهة اختبار')
        self.assertEqual(self.work(), result)
        self.assertEqual(self.db.total_changes, changes)
        self.assertEqual(self.db.execute('PRAGMA schema_version').fetchone()[0], schema)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0], audit_count)

    def test_future_drafts_remain_visible_and_later_closed_month_locks_are_labelled(self):
        past = self.invoice_draft('2026-09-04')
        future_locked = self.invoice_draft('2026-11-15')
        future_open = self.invoice_draft('2026-12-01')
        self.db.execute("INSERT INTO accounting_periods(month,status,updated_at) VALUES('2026-11','closed','test')")
        result = self.work('2026-10-01')
        by_id = {r['id']: r for r in result['drafts']}
        self.assertEqual(len(by_id), 3)
        self.assertFalse(by_id[past]['future_dated'])
        self.assertTrue(by_id[future_locked]['future_dated'])
        self.assertTrue(by_id[future_open]['future_dated'])
        self.assertEqual(by_id[past]['locked_period'], '2026-11')
        self.assertEqual(by_id[future_locked]['locked_period'], '2026-11')
        self.assertIsNone(by_id[future_open]['locked_period'])
        self.assertEqual((result['counts']['future_drafts'], result['counts']['locked_drafts']), (2, 2))
        self.assertEqual(result['basis'], 'current_worklist')

    def test_more_than_500_drafts_are_returned_without_silent_truncation(self):
        self.fixture.invoice()
        self.db.executemany('''INSERT INTO invoices(code,request_key,supplier_id,reference,business_date,
            department_id,status,subtotal_cents,total_cents,created_at,updated_at)
            VALUES(?,?,1,?,'2026-10-01',1,'draft',7,7,'test','test')''',
            [(f'BULK-{n}', f'bulk-key-{n}', f'bulk-ref-{n}') for n in range(507)])
        result = self.work()
        self.assertEqual(result['counts']['drafts'], 507)
        self.assertEqual(result['counts']['drafts_by_kind']['invoice'], 507)
        self.assertEqual(sum(r['amount_cents'] for r in result['drafts']), 3549)
        self.assertIn('BULK-506', {r['code'] for r in result['drafts']})

    def test_optional_categories_tax_ids_and_feature_mappings_do_not_create_findings(self):
        self.db.execute("UPDATE account_mappings SET status='proposed' WHERE role<>'payable_control'")
        self.assertTrue(all(r[0] is None for r in self.db.execute('SELECT category_id FROM products')))
        self.assertEqual(self.work()['setup_issues'], [])

    def test_required_mapping_confirmation_and_account_integrity_are_reported(self):
        mapping = self.mapping()
        self.db.execute("UPDATE account_mappings SET status='proposed' WHERE id=?", (mapping['id'],))
        issue = next(r for r in self.work()['setup_issues'] if r['kind'] == 'mapping')
        self.assertEqual(issue['reasons'], ['unconfirmed'])
        self.assertEqual(issue['action'], {'type': 'accounting_mappings', 'id': mapping['id']})
        self.db.execute("UPDATE account_mappings SET status='confirmed',account_id=? WHERE id=?", (self.fixture.cash, mapping['id']))
        issue = next(r for r in self.work()['setup_issues'] if r['kind'] == 'mapping')
        self.assertEqual(set(issue['reasons']), {'invalid_side', 'invalid_class'})
        self.db.execute('DELETE FROM account_mappings WHERE id=?', (mapping['id'],))
        issue = next(r for r in self.work()['setup_issues'] if r['kind'] == 'mapping')
        self.assertEqual(issue['reasons'], ['missing_mapping'])

    def test_product_account_missing_wrong_class_group_and_inactive_findings(self):
        self.db.execute('UPDATE products SET expense_account_id=NULL WHERE id=1')
        issue = next(r for r in self.work()['setup_issues'] if r['kind'] == 'product')
        self.assertEqual((issue['entity_id'], issue['reasons']), (1, ['missing_account']))
        self.assertEqual(issue['action'], {'type': 'product', 'id': 1})
        self.db.execute('UPDATE products SET expense_account_id=? WHERE id=1', (self.fixture.cash,))
        issue = next(r for r in self.work()['setup_issues'] if r['kind'] == 'product')
        self.assertEqual(issue['reasons'], ['invalid_class'])
        group = self.db.execute("SELECT id FROM accounts WHERE posting=0 AND code LIKE '6%' LIMIT 1").fetchone()[0]
        self.db.execute('UPDATE products SET expense_account_id=? WHERE id=1', (group,))
        issue = next(r for r in self.work()['setup_issues'] if r['kind'] == 'product')
        self.assertIn('not_posting', issue['reasons'])
        self.db.execute('UPDATE products SET expense_account_id=? WHERE id=1', (self.fixture.expense,))
        self.db.execute('UPDATE accounts SET active=0 WHERE id=?', (self.fixture.expense,))
        issues = [r for r in self.work()['setup_issues'] if r['kind'] == 'product']
        self.assertEqual(len(issues), 2)
        self.assertTrue(all(r['reasons'] == ['inactive'] for r in issues))

    def test_missing_tables_and_legacy_missing_account_are_readable_without_initialization(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        try:
            before = db.total_changes
            result = preparation.current_work(db, '2026-10-01')
            self.assertEqual(result['drafts'], [])
            self.assertEqual(result['setup_issues'][0]['reasons'], ['missing_mapping'])
            self.assertEqual(db.total_changes, before)
            db.executescript('''CREATE TABLE products(id INTEGER PRIMARY KEY,name TEXT,expense_account_id INTEGER);
                INSERT INTO products VALUES(1,'بند قديم',999);''')
            result = preparation.current_work(db, '2026-10-01')
            issue = next(r for r in result['setup_issues'] if r['kind'] == 'product')
            self.assertEqual(issue['reasons'], ['missing_account'])
        finally:
            db.close()

    def test_as_of_requires_canonical_real_date(self):
        for value in (None, True, 20261001, '20261001', '2026-02-30', '2026-13-01', ' 2026-10-01', '2026-10-01T00:00:00'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                preparation.current_work(self.db, value)


if __name__ == '__main__':
    unittest.main(verbosity=2)
