"""Meaningful chart invariants; all writes use an isolated in-memory database."""
import csv
import io
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting


class ChartFoundation(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('CREATE TABLE audit(id INTEGER PRIMARY KEY,entity TEXT,entity_id INTEGER,action TEXT,details TEXT,created_at TEXT)')
        with self.db:
            accounting.initialize(self.db)

    def tearDown(self):
        self.db.close()

    def by_code(self, code):
        return self.db.execute('SELECT * FROM accounts WHERE code=?', (code,)).fetchone()

    def create(self, parent, name, posting=True, normal_side='debit'):
        with self.db:
            return accounting.create_account(self.db, {'parent_id': self.by_code(parent)['id'], 'name': name, 'posting': posting, 'normal_side': normal_side})

    def test_seed_has_real_tree_control_accounts_and_contra_natures(self):
        state = accounting.snapshot(self.db)
        seed = json.loads(accounting.SEED.read_text())
        self.assertEqual(state['counts']['total'], len(seed['accounts']))
        self.assertEqual(len([a for a in state['accounts'] if a['parent_id'] is None]), 8)
        self.assertGreater(state['counts']['posting'], 150)
        self.assertEqual(state['counts']['confirmed_mappings'], 0)
        for account in state['accounts']:
            self.assertNotIn('balance', account)
            if account['parent_id'] is not None:
                parent = self.by_code(account['parent_code'])
                self.assertFalse(parent['posting'])
                self.assertEqual(parent['statement'], account['statement'])
        self.assertEqual(self.by_code('122001')['normal_side'], 'credit')
        self.assertEqual(self.by_code('114005')['normal_side'], 'credit')
        contra_revenue = [a for a in state['accounts'] if a['code'].startswith('4') and a['normal_side'] == 'debit']
        self.assertTrue(contra_revenue)
        self.assertEqual(self.by_code('211001')['normal_side'], 'credit')
        settings = state['settings']
        self.assertEqual((settings['country_code'], settings['base_currency'], settings['fiscal_start_month']), ('EG', 'EGP', 1))
        self.assertTrue(settings['user_confirmed'])

    def test_seed_is_idempotent_and_preserves_user_edits_local_accounts_and_settings(self):
        account = self.create('111000', 'خزينة محلية إضافية')
        with self.db:
            updated = accounting.update_account(self.db, account['id'], {'revision': account['revision'], 'name': 'خزينة الفرع'})
            settings = accounting.update_settings(self.db, {'revision': 1, 'fiscal_start_month': 4})
            mapping = self.db.execute('SELECT * FROM account_mappings LIMIT 1').fetchone()
            accounting.update_mapping(self.db, mapping['id'], {'revision': mapping['revision'], 'status': 'confirmed'})
        before = accounting.snapshot(self.db)
        with self.db:
            accounting.initialize(self.db)
        after = accounting.snapshot(self.db)
        self.assertEqual(after, before)
        self.assertEqual(self.by_code(account['code'])['name'], 'خزينة الفرع')
        self.assertEqual(settings['fiscal_start_month'], 4)
        self.assertEqual(updated['revision'], 2)

    def test_auto_codes_reserve_group_namespaces_and_handle_exhaustion(self):
        group = self.create('111000', 'تجميع فرعي للاختبار', posting=False)
        self.assertTrue(group['code'].endswith('00'))
        sibling = self.create('111000', 'حساب خارج التجميع')
        self.assertFalse(sibling['code'].startswith(group['code'].rstrip('0')))
        small = self.create(group['code'], 'نطاق محدود', posting=False)
        self.assertEqual(len(small['code'].rstrip('0')), 5)
        allocated = [self.create(small['code'], 'حساب اختبار '+str(i))['code'] for i in range(9)]
        self.assertEqual(len(set(allocated)), 9)
        self.assertTrue(all(code.startswith(small['code'].rstrip('0')) for code in allocated))
        with self.assertRaises(ValueError):
            self.create(small['code'], 'حساب زائد')
        with self.assertRaises(ValueError):
            self.create(small['code'], 'تجميع زائد', posting=False)

    def test_invalid_parent_and_structural_mutation_are_rejected(self):
        with self.assertRaises(ValueError):
            self.create('111001', 'لا يجوز داخل حساب حركة')
        with self.assertRaises(ValueError):
            accounting.create_account(self.db, {'parent_id': True, 'name': 'معرّف غير صالح'})
        account = self.create('111000', 'حساب مقابل', normal_side='credit')
        self.assertEqual(account['normal_side'], 'credit')
        with self.assertRaises(ValueError):
            accounting.update_account(self.db, account['id'], {'revision': 1, 'code': '999999'})
        with self.assertRaises(ValueError):
            accounting.update_account(self.db, account['id'], {'revision': 1, 'parent_id': self.by_code('112000')['id']})
        with self.db:
            accounting.update_account(self.db, account['id'], {'revision': 1, 'notes': 'ملاحظة'})
        with self.assertRaises(accounting.ConflictError):
            accounting.update_account(self.db, account['id'], {'revision': 1, 'name': 'تعديل قديم'})

    def test_mapping_requires_active_posting_correct_class_and_nature(self):
        mapping = self.db.execute("SELECT * FROM account_mappings WHERE role='payable_control'").fetchone()
        with self.assertRaises(ValueError):
            accounting.update_mapping(self.db, mapping['id'], {'revision': 1, 'account_id': self.by_code('210000')['id']})
        with self.assertRaises(ValueError):
            accounting.update_mapping(self.db, mapping['id'], {'revision': 1, 'account_id': self.by_code('111001')['id']})
        debit_liability = self.create('211000', 'حساب التزام مقابل للاختبار', normal_side='debit') if self.by_code('211000') else self.create('210000', 'حساب التزام مقابل للاختبار', normal_side='debit')
        with self.assertRaises(ValueError):
            accounting.update_mapping(self.db, mapping['id'], {'revision': 1, 'account_id': debit_liability['id']})
        with self.db:
            result = accounting.update_mapping(self.db, mapping['id'], {'revision': 1, 'status': 'confirmed'})
        self.assertEqual(result['status'], 'confirmed')
        with self.assertRaises(accounting.ConflictError):
            accounting.update_mapping(self.db, mapping['id'], {'revision': 1, 'status': 'proposed'})
        expense = self.db.execute("SELECT * FROM account_mappings WHERE role='default_direct_expense'").fetchone()
        alternate = self.db.execute("SELECT * FROM accounts WHERE posting=1 AND code LIKE '5%' AND normal_side='debit' LIMIT 1").fetchone()
        with self.db:
            result = accounting.update_mapping(self.db, expense['id'], {'revision': 1, 'account_id': alternate['id']})
        self.assertEqual(result['account_code'][0], '5')

    def test_deactivation_protects_mappings_and_active_descendants(self):
        mapped = self.db.execute('SELECT account_id FROM account_mappings LIMIT 1').fetchone()[0]
        with self.assertRaises(ValueError):
            accounting.update_account(self.db, mapped, {'revision': 1, 'active': False})
        with self.assertRaises(ValueError):
            accounting.update_account(self.db, self.by_code('110000')['id'], {'revision': 1, 'active': False})
        group = self.create('111000', 'تجميع قابل للإيقاف', posting=False)
        leaf = self.create(group['code'], 'حساب قابل للإيقاف')
        with self.db:
            accounting.update_account(self.db, leaf['id'], {'revision': 1, 'active': False})
            accounting.update_account(self.db, group['id'], {'revision': 1, 'active': False})
        with self.assertRaises(ValueError):
            accounting.update_account(self.db, leaf['id'], {'revision': 2, 'active': True})
        with self.assertRaises(ValueError):
            self.create(group['code'], 'ابن تحت تجميع موقوف')

    def test_settings_reject_relabeling_invoices_and_changes_after_journals(self):
        with self.assertRaises(ValueError):
            accounting.update_settings(self.db, {'revision': 1, 'base_currency': 'JPY'})
        with self.assertRaises(ValueError):
            accounting.update_settings(self.db, {'revision': 1, 'fiscal_start_month': True})
        self.db.execute('CREATE TABLE invoices(id INTEGER PRIMARY KEY)')
        self.db.execute('INSERT INTO invoices DEFAULT VALUES')
        with self.assertRaisesRegex(ValueError, 'فواتير'):
            accounting.update_settings(self.db, {'revision': 1, 'base_currency': 'USD'})
        self.db.execute('CREATE TABLE journal_entries(id INTEGER PRIMARY KEY)')
        self.db.execute('INSERT INTO journal_entries DEFAULT VALUES')
        with self.assertRaisesRegex(ValueError, 'قيود'):
            accounting.update_settings(self.db, {'revision': 1, 'fiscal_start_month': 4})
        with self.assertRaises(ValueError):
            accounting.update_settings(self.db, {'revision': 1, 'chart_status': 'reviewed'})

    def test_review_requires_all_mappings_and_returns_to_draft_after_chart_edit(self):
        for mapping in self.db.execute('SELECT * FROM account_mappings').fetchall():
            with self.db:
                accounting.update_mapping(self.db, mapping['id'], {'revision': mapping['revision'], 'status': 'confirmed'})
        with self.db:
            reviewed = accounting.update_settings(self.db, {'revision': 1, 'chart_status': 'reviewed'})
        self.assertEqual(reviewed['chart_status'], 'reviewed')
        self.create('111000', 'حساب أضيف بعد المراجعة')
        self.assertEqual(accounting.get_settings(self.db)['chart_status'], 'draft')
        self.assertEqual(accounting.get_settings(self.db)['revision'], 3)

    def test_csv_escapes_spreadsheet_formulas_and_has_no_balances(self):
        account = self.create('111000', '=HYPERLINK("unsafe")')
        text = accounting.export_csv(self.db)
        self.assertTrue(text.startswith('\ufeff'))
        rows = list(csv.reader(io.StringIO(text.lstrip('\ufeff'))))
        row = next(r for r in rows if r[0] == account['code'])
        self.assertTrue(row[1].startswith("'="))
        self.assertNotIn('رصيد', ''.join(rows[0]))
        self.assertGreater(len(rows), 200)


if __name__ == '__main__':
    unittest.main(verbosity=2)
