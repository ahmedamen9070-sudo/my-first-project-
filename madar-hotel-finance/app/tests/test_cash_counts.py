"""Count evidence does not create cash; snapshots survive later book changes."""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting
import cash_counts as counts
import payables
import test_treasury as fixtures


class CashCounts(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Treasury('runTest')
        self.f.setUp()
        self.db = self.f.db
        counts.initialize(self.db)
        self.cash = self.f.cash
        self.serial = 0
        self.db.commit()

    def tearDown(self):
        self.f.tearDown()

    def key(self):
        self.serial += 1
        return 'COUNT-TEST-' + str(self.serial)

    def payload(self, **extra):
        return {'request_key': self.key(), 'business_date': '2026-10-16',
                'reference': self.key(), 'cash_account_id': self.cash,
                'counted_by': 'القائم بالعد', 'reviewed_by': 'المراجع', 'notes': 'بيانات تجربة معزولة',
                'lines': [{'denomination_cents': 10000, 'quantity': 2},
                          {'denomination_cents': 50, 'quantity': 3}], **extra}

    def post_cash(self, amount='100', ds='2026-10-16', kind='receipt'):
        return self.f.post(self.f.payload(kind, amount=amount, business_date=ds))

    def confirm(self, data=None):
        draft = counts.save(self.db, data or self.payload())
        return counts.confirm(self.db, draft['id'], {'revision': draft['revision']})

    def void_data(self, doc, **extra):
        return {'request_key': self.key(), 'revision': doc['revision'], 'event_revision': 0,
                'business_date': '2026-10-20', 'reference': self.key(),
                'reason': 'محضر عد سجل بالخطأ، الأصل محفوظ', **extra}

    def force_closed(self, month='2026-10'):
        self.db.execute("INSERT INTO accounting_periods(month,status,updated_at) VALUES(?,'closed','test')", (month,))

    def journal_counts(self):
        return tuple(self.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                     for table in ('journal_entries', 'journal_lines'))

    def test_additive_initialization_and_zero_count_never_create_cash(self):
        accounts = self.db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
        counts.initialize(self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], accounts)
        self.assertEqual(counts.list_counts(self.db), [])
        doc = self.confirm(self.payload(lines=[{'denomination_cents': 20000, 'quantity': 0}]))
        self.assertEqual((doc['actual_cents'], doc['book_cents'], doc['variance_cents']), (0, 0, 0))
        self.assertEqual(doc['status'], 'confirmed')
        self.assertEqual(self.journal_counts(), (0, 0))
        self.assertTrue(all(note for note in doc['warnings']))

    def test_exact_denominations_snapshot_and_nonposting_confirmation(self):
        self.post_cash('200')
        data = self.payload()
        before = self.journal_counts()
        plan = counts.preview(self.db, data)
        self.assertEqual((plan['actual_cents'], plan['book_cents'], plan['variance_cents']), (20150, 20000, 150))
        self.assertEqual(plan['lines'][1]['line_total_cents'], 150)
        self.assertTrue(plan['can_confirm'])
        doc = self.confirm(data)
        self.assertRegex(doc['code'], r'^CCT-\d{6}$')
        self.assertEqual((doc['actual_cents'], doc['book_cents'], doc['variance_cents']), (20150, 20000, 150))
        self.assertTrue(doc['confirmed_at'])
        self.assertFalse(doc['book_changed'])
        self.assertEqual(doc['live_variance_cents'], 150)
        self.assertEqual(self.journal_counts(), before)

    def test_config_preview_saved_reads_are_read_only_and_include_pos_not_transit(self):
        doc = counts.save(self.db, self.payload())
        before = self.db.total_changes
        config = counts.config(self.db)
        self.assertEqual(config['denominations_cents'], list(counts.DENOMINATIONS))
        self.assertIn('111004', {row['code'] for row in config['cash_accounts']})
        self.assertNotIn('111005', {row['code'] for row in config['cash_accounts']})
        counts.preview(self.db, self.payload())
        counts.detail(self.db, doc['id'])
        counts.list_counts(self.db)
        self.assertEqual(self.db.total_changes, before)

    def test_custom_cent_denominations_and_reordered_input_are_exact(self):
        data = self.payload(lines=[{'denomination_cents': 123, 'quantity': 5}, {'denomination_cents': 1, 'quantity': 7}])
        draft = counts.save(self.db, data)
        again = counts.save(self.db, {**data, 'lines': list(reversed(data['lines']))})
        self.assertEqual(again['id'], draft['id'])
        self.assertEqual(draft['actual_cents'], 622)
        self.assertEqual(draft['lines'][1]['line_total_cents'], 7)

    def test_count_input_rejects_invalid_money_quantities_duplicate_totals_and_dates(self):
        bad_rows = ([], [{'denomination_cents': 0, 'quantity': 1}],
                    [{'denomination_cents': True, 'quantity': 1}],
                    [{'denomination_cents': 50.0, 'quantity': 1}],
                    [{'denomination_cents': 50, 'quantity': True}],
                    [{'denomination_cents': 50, 'quantity': -1}],
                    [{'denomination_cents': 50, 'quantity': 1.0}],
                    [{'denomination_cents': 50, 'quantity': 1000001}],
                    [{'denomination_cents': 50, 'quantity': 1}, {'denomination_cents': 50, 'quantity': 0}],
                    [{'denomination_cents': payables.MAX_CENTS, 'quantity': 2}],
                    [{'denomination_cents': 50, 'quantity': 1, 'line_total_cents': 51}],
                    [{'denomination_cents': i + 1, 'quantity': 1} for i in range(21)])
        for rows in bad_rows:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                counts.preview(self.db, self.payload(lines=rows))
        for changes in ({'currency': 'USD'}, {'actual_cents': 20151}, {'actual_cents': True},
                        {'total_cents': 0}, {'business_date': '2026-02-30'},
                        {'business_date': '20261016'}, {'cash_account_id': True},
                        {'counted_by': ''}, {'reviewed_by': ''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                counts.save(self.db, self.payload(**changes))
        self.assertEqual(counts.list_counts(self.db), [])

    def test_only_valid_cash_and_egp_base_currency_are_allowed_for_new_evidence(self):
        for code in ('111005', '112001', '111000'):
            with self.subTest(code=code), self.assertRaises(ValueError):
                counts.preview(self.db, self.payload(cash_account_id=self.f.f.account(code)))
        draft = counts.save(self.db, self.payload())
        self.db.execute("UPDATE accounting_settings SET base_currency='USD'")
        with self.assertRaises(ValueError):
            counts.preview(self.db, self.payload())
        with self.assertRaises(ValueError):
            counts.confirm(self.db, draft['id'], {'revision': draft['revision']})
        self.assertEqual(counts.detail(self.db, draft['id'])['currency'], 'EGP')

    def test_save_update_cancel_and_stale_revision_do_not_post(self):
        data = self.payload()
        draft = counts.save(self.db, data)
        changed = counts.save(self.db, {**data, 'revision': draft['revision'], 'notes': 'مراجعة العدد'}, draft['id'])
        self.assertEqual(changed['revision'], 2)
        with self.assertRaises(accounting.ConflictError):
            counts.save(self.db, {**data, 'revision': 1}, draft['id'])
        cancelled = counts.save(self.db, {'revision': 2, 'action': 'cancel'}, draft['id'])
        self.assertEqual(cancelled['effective_status'], 'cancelled')
        with self.assertRaises(ValueError):
            counts.confirm(self.db, draft['id'], {'revision': cancelled['revision']})
        self.assertEqual(self.journal_counts(), (0, 0))

    def test_request_replays_and_conflicts_before_current_month_locks(self):
        data = self.payload()
        draft = counts.save(self.db, data)
        doc = counts.confirm(self.db, draft['id'], {'revision': draft['revision']})
        self.force_closed()
        self.assertEqual(counts.save(self.db, data)['id'], doc['id'])
        self.assertEqual(counts.confirm(self.db, doc['id'], {'revision': draft['revision']})['id'], doc['id'])
        with self.assertRaises(accounting.ConflictError):
            counts.save(self.db, {**data, 'notes': 'بيانات أخرى'})
        with self.assertRaises(accounting.ConflictError):
            counts.confirm(self.db, doc['id'], {'revision': 999})

    def test_changed_balance_requires_resave_and_preserves_frozen_original(self):
        self.post_cash('200')
        data = self.payload()
        draft = counts.save(self.db, data)
        self.post_cash('10')
        visible = counts.detail(self.db, draft['id'])
        self.assertEqual((visible['book_cents'], visible['variance_cents']), (20000, 150))
        self.assertEqual((visible['live_book_cents'], visible['live_variance_cents']), (21000, -850))
        self.assertTrue(visible['book_changed'])
        with self.assertRaises(accounting.ConflictError):
            counts.confirm(self.db, draft['id'], {'revision': 1})
        refreshed = counts.save(self.db, {**data, 'revision': 1}, draft['id'])
        self.assertEqual(refreshed['book_cents'], 21000)
        self.assertFalse(refreshed['book_changed'])
        self.assertEqual(counts.confirm(self.db, draft['id'], {'revision': 2})['status'], 'confirmed')

    def test_net_zero_cash_journals_still_change_confirmation_evidence(self):
        draft = counts.save(self.db, self.payload())
        self.post_cash('10')
        self.post_cash('10', kind='payment')
        changed = counts.detail(self.db, draft['id'])
        self.assertEqual((changed['book_cents'], changed['live_book_cents']), (0, 0))
        self.assertTrue(changed['book_changed'])
        self.assertEqual(changed['live_evidence']['journal_count'], 2)
        with self.assertRaises(accounting.ConflictError):
            counts.confirm(self.db, draft['id'], {'revision': 1})

    def test_counterparty_dimension_changes_are_part_of_snapshot_freshness(self):
        original = self.post_cash('10', kind='payment')
        draft = counts.save(self.db, self.payload())
        triggers = self.db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='journal_lines'").fetchall()
        for row in triggers:
            self.db.execute('DROP TRIGGER ' + row['name'])
        self.db.execute('UPDATE journal_lines SET department_id=NULL WHERE journal_id=? AND debit_cents>0', (original['journal_id'],))
        self.assertTrue(counts.detail(self.db, draft['id'])['book_changed'])
        with self.assertRaises(accounting.ConflictError):
            counts.confirm(self.db, draft['id'], {'revision': 1})

    def test_damaged_draft_facts_cannot_be_confirmed_even_when_cash_hash_is_unchanged(self):
        for damaged in ('actual', 'lines', 'book_and_variance', 'evidence_summary'):
            with self.subTest(damaged=damaged):
                draft = counts.save(self.db, self.payload())
                if damaged == 'actual':
                    self.db.execute('UPDATE cash_counts SET actual_cents=actual_cents+1 WHERE id=?', (draft['id'],))
                elif damaged == 'lines':
                    rows = [{'denomination_cents': 100, 'quantity': 1}]
                    self.db.execute('UPDATE cash_counts SET lines_json=?,actual_cents=100,variance_cents=100 WHERE id=?',
                                    (json.dumps(rows), draft['id']))
                elif damaged == 'book_and_variance':
                    self.db.execute('UPDATE cash_counts SET book_cents=actual_cents,variance_cents=0 WHERE id=?', (draft['id'],))
                else:
                    self.db.execute("UPDATE cash_counts SET evidence_json='{}' WHERE id=?", (draft['id'],))
                with self.assertRaises(accounting.ConflictError):
                    counts.confirm(self.db, draft['id'], {'revision': 1})
                self.assertEqual(self.db.execute('SELECT status FROM cash_counts WHERE id=?', (draft['id'],)).fetchone()[0], 'draft')
        self.assertEqual(self.journal_counts(), (0, 0))

    def test_later_dated_posts_do_not_change_original_day_but_backdated_posts_do(self):
        self.post_cash('200')
        doc = self.confirm()
        self.post_cash('30', '2026-10-17')
        unchanged = counts.detail(self.db, doc['id'])
        self.assertFalse(unchanged['book_changed'])
        self.assertEqual(unchanged['live_book_cents'], 20000)
        self.post_cash('40', '2026-10-15')
        changed = counts.detail(self.db, doc['id'])
        self.assertTrue(changed['book_changed'])
        self.assertEqual((changed['book_cents'], changed['live_book_cents']), (20000, 24000))
        self.assertEqual(changed['variance_cents'], 150)

    def test_unsealed_or_malformed_book_is_readable_but_not_confirmable(self):
        stamp = payables._now()
        journal = self.db.execute('''INSERT INTO journal_entries(code,source_type,source_id,entry_date,
            sealed,request_fingerprint,created_at) VALUES('UNSEALED-COUNT','broken',1,'2026-10-16',0,'broken',?)''', (stamp,)).lastrowid
        self.db.execute('INSERT INTO journal_lines(journal_id,account_id,debit_cents,credit_cents) VALUES(?,?,100,0)', (journal, self.cash))
        data = self.payload()
        plan = counts.preview(self.db, data)
        self.assertEqual(plan['book_cents'], 0)
        self.assertFalse(plan['can_confirm'])
        self.assertEqual(plan['exceptions'][0]['type'], 'unsealed_journal')
        draft = counts.save(self.db, data)
        with self.assertRaises(ValueError):
            counts.confirm(self.db, draft['id'], {'revision': 1})
        self.assertEqual(self.journal_counts(), (1, 1))

    def test_confirmed_header_is_immutable_and_void_is_append_only_nonposting(self):
        self.post_cash('200')
        doc = self.confirm()
        before = self.journal_counts()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE cash_counts SET book_cents=1 WHERE id=?", (doc['id'],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM cash_counts WHERE id=?', (doc['id'],))
        data = self.void_data(doc)
        voided = counts.void(self.db, doc['id'], data)
        self.assertEqual((voided['status'], voided['effective_status']), ('confirmed', 'voided'))
        self.assertEqual((voided['actual_cents'], voided['book_cents'], voided['variance_cents']),
                         (doc['actual_cents'], doc['book_cents'], doc['variance_cents']))
        self.assertRegex(voided['void']['code'], r'^CCV-\d{6}$')
        self.assertEqual(counts.void(self.db, doc['id'], data)['void']['id'], voided['void']['id'])
        with self.assertRaises(accounting.ConflictError):
            counts.void(self.db, doc['id'], {**data, 'reason': 'سبب مختلف'})
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM cash_count_voids WHERE count_id=?', (doc['id'],))
        self.assertEqual(self.journal_counts(), before)

    def test_closed_periods_guard_draft_mutation_and_allow_new_open_period_void(self):
        data = self.payload()
        draft = counts.save(self.db, data)
        doc = self.confirm()
        self.force_closed()
        for fn, args in ((counts.save, (self.db, self.payload())),
                         (counts.save, (self.db, {**data, 'revision': 1, 'business_date': '2026-11-01'}, draft['id'])),
                         (counts.save, (self.db, {'revision': 1, 'action': 'cancel'}, draft['id'])),
                         (counts.confirm, (self.db, draft['id'], {'revision': 1})),
                         (counts.void, (self.db, doc['id'], self.void_data(doc)))):
            with self.subTest(fn=fn.__name__), self.assertRaisesRegex(ValueError, 'مقفولة'):
                fn(*args)
        undo = self.void_data(doc, business_date='2026-11-01')
        self.assertEqual(counts.void(self.db, doc['id'], undo)['effective_status'], 'voided')
        self.force_closed('2026-11')
        self.assertEqual(counts.void(self.db, doc['id'], undo)['effective_status'], 'voided')

    def test_saved_identity_stays_readable_when_account_inactive_or_mapping_changes(self):
        doc = self.confirm()
        self.db.execute("UPDATE accounts SET active=0,name='اسم جديد' WHERE id=?", (self.cash,))
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الخزينة' AND role='cash_transfer_clearing'", (self.cash,))
        before = self.db.total_changes
        visible = counts.detail(self.db, doc['id'])
        self.assertEqual(visible['cash_account_name'], doc['cash_account_name'])
        self.assertFalse(visible['book_changed'])
        self.assertEqual(counts.list_counts(self.db)[0]['id'], doc['id'])
        self.assertEqual(self.db.total_changes, before)
        self.assertEqual(counts.confirm(self.db, doc['id'], {'revision': doc['revision']})['id'], doc['id'])
        with self.assertRaises(ValueError):
            counts.preview(self.db, self.payload())

    def test_reference_reuse_requires_original_void_date_without_historical_overlap(self):
        data = self.payload(reference='SAME-REFERENCE')
        doc = self.confirm(data)
        with self.assertRaises(accounting.ConflictError):
            counts.save(self.db, self.payload(reference='same-reference'))
        counts.void(self.db, doc['id'], self.void_data(doc))
        with self.assertRaises(accounting.ConflictError):
            counts.save(self.db, self.payload(reference='SAME-REFERENCE', business_date='2026-10-19'))
        new = counts.save(self.db, self.payload(reference='SAME-REFERENCE', business_date='2026-10-20'))
        self.assertNotEqual(new['id'], doc['id'])

    def test_list_is_complete_above_500_and_read_only(self):
        for i in range(507):
            counts.save(self.db, self.payload(reference='MANY-' + str(i)))
        before = self.db.total_changes
        self.assertEqual(len(counts.list_counts(self.db)), 507)
        self.assertEqual(self.db.total_changes, before)
        self.assertEqual(self.journal_counts(), (0, 0))

    def test_operations_do_not_commit_the_callers_transaction(self):
        self.db.execute('BEGIN')
        data = self.payload()
        doc = self.confirm(data)
        counts.void(self.db, doc['id'], self.void_data(doc))
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(counts.list_counts(self.db), [])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cash_count_voids').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
