"""HTTP contracts for explicit withholding, net funding and dated corrections."""
import unittest
import uuid

import test_payables_api as base_api
import test_tax_api as tax_api


class WithholdingAPI(unittest.TestCase):
    setUpClass = classmethod(base_api.PayablesAPI.setUpClass.__func__)
    tearDownClass = classmethod(base_api.PayablesAPI.tearDownClass.__func__)
    request = classmethod(base_api.PayablesAPI.request.__func__)
    fixture = base_api.PayablesAPI.fixture
    invoice = base_api.PayablesAPI.invoice
    payment = base_api.PayablesAPI.payment
    statement = base_api.PayablesAPI.statement
    configure = tax_api.TaxAndPeriodsAPI.configure
    code = tax_api.TaxAndPeriodsAPI.code
    bill = tax_api.TaxAndPeriodsAPI.bill

    def wh_code(self, rate='3', confirmed=True):
        chart = self.request('accounting')
        mapping = next(m for m in chart['mappings'] if m['role'] == 'withholding_payable')
        if mapping['status'] != 'confirmed':
            self.request(f"account-mappings/{mapping['id']}", 'PUT',
                         {'revision': mapping['revision'], 'status': 'confirmed'})
        self.wht_account = mapping['account_id']
        return self.request('withholding-codes', 'POST', {
            'name': 'خصم اختبار HTTP ' + uuid.uuid4().hex, 'rate': rate,
            'active': True, 'confirmed': confirmed})

    def payment_data(self, supplier, bill, code, ds, amount='57', method='cash'):
        details = {'received_by': 'مندوب المورد'}
        if method == 'cheque':
            details = {'bank_name': 'بنك الاختبار', 'cheque_number': uuid.uuid4().hex,
                       'cheque_book': 'دفتر اختبار', 'beneficiary': supplier['name'],
                       'cheque_date': ds, 'due_date': ds}
        return {'request_key': uuid.uuid4().hex, 'supplier_id': supplier['id'],
                'payment_date': ds, 'amount': amount, 'method': method,
                'funding_account_id': self.bank if method == 'cheque' else self.cash,
                'allocation_mode': 'invoice', 'target_invoice_id': bill['id'],
                'withholding_code_id': code['id'], 'details': details}

    def post_payment(self, payload):
        saved = self.request('supplier-payments', 'POST', payload)
        return self.request(f"supplier-payments/{saved['id']}/post", 'POST', {'revision': saved['revision']})

    def test_vat_partial_payments_settle_gross_and_disburse_only_net(self):
        supplier, product = self.fixture(terms=0)
        vat = self.code()
        bill, _ = self.bill(supplier, product, vat, ds='2026-03-01')
        code = self.wh_code()
        first_data = self.payment_data(supplier, bill, code, '2026-03-02')
        preview = self.request('payment-preview', 'POST', first_data)
        self.assertEqual((preview['allocated_cents'], preview['withholding_base_cents'],
                          preview['withholding_cents'], preview['net_cash_cents']), (5700, 5000, 150, 5550))
        first = self.post_payment(first_data)
        second = self.post_payment(self.payment_data(supplier, bill, code, '2026-03-03'))
        self.assertEqual(first['withholding']['supplier_name'], supplier['name'])
        self.assertTrue(first['withholding']['certificate_code'].endswith(first['code']))
        self.assertEqual(first['withholding']['lines'][0]['allocated_cents'], 5700)
        self.assertEqual(self.statement(supplier, '2026-03-03')['totals']['payable_balance_cents'], 0)
        self.assertEqual(sum(l['credit_cents'] for l in first['journal']['lines'] if l['account_id'] == self.cash), 5550)
        self.assertEqual(sum(l['credit_cents'] for l in second['journal']['lines'] if l['account_id'] == self.wht_account), 150)
        report = self.request(f"withholding-report?from_date=2026-01-01&to_date=2026-03-31&supplier_id={supplier['id']}")
        self.assertEqual((report['totals']['gross_cents'], report['totals']['base_cents'],
                          report['totals']['withholding_cents'], report['totals']['net_cash_cents']), (11400, 10000, 300, 11100))
        self.assertTrue(self.request('supplier-reconciliation?as_of=2026-03-03')['matched'])
        self.assertTrue(self.request('trial-balance?to_date=2026-03-31')['balanced'])

    def test_stale_draft_requires_resave_and_old_snapshot_and_retry_are_preserved(self):
        supplier, product = self.fixture(terms=0)
        bill = self.invoice(supplier, product, '100', '2026-04-01')
        code = self.wh_code('1')
        payload = self.payment_data(supplier, bill, code, '2026-04-02', '30')
        saved = self.request('supplier-payments', 'POST', payload)
        current = self.request(f"withholding-codes/{code['id']}", 'PUT', {'revision': code['revision'], 'rate': '2'})
        self.assertEqual(self.request(f"supplier-payments/{saved['id']}")['withholding_cents'], 30)
        self.assertEqual(self.request('supplier-payments', 'POST', payload)['withholding_cents'], 30)
        self.request(f"supplier-payments/{saved['id']}/post", 'POST', {'revision': saved['revision']}, expected=409)
        updated = self.request(f"supplier-payments/{saved['id']}", 'PUT', {**payload, 'revision': saved['revision']})
        posted = self.request(f"supplier-payments/{saved['id']}/post", 'POST', {'revision': updated['revision']})
        self.assertEqual((posted['withholding_cents'], posted['net_cash_cents']), (60, 2940))
        self.request(f"withholding-codes/{code['id']}", 'PUT', {'revision': current['revision'], 'rate': '7', 'active': False})
        same = self.request(f"supplier-payments/{saved['id']}/post", 'POST', {'revision': updated['revision']})
        self.assertEqual(same['withholding']['rate'], '2.00')
        self.assertEqual(self.request('supplier-payments', 'POST', payload)['id'], same['id'])
        self.request('supplier-payments', 'POST', {**payload, 'amount': '31'}, expected=409)

    def test_cheque_uses_net_on_issue_clearance_and_bounce_signed_report(self):
        supplier, product = self.fixture(terms=0)
        bill = self.invoice(supplier, product, '100', '2026-06-29')
        code = self.wh_code()
        cheque = self.post_payment(self.payment_data(supplier, bill, code, '2026-06-30', '100', 'cheque'))
        cleared = self.request(f"supplier-payments/{cheque['id']}/clear", 'POST', {
            'revision': cheque['revision'], 'clearance_date': '2026-07-01',
            'reference': 'CLEAR-' + uuid.uuid4().hex, 'bank_account_id': self.bank})
        self.assertEqual(sum(l['credit_cents'] for l in cleared['clearance_journal']['lines']), 9700)
        self.assertTrue(all(l['account_id'] != self.wht_account for l in cleared['clearance_journal']['lines']))
        self.request(f"supplier-payments/{cheque['id']}/cheque-event", 'POST', {
            'request_key': uuid.uuid4().hex, 'revision': cleared['revision'], 'event_revision': 0,
            'action': 'bounce', 'business_date': '2026-07-02',
            'reference': 'BOUNCE-' + uuid.uuid4().hex, 'reason': 'ارتداد شيك الاختبار'})
        q2 = self.request(f"withholding-report?from_date=2026-04-01&to_date=2026-06-30&supplier_id={supplier['id']}")
        q3 = self.request(f"withholding-report?from_date=2026-07-01&to_date=2026-09-30&supplier_id={supplier['id']}")
        self.assertEqual(q2['totals']['withholding_cents'], 300)
        self.assertEqual(q3['totals']['withholding_cents'], -300)
        self.assertEqual(q3['rows'][0]['original_code'], cheque['code'])
        self.assertEqual(q3['rows'][0]['net_cash_cents'], -9700)
        self.assertEqual(self.statement(supplier, '2026-07-02')['totals']['payable_balance_cents'], 10000)
        self.assertTrue(self.request('supplier-reconciliation?as_of=2026-07-02')['matched'])

    def test_explicit_codes_validation_and_no_code_legacy_path(self):
        supplier, product = self.fixture(terms=0)
        bill = self.invoice(supplier, product, '100', '2026-08-01')
        code = self.wh_code(confirmed=False)
        payload = self.payment_data(supplier, bill, code, '2026-08-02', '20')
        self.request('payment-preview', 'POST', payload, expected=400)
        self.request('withholding-codes', 'POST', {'name': uuid.uuid4().hex, 'rate': '3'}, expected=403, csrf=False)
        for rate in ['0', '-1', '100.01', '1.001', True]:
            self.request('withholding-codes', 'POST', {'name': uuid.uuid4().hex, 'rate': rate}, expected=400)
        legacy = self.payment(supplier, '20', '2026-08-02', mode='invoice', target=bill['id'])
        self.assertEqual((legacy['withholding_cents'], legacy['net_cash_cents']), (0, 2000))
        self.assertIsNone(legacy['withholding'])
        report_before = self.request('audit')
        self.request('withholding-report?from_date=2026-02-01&to_date=2026-01-01', expected=400)
        self.request('withholding-report?supplier_id=999999', expected=400)
        self.assertEqual(self.request('audit'), report_before)

    def test_period_lock_blocks_deduction_and_identical_post_retry_remains_safe(self):
        supplier, product = self.fixture(terms=0)
        bill = self.invoice(supplier, product, '100', '2036-01-01')
        code = self.wh_code()
        payload = self.payment_data(supplier, bill, code, '2036-01-02', '100')
        payment = self.post_payment(payload)
        preview = self.request('period-preview?month=2036-01')
        self.assertTrue(preview['can_close'], preview['blockers'])
        lock = lambda action, revision: self.request(f'accounting-periods/2036-01/{action}', 'POST', {
            'request_key': uuid.uuid4().hex, 'revision': revision,
            'reference': uuid.uuid4().hex, 'reason': 'فحص قفل خصم الموردين'})
        lock('close', preview['period']['revision'])
        try:
            self.request('supplier-payments', 'POST', {**payload, 'request_key': uuid.uuid4().hex}, expected=400)
            retry = self.request(f"supplier-payments/{payment['id']}/post", 'POST', {'revision': payment['revision']})
            self.assertEqual(retry['net_cash_cents'], 9700)
            self.assertEqual(self.request('supplier-payments', 'POST', payload)['id'], payment['id'])
            self.assertTrue(self.request('withholding-report?from_date=2036-01-01&to_date=2036-01-31')['rows'])
        finally:
            current = self.request('accounting-periods?year=2036')['periods'][0]
            lock('reopen', current['revision'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
