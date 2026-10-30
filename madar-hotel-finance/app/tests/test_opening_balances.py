"""Opening journals balance, preserve their detail, and never infer funding."""
import copy
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting
import accounting_periods
import banks
import opening_balances as opening
import opening_suppliers
import payables
import test_supplier_payments as fixtures


class OpeningBalances(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SupplierPayments('runTest')
        self.fixture.setUp()
        self.db = self.fixture.db
        accounting_periods.initialize(self.db)
        banks.initialize(self.db)
        opening.initialize(self.db)
        self.db.execute("UPDATE account_mappings SET status='confirmed'")
        self.db.commit()
        self.cash = self.fixture.cash
        self.capital = self.fixture.account('300001')
        self.sequence = 0

    def tearDown(self):
        self.fixture.tearDown()

    def payload(self, **changes):
        self.sequence += 1
        value = {'request_key': 'opening-' + str(self.sequence), 'business_date': '2026-01-01',
                 'reference': 'OPEN-' + str(self.sequence), 'notes': 'أرصدة مراجعة فعلية',
                 'lines': [{'kind': 'general', 'account_id': self.cash, 'side': 'debit', 'amount': '100.00'},
                           {'kind': 'general', 'account_id': self.capital, 'side': 'credit', 'amount': '100.00'}]}
        value.update(changes)
        return value

    def post(self, payload=None):
        saved = opening.save(self.db, payload or self.payload())
        return opening.approve(self.db, saved['id'], {'revision': saved['revision']})

    def test_initialization_is_additive_idempotent_and_has_no_balances(self):
        account = self.db.execute("SELECT * FROM accounts WHERE role='opening_balance_clearing'").fetchone()
        self.assertEqual(account['code'], '300007')
        self.db.execute("UPDATE accounts SET name='تعديل المستخدم' WHERE id=?", (account['id'],))
        before = self.db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
        opening.initialize(self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], before)
        self.assertEqual(self.db.execute('SELECT name FROM accounts WHERE id=?', (account['id'],)).fetchone()[0], 'تعديل المستخدم')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], 0)
        self.assertEqual(opening.list_batches(self.db), [])

    def test_initialization_does_not_repurpose_an_existing_equity_account(self):
        other = fixtures.SupplierPayments('runTest')
        other.setUp()
        try:
            parent = other.account('300000')
            other.db.execute('''INSERT INTO accounts(code,name,parent_id,posting,normal_side,statement,
                active,system,notes,created_at,updated_at) VALUES('300007','حساب خاص بالمستخدم',?,1,'credit',
                'balance_sheet',1,0,'ملاحظات محفوظة','2026-01-01','2026-01-01')''', (parent,))
            opening.initialize(other.db)
            self.assertEqual(other.db.execute("SELECT name FROM accounts WHERE code='300007'").fetchone()[0], 'حساب خاص بالمستخدم')
            account = other.db.execute("SELECT code FROM accounts WHERE role='opening_balance_clearing'").fetchone()
            self.assertNotEqual(account[0], '300007')
            self.assertEqual(opening.config(other.db)['clearing_mapping']['status'], 'proposed')
        finally:
            other.tearDown()

    def test_unbalanced_draft_never_invents_capital_or_posts(self):
        payload = self.payload()
        payload['lines'].pop()
        saved = opening.save(self.db, payload)
        self.assertEqual(saved['totals']['difference_cents'], 10000)
        self.assertFalse(saved['preview']['can_approve'])
        with self.assertRaises(ValueError):
            opening.approve(self.db, saved['id'], {'revision': saved['revision']})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], 0)
        self.assertEqual(opening.detail(self.db, saved['id'])['status'], 'draft')

    def test_explicit_balanced_batch_creates_one_journal_per_line_clearing_zero(self):
        posted = self.post()
        self.assertEqual(posted['status'], 'posted')
        self.assertEqual(len(posted['journals']), 2)
        self.assertEqual(posted['clearing_balance_cents'], 0)
        for line, journal in zip(posted['lines'], posted['journals']):
            self.assertEqual(journal['source_type'], 'opening_balance')
            self.assertEqual(journal['source_id'], line['id'])
            self.assertEqual(sum(row['debit_cents'] for row in journal['lines']), 10000)
            self.assertEqual(sum(row['credit_cents'] for row in journal['lines']), 10000)
            counterpart = next(row for row in journal['lines'] if row['account_id'] == posted['clearing_account_id'])
            self.assertIsNone(counterpart['supplier_id'])
            self.assertIsNone(counterpart['department_id'])
        totals = self.db.execute('SELECT SUM(debit_cents),SUM(credit_cents) FROM journal_lines').fetchone()
        self.assertEqual(totals[0], totals[1])
        self.assertEqual(self.db.execute('SELECT SUM(debit_cents-credit_cents) FROM journal_lines WHERE account_id=?', (posted['clearing_account_id'],)).fetchone()[0], 0)

    def test_only_one_posted_batch_and_sql_immutability(self):
        first = opening.save(self.db, self.payload())
        with self.assertRaises(accounting.ConflictError):
            opening.save(self.db, self.payload())
        posted = opening.approve(self.db, first['id'], {'revision': first['revision']})
        with self.assertRaises(ValueError):
            opening.save(self.db, self.payload())
        for statement, parameters in (
            ('UPDATE opening_batches SET notes=? WHERE id=?', ('changed', posted['id'])),
            ('DELETE FROM opening_batches WHERE id=?', (posted['id'],)),
            ('UPDATE opening_lines SET amount_cents=1 WHERE id=?', (posted['lines'][0]['id'],)),
            ('DELETE FROM opening_lines WHERE batch_id=?', (posted['id'],)),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(statement, parameters)

    def test_safe_request_and_approval_replay_after_month_closes(self):
        payload = self.payload()
        saved = opening.save(self.db, payload)
        self.assertEqual(opening.save(self.db, copy.deepcopy(payload))['id'], saved['id'])
        changed = copy.deepcopy(payload)
        changed['notes'] = 'different'
        with self.assertRaises(accounting.ConflictError):
            opening.save(self.db, changed)
        posted = opening.approve(self.db, saved['id'], {'revision': saved['revision']})
        self.db.execute("INSERT INTO accounting_periods(month,status,updated_at) VALUES('2026-01','closed','2026-02-01')")
        self.assertEqual(opening.save(self.db, payload)['id'], posted['id'])
        self.assertEqual(opening.approve(self.db, saved['id'], {'revision': saved['revision']})['id'], posted['id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], 2)

    def test_draft_revision_update_cancel_and_locked_old_date(self):
        payload = self.payload()
        saved = opening.save(self.db, payload)
        edited = {**payload, 'revision': saved['revision'], 'notes': 'reviewed'}
        updated = opening.save(self.db, edited, saved['id'])
        self.assertEqual(updated['revision'], 2)
        with self.assertRaises(accounting.ConflictError):
            opening.save(self.db, edited, saved['id'])
        self.db.execute("INSERT INTO accounting_periods(month,status,updated_at) VALUES('2026-01','closed','2026-02-01')")
        with self.assertRaises(ValueError):
            opening.save(self.db, {**edited, 'revision': 2, 'business_date': '2026-02-01'}, saved['id'])
        self.db.execute("UPDATE accounting_periods SET status='open'")
        cancelled = opening.save(self.db, {'revision': 2, 'action': 'cancel'}, saved['id'])
        self.assertEqual(cancelled['status'], 'cancelled')
        with self.assertRaises(ValueError):
            opening.approve(self.db, saved['id'], {'revision': cancelled['revision']})

    def test_mapping_confirmation_does_not_change_frozen_route(self):
        self.db.execute("UPDATE account_mappings SET status='proposed' WHERE module=? AND role=?", opening.CLEARING)
        saved = opening.save(self.db, self.payload())
        self.assertFalse(saved['preview']['can_approve'])
        self.db.execute("UPDATE account_mappings SET status='confirmed' WHERE module=? AND role=?", opening.CLEARING)
        self.assertEqual(opening.approve(self.db, saved['id'], {'revision': saved['revision']})['status'], 'posted')

    def test_stale_account_facts_require_resave(self):
        payload = self.payload()
        saved = opening.save(self.db, payload)
        self.db.execute("UPDATE accounts SET name='اسم معدل' WHERE id=?", (self.cash,))
        with self.assertRaises(accounting.ConflictError):
            opening.approve(self.db, saved['id'], {'revision': saved['revision']})
        updated = opening.save(self.db, {**payload, 'revision': saved['revision']}, saved['id'])
        self.assertEqual(opening.approve(self.db, updated['id'], {'revision': updated['revision']})['status'], 'posted')

    def test_opening_cannot_follow_earliest_existing_financial_entry(self):
        self.fixture.invoice(business_date='2026-02-01')
        payload = self.payload(business_date='2026-02-02')
        plan = opening.preview(self.db, payload)
        self.assertIn('baseline_after_activity', [row['code'] for row in plan['blockers']])
        saved = opening.save(self.db, payload)
        with self.assertRaises(ValueError):
            opening.approve(self.db, saved['id'], {'revision': saved['revision']})
        self.assertEqual(opening.config(self.db)['earliest_financial_date'], '2026-02-01')

    def test_earlier_operating_drafts_must_be_resolved_before_cutoff(self):
        self.db.execute('''INSERT INTO invoices(code,request_key,supplier_id,reference,business_date,
            department_id,status,subtotal_cents,total_cents,created_at,updated_at)
            VALUES('PENDING','pending',1,'PENDING','2025-12-31',1,'draft',100,100,'2025-12-31','2025-12-31')''')
        payload = self.payload()
        draft = opening.save(self.db, payload)
        blocker = next(row for row in draft['preview']['blockers'] if row['code'] == 'drafts_before_baseline')
        self.assertEqual(blocker['count'], 1)
        with self.assertRaises(ValueError):
            opening.approve(self.db, draft['id'], {'revision': draft['revision']})
        self.db.execute("UPDATE invoices SET status='cancelled' WHERE code='PENDING'")
        self.assertEqual(opening.approve(self.db, draft['id'], {'revision': draft['revision']})['status'], 'posted')

    def test_cancelled_initial_draft_allows_replacement_and_date_edit_excludes_itself(self):
        payload = self.payload()
        draft = opening.save(self.db, payload)
        edited = opening.save(self.db, {**payload, 'business_date': '2026-02-01', 'revision': draft['revision']}, draft['id'])
        self.assertNotIn('drafts_before_baseline', [row['code'] for row in edited['preview']['blockers']])
        opening.save(self.db, {'action': 'cancel', 'revision': edited['revision']}, draft['id'])
        replacement = opening.save(self.db, self.payload())
        self.assertNotEqual(replacement['id'], draft['id'])

    def test_money_dates_identifiers_and_line_limits_are_strict(self):
        for amount in ('0', '-1', '0.001', 'NaN', 'Infinity', True, 1.1, '1000000000000'):
            payload = self.payload()
            payload['lines'][0]['amount'] = amount
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                opening.preview(self.db, payload)
        for ident in (True, 0, -1, '1', 10**20):
            payload = self.payload()
            payload['lines'][0]['account_id'] = ident
            with self.subTest(ident=ident), self.assertRaises(ValueError):
                opening.preview(self.db, payload)
        for business_date in ('20260101', '2026-1-01', '2026-02-30', None):
            with self.subTest(date=business_date), self.assertRaises(ValueError):
                opening.preview(self.db, self.payload(business_date=business_date))
        payload = self.payload()
        payload['lines'][0]['amount_cents'] = 1
        with self.assertRaises(ValueError):
            opening.preview(self.db, payload)
        with self.assertRaises(ValueError):
            opening.preview(self.db, self.payload(lines=[self.payload()['lines'][0]] * 1001))

    def test_general_accounts_exclude_current_and_historical_controls(self):
        controls = [self.fixture.ap, self.fixture.advance, self.fixture.cheques,
                    self.fixture.account('115003'), self.fixture.account('213004')]
        for account_id in controls:
            payload = self.payload()
            payload['lines'][0]['account_id'] = account_id
            with self.subTest(account=account_id), self.assertRaises(ValueError):
                opening.preview(self.db, payload)
        config = opening.config(self.db)
        general = {row['id'] for row in config['general_accounts']}
        self.assertIn(self.cash, general)
        self.assertIn(self.fixture.bank, general)
        self.assertTrue(set(controls).isdisjoint(general))
        payload = self.payload()
        payload['lines'][0]['supplier_id'] = 1
        with self.assertRaises(ValueError):
            opening.preview(self.db, payload)

    def test_supplier_sources_are_real_allocation_identities_without_purchase_or_cash(self):
        payload = self.payload(lines=[
            {'kind': 'general', 'account_id': self.cash, 'side': 'debit', 'amount': '75.00'},
            {'kind': 'supplier_invoice', 'supplier_id': 1, 'amount': '100.00', 'reference': 'OLD-INV',
             'source_date': '2025-11-01', 'due_date': '2025-12-01', 'remaining_net_amount': '90.00'},
            {'kind': 'supplier_credit', 'supplier_id': 1, 'amount': '10.00', 'reference': 'OLD-CR'},
            {'kind': 'supplier_advance', 'supplier_id': 2, 'amount': '15.00', 'reference': 'OLD-ADV'},
        ])
        posted = self.post(payload)
        self.assertEqual(len(posted['journals']), 4)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM invoice_lines').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM supplier_payments').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM opening_supplier_invoices').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM opening_supplier_advances').fetchone()[0], 1)
        for line, journal in zip(posted['lines'][1:], posted['journals'][1:]):
            actual = next(row for row in journal['lines'] if row['account_id'] == line['account_id'])
            self.assertEqual(actual['supplier_id'], line['supplier_id'])
            self.assertIsNotNone(line['document_id'])

    def test_duplicate_supplier_reference_and_wrong_typed_side_rejected(self):
        line = {'kind': 'supplier_invoice', 'supplier_id': 1, 'amount': '100.00', 'reference': 'DUP',
                'source_date': '2025-11-01', 'due_date': '2025-12-01'}
        with self.assertRaises(ValueError):
            opening.preview(self.db, self.payload(lines=[line, dict(line)]))
        with self.assertRaises(ValueError):
            opening.preview(self.db, self.payload(lines=[{**line, 'side': 'debit'}]))

    def test_issued_cheque_has_real_outstanding_identity_and_no_ap_payment(self):
        cheque = {'kind': 'issued_cheque', 'amount': '100.00', 'reference': 'OLD-CHEQUE',
                  'source_date': '2025-12-01', 'due_date': '2026-01-10',
                  'extra': {'bank_account_id': self.fixture.bank, 'bank_name': 'بنك مصر',
                            'beneficiary': 'جهة سابقة', 'cheque_number': 'CH-001'}}
        payload = self.payload()
        payload['lines'][1] = cheque
        posted = self.post(payload)
        line = posted['lines'][1]
        self.assertEqual(line['document_type'], 'opening_bank_cheque')
        self.assertIsNotNone(line['document_id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM opening_bank_cheques').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM supplier_payments').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM invoices').fetchone()[0], 0)

    def test_duplicate_issued_cheques_are_rejected_before_journals_exist(self):
        cheque = {'kind': 'issued_cheque', 'amount': '50.00', 'reference': 'CH-OPEN',
                  'source_date': '2025-12-01', 'due_date': '2026-01-10',
                  'extra': {'bank_account_id': self.fixture.bank, 'bank_name': 'بنك مصر',
                            'beneficiary': 'جهة سابقة', 'cheque_number': 'CH-001'}}
        payload = self.payload()
        payload['lines'] = [payload['lines'][0], cheque, copy.deepcopy(cheque)]
        with self.assertRaises(ValueError):
            opening.save(self.db, payload)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], 0)

    def test_caller_rollback_is_atomic_on_adapter_failure_and_success_has_no_commit(self):
        payload = self.payload(lines=[
            {'kind': 'general', 'account_id': self.cash, 'side': 'debit', 'amount': '100.00'},
            {'kind': 'supplier_invoice', 'supplier_id': 1, 'amount': '100.00', 'reference': 'FAIL',
             'source_date': '2025-11-01', 'due_date': '2025-12-01'},
        ])
        saved = opening.save(self.db, payload)
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'adapter failed'):
            with self.db:
                with patch.object(opening, '_adapter', return_value=(opening_suppliers.prepare, lambda *args: (_ for _ in ()).throw(ValueError('adapter failed')))):
                    opening.approve(self.db, saved['id'], {'revision': saved['revision']})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], 0)
        self.assertEqual(opening.detail(self.db, saved['id'])['status'], 'draft')
        opening.approve(self.db, saved['id'], {'revision': saved['revision']})
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], 0)
        self.assertEqual(opening.detail(self.db, saved['id'])['status'], 'draft')

    def test_readonly_configuration_and_all_saved_lines_are_returned(self):
        payload = self.payload()
        payload['lines'] = payload['lines'] * 300
        saved = opening.save(self.db, payload)
        self.assertEqual(len(saved['lines']), 600)
        count = self.db.total_changes
        opening.config(self.db)
        opening.preview(self.db, payload)
        opening.detail(self.db, saved['id'])
        opening.list_batches(self.db)
        self.assertEqual(self.db.total_changes, count)


if __name__ == '__main__':
    unittest.main()
