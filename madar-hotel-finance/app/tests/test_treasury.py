"""Exact cash postings, source boundaries and immutable dated corrections."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting
import accounting_periods
import payables
import supplier_payments
import treasury
import test_supplier_payments as fixtures


class Treasury(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SupplierPayments('runTest')
        self.f.setUp()
        self.db = self.f.db
        treasury.initialize(self.db)
        accounting_periods.initialize(self.db)
        self.config = treasury.config(self.db)
        self.cash = self.f.cash
        self.petty = self.f.account('111003')
        self.pos = self.f.account('111004')
        self.receipt_counter = next(a['id'] for a in self.config['counter_accounts_by_kind']['receipt']
                                    if a['code'].startswith('3'))
        self.expense = self.config['counter_accounts_by_kind']['payment'][0]['id']
        self.serial = 0
        self.db.commit()

    def tearDown(self):
        self.f.tearDown()

    def payload(self, kind='receipt', **changes):
        self.serial += 1
        value = {'request_key': 'treasury-' + str(self.serial), 'kind': kind,
                 'business_date': '2026-10-16', 'reference': 'REFERENCE-' + str(self.serial),
                 'amount': '100.00', 'currency': 'EGP', 'cash_account_id': self.cash,
                 'counter_account_id': self.expense if kind == 'payment' else self.receipt_counter,
                 'party_name': 'مستلم أو مسلّم النقد الفعلي', 'department_id': 1 if kind == 'payment' else None,
                 'notes': 'بيانات في قاعدة اختبار مؤقتة فقط'}
        if kind == 'transfer':
            value.update(counter_account_id=None, destination_cash_account_id=self.petty,
                         party_name='', department_id=None)
        value.update(changes)
        return value

    def post(self, data):
        draft = treasury.save(self.db, data)
        return treasury.approve(self.db, draft['id'], {'revision': draft['revision']})

    def undo(self, doc, **changes):
        self.serial += 1
        return {'request_key': 'undo-' + str(self.serial), 'revision': doc['revision'],
                'event_revision': 0, 'business_date': '2026-10-20',
                'reference': 'CORRECTION-' + str(self.serial), 'reason': 'تصحيح مستند سجل بالخطأ', **changes}

    def balance(self, account=None, ds=None):
        return supplier_payments.account_balance(self.db, account or self.cash, ds)

    def close(self, month='2026-10'):
        self.db.execute("INSERT INTO accounting_periods(month,status,updated_at) VALUES(?,'closed','test')", (month,))

    def test_receipt_payment_and_transfer_make_exact_distinct_journals(self):
        receipt = self.post(self.payload(amount='1000'))
        payment = self.post(self.payload('payment', amount='100'))
        transfer = self.post(self.payload('transfer', amount='200'))
        self.assertEqual(self.balance(), 70000)
        self.assertEqual(self.balance(self.petty), 20000)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], 3)
        for doc, source, code in ((receipt, 'treasury_receipt', 'TRC-'),
                                  (payment, 'treasury_payment', 'TPY-'),
                                  (transfer, 'treasury_transfer', 'TTR-')):
            self.assertTrue(doc['code'].startswith(code))
            self.assertEqual(doc['journal']['source_type'], source)
            self.assertEqual(doc['journal']['debit_cents'], doc['amount_cents'])
            self.assertEqual(doc['journal']['credit_cents'], doc['amount_cents'])
            self.assertTrue(all(line['supplier_id'] is None for line in doc['journal']['lines']))
        expense = next(line for line in payment['journal']['lines'] if line['account_id'] == self.expense)
        self.assertEqual(expense['department_id'], 1)
        self.assertEqual(sum(line['debit_cents'] for line in transfer['journal']['lines']), 20000)

    def test_draft_save_update_cancel_have_no_financial_effect(self):
        data = self.payload('payment')
        draft = treasury.save(self.db, data)
        self.assertEqual(draft['status'], 'draft')
        self.assertIsNone(draft['journal'])
        newer = treasury.save(self.db, {**data, 'revision': draft['revision'], 'amount': '20.50'}, draft['id'])
        self.assertEqual((newer['amount_cents'], newer['revision']), (2050, 2))
        cancelled = treasury.save(self.db, {'revision': newer['revision'], 'action': 'cancel'}, newer['id'])
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertEqual(self.balance(), 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], 0)
        with self.assertRaises(ValueError):
            treasury.approve(self.db, cancelled['id'], {'revision': cancelled['revision']})

    def test_preview_and_config_are_read_only_and_warn_without_inventing_funds(self):
        before = self.db.total_changes
        plan = treasury.preview(self.db, self.payload('payment', amount='0.01'))
        treasury.config(self.db)
        self.assertEqual(plan['cash_balance_before_cents'], 0)
        self.assertEqual(plan['cash_balance_after_cents'], -1)
        self.assertTrue(plan['warnings'])
        self.assertTrue(plan['can_approve'])
        self.assertEqual(self.db.total_changes, before)
        posted = self.post(self.payload('payment', amount='0.01'))
        self.assertEqual(posted['amount_cents'], 1)
        self.assertEqual(self.balance(), -1)

    def test_pos_cash_is_valid_but_transit_bank_and_groups_are_not(self):
        ids = {a['id'] for a in self.config['cash_accounts']}
        self.assertIn(self.pos, ids)
        transit = self.f.account('111005')
        self.assertNotIn(transit, ids)
        self.post(self.payload(cash_account_id=self.pos, amount='5'))
        self.assertEqual(self.balance(self.pos), 500)
        for ident in (transit, self.f.bank, self.f.account('111000')):
            with self.subTest(account=ident), self.assertRaises(ValueError):
                treasury.preview(self.db, self.payload(cash_account_id=ident))
        with self.assertRaises(ValueError):
            treasury.preview(self.db, self.payload('transfer', destination_cash_account_id=self.cash))

    def test_mapped_and_historical_supplier_tax_custody_accounts_are_protected(self):
        blocked = treasury.blocked_counter_account_ids(self.db)
        self.assertIn(self.f.ap, blocked)
        self.assertIn(self.f.advance, blocked)
        self.assertIn(self.f.account('115003'), blocked)
        self.assertIn(self.f.account('213004'), blocked)
        for ident in (self.f.ap, self.f.cheques, self.f.account('213004')):
            with self.subTest(account=ident), self.assertRaises(ValueError):
                treasury.preview(self.db, self.payload(counter_account_id=ident))
        with self.assertRaises(ValueError):
            treasury.preview(self.db, self.payload('payment', counter_account_id=self.f.expense))
        # Move the current AP mapping after a genuine invoice was posted: its
        # original control must still be excluded from a direct cash receipt.
        self.f.invoice()
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الموردون' AND role='payable_control'", (self.f.account('211002'),))
        self.assertIn(self.f.ap, treasury.blocked_counter_account_ids(self.db))
        with self.assertRaises(ValueError):
            treasury.preview(self.db, self.payload(counter_account_id=self.f.ap))

    def test_past_vat_withholding_and_custody_controls_remain_blocked(self):
        target = self.receipt_counter
        # Optional legacy source tables are queried for original account IDs,
        # independently of today's mappings or valid future classifications.
        self.db.execute('CREATE TABLE IF NOT EXISTS custody_documents(id INTEGER PRIMARY KEY,custody_account_id INTEGER)')
        self.db.execute('INSERT INTO custody_documents(custody_account_id) VALUES(?)', (target,))
        self.assertIn(target, treasury.blocked_counter_account_ids(self.db))
        with self.assertRaises(ValueError):
            treasury.preview(self.db, self.payload(counter_account_id=target))

    def test_cash_payments_from_ap_are_not_recreated_or_changed(self):
        bill = self.f.invoice()
        supplier = self.f.post(self.f.payload(amount='30', allocation_mode='invoice', target_invoice_id=bill['id']))
        journals = self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0]
        self.assertEqual(self.balance(), -3000)
        plan = treasury.preview(self.db, self.payload('payment', amount='5'))
        self.assertEqual(plan['cash_balance_before_cents'], -3000)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM treasury_documents').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], journals)
        self.assertEqual(supplier_payments.get_payment(self.db, supplier['id'])['amount_cents'], 3000)

    def test_exact_cents_and_strict_dates_identifiers_and_kind(self):
        for amount in ('0', '-1', 'NaN', 'Infinity', '1.001', True, None, '100000000000.01'):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                treasury.preview(self.db, self.payload(amount=amount))
        self.assertEqual(treasury.preview(self.db, self.payload(amount='100000000000'))['amount_cents'], 10**13)
        with self.assertRaises(ValueError):
            treasury.preview(self.db, self.payload(amount='1', amount_cents=101))
        for changes in ({'business_date': '2026-2-01'}, {'business_date': '2026-02-30'},
                        {'cash_account_id': True}, {'kind': []}, {'currency': 'USD'},
                        {'party_name': ''}, {'reference': ''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                treasury.preview(self.db, self.payload(**changes))
        for changes in ({'department_id': None}, {'department_id': 999999}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                treasury.preview(self.db, self.payload('payment', **changes))

    def test_receipt_and_payment_counter_classes_and_inactive_accounts(self):
        with self.assertRaises(ValueError):
            treasury.preview(self.db, self.payload(counter_account_id=self.expense))
        with self.assertRaises(ValueError):
            treasury.preview(self.db, self.payload('payment', counter_account_id=self.receipt_counter))
        self.db.execute('UPDATE accounts SET active=0 WHERE id=?', (self.expense,))
        with self.assertRaises(ValueError):
            treasury.preview(self.db, self.payload('payment'))
        self.assertNotIn(self.expense, {a['id'] for a in treasury.config(self.db)['counter_accounts_by_kind']['payment']})

    def test_posting_revalidates_accounts_and_stale_updates_are_rejected(self):
        data = self.payload('payment')
        draft = treasury.save(self.db, data)
        self.db.execute('UPDATE accounts SET active=0 WHERE id=?', (self.expense,))
        with self.assertRaises(ValueError):
            treasury.approve(self.db, draft['id'], {'revision': draft['revision']})
        self.assertEqual(self.balance(), 0)
        self.db.execute('UPDATE accounts SET active=1 WHERE id=?', (self.expense,))
        changed = treasury.save(self.db, {**data, 'revision': 1, 'amount': '20'}, draft['id'])
        with self.assertRaises(accounting.ConflictError):
            treasury.save(self.db, {**data, 'revision': 1}, draft['id'])
        with self.assertRaises(accounting.ConflictError):
            treasury.approve(self.db, draft['id'], {'revision': 1})
        self.assertEqual(changed['revision'], 2)

    def test_request_retries_and_changed_duplicate_keys_are_safe_after_close(self):
        data = self.payload()
        doc = self.post(data)
        self.close()
        before = self.db.total_changes
        self.assertEqual(treasury.save(self.db, data)['id'], doc['id'])
        self.assertEqual(treasury.approve(self.db, doc['id'], {'revision': 1})['journal_id'], doc['journal_id'])
        self.assertEqual(self.db.total_changes, before)
        with self.assertRaises(accounting.ConflictError):
            treasury.save(self.db, {**data, 'amount': '2'})

    def test_duplicates_and_kind_changes_do_not_create_extra_vouchers(self):
        data = self.payload()
        doc = treasury.save(self.db, data)
        with self.assertRaises(ValueError):
            treasury.save(self.db, {**data, 'request_key': 'another-key'})
        with self.assertRaises(ValueError):
            treasury.save(self.db, {**self.payload('payment'), 'revision': doc['revision']}, doc['id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM treasury_documents').fetchone()[0], 1)

    def test_closed_same_and_later_months_prevent_new_update_cancel_and_post(self):
        data = self.payload()
        draft = treasury.save(self.db, data)
        self.close('2026-11')
        for function, args in (
            (treasury.save, (self.db, self.payload())),
            (treasury.save, (self.db, {**data, 'revision': 1, 'business_date': '2026-12-01'}, draft['id'])),
            (treasury.save, (self.db, {'revision': 1, 'action': 'cancel'}, draft['id'])),
            (treasury.approve, (self.db, draft['id'], {'revision': 1})),
        ):
            with self.subTest(function=function.__name__), self.assertRaisesRegex(ValueError, 'مقفولة'):
                function(*args)
        self.assertEqual(treasury.detail(self.db, draft['id'])['status'], 'draft')
        self.assertEqual(self.balance(), 0)
        self.assertEqual(treasury.preview(self.db, data)['amount_cents'], 10000)

    def test_reversal_preserves_original_and_has_dated_exact_opposite(self):
        doc = self.post(self.payload('payment', amount='37.25'))
        data = self.undo(doc)
        corrected = treasury.reverse(self.db, doc['id'], data)
        self.assertEqual(corrected['status'], 'posted')
        self.assertEqual(corrected['effective_status'], 'reversed')
        self.assertEqual(corrected['reversal']['code'], 'TRV-000001')
        self.assertEqual(self.balance(ds='2026-10-19'), -3725)
        self.assertEqual(self.balance(ds='2026-10-20'), 0)
        self.assertEqual(treasury.detail(self.db, doc['id'], '2026-10-19')['effective_status'], 'posted')
        old = [(l['account_id'], l['debit_cents'], l['credit_cents'], l['department_id']) for l in doc['journal']['lines']]
        new = [(l['account_id'], l['credit_cents'], l['debit_cents'], l['department_id']) for l in corrected['reversal']['journal']['lines']]
        self.assertEqual(old, new)
        self.assertEqual(corrected['reversal']['journal']['source_type'], 'treasury_reversal')
        self.close()
        before = self.db.total_changes
        self.assertEqual(treasury.reverse(self.db, doc['id'], data)['reversal']['id'], corrected['reversal']['id'])
        self.assertEqual(self.db.total_changes, before)

    def test_reversal_guards_draft_early_date_conflict_and_reuse_after_correction(self):
        data = self.payload()
        draft = treasury.save(self.db, data)
        with self.assertRaises(ValueError):
            treasury.reverse(self.db, draft['id'], self.undo(draft))
        doc = treasury.approve(self.db, draft['id'], {'revision': draft['revision']})
        with self.assertRaises(ValueError):
            treasury.reverse(self.db, doc['id'], self.undo(doc, business_date='2026-10-15'))
        with self.assertRaises(accounting.ConflictError):
            treasury.reverse(self.db, doc['id'], self.undo(doc, event_revision=True))
        corrected = treasury.reverse(self.db, doc['id'], self.undo(doc))
        with self.assertRaises(accounting.ConflictError):
            treasury.reverse(self.db, doc['id'], self.undo(doc))
        with self.assertRaises(ValueError):
            treasury.save(self.db, {**self.payload(), 'reference': data['reference'], 'business_date': '2026-10-19'})
        reused = self.post({**self.payload(), 'reference': data['reference'], 'business_date': '2026-10-20'})
        self.assertNotEqual(reused['id'], corrected['id'])

    def test_posted_documents_and_reversals_are_sql_immutable(self):
        doc = self.post(self.payload())
        reversed_doc = treasury.reverse(self.db, doc['id'], self.undo(doc))
        for sql, args in (
            ('UPDATE treasury_documents SET amount_cents=1 WHERE id=?', (doc['id'],)),
            ('DELETE FROM treasury_documents WHERE id=?', (doc['id'],)),
            ('UPDATE treasury_reversals SET reason=? WHERE id=?', ('changed', reversed_doc['reversal']['id'])),
            ('DELETE FROM treasury_reversals WHERE id=?', (reversed_doc['reversal']['id'],)),
        ):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql, args)

    def test_lists_are_complete_historical_and_read_only(self):
        first = self.post(self.payload(business_date='2026-10-01'))
        treasury.save(self.db, self.payload(business_date='2026-11-01'))
        for _ in range(500):
            treasury.save(self.db, self.payload(business_date='2026-11-02'))
        before = self.db.total_changes
        self.assertEqual(len(treasury.list_documents(self.db)), 502)
        self.assertEqual([row['id'] for row in treasury.list_documents(self.db, '2026-10-31')], [first['id']])
        self.assertEqual(self.db.total_changes, before)

    def test_initialize_is_additive_and_does_not_commit_caller_transaction(self):
        account_count = self.db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
        self.assertFalse(self.db.in_transaction)
        treasury.initialize(self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], account_count)
        treasury.save(self.db, self.payload())
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM treasury_documents').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
