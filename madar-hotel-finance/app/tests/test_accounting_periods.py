"""Monthly locks are reviewed, dated and never rewrite financial documents."""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting
import accounting_periods as periods
import payables
import test_supplier_payments as fixtures


class AccountingPeriods(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SupplierPayments('runTest')
        self.fixture.setUp()
        self.db = self.fixture.db
        periods.initialize(self.db)
        self.serial = 0

    def tearDown(self):
        self.fixture.tearDown()

    def payload(self, revision=1, **extra):
        self.serial += 1
        return {'request_key': f'period-request-{self.serial}', 'revision': revision,
                'reference': f'REVIEW-{self.serial}', 'reason': 'تمت مراجعة المستندات والمطابقة', **extra}

    def codes(self, report):
        return {item['code'] for item in report['blockers']}

    def test_snapshot_twelve_virtual_months_is_read_only_and_empty_without_balances(self):
        before = self.db.total_changes
        result = periods.snapshot(self.db, 2026)
        self.assertEqual(len(result['periods']), 12)
        self.assertEqual(result['periods'][0]['month'], '2026-01')
        self.assertEqual(result['periods'][-1]['end_date'], '2026-12-31')
        self.assertTrue(all(row['status'] == 'open' and row['revision'] == 1 for row in result['periods']))
        self.assertEqual(self.db.total_changes, before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM accounting_periods').fetchone()[0], 0)
        self.assertEqual(periods.snapshot(self.db, '2024')['periods'][1]['end_date'], '2024-02-29')
        review = periods.preview(self.db, '2026-10')
        self.assertTrue(review['can_close'])
        self.assertEqual(review['journal_totals']['count'], 0)
        self.assertTrue(review['warnings'])
        self.assertEqual(self.db.total_changes, before)

    def test_close_balanced_month_preserves_review_and_reopens_with_audited_reason(self):
        invoice = self.fixture.invoice()
        before = periods.preview(self.db, '2026-10')
        self.assertTrue(before['can_close'], before)
        self.assertEqual(before['journal_totals']['debit_cents'], invoice['total_cents'])
        data = self.payload()
        with self.db:
            result = periods.close(self.db, '2026-10', data)
        self.assertEqual(result['period']['status'], 'closed')
        self.assertEqual(result['period']['revision'], 2)
        self.assertEqual(result['event']['code'], 'PER-000001')
        self.assertEqual(result['event']['snapshot']['journal_totals'], before['journal_totals'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, 'مقفولة'):
            periods.assert_open(self.db, '2026-10-01')
        periods.assert_open(self.db, '2026-11-01')
        with self.db:
            reopened = periods.reopen(self.db, '2026-10', self.payload(revision=2, reason='تصحيح مستند بتاريخ أكتوبر'))
        self.assertEqual(reopened['period']['status'], 'open')
        self.assertEqual(reopened['period']['revision'], 3)
        self.assertEqual(reopened['event']['action'], 'reopen')
        periods.assert_open(self.db, '2026-10-15')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM accounting_period_events').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM audit WHERE entity=\'accounting_period\'').fetchone()[0], 2)

    def test_invoice_draft_blocks_close_but_posted_overlay_and_cancelled_do_not(self):
        pk = self.db.execute("INSERT INTO invoices(code,request_key,supplier_id,reference,business_date,department_id,status,subtotal_cents,total_cents,created_at,updated_at) VALUES('DRAFT-PERIOD','draft-period',1,'draft-ref','2026-10-01',1,'draft',10000,10000,'now','now')").lastrowid
        invoice = {'id': pk}
        report = periods.preview(self.db, '2026-10')
        self.assertFalse(report['can_close'])
        self.assertEqual(report['draft_counts']['invoice'], 1)
        self.assertEqual(report['pending_drafts'][0]['id'], invoice['id'])
        with self.assertRaisesRegex(ValueError, 'المسودات'):
            periods.close(self.db, '2026-10', self.payload())
        self.db.execute("UPDATE invoices SET status='cancelled' WHERE id=?", (invoice['id'],))
        self.assertTrue(periods.preview(self.db, '2026-10')['can_close'])
        self.fixture.invoice()
        report = periods.preview(self.db, '2026-10')
        self.assertEqual(report['draft_counts']['invoice'], 0)
        self.assertTrue(report['can_close'], report)

    def test_each_supplier_document_draft_including_zero_return_blocks_close(self):
        invoice = self.fixture.invoice()
        payment = self.fixture.payload(amount='10', allocation_mode='invoice', target_invoice_id=invoice['id'])
        import supplier_payments
        import supplier_settlements
        import supplier_adjustments
        supplier_settlements.initialize(self.db)
        supplier_adjustments.initialize(self.db)
        supplier_payments.save_payment(self.db, payment)
        line = invoice['lines'][0]
        payables.save_return(self.db, {'invoice_id': invoice['id'], 'business_date': '2026-10-21',
                                      'reference': 'RET-DRAFT', 'lines': [{'invoice_line_id': line['id'], 'quantity': '.01'}]})
        # Pending zero-value drafts also represent unfinished reviewed records.
        self.db.execute("UPDATE supplier_returns SET total_cents=0 WHERE reference='RET-DRAFT'")
        report = periods.preview(self.db, '2026-10')
        self.assertEqual(report['draft_counts']['payment'], 1)
        self.assertEqual(report['draft_counts']['return'], 1)
        self.assertFalse(report['can_close'])
        self.assertTrue(all(row['date'].startswith('2026-10') for row in report['pending_drafts']))

    def test_reconciliation_difference_blocks_close_even_when_journal_balances(self):
        self.fixture.invoice()
        payables.create_journal(self.db, 'review_mismatch', 1, '2026-10-22', [
            {'account_id': self.fixture.ap, 'debit_cents': 100, 'supplier_id': 1},
            {'account_id': self.fixture.cash, 'credit_cents': 100, 'supplier_id': 1},
        ])
        report = periods.preview(self.db, '2026-10')
        self.assertIn('supplier_reconciliation', self.codes(report))
        self.assertFalse(report['can_close'])
        self.assertFalse(report['reconciliation']['matched'])
        self.assertEqual(report['unbalanced_journals'], [])

    def test_balanced_month_totals_cannot_hide_two_unbalanced_journals(self):
        # Simulate a damaged import without weakening the production protections.
        stamp = accounting._now()
        first = self.db.execute("INSERT INTO journal_entries(code,source_type,source_id,entry_date,created_at,sealed,request_fingerprint) VALUES('BAD-1','broken',1,'2026-10-01',?,0,'a')", (stamp,)).lastrowid
        second = self.db.execute("INSERT INTO journal_entries(code,source_type,source_id,entry_date,created_at,sealed,request_fingerprint) VALUES('BAD-2','broken',2,'2026-10-02',?,0,'b')", (stamp,)).lastrowid
        self.db.execute('INSERT INTO journal_lines(journal_id,account_id,debit_cents,credit_cents) VALUES(?,?,100,0)', (first, self.fixture.cash))
        self.db.execute('INSERT INTO journal_lines(journal_id,account_id,debit_cents,credit_cents) VALUES(?,?,0,100)', (second, self.fixture.cash))
        self.db.execute('UPDATE journal_entries SET sealed=1 WHERE id IN(?,?)', (first, second))
        report = periods.preview(self.db, '2026-10')
        self.assertEqual(report['journal_totals']['debit_cents'], report['journal_totals']['credit_cents'])
        self.assertEqual(len(report['unbalanced_journals']), 2)
        self.assertIn('unbalanced_journals', self.codes(report))
        self.assertFalse(report['can_close'])

    def test_unsealed_journal_without_lines_is_not_silently_ignored(self):
        self.db.execute("INSERT INTO journal_entries(code,source_type,source_id,entry_date,created_at,sealed,request_fingerprint) VALUES('WAIT-1','waiting',1,'2026-10-01','now',0,'x')")
        report = periods.preview(self.db, '2026-10')
        self.assertEqual(len(report['unsealed_journals']), 1)
        self.assertIn('unsealed_journals', self.codes(report))
        self.assertFalse(report['can_close'])

    def test_later_close_does_not_ignore_earlier_damaged_non_supplier_journals(self):
        first = self.db.execute("INSERT INTO journal_entries(code,source_type,source_id,entry_date,created_at,sealed,request_fingerprint) VALUES('EARLY-BAD','broken',1,'2026-09-01','now',0,'a')").lastrowid
        self.db.execute('INSERT INTO journal_lines(journal_id,account_id,debit_cents,credit_cents) VALUES(?,?,100,0)', (first, self.fixture.cash))
        self.db.execute('UPDATE journal_entries SET sealed=1 WHERE id=?', (first,))
        self.db.execute("INSERT INTO journal_entries(code,source_type,source_id,entry_date,created_at,sealed,request_fingerprint) VALUES('EARLY-WAIT','waiting',2,'2026-09-02','now',0,'b')")
        review = periods.preview(self.db, '2026-10')
        self.assertTrue(review['reconciliation']['matched'])
        self.assertEqual(review['journal_totals']['count'], 0)
        self.assertEqual(review['unbalanced_journals'], [])
        self.assertEqual(len(review['earlier_unbalanced_journals']), 1)
        self.assertEqual(len(review['earlier_unsealed_journals']), 1)
        self.assertIn('earlier_unbalanced_journals', self.codes(review))
        self.assertIn('earlier_unsealed_journals', self.codes(review))
        self.assertFalse(review['can_close'])

    def test_retries_revisions_and_changed_request_identity_are_checked(self):
        data = self.payload()
        first = periods.close(self.db, '2026-10', data)
        again = periods.close(self.db, '2026-10', data)
        self.assertEqual(first['event']['id'], again['event']['id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM accounting_period_events').fetchone()[0], 1)
        with self.assertRaises(accounting.ConflictError):
            periods.close(self.db, '2026-11', data)
        with self.assertRaises(accounting.ConflictError):
            periods.close(self.db, '2026-10', {**data, 'reason': 'سبب مختلف'})
        with self.assertRaises(accounting.ConflictError):
            periods.reopen(self.db, '2026-10', self.payload(revision=1))
        with self.assertRaises(accounting.ConflictError):
            periods.reopen(self.db, '2026-10', self.payload(revision=True))
        retry = self.payload(revision=2)
        reopened = periods.reopen(self.db, '2026-10', retry)
        self.assertEqual(periods.reopen(self.db, '2026-10', retry)['event']['id'], reopened['event']['id'])

    def test_event_log_cannot_be_changed_or_deleted_and_caller_can_rollback(self):
        closed = periods.close(self.db, '2026-10', self.payload())
        for sql in ('UPDATE accounting_period_events SET reason=\'changed\'', 'DELETE FROM accounting_period_events'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql)
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM accounting_period_events').fetchone()[0], 0)
        self.assertEqual(periods.snapshot(self.db, 2026)['periods'][9]['status'], 'open')
        # The failed event and its sequence are transactional together.
        again = periods.close(self.db, '2026-10', self.payload())
        self.assertEqual(again['event']['code'], closed['event']['code'])

    def test_reopen_requires_later_closed_periods_to_open_first(self):
        periods.close(self.db, '2026-10', self.payload())
        periods.close(self.db, '2026-11', self.payload())
        with self.assertRaisesRegex(ValueError, 'اللاحقة'):
            periods.reopen(self.db, '2026-10', self.payload(revision=2))
        periods.reopen(self.db, '2026-11', self.payload(revision=2))
        periods.reopen(self.db, '2026-10', self.payload(revision=2))
        self.assertTrue(all(p['status'] == 'open' for p in periods.snapshot(self.db, 2026)['periods']))

    def test_date_validation_and_open_fallback_for_legacy_fixtures(self):
        legacy = sqlite3.connect(':memory:')
        legacy.row_factory = sqlite3.Row
        periods.assert_open(legacy, '2026-01-31')
        legacy.close()
        for value in ('2026-1', '2026-00', '2026-13', '0000-01', '202610', None):
            with self.assertRaises(ValueError):
                periods.preview(self.db, value)
        for value in (0, True, '26', 10000):
            with self.assertRaises(ValueError):
                periods.snapshot(self.db, value)
        for value in ('2026-1-1', '2026-02-30', 20261001, None):
            with self.assertRaises(ValueError):
                periods.assert_open(self.db, value)
        for field in ('reference', 'reason'):
            with self.assertRaises(ValueError):
                periods.close(self.db, '2026-10', {**self.payload(), field: ''})

    def test_later_closed_month_blocks_backdated_write_to_earlier_open_month(self):
        periods.close(self.db, '2026-11', self.payload())
        earlier = periods.snapshot(self.db, 2026)['periods'][9]
        self.assertEqual(earlier['status'], 'open')
        self.assertTrue(earlier['effective_locked'])
        self.assertEqual(earlier['blocked_by_later_closed'], '2026-11')
        with self.assertRaisesRegex(ValueError, 'اللاحقة'):
            periods.assert_open(self.db, '2026-10-15')
        periods.assert_open(self.db, '2026-12-01')
        periods.reopen(self.db, '2026-11', self.payload(revision=2))
        periods.assert_open(self.db, '2026-10-15')

    def test_earlier_drafts_cannot_be_stranded_by_closing_a_later_month(self):
        self.db.execute("INSERT INTO invoices(code,request_key,supplier_id,reference,business_date,department_id,status,subtotal_cents,total_cents,created_at,updated_at) VALUES('EARLIER','earlier',1,'earlier','2026-09-30',1,'draft',100,100,'now','now')")
        review = periods.preview(self.db, '2026-10')
        self.assertEqual(review['pending_drafts'], [])
        self.assertEqual(len(review['earlier_pending_drafts']), 1)
        self.assertIn('earlier_pending_drafts', self.codes(review))
        self.assertFalse(review['can_close'])

    def test_event_snapshots_do_not_recursively_embed_the_event_history(self):
        periods.close(self.db, '2026-10', self.payload())
        periods.reopen(self.db, '2026-10', self.payload(revision=2))
        periods.close(self.db, '2026-10', self.payload(revision=3))
        result = periods.preview(self.db, '2026-10')
        self.assertEqual(len(result['events']), 3)
        for event in result['events']:
            self.assertNotIn('events', event['snapshot'])
            prior = event['snapshot'].get('period', {}).get('last_event')
            if prior:
                self.assertNotIn('snapshot', prior)

    def test_moving_document_date_checks_old_and_new_period(self):
        periods.close(self.db, '2026-10', self.payload())
        with self.assertRaises(ValueError):
            periods.assert_document_dates(self.db, '2026-11-01', '2026-10-01')
        with self.assertRaises(ValueError):
            periods.assert_document_dates(self.db, '2026-10-01', '2026-11-01')
        periods.assert_document_dates(self.db, '2026-11-02', '2026-11-01')


if __name__ == '__main__':
    unittest.main(verbosity=2)
