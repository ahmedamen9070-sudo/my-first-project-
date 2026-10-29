"""Nonposting cash-count certificates through real HTTP transaction boundaries."""
import sqlite3
import unittest
import uuid

import test_payables_api as base


class CashCountsAPI(unittest.TestCase):
    request = classmethod(base.PayablesAPI.request.__func__)

    def setUp(self):
        base.PayablesAPI.setUpClass.__func__(self.__class__)
        config = self.request('treasury-config')
        self.cash = next(a['id'] for a in config['cash_accounts'] if a['code'] == '111002')
        self.contra = next(a['id'] for a in config['counter_accounts_by_kind']['receipt'] if a['code'].startswith('3'))
        self.expense = config['counter_accounts_by_kind']['payment'][0]['id']

    def tearDown(self):
        base.PayablesAPI.tearDownClass.__func__(self.__class__)

    def cash_movement(self, kind='receipt', amount='1000', ds='2026-01-01'):
        payload = {'request_key': uuid.uuid4().hex, 'kind': kind, 'business_date': ds,
                   'reference': uuid.uuid4().hex, 'amount': amount, 'cash_account_id': self.cash,
                   'counter_account_id': self.contra if kind == 'receipt' else self.expense,
                   'party_name': 'طرف مستند مؤيد في اختبار معزول', 'department_id': self.department}
        saved = self.request('treasury-documents', 'POST', payload)
        return self.request(f"treasury-documents/{saved['id']}/post", 'POST', {'revision': saved['revision']})

    def payload(self, ds='2026-01-10', **extra):
        return {'request_key': uuid.uuid4().hex, 'business_date': ds,
                'reference': uuid.uuid4().hex, 'cash_account_id': self.cash,
                'counted_by': 'أمين الخزينة', 'reviewed_by': 'مراجع الجرد', 'notes': 'عد نقدي فعلي للاختبار',
                'lines': [{'denomination_cents': 20000, 'quantity': 4},
                          {'denomination_cents': 2000, 'quantity': 4}], **extra}

    def financial_counts(self):
        with sqlite3.connect(self.tmp.name + '/api.sqlite') as db:
            return {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                    for table in ('journal_entries', 'journal_lines', 'treasury_documents',
                                  'supplier_payments', 'custody_documents')}

    def confirm(self, doc):
        return self.request(f"cash-counts/{doc['id']}/confirm", 'POST', {'revision': doc['revision']})

    def test_count_draft_confirm_void_never_creates_financial_journal(self):
        self.cash_movement()
        self.cash_movement('payment', '100', '2026-01-02')
        baseline = self.financial_counts()
        data = self.payload()
        preview = self.request('cash-count-preview', 'POST', data)
        self.assertEqual((preview['actual_cents'], preview['book_cents'], preview['variance_cents']), (88000, 90000, -2000))
        self.request('cash-counts', 'POST', data, expected=403, csrf=False)
        draft = self.request('cash-counts', 'POST', data)
        self.assertEqual(self.request('cash-counts', 'POST', data)['id'], draft['id'])
        confirmed = self.confirm(draft)
        self.assertEqual(confirmed['status'], 'confirmed')
        self.assertEqual((confirmed['actual_cents'], confirmed['book_cents'], confirmed['variance_cents']), (88000, 90000, -2000))
        self.assertFalse(confirmed['book_changed'])
        self.request(f"cash-counts/{draft['id']}", 'PUT', {**data, 'revision': confirmed['revision'], 'counted_by': 'شخص آخر'}, expected=400)
        void_data = {'request_key': uuid.uuid4().hex, 'revision': confirmed['revision'],
                     'event_revision': confirmed.get('adjustment_revision', 0), 'business_date': '2026-01-11',
                     'reference': uuid.uuid4().hex, 'reason': 'محضر جرد سجل بالخطأ ويستبدل بمحضر جديد'}
        voided = self.request(f"cash-counts/{draft['id']}/void", 'POST', void_data)
        self.assertEqual(voided['effective_status'], 'voided')
        self.assertEqual(voided['status'], 'confirmed')
        self.assertEqual(voided['variance_cents'], -2000)
        self.assertEqual(baseline, self.financial_counts())
        self.assertEqual(self.request(f"cash-counts/{draft['id']}/void", 'POST', void_data)['void']['id'], voided['void']['id'])

    def test_zero_net_book_changes_require_resaving_before_confirmation(self):
        self.cash_movement()
        self.cash_movement('payment', '100', '2026-01-02')
        data = self.payload()
        draft = self.request('cash-counts', 'POST', data)
        self.cash_movement('receipt', '10', '2026-01-04')
        self.cash_movement('payment', '10', '2026-01-04')
        changed = self.request(f"cash-counts/{draft['id']}")
        self.assertEqual(changed['live_book_cents'], changed['book_cents'])
        self.assertTrue(changed['book_changed'])
        self.request(f"cash-counts/{draft['id']}/confirm", 'POST', {'revision': draft['revision']}, expected=409)
        resaved = self.request(f"cash-counts/{draft['id']}", 'PUT', {**data, 'revision': draft['revision']})
        confirmed = self.confirm(resaved)
        self.assertFalse(confirmed['book_changed'])
        self.assertEqual(confirmed['book_cents'], 90000)

    def test_later_date_is_excluded_but_backdated_movement_has_separate_live_position(self):
        self.cash_movement()
        self.cash_movement('payment', '100', '2026-01-02')
        confirmed = self.confirm(self.request('cash-counts', 'POST', self.payload()))
        self.cash_movement('payment', '50', '2026-01-11')
        later = self.request(f"cash-counts/{confirmed['id']}")
        self.assertFalse(later['book_changed'])
        self.assertEqual(later['live_book_cents'], 90000)
        self.cash_movement('payment', '50', '2026-01-05')
        backdated = self.request(f"cash-counts/{confirmed['id']}")
        self.assertTrue(backdated['book_changed'])
        self.assertEqual((backdated['book_cents'], backdated['variance_cents']), (90000, -2000))
        self.assertEqual((backdated['live_book_cents'], backdated['live_variance_cents']), (85000, 3000))

    def test_count_drafts_block_month_close_and_confirmed_counts_have_no_journal_requirement(self):
        data = self.payload()
        saved = self.request('cash-counts', 'POST', data)
        review = self.request('period-preview?month=2026-01')
        self.assertEqual(review['draft_counts']['cash_count'], 1)
        self.assertFalse(review['can_close'])
        confirmed = self.confirm(saved)
        review = self.request('period-preview?month=2026-01')
        self.assertTrue(review['can_close'], review['blockers'])
        self.assertEqual(review['journal_totals']['count'], 0)
        self.assertEqual(review['cash_count_review']['confirmed_count'], 1)
        self.assertEqual(review['cash_count_review']['variance_count'], 1)
        self.assertTrue(any('محاضر جرد' in text for text in review['warnings']))
        event = {'request_key': uuid.uuid4().hex, 'revision': 1, 'reference': 'COUNT-MONTH-REVIEW', 'reason': 'مراجعة المستندات المسجلة'}
        self.request('accounting-periods/2026-01/close', 'POST', event)
        self.assertEqual(self.request('cash-counts', 'POST', data)['id'], confirmed['id'])
        self.assertEqual(self.confirm(confirmed)['id'], confirmed['id'])
        self.request('cash-counts', 'POST', self.payload(), expected=400)
        self.request(f"cash-counts/{saved['id']}/void", 'POST', {
            'request_key': uuid.uuid4().hex, 'revision': confirmed['revision'], 'event_revision': 0,
            'business_date': '2026-01-11', 'reference': 'CLOSED-VOID', 'reason': 'اختبار فترة مقفلة'}, expected=400)

    def test_validation_and_reads_do_not_change_financial_or_audit_state(self):
        for changes in ({'currency': 'USD'}, {'business_date': '2026-02-30'},
                        {'cash_account_id': self.bank}, {'counted_by': ''},
                        {'lines': [{'denomination_cents': 20000, 'quantity': True}]},
                        {'lines': [{'denomination_cents': 20000, 'quantity': 1.5}]},
                        {'lines': [{'denomination_cents': 20000, 'quantity': -1}]},
                        {'lines': [{'denomination_cents': 20000, 'quantity': 1}, {'denomination_cents': 20000, 'quantity': 2}]}):
            self.request('cash-count-preview', 'POST', self.payload(**changes), expected=400)
        draft = self.request('cash-counts', 'POST', self.payload())
        before = self.request('audit')
        baseline = self.financial_counts()
        self.request('cash-count-config')
        self.assertEqual(len(self.request('cash-counts')), 1)
        self.request(f"cash-counts/{draft['id']}")
        self.request('cash-count-preview', 'POST', self.payload())
        self.assertEqual(self.request('audit'), before)
        self.assertEqual(self.financial_counts(), baseline)


if __name__ == '__main__':
    unittest.main()
