"""Dated custody capacity, immutable journals and independently matched positions."""
import json
import sqlite3
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

import test_supplier_payments as fixtures
import accounting
import accounting_periods
import custodies
import payables
import treasury


class Custodies(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SupplierPayments('runTest')
        self.f.setUp()
        self.db = self.f.db
        accounting_periods.initialize(self.db)
        custodies.initialize(self.db)
        treasury.initialize(self.db)
        self.person = custodies.save_custodian(self.db, {'name': 'أمين عهدة اختبار', 'phone': '01000000000'})
        self.cash = self.f.cash
        self.expense = custodies.config(self.db)['expense_accounts'][0]['id']
        self.control = self.f.account('115003')
        self.serial = 0
        self.db.commit()

    def tearDown(self):
        self.f.tearDown()

    def key(self):
        self.serial += 1
        return 'CUSTODY-TEST-' + str(self.serial)

    def issue_data(self, amount='150', day='2026-10-01', **extra):
        return {'request_key': self.key(), 'kind': 'issue', 'business_date': day,
                'reference': self.key(), 'custodian_id': self.person['id'],
                'cash_account_id': self.cash, 'amount': amount, 'purpose': 'مصروف تشغيل مثبت بالمستند', **extra}

    def settlement_data(self, source, amount='60', day='2026-10-02', **extra):
        return {'request_key': self.key(), 'kind': 'settlement', 'business_date': day,
                'reference': self.key(), 'source_issue_id': source['id'],
                'lines': [{'expense_account_id': self.expense, 'department_id': 1,
                           'reference': self.key(), 'amount': amount}], **extra}

    def return_data(self, source, amount='90', day='2026-10-03', **extra):
        return {'request_key': self.key(), 'kind': 'return', 'business_date': day,
                'reference': self.key(), 'source_issue_id': source['id'],
                'cash_account_id': self.cash, 'amount': amount, **extra}

    def post(self, data):
        doc = custodies.save(self.db, data)
        return custodies.approve(self.db, doc['id'], {'revision': doc['revision']})

    def reverse_data(self, document, day='2026-10-04', **extra):
        return {'request_key': self.key(), 'revision': document['revision'], 'event_revision': 0,
                'business_date': day, 'reference': self.key(), 'reason': 'تصحيح مستند اختبار', **extra}

    def test_issue_settlement_and_return_journals_reconcile_without_double_cash(self):
        source = self.post(self.issue_data())
        settled = self.post(self.settlement_data(source))
        returned = self.post(self.return_data(source))
        self.assertEqual([(r['account_id'], r['debit_cents'], r['credit_cents']) for r in source['journal']['lines']],
                         [(self.control, 15000, 0), (self.cash, 0, 15000)])
        self.assertEqual(settled['journal']['lines'][0]['department_id'], 1)
        self.assertTrue(all(r['supplier_id'] is None for r in settled['journal']['lines']))
        self.assertEqual(returned['journal']['debit_cents'], 9000)
        report = custodies.balance_report(self.db, '2026-10-03')
        self.assertTrue(report['matched'], report['exceptions'])
        self.assertEqual(report['totals'], {'issued_cents': 15000, 'settled_cents': 6000,
                         'returned_cents': 9000, 'remaining_cents': 0, 'ledger_cents': 0, 'difference_cents': 0})
        self.assertEqual(sum(r[0] for r in self.db.execute('SELECT debit_cents-credit_cents FROM journal_lines WHERE account_id=?', (self.cash,))), -6000)

    def test_drafts_do_not_reserve_and_revalidation_prevents_overuse(self):
        source = self.post(self.issue_data('100'))
        first = custodies.save(self.db, self.settlement_data(source, '80'))
        second = custodies.save(self.db, self.return_data(source, '80'))
        self.assertEqual(custodies.balance_report(self.db, '2026-10-03')['totals']['remaining_cents'], 10000)
        custodies.approve(self.db, first['id'], {'revision': first['revision']})
        with self.assertRaisesRegex(ValueError, 'المتاح'):
            custodies.approve(self.db, second['id'], {'revision': second['revision']})
        self.assertEqual(custodies.detail(self.db, second['id'])['status'], 'draft')

    def test_capacity_checks_intermediate_future_consumption_even_when_reversed_later(self):
        source = self.post(self.issue_data('100'))
        future = self.post(self.settlement_data(source, '80', '2026-10-20'))
        custodies.reverse(self.db, future['id'], self.reverse_data(future, '2026-10-25'))
        self.assertEqual(custodies.capacity(self.db, source['id'], '2026-10-10'), 2000)
        with self.assertRaisesRegex(ValueError, 'لاحقة'):
            self.post(self.return_data(source, '30', '2026-10-10'))
        self.post(self.return_data(source, '20', '2026-10-10'))
        report = custodies.balance_report(self.db, '2026-10-20')
        self.assertTrue(report['matched'], report['exceptions'])
        self.assertEqual(report['totals']['remaining_cents'], 0)
        self.assertEqual(custodies.balance_report(self.db, '2026-10-25')['totals']['remaining_cents'], 8000)

    def test_dated_corrections_restore_remaining_only_from_reversal_date(self):
        source = self.post(self.issue_data('100'))
        settled = self.post(self.settlement_data(source, '60'))
        returned = self.post(self.return_data(source, '40'))
        custodies.reverse(self.db, returned['id'], self.reverse_data(returned, '2026-10-05'))
        self.assertEqual(custodies.balance_report(self.db, '2026-10-04')['totals']['remaining_cents'], 0)
        self.assertEqual(custodies.balance_report(self.db, '2026-10-05')['totals']['remaining_cents'], 4000)
        with self.assertRaisesRegex(ValueError, 'أولًا'):
            custodies.reverse(self.db, source['id'], self.reverse_data(source, '2026-10-06'))
        custodies.reverse(self.db, settled['id'], self.reverse_data(settled, '2026-10-07'))
        with self.assertRaisesRegex(ValueError, 'التابعة'):
            custodies.reverse(self.db, source['id'], self.reverse_data(source, '2026-10-06'))
        custodies.reverse(self.db, source['id'], self.reverse_data(source, '2026-10-07'))
        self.assertTrue(custodies.balance_report(self.db, '2026-10-07')['matched'])
        self.assertEqual(custodies.balance_report(self.db, '2026-10-07')['totals']['remaining_cents'], 0)

    def test_source_cannot_be_used_after_future_reversal(self):
        source = self.post(self.issue_data('100'))
        custodies.reverse(self.db, source['id'], self.reverse_data(source, '2026-10-20'))
        with self.assertRaisesRegex(ValueError, 'المتاح'):
            self.post(self.return_data(source, '1', '2026-10-10'))

    def test_posted_docs_lines_and_corrections_are_immutable(self):
        source = self.post(self.issue_data())
        settled = self.post(self.settlement_data(source))
        custodies.reverse(self.db, settled['id'], self.reverse_data(settled))
        for sql, values in (
            ('UPDATE custody_documents SET amount_cents=1 WHERE id=?', (source['id'],)),
            ('DELETE FROM custody_documents WHERE id=?', (source['id'],)),
            ('UPDATE custody_lines SET amount_cents=1 WHERE document_id=?', (settled['id'],)),
            ('DELETE FROM custody_lines WHERE document_id=?', (settled['id'],)),
            ('UPDATE custody_reversals SET reason=? WHERE document_id=?', ('تغيير', settled['id'])),
        ):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql, values)

    def test_retry_create_post_and_reverse_after_lock_never_duplicate(self):
        data = self.issue_data('100')
        source = self.post(data)
        reversal_data = self.reverse_data(source)
        reversed_doc = custodies.reverse(self.db, source['id'], reversal_data)
        self.db.execute("INSERT INTO accounting_periods(month,status,updated_at) VALUES('2026-10','closed','test')")
        before = self.db.total_changes
        self.assertEqual(custodies.save(self.db, data)['id'], source['id'])
        self.assertEqual(custodies.approve(self.db, source['id'], {'revision': 1})['id'], source['id'])
        self.assertEqual(custodies.reverse(self.db, source['id'], reversal_data)['reversal']['id'], reversed_doc['reversal']['id'])
        self.assertEqual(before, self.db.total_changes)
        with self.assertRaises(accounting.ConflictError):
            custodies.save(self.db, {**data, 'amount': '101'})

    def test_draft_old_and_new_period_lock_cancel_and_post(self):
        data = self.issue_data()
        draft = custodies.save(self.db, data)
        self.db.execute("INSERT INTO accounting_periods(month,status,updated_at) VALUES('2026-11','closed','test')")
        for action in ({'action': 'cancel'}, {**data, 'business_date': '2026-12-01'}):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'مقفولة'):
                custodies.save(self.db, {**action, 'revision': draft['revision']}, draft['id'])
        with self.assertRaisesRegex(ValueError, 'مقفولة'):
            custodies.approve(self.db, draft['id'], {'revision': draft['revision']})
        with self.assertRaisesRegex(ValueError, 'مقفولة'):
            custodies.save(self.db, self.issue_data('10', '2026-10-31'))

    def test_explicit_mapping_and_original_accounts_survive_mapping_change(self):
        self.db.execute("UPDATE account_mappings SET status='proposed' WHERE module='الخزينة' AND role='operating_custodies'")
        with self.assertRaisesRegex(ValueError, 'تأكيد'):
            custodies.save(self.db, self.issue_data())
        self.db.execute("UPDATE account_mappings SET status='confirmed' WHERE module='الخزينة' AND role='operating_custodies'")
        source = self.post(self.issue_data())
        other = self.f.account('115001')
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الخزينة' AND role='operating_custodies'", (other,))
        returned = self.post(self.return_data(source))
        self.assertEqual(returned['custody_account_id'], self.control)
        report = custodies.balance_report(self.db, '2026-10-03')
        self.assertTrue(report['matched'], report['exceptions'])

    def test_stale_routing_requires_resave_and_overlap_controls_rejected(self):
        data = self.issue_data()
        draft = custodies.save(self.db, data)
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الخزينة' AND role='operating_custodies'", (self.f.account('115001'),))
        with self.assertRaisesRegex(accounting.ConflictError, 'حفظ'):
            custodies.approve(self.db, draft['id'], {'revision': draft['revision']})
        saved = custodies.save(self.db, {**data, 'revision': draft['revision']}, draft['id'])
        self.assertEqual(custodies.approve(self.db, saved['id'], {'revision': saved['revision']})['custody_account_id'], self.f.account('115001'))
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الخزينة' AND role='operating_custodies'", (self.f.advance,))
        with self.assertRaisesRegex(ValueError, 'مستقل'):
            custodies.save(self.db, self.issue_data())

    def test_rejects_mapped_expense_cash_clearing_currency_and_invalid_amounts(self):
        source = self.post(self.issue_data())
        invalid = [self.issue_data('0'), self.issue_data('1.001'), self.issue_data('NaN'),
                   self.issue_data(True), self.issue_data(currency='USD'), self.issue_data(business_date='20261001'),
                   self.issue_data(cash_account_id=self.f.account('111005')),
                   self.issue_data(cash_account_id=self.f.bank),
                   self.settlement_data(source, lines=[{'expense_account_id':self.f.expense,'department_id':1,'reference':'OTHER','amount':'1'}])]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                custodies.preview(self.db, payload)
        valid = self.post(self.issue_data('1', cash_account_id=self.f.account('111004')))
        self.assertEqual(valid['cash_account_code'], '111004')

    def test_inactive_person_only_prevents_new_issue_not_return_or_settlement(self):
        source = self.post(self.issue_data())
        updated = custodies.save_custodian(self.db, {**self.person,'name':'اسم صاحب العهدة بعد التعديل','active':False}, self.person['id'])
        self.assertFalse(updated['active'])
        with self.assertRaisesRegex(ValueError, 'موقوف'):
            custodies.save(self.db, self.issue_data())
        self.assertEqual(self.post(self.return_data(source))['amount_cents'], 9000)
        self.assertEqual(custodies.detail(self.db, source['id'])['custodian_name'], 'أمين عهدة اختبار')
        self.assertTrue(custodies.balance_report(self.db, '2026-10-03')['matched'])

    def test_read_only_empty_and_populated_reports_and_timezone_fallback(self):
        before = self.db.total_changes
        self.assertEqual(custodies.balance_report(self.db, '2026-10-01')['issues'], [])
        self.assertTrue(custodies.balance_report(self.db, '2026-10-01')['matched'])
        self.assertEqual(before, self.db.total_changes)
        source = self.post(self.issue_data())
        before = self.db.total_changes
        custodies.balance_report(self.db, '2026-10-03')
        custodies.detail(self.db, source['id'])
        custodies.list_documents(self.db)
        custodies.config(self.db)
        with patch.object(custodies, 'ZoneInfo', side_effect=ZoneInfoNotFoundError):
            self.assertRegex(custodies.balance_report(self.db)['as_of'], r'^\d{4}-\d{2}-\d{2}$')
        self.assertEqual(before, self.db.total_changes)

    def test_offset_unknown_journals_never_report_matched_even_with_zero_difference(self):
        self.post(self.issue_data())
        for source_id, lines in ((1,[{'account_id':self.control,'debit_cents':100},{'account_id':self.cash,'credit_cents':100}]),
                                 (2,[{'account_id':self.cash,'debit_cents':100},{'account_id':self.control,'credit_cents':100}])):
            payables.create_journal(self.db, 'untracked_import', source_id, '2026-10-02', lines)
        report = custodies.balance_report(self.db, '2026-10-03')
        self.assertEqual(report['totals']['difference_cents'], 0)
        self.assertFalse(report['matched'])
        self.assertEqual(sum(e['type']=='untracked_custody_journal' for e in report['exceptions']),2)
        self.assertFalse(next(r for r in report['accounts'] if r['account_id']==self.control)['matched'])

    def test_reconciliation_detects_wrong_expense_dimensions_and_unsealed_journal(self):
        source = self.post(self.issue_data())
        settled = self.post(self.settlement_data(source))
        # Isolated imported corruption bypasses immutable triggers deliberately.
        for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN('journal_lines','journal_entries')").fetchall():
            self.db.execute('DROP TRIGGER ' + row['name'])
        self.db.execute('UPDATE journal_lines SET department_id=NULL WHERE journal_id=? AND debit_cents>0', (settled['journal_id'],))
        report = custodies.balance_report(self.db, '2026-10-03')
        self.assertEqual(report['totals']['difference_cents'], 0)
        self.assertFalse(report['matched'])
        self.assertIn('custody_journal_lines_mismatch', {e['type'] for e in report['exceptions']})
        self.db.execute('UPDATE journal_entries SET sealed=0 WHERE id=?', (source['journal_id'],))
        self.assertIn('custody_source_journal_mismatch', {e['type'] for e in custodies.balance_report(self.db,'2026-10-03')['exceptions']})


if __name__ == '__main__':
    unittest.main()
