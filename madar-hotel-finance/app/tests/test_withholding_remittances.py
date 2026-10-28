"""Recorded tax remittances preserve source liabilities and dated corrections."""
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
import withholding
import withholding_balances as balances
import withholding_remittances as remittances
import test_supplier_payments as fixtures


class WithholdingRemittances(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SupplierPayments('runTest')
        self.fixture.setUp()
        self.db = self.fixture.db
        adjustments.initialize(self.db)
        periods.initialize(self.db)
        remittances.initialize(self.db)
        self.code = withholding.create_code(self.db, {
            'name': 'خصم اختبار توريد', 'rate': '3', 'active': True, 'confirmed': True})
        self.wh_account = self.fixture.account('213004')
        self.counter = 0

    def tearDown(self):
        self.fixture.tearDown()

    def key(self, prefix):
        self.counter += 1
        return prefix + '-' + str(self.counter)

    def source(self, amount='100', date='2026-10-16', supplier=1):
        invoice = self.fixture.invoice(cents=int(float(amount) * 100), supplier_id=supplier)
        draft = payments.save_payment(self.db, self.fixture.payload(amount=amount,
            supplier_id=supplier, payment_date=date, allocation_mode='invoice',
            target_invoice_id=invoice['id'], withholding_code_id=self.code['id']))
        return payments.approve_payment(self.db, draft['id'], {'revision': draft['revision']})

    def data(self, source, amount='1', date='2026-10-20', method='cash', **changes):
        payload = {'request_key': self.key('remit'), 'business_date': date,
            'period_from': '2026-10-01', 'period_to': date,
            'authority_name': 'جهة ضريبية للاختبار', 'reference': self.key('receipt'),
            'method': method, 'funding_account_id': self.fixture.cash if method == 'cash' else self.fixture.bank,
            'details': {'received_by': 'مندوب الجهة'} if method == 'cash' else {
                'bank_name': 'بنك الاختبار', 'transaction_reference': self.key('bank'), 'value_date': '2026-10-25'},
            'allocations': [{'payment_id': source['id'], 'amount': amount}]}
        payload.update(changes)
        return payload

    def post(self, payload):
        saved = remittances.save(self.db, payload)
        return remittances.approve(self.db, saved['id'], {'revision': saved['revision']})

    def reverse(self, original, date='2026-10-22', **changes):
        data = {'request_key': self.key('reversal'), 'revision': original['revision'],
            'event_revision': 0, 'business_date': date, 'reference': self.key('correction'),
            'reason': 'تصحيح تسجيل توريد اختباري'}
        data.update(changes)
        return remittances.reverse(self.db, original['id'], data)

    def reverse_source(self, source, date='2026-10-21'):
        return adjustments.reverse_payment(self.db, source['id'], {
            'request_key': self.key('source-reversal'), 'revision': source['revision'],
            'event_revision': 0, 'business_date': date, 'reference': self.key('REV'),
            'reason': 'إلغاء سداد اختبار بعد تسجيل التوريد'})

    def test_initialize_is_additive_and_repeated_without_balances_or_defaults(self):
        source = self.source()
        original = dict(self.db.execute('SELECT * FROM payment_withholding WHERE payment_id=?', (source['id'],)).fetchone())
        count = self.db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
        remittances.initialize(self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM withholding_remittances').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], count)
        self.assertEqual(dict(self.db.execute('SELECT * FROM payment_withholding WHERE payment_id=?', (source['id'],)).fetchone()), original)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_preview_and_draft_do_not_reserve_or_move_any_money(self):
        source = self.source()
        payload = self.data(source)
        count = self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0]
        changes = self.db.total_changes
        plan = remittances.preview(self.db, payload)
        self.assertEqual(self.db.total_changes, changes)
        self.assertTrue(plan['can_approve'])
        self.assertEqual(plan['amount_cents'], 100)
        self.assertEqual(plan['allocations'][0]['available_cents'], 300)
        first = remittances.save(self.db, payload)
        second = remittances.save(self.db, self.data(source, '3'))
        self.assertEqual(second['amount_cents'], 300)
        self.assertEqual(balances.capacity(self.db, source['id'], '2026-10-20'), 300)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], count)
        self.assertEqual(remittances.save(self.db, payload)['id'], first['id'])
        with self.assertRaises(accounting.ConflictError):
            remittances.save(self.db, {**payload, 'notes': 'محتوى مختلف'})

    def test_cash_partial_remittance_posts_original_tax_account_and_preserves_ap(self):
        source = self.source()
        ap_before = payables.supplier_balances(self.db, 1)
        cash_before = payments.account_balance(self.db, self.fixture.cash)
        doc = self.post(self.data(source, '1'))
        self.assertEqual(doc['effective_status'], 'posted')
        self.assertEqual(doc['journal']['debit_cents'], 100)
        debit = next(line for line in doc['journal']['lines'] if line['debit_cents'])
        self.assertEqual((debit['account_id'], debit['supplier_id'], debit['debit_cents']), (self.wh_account, 1, 100))
        self.assertEqual(payments.account_balance(self.db, self.fixture.cash), cash_before - 100)
        self.assertEqual(payables.supplier_balances(self.db, 1), ap_before)
        self.assertEqual(balances.capacity(self.db, source['id'], '2026-10-20'), 200)
        self.assertTrue(balances.balance_report(self.db)['matched'])

    def test_bank_remittance_can_combine_sources_and_keeps_value_date_independent(self):
        first, second = self.source(), self.source('200', supplier=2)
        doc = self.post(self.data(first, '1', method='bank_transfer', allocations=[
            {'payment_id': first['id'], 'amount': '1'}, {'payment_id': second['id'], 'amount': '3'}]))
        self.assertEqual(doc['amount_cents'], 400)
        self.assertEqual(doc['details']['value_date'], '2026-10-25')
        self.assertEqual(doc['business_date'], '2026-10-20')
        self.assertEqual(payments.account_balance(self.db, self.fixture.bank), -400)
        self.assertEqual({line['supplier_id'] for line in doc['journal']['lines'] if line['debit_cents']}, {1, 2})
        self.assertTrue(balances.balance_report(self.db)['matched'])

    def test_original_account_and_saved_rate_survive_current_mapping_changes(self):
        source = self.source()
        payload = self.data(source)
        doc = remittances.save(self.db, payload)
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE role='withholding_payable'", (self.fixture.account('213005'),))
        withholding.update_code(self.db, self.code['id'], {'revision': self.code['revision'], 'rate': '9', 'active': False})
        self.db.execute("UPDATE suppliers SET name='اسم لاحق',tax_number='تسجيل لاحق' WHERE id=1")
        posted = remittances.approve(self.db, doc['id'], {'revision': doc['revision']})
        row = posted['allocations'][0]
        self.assertEqual(row['account_id'], self.wh_account)
        self.assertEqual(row['rate_basis'], 300)
        self.assertEqual(row['supplier_name'], source['withholding']['supplier_name'])
        self.assertTrue(balances.balance_report(self.db)['matched'])

    def test_competing_drafts_are_rechecked_at_approval(self):
        source = self.source()
        first = remittances.save(self.db, self.data(source, '2'))
        second_payload = self.data(source, '2')
        second = remittances.save(self.db, second_payload)
        remittances.approve(self.db, first['id'], {'revision': first['revision']})
        with self.assertRaisesRegex(ValueError, 'يتجاوز'):
            remittances.approve(self.db, second['id'], {'revision': second['revision']})
        updated = remittances.save(self.db, {**second_payload, 'revision': second['revision'],
            'allocations': [{'payment_id': source['id'], 'amount': '1'}]}, second['id'])
        remittances.approve(self.db, updated['id'], {'revision': updated['revision']})
        self.assertEqual(balances.capacity(self.db, source['id'], '2026-10-20'), 0)
        self.assertTrue(balances.balance_report(self.db)['matched'])

    def test_future_usage_and_later_reversal_do_not_allow_historical_double_remit(self):
        source = self.source()
        future = self.post(self.data(source, '2', date='2026-10-25'))
        self.reverse(future, '2026-10-28')
        with self.assertRaisesRegex(ValueError, 'يتجاوز'):
            remittances.save(self.db, self.data(source, '2', date='2026-10-20'))
        self.post(self.data(source, '1', date='2026-10-20'))
        self.assertEqual(balances.balance_report(self.db, '2026-10-26')['sources'][0]['remaining_cents'], 0)
        self.assertEqual(balances.balance_report(self.db, '2026-10-29')['sources'][0]['remaining_cents'], 200)

    def test_known_future_source_cancellation_blocks_new_historical_remittance(self):
        source = self.source()
        self.reverse_source(source, '2026-10-25')
        with self.assertRaisesRegex(ValueError, 'يتجاوز'):
            remittances.save(self.db, self.data(source, '1', date='2026-10-20'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM withholding_remittances').fetchone()[0], 0)

    def test_source_reversal_after_remittance_retains_credit_and_caps_new_net_payment(self):
        first = self.source()
        remit = self.post(self.data(first, '3'))
        self.reverse_source(first, '2026-10-21')
        report = balances.balance_report(self.db, '2026-10-21')
        self.assertTrue(report['matched'])
        self.assertEqual(report['sources'][0]['remaining_cents'], -300)
        self.assertEqual(remittances.detail(self.db, remit['id'])['effective_status'], 'posted')
        second = self.source('200', date='2026-10-22')
        with self.assertRaisesRegex(ValueError, 'صافي المستحق'):
            remittances.save(self.db, self.data(second, '4', date='2026-10-23'))
        self.post(self.data(second, '3', date='2026-10-23'))
        report = balances.balance_report(self.db, '2026-10-23')
        self.assertTrue(report['matched'])
        self.assertEqual(report['all_totals']['net_due_cents'], 0)
        self.assertEqual(sorted(row['remaining_cents'] for row in report['sources']), [-300, 300])

    def test_dated_reversal_is_exact_immutable_and_safe_to_retry(self):
        source = self.source()
        doc = self.post(self.data(source, '2'))
        original_row = dict(self.db.execute('SELECT * FROM withholding_remittances WHERE id=?', (doc['id'],)).fetchone())
        payload = {'request_key': 'same-correction', 'revision': doc['revision'], 'event_revision': 0,
            'business_date': '2026-10-22', 'reference': 'REV-1', 'reason': 'تصحيح محفوظ'}
        reversed_doc = remittances.reverse(self.db, doc['id'], payload)
        self.assertEqual(reversed_doc['status'], 'posted')
        self.assertEqual(reversed_doc['effective_status'], 'reversed')
        self.assertEqual(remittances.detail(self.db, doc['id'], '2026-10-21')['effective_status'], 'posted')
        self.assertEqual(balances.balance_report(self.db, '2026-10-21')['sources'][0]['remaining_cents'], 100)
        self.assertEqual(balances.balance_report(self.db, '2026-10-22')['sources'][0]['remaining_cents'], 300)
        self.assertEqual(dict(self.db.execute('SELECT * FROM withholding_remittances WHERE id=?', (doc['id'],)).fetchone()), original_row)
        reverse_lines = reversed_doc['reversal']['journal']['lines']
        self.assertEqual([(row['account_id'], row['debit_cents'], row['credit_cents']) for row in reverse_lines],
                         [(row['account_id'], row['credit_cents'], row['debit_cents']) for row in doc['journal']['lines']])
        self.assertEqual(remittances.reverse(self.db, doc['id'], payload)['reversal']['id'], reversed_doc['reversal']['id'])
        with self.assertRaises(accounting.ConflictError):
            remittances.reverse(self.db, doc['id'], {**payload, 'reason': 'محتوى مختلف'})
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE withholding_remittance_reversals SET reason='تغيير' WHERE remittance_id=?", (doc['id'],))

    def test_receipt_reference_can_be_reused_only_after_recorded_reversal(self):
        source = self.source('200')
        payload = self.data(source, '1')
        original = self.post(payload)
        with self.assertRaisesRegex(ValueError, 'مرجع'):
            remittances.save(self.db, {**payload, 'request_key': self.key('new')})
        self.reverse(original, '2026-10-22')
        with self.assertRaisesRegex(ValueError, 'مرجع'):
            remittances.save(self.db, {**payload, 'request_key': self.key('new'), 'business_date': '2026-10-21'})
        corrected = self.post({**payload, 'request_key': self.key('new'), 'business_date': '2026-10-22'})
        self.assertNotEqual(original['id'], corrected['id'])

    def test_month_guards_and_replays_after_close_keep_original_journal(self):
        source = self.source()
        payload = self.data(source, '1')
        original = self.post(payload)
        review = periods.preview(self.db, '2026-10')
        self.assertTrue(review['can_close'], review['blockers'])
        periods.close(self.db, '2026-10', {'request_key': self.key('close'),
            'revision': review['period']['revision'], 'reference': 'CLOSE', 'reason': 'قفل اختبار'})
        self.assertEqual(remittances.save(self.db, payload)['id'], original['id'])
        self.assertEqual(remittances.approve(self.db, original['id'], {'revision': original['revision']})['journal_id'], original['journal_id'])
        with self.assertRaisesRegex(ValueError, 'مقفولة'):
            remittances.save(self.db, self.data(source, '1'))
        with self.assertRaisesRegex(ValueError, 'مقفولة'):
            self.reverse(original, '2026-10-23')
        self.assertEqual(self.reverse(original, '2026-11-01')['effective_status'], 'reversed')

    def test_posted_header_allocations_and_new_parent_are_protected(self):
        source = self.source()
        posted = self.post(self.data(source, '1'))
        draft = remittances.save(self.db, self.data(source, '1'))
        for sql, args in (
            ("UPDATE withholding_remittances SET amount_cents=2 WHERE id=?", (posted['id'],)),
            ('DELETE FROM withholding_remittances WHERE id=?', (posted['id'],)),
            ('DELETE FROM withholding_remittance_allocations WHERE remittance_id=?', (posted['id'],)),
            ('UPDATE withholding_remittance_allocations SET remittance_id=? WHERE remittance_id=?', (posted['id'], draft['id'])),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql, args)

    def test_future_ledger_difference_blocks_approval_without_changing_preview_date(self):
        source = self.source()
        payload = self.data(source, '1', date='2026-10-20')
        draft = remittances.save(self.db, payload)
        # A balanced but unidentified later entry on the control account must
        # not be hidden by a currently matched, backdated approval preview.
        payables.create_journal(self.db, 'unidentified_tax_movement', 1, '2026-10-25', [
            {'account_id': self.fixture.bank, 'debit_cents': 100},
            {'account_id': self.wh_account, 'credit_cents': 100}])
        self.assertTrue(remittances.preview(self.db, payload)['matched'])
        self.assertFalse(balances.balance_report(self.db, '2026-10-25')['matched'])
        count = self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0]
        with self.assertRaisesRegex(ValueError, 'فروق لاحقة'):
            remittances.approve(self.db, draft['id'], {'revision': draft['revision']})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], count)

    def test_current_ledger_difference_can_be_reviewed_as_draft_but_not_approved(self):
        source = self.source()
        payables.create_journal(self.db, 'unidentified_tax_movement', 1, '2026-10-17', [
            {'account_id': self.wh_account, 'debit_cents': 100},
            {'account_id': self.fixture.bank, 'credit_cents': 100}])
        payload = self.data(source)
        self.assertFalse(remittances.preview(self.db, payload)['can_approve'])
        draft = remittances.save(self.db, payload)
        with self.assertRaisesRegex(ValueError, 'مطابقة'):
            remittances.approve(self.db, draft['id'], {'revision': draft['revision']})

    def test_invalid_inputs_and_stale_revisions_make_no_financial_changes(self):
        source = self.source()
        base = self.data(source)
        count = self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0]
        for changes in ({'allocations': []}, {'amount': '2'}, {'business_date': '2026-10-15'},
                        {'period_from': '2026-10-21'}, {'method': 'cheque'},
                        {'details': {'received_by': 'الجهة', 'cvv': '123'}},
                        {'funding_account_id': self.fixture.bank},
                        {'allocations': [{'payment_id': source['id'], 'amount': True}]},
                        {'allocations': [{'payment_id': source['id'], 'amount': '1.001'}]},
                        {'allocations': base['allocations'] * 2}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                remittances.save(self.db, {**base, **changes})
        draft = remittances.save(self.db, base)
        with self.assertRaises(accounting.ConflictError):
            remittances.approve(self.db, draft['id'], {'revision': True})
        with self.assertRaises(accounting.ConflictError):
            remittances.save(self.db, {'revision': draft['revision'] + 1, 'action': 'cancel'}, draft['id'])
        cancelled = remittances.save(self.db, {'revision': draft['revision'], 'action': 'cancel'}, draft['id'])
        self.assertEqual(cancelled['effective_status'], 'cancelled')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], count)


if __name__ == '__main__':
    unittest.main(verbosity=2)
