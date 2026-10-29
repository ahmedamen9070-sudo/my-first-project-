"""Cash/custody HTTP integration with AP, historical reports and month locks."""
import sqlite3
import unittest
import uuid

import test_payables_api as base
import test_withholding_api as wh


class TreasuryAPI(unittest.TestCase):
    request = classmethod(base.PayablesAPI.request.__func__)
    fixture = base.PayablesAPI.fixture
    invoice = base.PayablesAPI.invoice
    payment = base.PayablesAPI.payment
    wh_code = wh.WithholdingAPI.wh_code
    payment_data = wh.WithholdingAPI.payment_data
    post_payment = wh.WithholdingAPI.post_payment

    def setUp(self):
        base.PayablesAPI.setUpClass.__func__(self.__class__)
        chart = self.request('accounting')
        self.cash = next(a['id'] for a in chart['accounts'] if a['code'] == '111002')
        self.petty = next(a['id'] for a in chart['accounts'] if a['code'] == '111003')
        mapping = next(m for m in chart['mappings'] if m['module'] == 'الخزينة' and m['role'] == 'operating_custodies')
        self.request(f"account-mappings/{mapping['id']}", 'PUT', {'revision': mapping['revision'], 'status': 'confirmed'})
        config = self.request('treasury-config')
        self.contra = next(a['id'] for a in config['counter_accounts_by_kind']['receipt'] if a['code'].startswith('3'))
        self.direct_expense = config['counter_accounts_by_kind']['payment'][0]['id']
        self.person = self.request('custodians', 'POST', {'name': 'مسؤول عهدة اختبار', 'phone': 'TEST'})

    def tearDown(self):
        base.PayablesAPI.tearDownClass.__func__(self.__class__)

    def treasury_data(self, kind='receipt', amount='1000', ds='2026-01-01'):
        return {'request_key': uuid.uuid4().hex, 'kind': kind, 'business_date': ds,
                'reference': uuid.uuid4().hex, 'amount': amount, 'cash_account_id': self.cash,
                'counter_account_id': None if kind == 'transfer' else self.contra if kind == 'receipt' else self.direct_expense,
                'destination_cash_account_id': self.petty if kind == 'transfer' else None,
                'party_name': 'طرف مستند مؤيد في الاختبار', 'department_id': self.department, 'notes': ''}

    def post(self, route, data):
        saved = self.request(route, 'POST', data)
        return self.request(f"{route}/{saved['id']}/post", 'POST', {'revision': saved['revision']})

    def custody_data(self, kind='issue', amount='150', ds='2026-01-04', issue=None):
        return {'request_key': uuid.uuid4().hex, 'kind': kind, 'business_date': ds,
                'reference': uuid.uuid4().hex, 'amount': amount, 'cash_account_id': self.cash,
                'custodian_id': self.person['id'], 'source_issue_id': issue['id'] if issue else None,
                'purpose': 'شراء مصروف مباشر بمستند مؤيد', 'notes': '',
                'lines': [{'expense_account_id': self.direct_expense, 'department_id': self.department,
                           'reference': 'مستند صرف فعلي للاختبار', 'amount': amount}] if kind == 'settlement' else []}

    def book(self, ds='2026-01-31'):
        return self.request(f'cash-book?account_id={self.cash}&from_date=2026-01-01&to_date={ds}')

    def correction(self, doc, ds='2026-02-02'):
        return {'request_key': uuid.uuid4().hex, 'revision': doc['revision'],
                'event_revision': doc.get('adjustment_revision', 0), 'business_date': ds,
                'reference': uuid.uuid4().hex, 'reason': 'تصحيح مستند سجل بالخطأ'}

    def test_funding_expense_transfer_custody_settlement_and_return(self):
        self.post('treasury-documents', self.treasury_data())
        self.post('treasury-documents', self.treasury_data('payment', '100', '2026-01-02'))
        self.post('treasury-documents', self.treasury_data('transfer', '200', '2026-01-03'))
        issue = self.post('custody-documents', self.custody_data())
        self.post('custody-documents', self.custody_data('settlement', '60', '2026-01-05', issue))
        self.post('custody-documents', self.custody_data('return', '90', '2026-01-06', issue))
        self.assertEqual(self.book()['closing_balance_cents'], 64000)
        position = self.request('treasury-position?as_of=2026-01-31')
        self.assertEqual(position['totals']['balance_cents'], 84000)
        report = self.request('custody-balances?as_of=2026-01-31')
        self.assertTrue(report['matched'], report['exceptions'])
        self.assertEqual(report['totals']['remaining_cents'], 0)
        self.assertEqual(report['totals']['settled_cents'], 6000)
        self.assertTrue(self.request('trial-balance?to_date=2026-01-31')['balanced'])
        self.assertTrue(self.request('supplier-reconciliation?as_of=2026-01-31')['matched'])

    def test_existing_supplier_cash_is_net_and_appears_exactly_once(self):
        self.post('treasury-documents', self.treasury_data())
        supplier, product = self.fixture(terms=0)
        bill = self.invoice(supplier, product, '100', '2026-01-02')
        code = self.wh_code('3')
        payment = self.post_payment(self.payment_data(supplier, bill, code, '2026-01-03', '100'))
        self.assertEqual(payment['net_cash_cents'], 9700)
        report = self.book()
        rows = [r for r in report['rows'] if r['source_type'] == 'supplier_payment']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['credit_cents'], 9700)
        self.assertEqual(report['closing_balance_cents'], 90300)
        self.assertEqual(len(self.request('treasury-documents')), 1)
        self.assertTrue(self.request('supplier-reconciliation?as_of=2026-01-31')['matched'])

    def test_drafts_idempotency_csrf_and_locked_dates(self):
        payload = self.treasury_data()
        self.request('treasury-documents', 'POST', payload, expected=403, csrf=False)
        saved = self.request('treasury-documents', 'POST', payload)
        self.assertEqual(self.request('treasury-documents', 'POST', payload)['id'], saved['id'])
        self.assertEqual(self.book()['closing_balance_cents'], 0)
        preview = self.request('period-preview?month=2026-01')
        self.assertEqual(preview['draft_counts']['treasury'], 1)
        self.assertFalse(preview['can_close'])
        posted = self.request(f"treasury-documents/{saved['id']}/post", 'POST', {'revision': saved['revision']})
        self.request(f"treasury-documents/{saved['id']}", 'PUT', {**payload, 'revision': posted['revision'], 'amount': '1'}, expected=400)
        event = {'request_key': uuid.uuid4().hex, 'revision': 1, 'reference': 'MONTH-TEST', 'reason': 'مراجعة الحركات المسجلة'}
        self.request('accounting-periods/2026-01/close', 'POST', event)
        self.assertEqual(self.request('treasury-documents', 'POST', payload)['id'], posted['id'])
        self.assertEqual(self.request(f"treasury-documents/{posted['id']}/post", 'POST', {'revision': posted['revision']})['id'], posted['id'])
        self.request('treasury-documents', 'POST', self.treasury_data('payment', '1'), expected=400)
        self.request('custody-documents', 'POST', self.custody_data(), expected=400)
        corrected = self.request(f"treasury-documents/{posted['id']}/reverse", 'POST', self.correction(posted))
        self.assertEqual(corrected['effective_status'], 'reversed')
        self.assertEqual(self.book()['closing_balance_cents'], 100000)
        self.assertEqual(self.book('2026-02-02')['closing_balance_cents'], 0)

    def test_custody_drafts_and_offsets_cannot_bypass_period_review(self):
        issue = self.post('custody-documents', self.custody_data())
        data = self.custody_data('return', '10', '2026-01-05', issue)
        draft = self.request('custody-documents', 'POST', data)
        report = self.request('period-preview?month=2026-01')
        self.assertEqual(report['draft_counts']['custody'], 1)
        self.assertFalse(report['can_close'])
        self.assertTrue(report['custody_reconciliation']['matched'])
        self.request(f"custody-documents/{draft['id']}", 'PUT', {'action': 'cancel', 'revision': draft['revision']})
        self.assertTrue(self.request('period-preview?month=2026-01')['can_close'])
        # Two balanced imported movements can cancel financially and still lack
        # custody source documents. Month close must retain that diagnostic.
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import payables
        with sqlite3.connect(self.tmp.name + '/api.sqlite') as db:
            db.row_factory = sqlite3.Row
            control = issue['custody_account_id']
            for source_id, dr, cr in ((1, control, self.cash), (2, self.cash, control)):
                payables.create_journal(db, 'isolated_untracked_import', source_id, '2026-01-07', [
                    {'account_id': dr, 'debit_cents': 100},
                    {'account_id': cr, 'credit_cents': 100}])
        reviewed = self.request('period-preview?month=2026-01')
        self.assertFalse(reviewed['can_close'])
        self.assertFalse(reviewed['custody_reconciliation']['matched'])
        self.assertEqual(reviewed['custody_reconciliation']['totals']['difference_cents'], 0)
        self.assertIn('custody_reconciliation', {b['code'] for b in reviewed['blockers']})

    def test_dated_custody_corrections_restore_capacity_without_rewriting(self):
        issue = self.post('custody-documents', self.custody_data())
        settlement = self.post('custody-documents', self.custody_data('settlement', '60', '2026-01-05', issue))
        self.assertEqual(self.request('custody-balances?as_of=2026-01-05')['totals']['remaining_cents'], 9000)
        self.request(f"custody-documents/{issue['id']}/reverse", 'POST', self.correction(issue), expected=400)
        fixed = self.request(f"custody-documents/{settlement['id']}/reverse", 'POST', self.correction(settlement))
        self.assertEqual(fixed['effective_status'], 'reversed')
        self.assertEqual(fixed['journal'], settlement['journal'])
        self.assertEqual(self.request('custody-balances?as_of=2026-01-05')['totals']['remaining_cents'], 9000)
        self.assertEqual(self.request('custody-balances?as_of=2026-02-02')['totals']['remaining_cents'], 15000)

    def test_report_validation_and_reads_do_not_write(self):
        before = self.request('audit')
        for route in ('cash-book?account_id=0', f'cash-book?account_id={self.bank}',
                      'cash-book?from_date=2026-02-30', 'cash-book?from_date=2026-02-01&to_date=2026-01-01',
                      'treasury-position?as_of=20260101', 'custody-balances?as_of=2026-02-30'):
            self.request(route, expected=400)
        for route in ('treasury-config', 'custody-config', 'custodians', 'treasury-documents',
                      'custody-documents', 'cash-book', 'treasury-position', 'custody-balances'):
            self.request(route)
        self.assertEqual(self.request('audit'), before)


if __name__ == '__main__':
    unittest.main()
