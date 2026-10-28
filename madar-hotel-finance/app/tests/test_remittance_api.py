"""Local tax-remittance HTTP records, dated credits and transaction boundaries."""
import unittest
import uuid

import test_payables_api as base_api
import test_withholding_api as wh_api


class RemittanceAPI(unittest.TestCase):
    request = classmethod(base_api.PayablesAPI.request.__func__)
    fixture = base_api.PayablesAPI.fixture
    invoice = base_api.PayablesAPI.invoice
    statement = base_api.PayablesAPI.statement
    wh_code = wh_api.WithholdingAPI.wh_code
    payment_data = wh_api.WithholdingAPI.payment_data
    post_payment = wh_api.WithholdingAPI.post_payment

    def setUp(self):
        # Every case has its own HTTP service/database: future government-credit
        # capacity must not inherit another test's later correction.
        base_api.PayablesAPI.setUpClass.__func__(self.__class__)

    def tearDown(self):
        base_api.PayablesAPI.tearDownClass.__func__(self.__class__)

    def source(self, amount='100', ds='2026-01-02'):
        supplier, product = self.fixture(terms=0)
        bill = self.invoice(supplier, product, amount, ds)
        code = self.wh_code('3')
        payment = self.post_payment(self.payment_data(supplier, bill, code, ds, amount))
        return supplier, payment

    def data(self, source, amount='1', ds='2026-04-01', method='cash', **extra):
        return {'request_key': uuid.uuid4().hex, 'business_date': ds,
                'period_from': '2026-01-01', 'period_to': '2026-03-31',
                'authority_name': 'جهة ضريبية في بيانات اختبار معزولة',
                'reference': uuid.uuid4().hex, 'method': method,
                'funding_account_id': self.cash if method == 'cash' else self.bank,
                'details': {'received_by': 'مستلم الجهة', 'receipt_reference': 'RCPT-TEST'}
                    if method == 'cash' else {'bank_name': 'بنك الاختبار',
                    'transaction_reference': uuid.uuid4().hex, 'value_date': ds},
                'allocations': [{'payment_id': source['id'], 'amount': amount}], **extra}

    def remit(self, data):
        doc = self.request('withholding-remittances', 'POST', data)
        return self.request(f"withholding-remittances/{doc['id']}/post", 'POST', {'revision': doc['revision']})

    def reverse(self, doc, ds='2026-04-03'):
        data = {'request_key': uuid.uuid4().hex, 'revision': doc['revision'],
                'event_revision': doc['adjustment_revision'], 'business_date': ds,
                'reference': uuid.uuid4().hex, 'reason': 'تصحيح مستند سجل بالخطأ'}
        return self.request(f"withholding-remittances/{doc['id']}/reverse", 'POST', data), data

    def balances(self, ds='2026-12-31', **filters):
        return self.request('withholding-balances?as_of=' + ds + ''.join('&' + k + '=' + v for k, v in filters.items()))

    def test_partial_cash_and_bank_remittances_clear_tax_without_changing_ap(self):
        supplier, source = self.source()
        payload = self.data(source)
        preview = self.request('withholding-remittance-preview', 'POST', payload)
        self.assertEqual(preview['amount_cents'], 100)
        self.assertTrue(preview['can_approve'])
        draft = self.request('withholding-remittances', 'POST', payload)
        self.assertEqual(self.balances('2026-04-01')['all_totals']['remitted_cents'], 0)
        first = self.request(f"withholding-remittances/{draft['id']}/post", 'POST', {'revision': draft['revision']})
        second = self.remit(self.data(source, '2', '2026-04-02', 'bank_transfer'))
        self.assertRegex(first['code'], r'^WTR-')
        self.assertEqual(first['journal']['credit_cents'], 100)
        self.assertEqual(second['journal']['credit_cents'], 200)
        report = self.balances('2026-04-02')
        self.assertTrue(report['matched'], report['exceptions'])
        self.assertEqual((report['all_totals']['withheld_cents'], report['all_totals']['remitted_cents'],
                          report['all_totals']['remaining_cents']), (300, 300, 0))
        self.assertEqual(self.statement(supplier, '2026-04-02')['totals']['payable_balance_cents'], 0)
        self.assertTrue(self.request('supplier-reconciliation?as_of=2026-04-02')['matched'])
        ledger = self.request(f"account-ledger?account_id={self.wht_account}&from_date=2026-01-01&to_date=2026-04-02")
        self.assertEqual(ledger['closing_balance_cents'], 0)
        self.assertEqual({r['document_code'] for r in ledger['rows'] if r['source_type'] == 'withholding_remittance'}, {first['code'], second['code']})
        self.assertEqual(len(self.request('withholding-remittances')), 2)
        self.assertTrue(self.request('trial-balance?to_date=2026-04-02')['balanced'])

    def test_source_reversal_retains_overpaid_credit_and_caps_other_period_net_due(self):
        supplier, source = self.source()
        original = self.remit(self.data(source, '3'))
        self.request(f"supplier-payments/{source['id']}/reverse", 'POST', {
            'request_key': uuid.uuid4().hex, 'revision': source['revision'], 'event_revision': 0,
            'business_date': '2026-04-03', 'reference': uuid.uuid4().hex, 'reason': 'عكس سند مورد بعد التوريد'})
        report = self.balances('2026-04-03')
        own = next(r for r in report['sources'] if r['payment_id'] == source['id'])
        self.assertEqual((own['withheld_cents'], own['remitted_cents'], own['remaining_cents']), (0, 300, -300))
        self.assertEqual(report['all_totals']['account_credit_cents'], 300)
        self.assertEqual(self.request(f"withholding-remittances/{original['id']}")['effective_status'], 'posted')
        _, future = self.source('200', '2026-04-04')
        data = self.data(future, '4', '2026-04-05', period_from='2026-04-01', period_to='2026-04-04')
        self.request('withholding-remittance-preview', 'POST', data, expected=400)
        self.remit({**data, 'allocations': [{'payment_id': future['id'], 'amount': '3'}]})
        selected = self.balances('2026-04-05', period_from='2026-04-01', period_to='2026-04-04')
        self.assertEqual(selected['totals']['remaining_cents'], 300)
        self.assertEqual(selected['all_totals']['remaining_cents'], 0)
        self.assertEqual(selected['all_totals']['net_due_cents'], 0)
        self.assertTrue(selected['matched'])
        self.assertTrue(self.request('supplier-reconciliation?as_of=2026-04-05')['matched'])

    def test_correction_is_dated_retry_safe_and_receipt_can_be_reused_after_reversal(self):
        _, source = self.source()
        payload = self.data(source)
        original = self.remit(payload)
        self.request('withholding-remittances', 'POST', {**payload, 'request_key': uuid.uuid4().hex}, expected=400)
        reversed_doc, reverse_data = self.reverse(original)
        route = f"withholding-remittances/{original['id']}/reverse"
        self.assertEqual(self.request(route, 'POST', reverse_data)['reversal']['id'], reversed_doc['reversal']['id'])
        self.request(route, 'POST', {**reverse_data, 'reason': 'سبب مختلف'}, expected=409)
        self.assertEqual(self.balances('2026-04-02')['all_totals']['remitted_cents'], 100)
        self.assertEqual(self.balances('2026-04-03')['all_totals']['remitted_cents'], 0)
        self.assertEqual(self.request(f"withholding-remittances/{original['id']}?as_of=2026-04-02")['effective_status'], 'posted')
        replacement = self.remit({**payload, 'request_key': uuid.uuid4().hex, 'business_date': '2026-04-04'})
        self.assertEqual(replacement['reference'], original['reference'])
        replay = self.request('withholding-remittances', 'POST', payload)
        self.assertEqual((replay['id'], replay['effective_status']), (original['id'], 'reversed'))
        self.assertEqual(original['journal'], reversed_doc['journal'])

    def test_draft_close_guard_locked_writes_and_open_dated_correction(self):
        _, source = self.source()
        payload = self.data(source)
        doc = self.request('withholding-remittances', 'POST', payload)
        preview = self.request('period-preview?month=2026-04')
        self.assertFalse(preview['can_close'])
        self.assertEqual(preview['draft_counts']['withholding_remittance'], 1)
        posted = self.request(f"withholding-remittances/{doc['id']}/post", 'POST', {'revision': doc['revision']})
        preview = self.request('period-preview?month=2026-04')
        self.assertTrue(preview['can_close'], preview['blockers'])
        self.request('accounting-periods/2026-04/close', 'POST', {
            'request_key': uuid.uuid4().hex, 'revision': preview['period']['revision'],
            'reference': uuid.uuid4().hex, 'reason': 'فحص قفل توريد الضريبة'})
        self.request('withholding-remittances', 'POST', self.data(source, '1', '2026-04-02'), expected=400)
        self.assertEqual(self.request('withholding-remittances', 'POST', payload)['id'], doc['id'])
        self.assertEqual(self.request(f"withholding-remittances/{doc['id']}/post", 'POST', {'revision': doc['revision']})['id'], doc['id'])
        self.reverse(posted, '2026-05-01')
        self.assertEqual(self.balances('2026-04-30')['all_totals']['remitted_cents'], 100)
        self.assertEqual(self.balances('2026-05-01')['all_totals']['remitted_cents'], 0)

    def test_validation_csrf_stale_draft_capacity_and_read_only_reports(self):
        _, source = self.source()
        payload = self.data(source, '3')
        self.request('withholding-remittances', 'POST', payload, expected=403, csrf=False)
        for update in ({'authority_name': ''}, {'period_to': '2026-04-02'}, {'details': {}},
                       {'amount': '2'}, {'funding_account_id': self.bank},
                       {'allocations': [{'payment_id': source['id'], 'amount': '1.001'}]}):
            self.request('withholding-remittances', 'POST', {**payload, **update}, expected=400)
        draft = self.request('withholding-remittances', 'POST', payload)
        self.remit(self.data(source, '1', '2026-04-02'))
        self.request(f"withholding-remittances/{draft['id']}/post", 'POST', {'revision': draft['revision']}, expected=400)
        self.assertEqual(self.request(f"withholding-remittances/{draft['id']}")['status'], 'draft')
        before = self.request('audit')
        self.balances('2026-04-02')
        self.request('withholding-balances?as_of=2026-02-30', expected=400)
        self.request('withholding-balances?period_from=2026-12-01&period_to=2026-01-01', expected=400)
        self.assertEqual(self.request('audit'), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
