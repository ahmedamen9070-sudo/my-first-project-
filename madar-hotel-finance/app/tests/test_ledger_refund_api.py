"""HTTP evidence for dated ledger reports and refund corrections on isolated data."""
import unittest
import uuid

import test_payables_api as base_api


class LedgerAndRefundAPI(unittest.TestCase):
    setUpClass = classmethod(base_api.PayablesAPI.setUpClass.__func__)
    tearDownClass = classmethod(base_api.PayablesAPI.tearDownClass.__func__)
    request = classmethod(base_api.PayablesAPI.request.__func__)
    fixture = base_api.PayablesAPI.fixture
    invoice = base_api.PayablesAPI.invoice
    payment = base_api.PayablesAPI.payment
    statement = base_api.PayablesAPI.statement

    def refund(self, supplier, source, amount='40', ds='2026-06-05', post=True):
        saved = self.request('supplier-refunds', 'POST', {
            'request_key': uuid.uuid4().hex, 'supplier_id': supplier['id'],
            'source_type': 'advance', 'source_id': source['id'], 'amount': amount,
            'business_date': ds, 'reference': uuid.uuid4().hex, 'reason': 'إعادة رصيد مورد',
            'method': 'cash', 'funding_account_id': self.cash,
            'details': {'received_by': 'أمين الخزينة'}})
        return self.request(f"supplier-refunds/{saved['id']}/post", 'POST',
                            {'revision': saved['revision']}) if post else saved

    def reversal_payload(self, refund, ds='2026-06-10'):
        return {'request_key': uuid.uuid4().hex, 'revision': refund['revision'],
                'event_revision': refund.get('adjustment_revision', 0), 'business_date': ds,
                'reference': uuid.uuid4().hex, 'reason': 'القبض سجل بالخطأ ولم يتم فعليًا'}

    def test_trial_balance_and_supplier_filtered_ledger_agree(self):
        supplier, product = self.fixture(terms=0)
        bill = self.invoice(supplier, product, '100', '2025-12-31')
        payment = self.payment(supplier, '30', '2026-01-05', mode='invoice', target=bill['id'])
        chart = self.request('accounting')
        ap = next(m['account_id'] for m in chart['mappings'] if m['role'] == 'payable_control')
        report = self.request('trial-balance?from_date=2026-01-01&to_date=2026-01-31')
        self.assertTrue(report['balanced'], report['exceptions'])
        self.assertEqual(report['totals']['movement_difference_cents'], 0)
        self.assertEqual(report['totals']['closing_difference_cents'], 0)
        self.assertEqual(report['totals']['debit_cents'], sum(r['debit_cents'] for r in report['rows']))
        ledger = self.request(f"account-ledger?account_id={ap}&supplier_id={supplier['id']}&from_date=2026-01-01&to_date=2026-01-31")
        self.assertEqual((ledger['opening_balance_cents'], ledger['debit_cents'], ledger['closing_balance_cents']), (-10000, 3000, -7000))
        self.assertEqual(ledger['rows'][0]['document_code'], payment['code'])
        journal = self.request('journals/' + str(ledger['rows'][0]['journal_id']))
        self.assertEqual(journal['debit_cents'], journal['credit_cents'])

    def test_report_routes_validate_inputs_and_do_not_write(self):
        before = self.request('audit')
        for route in ['trial-balance?from_date=2026-02-30',
                      'trial-balance?from_date=2026-02-01&to_date=2026-01-01',
                      'trial-balance?include_zero=true', 'account-ledger?account_id=0',
                      'account-ledger?account_id=missing',
                      f'account-ledger?account_id={self.cash}&supplier_id=999999']:
            self.request(route, expected=400)
        report = self.request('trial-balance?from_date=2026-01-01&to_date=2026-12-31&include_zero=1')
        self.assertGreaterEqual(len(report['rows']), 194)
        self.assertEqual(self.request('audit'), before)

    def test_refund_reversal_restores_source_from_its_date_and_preserves_original(self):
        supplier, _ = self.fixture(terms=0)
        advance = self.payment(supplier, '100', '2026-06-01', mode='manual', allow_advance=True)
        refund = self.refund(supplier, advance)
        payload = self.reversal_payload(refund)
        route = f"supplier-refunds/{refund['id']}/reverse"
        self.request(route, 'POST', payload, expected=403, csrf=False)
        reversed_doc = self.request(route, 'POST', payload)
        self.assertEqual(reversed_doc['effective_status'], 'reversed')
        self.assertEqual(reversed_doc['status'], 'posted')
        self.assertEqual(reversed_doc['journal'], refund['journal'])
        self.assertEqual(reversed_doc['amount_cents'], refund['amount_cents'])
        self.assertEqual(reversed_doc['reversal']['reference'], payload['reference'])
        self.assertEqual(self.request(route, 'POST', payload)['reversal']['id'], reversed_doc['reversal']['id'])
        self.request(route, 'POST', {**payload, 'reason': 'سبب آخر'}, expected=409)
        historical = self.request(f"supplier-refunds/{refund['id']}?as_of=2026-06-09")
        self.assertEqual(historical['effective_status'], 'posted')
        self.assertIsNone(historical['reversal'])
        self.assertEqual(self.statement(supplier, '2026-06-09')['totals']['advance_cents'], 6000)
        self.assertEqual(self.statement(supplier, '2026-06-10')['totals']['advance_cents'], 10000)
        for ds in ['2026-06-09', '2026-06-10']:
            self.assertTrue(self.request('supplier-reconciliation?as_of=' + ds)['matched'])
        ledger = self.request(f"account-ledger?account_id={self.cash}&supplier_id={supplier['id']}&from_date=2026-06-01&to_date=2026-06-30")
        row = next(r for r in ledger['rows'] if r['source_type'] == 'supplier_refund_reversal')
        self.assertEqual(row['document_code'], reversed_doc['reversal']['code'])
        self.assertEqual(row['credit_cents'], 4000)
        self.assertEqual(ledger['closing_balance_cents'], -10000)

    def test_restored_capacity_cannot_be_reused_before_reversal_date(self):
        supplier, _ = self.fixture(terms=0)
        advance = self.payment(supplier, '100', '2026-07-01', mode='manual', allow_advance=True)
        refund = self.refund(supplier, advance, '40', '2026-07-05')
        self.request(f"supplier-refunds/{refund['id']}/reverse", 'POST', self.reversal_payload(refund, '2026-07-10'))
        data = {'request_key': uuid.uuid4().hex, 'supplier_id': supplier['id'],
                'source_type': 'advance', 'source_id': advance['id'], 'amount': '70',
                'business_date': '2026-07-07', 'reference': uuid.uuid4().hex,
                'reason': 'اختبار توفر الرصيد بتاريخ المستند', 'method': 'cash',
                'funding_account_id': self.cash, 'details': {'received_by': 'أمين الخزينة'}}
        self.request('supplier-refunds', 'POST', data, expected=400)
        saved = self.request('supplier-refunds', 'POST', {**data, 'business_date': '2026-07-10'})
        self.request(f"supplier-refunds/{saved['id']}/post", 'POST', {'revision': saved['revision']})
        self.assertEqual(self.statement(supplier, '2026-07-10')['totals']['advance_cents'], 3000)

    def test_refund_correction_obeys_month_lock_and_retry_is_safe_after_close(self):
        supplier, _ = self.fixture(terms=0)
        advance = self.payment(supplier, '100', '2035-01-01', mode='manual', allow_advance=True)
        refund = self.refund(supplier, advance, '40', '2035-01-05')
        route = f"supplier-refunds/{refund['id']}/reverse"
        payload = self.reversal_payload(refund, '2035-01-10')
        preview = self.request('period-preview?month=2035-01')
        lock = lambda action, rev: self.request(f'accounting-periods/2035-01/{action}', 'POST', {
            'request_key': uuid.uuid4().hex, 'revision': rev, 'reference': uuid.uuid4().hex,
            'reason': 'مراجعة اختبار تصحيح الاسترداد'})
        self.assertTrue(preview['can_close'], preview['blockers'])
        lock('close', preview['period']['revision'])
        try:
            self.request(route, 'POST', payload, expected=400)
            self.assertTrue(self.request('trial-balance?from_date=2035-01-01&to_date=2035-01-31')['balanced'])
            current = self.request('accounting-periods?year=2035')['periods'][0]
            lock('reopen', current['revision'])
            posted = self.request(route, 'POST', payload)
            preview = self.request('period-preview?month=2035-01')
            lock('close', preview['period']['revision'])
            self.assertEqual(self.request(route, 'POST', payload)['reversal']['id'], posted['reversal']['id'])
        finally:
            current = self.request('accounting-periods?year=2035')['periods'][0]
            if current['status'] == 'closed':
                lock('reopen', current['revision'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
