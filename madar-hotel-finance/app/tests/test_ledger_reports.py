"""Exact dated trial balance and ledger, including damaged journal diagnostics."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting
import ledger_reports as reports
import payables
import supplier_adjustments
import supplier_settlements
import test_supplier_payments as fixtures


class LedgerReports(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SupplierPayments('runTest')
        self.fixture.setUp()
        self.db = self.fixture.db

    def tearDown(self):
        self.fixture.tearDown()

    def trial(self, start='2026-10-16', end='2026-10-31', **options):
        return reports.trial_balance(self.db, start, end, **options)

    def test_empty_database_is_read_only_and_never_infers_starting_cash(self):
        changes = self.db.total_changes
        report = self.trial()
        self.assertTrue(report['balanced'])
        self.assertEqual(report['rows'], [])
        self.assertEqual(report['groups'], [])
        self.assertTrue(all(value == 0 for value in report['totals'].values()))
        ledger = reports.account_ledger(self.db, self.fixture.cash, '2026-01-01', '2026-12-31')
        self.assertEqual(ledger['rows'], [])
        self.assertEqual(ledger['opening_balance_cents'], 0)
        self.assertEqual(self.db.total_changes, changes)

    def test_pristine_chart_without_journal_tables_remains_readable(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        try:
            accounting.initialize(db)
            report = reports.trial_balance(db, '2026-01-01', '2026-12-31', include_zero=True)
            self.assertEqual(len(report['rows']), 194)
            self.assertEqual(len(report['groups']), 56)
            self.assertTrue(report['balanced'])
            cash = db.execute("SELECT id FROM accounts WHERE code='111002'").fetchone()[0]
            self.assertEqual(reports.account_ledger(db, cash)['closing_balance_cents'], 0)
        finally:
            db.close()

    def test_period_opening_movement_closing_and_rollups_do_not_double_count(self):
        invoice = self.fixture.invoice()
        self.fixture.post(self.fixture.payload(amount='30', allocation_mode='invoice', target_invoice_id=invoice['id']))
        changes = self.db.total_changes
        report = self.trial()
        rows = {r['account_id']: r for r in report['rows']}
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[self.fixture.expense]['opening_debit_cents'], 10000)
        self.assertEqual(rows[self.fixture.ap]['opening_credit_cents'], 10000)
        self.assertEqual(rows[self.fixture.ap]['debit_cents'], 3000)
        self.assertEqual(rows[self.fixture.ap]['closing_balance_cents'], -7000)
        self.assertEqual(rows[self.fixture.cash]['credit_cents'], 3000)
        self.assertEqual(rows[self.fixture.cash]['closing_credit_cents'], 3000)
        self.assertEqual(report['totals'], {
            'opening_debit_cents': 10000, 'opening_credit_cents': 10000,
            'debit_cents': 3000, 'credit_cents': 3000,
            'closing_debit_cents': 10000, 'closing_credit_cents': 10000,
            'opening_difference_cents': 0, 'movement_difference_cents': 0, 'closing_difference_cents': 0})
        assets = next(r for r in report['groups'] if r['code'] == '100000')
        liabilities = next(r for r in report['groups'] if r['code'] == '200000')
        self.assertEqual(assets['closing_credit_cents'], 3000)
        self.assertEqual(liabilities['closing_credit_cents'], 7000)
        self.assertEqual(report['totals']['credit_cents'], sum(r['credit_cents'] for r in report['rows']))
        self.assertTrue(report['balanced'])
        self.assertEqual(self.db.total_changes, changes)

    def test_inactive_and_unusually_sided_accounts_preserve_history(self):
        self.fixture.invoice()
        self.db.execute('UPDATE accounts SET active=0 WHERE id=?', (self.fixture.expense,))
        report = self.trial()
        expense = next(r for r in report['rows'] if r['account_id'] == self.fixture.expense)
        self.assertFalse(expense['active'])
        self.assertEqual(expense['closing_balance_cents'], 10000)
        account = reports.account_ledger(self.db, self.fixture.expense, '2026-10-01', '2026-10-31')
        self.assertEqual(account['closing_debit_cents'], 10000)
        self.assertTrue(account['complete'])

    def test_zero_closing_account_with_period_movement_stays_visible(self):
        payables.create_journal(self.db, 'test_in', 1, '2026-10-16', [
            {'account_id': self.fixture.cash, 'debit_cents': 100}, {'account_id': self.fixture.bank, 'credit_cents': 100}])
        payables.create_journal(self.db, 'test_out', 1, '2026-10-17', [
            {'account_id': self.fixture.cash, 'credit_cents': 100}, {'account_id': self.fixture.bank, 'debit_cents': 100}])
        report = self.trial()
        self.assertEqual(len(report['rows']), 2)
        self.assertTrue(all(row['closing_balance_cents'] == 0 for row in report['rows']))
        self.assertEqual(report['totals']['debit_cents'], 200)
        self.assertEqual(len(self.trial(include_zero=True)['rows']), 195)

    def test_account_ledger_runs_from_historical_balance_and_links_source(self):
        invoice = self.fixture.invoice()
        payment = self.fixture.post(self.fixture.payload(amount='30', allocation_mode='invoice', target_invoice_id=invoice['id']))
        ledger = reports.account_ledger(self.db, self.fixture.ap, '2026-10-16', '2026-10-31')
        self.assertEqual(ledger['opening_balance_cents'], -10000)
        self.assertEqual(ledger['closing_balance_cents'], -7000)
        row = ledger['rows'][0]
        self.assertEqual(row['running_balance_cents'], -7000)
        self.assertEqual(row['running_credit_cents'], 7000)
        self.assertEqual(row['date'], '2026-10-16')
        self.assertEqual(row['document_code'], payment['code'])
        self.assertEqual(row['source_type'], 'supplier_payment')
        self.assertEqual(row['source_id'], payment['id'])
        self.assertEqual(row['supplier_name'], 'مورد أغذية')
        self.assertEqual(row['journal_id'], payment['journal']['id'])
        self.assertEqual(ledger['totals']['debit_cents'], 3000)
        self.assertEqual(ledger['currency'], 'EGP')

    def test_supplier_filter_applies_to_opening_and_movements_and_keeps_order(self):
        self.fixture.invoice(cents=5000, supplier_id=1)
        self.fixture.invoice(cents=9000, supplier_id=2)
        self.fixture.post(self.fixture.payload(amount='10', supplier_id=1, allocation_mode='fifo'))
        second = self.fixture.post(self.fixture.payload(amount='20', supplier_id=2, allocation_mode='fifo'))
        ledger = reports.account_ledger(self.db, self.fixture.ap, '2026-10-16', '2026-10-31', supplier_id=2)
        self.assertEqual(ledger['supplier']['id'], 2)
        self.assertEqual(ledger['opening_balance_cents'], -9000)
        self.assertEqual(ledger['debit_cents'], 2000)
        self.assertEqual(ledger['closing_balance_cents'], -7000)
        self.assertEqual([row['source_id'] for row in ledger['rows']], [second['id']])
        all_rows = reports.account_ledger(self.db, self.fixture.ap, '2026-10-16', '2026-10-31')['rows']
        self.assertEqual([row['source_id'] for row in all_rows], sorted(row['source_id'] for row in all_rows))

    def test_refund_reversal_preserves_historical_cash_and_resolves_its_own_document(self):
        supplier_settlements.initialize(self.db)
        supplier_adjustments.initialize(self.db)
        advance = self.fixture.post(self.fixture.payload(amount='100', allow_advance=True))
        draft = supplier_adjustments.save_refund(self.db, {
            'request_key': 'ledger-refund', 'supplier_id': 1, 'source_type': 'advance', 'source_id': advance['id'],
            'business_date': '2026-10-20', 'reference': 'REFUND-REF', 'reason': 'رد جزء من المقدم',
            'method': 'cash', 'funding_account_id': self.fixture.cash, 'amount': '25',
            'details': {'received_by': 'أمين الخزينة'}})
        refund = supplier_adjustments.approve_refund(self.db, draft['id'], {'revision': draft['revision']})
        reversed_doc = supplier_adjustments.reverse_refund(self.db, refund['id'], {
            'request_key': 'ledger-refund-reversal', 'revision': refund['revision'], 'event_revision': 0,
            'business_date': '2026-10-22', 'reference': 'REV-REFUND-REF', 'reason': 'تصحيح رد المبلغ'})
        before = reports.account_ledger(self.db, self.fixture.cash, '2026-10-01', '2026-10-21')
        after = reports.account_ledger(self.db, self.fixture.cash, '2026-10-01', '2026-10-22')
        self.assertEqual(before['closing_balance_cents'], -7500)
        self.assertEqual(after['closing_balance_cents'], -10000)
        row = after['rows'][-1]
        self.assertEqual(row['source_type'], 'supplier_refund_reversal')
        self.assertEqual(row['document_code'], reversed_doc['reversal']['code'])
        self.assertEqual(row['reference'], 'REV-REFUND-REF')
        self.assertEqual(row['original_document_code'], refund['code'])
        self.assertTrue(self.trial()['balanced'])

    def raw_journal(self, ident, day='2026-10-17', sealed=1, debit=100, credit=50, debit_account=None):
        journal = self.db.execute('''INSERT INTO journal_entries(code,source_type,source_id,entry_date,created_at,sealed,request_fingerprint)
            VALUES(?, 'test_corrupt', ?, ?, 'now', 0, ?)''', ('BAD-' + str(ident), ident, day, 'bad-' + str(ident))).lastrowid
        self.db.execute('INSERT INTO journal_lines(journal_id,account_id,debit_cents) VALUES(?,?,?)', (journal, self.fixture.cash if debit_account is None else debit_account, debit))
        self.db.execute('INSERT INTO journal_lines(journal_id,account_id,credit_cents) VALUES(?,?,?)', (journal, self.fixture.bank, credit))
        if sealed:
            self.db.execute('UPDATE journal_entries SET sealed=1 WHERE id=?', (journal,))
        return journal

    def test_offsetting_bad_journals_do_not_pass_balanced_totals(self):
        first = self.raw_journal(1, debit=100, credit=50)
        second = self.raw_journal(2, debit=50, credit=100)
        report = self.trial()
        self.assertEqual(report['totals']['movement_difference_cents'], 0)
        self.assertEqual(report['totals']['closing_difference_cents'], 0)
        self.assertFalse(report['balanced'])
        self.assertEqual({e['journal_id'] for e in report['exceptions']}, {first, second})
        self.assertTrue(all(e['type'] == 'unbalanced_journal' for e in report['exceptions']))
        self.assertFalse(reports.account_ledger(self.db, self.fixture.cash, '2026-10-16', '2026-10-31')['complete'])
        self.assertTrue(self.trial(end='2026-10-16')['balanced'])

    def test_unsealed_journals_are_excluded_but_reported_and_future_ignored(self):
        pending = self.raw_journal(1, sealed=0, debit=100, credit=100)
        self.raw_journal(2, day='2026-11-01', sealed=0)
        report = self.trial()
        self.assertEqual(report['rows'], [])
        self.assertFalse(report['balanced'])
        self.assertEqual([e['journal_id'] for e in report['exceptions']], [pending])
        self.assertEqual(report['exceptions'][0]['type'], 'unsealed_journal')
        ledger = reports.account_ledger(self.db, self.fixture.cash, '2026-10-16', '2026-10-31')
        self.assertEqual(ledger['rows'], [])
        self.assertEqual(ledger['closing_balance_cents'], 0)
        self.assertFalse(ledger['complete'])

    def test_direct_group_posting_is_visible_and_fails_review(self):
        group = self.fixture.account('111000')
        journal = self.raw_journal(1, debit=100, credit=100)
        self.db.execute("DROP TRIGGER ap_immutable_journal_lines_update")
        self.db.execute('UPDATE journal_lines SET account_id=? WHERE journal_id=? AND debit_cents>0', (group, journal))
        report = self.trial()
        self.assertFalse(report['balanced'])
        self.assertEqual(report['totals']['debit_cents'], 100)
        self.assertEqual(report['totals']['credit_cents'], 100)
        self.assertTrue(any(row['account_id'] == group and not row['posting'] for row in report['rows']))
        self.assertEqual(report['exceptions'][0]['type'], 'nonposting_account')

    def test_missing_account_corruption_is_counted_and_counterparty_ledger_warns(self):
        # Simulate an externally damaged file, not a supported application path.
        # Normal writes run with foreign keys on and cannot create this state.
        self.db.commit()
        self.db.execute('PRAGMA foreign_keys=OFF')
        journal = self.raw_journal(1, debit=100, credit=100, debit_account=999999)
        self.db.commit()
        self.db.execute('PRAGMA foreign_keys=ON')
        report = self.trial()
        self.assertFalse(report['balanced'])
        self.assertEqual(report['totals']['debit_cents'], 100)
        self.assertEqual(report['totals']['credit_cents'], 100)
        missing = next(row for row in report['rows'] if row['account_id'] == 999999)
        self.assertEqual(missing['closing_debit_cents'], 100)
        self.assertEqual(report['exceptions'][0]['type'], 'missing_account')
        ledger = reports.account_ledger(self.db, self.fixture.bank, '2026-10-16', '2026-10-31')
        self.assertFalse(ledger['complete'])
        self.assertEqual(ledger['exceptions'][0]['journal_id'], journal)
        self.assertEqual(ledger['exceptions'][0]['account_id'], 999999)

    def test_invalid_ranges_types_and_group_ledger_are_rejected(self):
        for start, end in [('20261016', '2026-10-31'), ('2026-02-30', '2026-10-31'),
                           ('2026-10-31', '2026-10-16'), (True, '2026-10-31'), ('', '2026-10-31')]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                reports.trial_balance(self.db, start, end)
        with self.assertRaises(ValueError):
            self.trial(include_zero='false')
        for ident in (True, '1', 0, -1, 99999999, self.fixture.account('111000')):
            with self.subTest(account=ident), self.assertRaises(ValueError):
                reports.account_ledger(self.db, ident)
        for supplier in (True, '1', 0, 999999):
            with self.subTest(supplier=supplier), self.assertRaises(ValueError):
                reports.account_ledger(self.db, self.fixture.cash, supplier_id=supplier)

    def test_default_start_tracks_explicit_end_year(self):
        report = reports.trial_balance(self.db, to_date='2024-02-29')
        self.assertEqual(report['from_date'], '2024-01-01')
        self.assertEqual(report['to_date'], '2024-02-29')


if __name__ == '__main__':
    unittest.main(verbosity=2)
