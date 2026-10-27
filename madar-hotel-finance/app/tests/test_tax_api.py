"""HTTP proof for explicit input VAT, original returns and monthly locks."""
import unittest
import uuid
import test_payables_api as base_api


class TaxAndPeriodsAPI(unittest.TestCase):
    setUpClass = classmethod(base_api.PayablesAPI.setUpClass.__func__)
    tearDownClass = classmethod(base_api.PayablesAPI.tearDownClass.__func__)
    request = classmethod(base_api.PayablesAPI.request.__func__)
    fixture = base_api.PayablesAPI.fixture
    payment = base_api.PayablesAPI.payment
    statement = base_api.PayablesAPI.statement

    def configure(self):
        config = self.request('tax-config')
        if config['settings']['registration_status'] != 'registered':
            self.request('tax-settings', 'PUT', {'revision': config['settings']['revision'],
                'registration_status': 'registered', 'tax_number': 'TEST-VAT-ONLY'})
        chart = self.request('accounting')
        mapping = next(m for m in chart['mappings'] if m['role'] == 'recoverable_input_vat')
        if mapping['status'] != 'confirmed':
            self.request(f"account-mappings/{mapping['id']}", 'PUT',
                         {'revision': mapping['revision'], 'status': 'confirmed'})
        self.vat = mapping['account_id']

    def code(self, rate='14', recoverable='100', kind='vat'):
        self.configure()
        return self.request('tax-codes', 'POST', {'name': 'كود ضريبة اختبار ' + uuid.uuid4().hex,
            'kind': kind, 'rate': rate, 'recoverable_percent': recoverable,
            'confirmed': True, 'active': True})

    def bill(self, supplier, product, code=None, ds='2026-01-01', price='100', qty='1', post=True):
        payload = {'request_key': uuid.uuid4().hex, 'supplier_id': supplier['id'],
            'department_id': self.department, 'reference': uuid.uuid4().hex, 'business_date': ds,
            'due_date': ds, 'kind': 'service', 'tax_code_id': code['id'] if code else None,
            'lines': [{'product_id': product['id'], 'quantity': qty, 'price': price}]}
        doc = self.request('invoices', 'POST', payload)
        if post:
            doc = self.request(f"invoices/{doc['id']}/post", 'POST', {'revision': doc['revision']})
        return doc, payload

    def returned(self, bill, qty, ds, post=True):
        payload = {'request_key': uuid.uuid4().hex, 'invoice_id': bill['id'],
            'business_date': ds, 'reference': uuid.uuid4().hex,
            'lines': [{'invoice_line_id': bill['lines'][0]['id'], 'quantity': qty}]}
        preview = self.request('supplier-return-preview', 'POST', payload)
        doc = self.request('supplier-returns', 'POST', payload)
        self.assertEqual(preview['total_cents'], doc['total_cents'])
        if post:
            doc = self.request(f"supplier-returns/{doc['id']}/post", 'POST', {'revision': doc['revision']})
        return doc

    def period_action(self, month, action, revision):
        return self.request(f'accounting-periods/{month}/{action}', 'POST', {
            'request_key': uuid.uuid4().hex, 'revision': revision,
            'reference': 'TEST-' + uuid.uuid4().hex, 'reason': 'مراجعة اختبار معزولة'})

    def test_explicit_tax_code_gross_journal_payment_and_original_return(self):
        supplier, product = self.fixture(terms=0)
        code = self.code(recoverable='50')
        preview = self.request('tax-preview', 'POST', {'tax_code_id': code['id'],
            'lines': [{'product_id': product['id'], 'quantity': '1', 'price': '100'}]})
        self.assertEqual((preview['subtotal_cents'], preview['tax_cents'], preview['total_cents']), (10000, 1400, 11400))
        bill, payload = self.bill(supplier, product, code)
        self.assertEqual((bill['tax_recoverable_cents'], bill['tax_nonrecoverable_cents']), (700, 700))
        journal = self.request(f"journals/{bill['journal_id']}")
        self.assertEqual(journal['debit_cents'], 11400)
        self.assertEqual(sum(l['debit_cents'] for l in journal['lines'] if l['account_id'] == self.expense), 10700)
        self.assertEqual(sum(l['debit_cents'] for l in journal['lines'] if l['account_id'] == self.vat), 700)
        self.assertEqual(self.request('invoices', 'POST', payload)['id'], bill['id'])
        self.payment(supplier, '114', '2026-01-02', mode='invoice', target=bill['id'])
        # A later configuration edit cannot change this posted invoice or its return.
        self.request(f"tax-codes/{code['id']}", 'PUT', {'revision': code['revision'], 'rate': '20', 'recoverable_percent': '0'})
        returned = self.returned(bill, '.5', '2026-01-03')
        self.assertEqual((returned['subtotal_cents'], returned['tax_cents'], returned['total_cents']), (5000, 700, 5700))
        line = returned['lines'][0]
        self.assertEqual((line['recoverable_cents'], line['nonrecoverable_cents']), (350, 350))
        statement = self.statement(supplier, '2026-01-03')['totals']
        self.assertEqual((statement['payable_balance_cents'], statement['unapplied_return_credit_cents']), (-5700, 5700))
        self.assertTrue(self.request('supplier-reconciliation?as_of=2026-01-03')['matched'])
        self.assertEqual(self.request('bootstrap')['products'][0]['last_purchase_price_cents'], 10000)

    def test_tax_snapshot_requires_resave_and_retry_does_not_use_current_rate(self):
        supplier, product = self.fixture(terms=0)
        code = self.code()
        bill, payload = self.bill(supplier, product, code, ds='2026-02-01', post=False)
        self.request(f"tax-codes/{code['id']}", 'PUT', {'revision': code['revision'], 'rate': '20'})
        self.assertEqual(self.request('invoices', 'POST', payload)['total_cents'], 11400)
        self.request('invoices', 'POST', {**payload,'tax_rate':'99'}, expected=409)
        self.request(f"invoices/{bill['id']}/post", 'POST', {'revision': bill['revision']}, expected=409)
        refreshed = self.request(f"invoices/{bill['id']}", 'PUT', {**payload, 'revision': bill['revision']})
        self.assertEqual(refreshed['total_cents'], 12000)
        posted = self.request(f"invoices/{bill['id']}/post", 'POST', {'revision': refreshed['revision']})
        self.assertEqual(posted['total_cents'], 12000)
        self.request(f"invoices/{bill['id']}", 'PUT', {**payload, 'revision': posted['revision']}, expected=400)

    def test_per_line_rounding_and_net_last_purchase_price_are_preserved(self):
        supplier, product = self.fixture(terms=0)
        code = self.code(rate='50', recoverable='33.33')
        bill, _ = self.bill(supplier, product, code, ds='2026-03-01', price='.01', qty='3')
        self.assertEqual((bill['subtotal_cents'], bill['tax_cents'], bill['total_cents']), (3, 2, 5))
        returns = [self.returned(bill, '.5', f'2026-03-{day:02d}') for day in range(2, 8)]
        self.assertEqual(sum(r['total_cents'] for r in returns), 5)
        self.assertEqual(sum(r['subtotal_cents'] for r in returns), 3)
        self.assertEqual(sum(r['tax_cents'] for r in returns), 2)
        report = self.request('supplier-reconciliation?as_of=2026-03-07')
        self.assertTrue(report['matched'])
        product_after = next(p for p in self.request('bootstrap')['products'] if p['id'] == product['id'])
        self.assertEqual(product_after['last_purchase_price_cents'], 1)

    def test_tax_report_as_of_uses_end_date_and_preserves_returns(self):
        supplier, product = self.fixture(terms=0)
        code = self.code()
        bill, _ = self.bill(supplier, product, code, ds='2024-05-01')
        self.returned(bill, '.25', '2024-05-02')
        report = self.request('tax-report?as_of=2024-05-02')
        self.assertEqual((report['from_date'], report['to_date']), ('2024-01-01', '2024-05-02'))
        own = [row for row in report['rows'] if row['supplier_id'] == supplier['id']]
        self.assertEqual(sum(row['tax_cents'] for row in own), 1050)
        self.assertEqual(sum(row['gross_cents'] for row in own), 8550)
        before = self.request('tax-report?from_date=2024-05-01&to_date=2024-05-01')
        self.assertEqual(sum(row['tax_cents'] for row in before['rows'] if row['supplier_id'] == supplier['id']), 1400)

    def test_period_close_blocks_new_documents_but_allows_read_and_identical_retry(self):
        supplier, product = self.fixture(terms=0)
        code = self.code()
        bill, payload = self.bill(supplier, product, code, ds='2031-01-01')
        preview = self.request('period-preview?month=2031-01')
        self.assertTrue(preview['can_close'], preview['blockers'])
        closed = self.period_action('2031-01', 'close', preview['period']['revision'])
        try:
            self.assertEqual(closed['period']['status'], 'closed')
            self.assertEqual(self.request('invoices', 'POST', payload)['id'], bill['id'])
            self.assertEqual(self.request(f"invoices/{bill['id']}/post", 'POST', {'revision': bill['revision']})['id'], bill['id'])
            self.assertEqual(self.request(f"invoices/{bill['id']}")['total_cents'], 11400)
            self.request('invoices', 'POST', {**payload, 'request_key': uuid.uuid4().hex,
                         'reference': uuid.uuid4().hex}, expected=400)
            self.request('supplier-payments', 'POST', {'request_key': uuid.uuid4().hex,
                'supplier_id': supplier['id'], 'payment_date': '2031-01-03', 'amount': '1',
                'method': 'cash', 'funding_account_id': self.cash, 'allocation_mode': 'invoice',
                'target_invoice_id': bill['id'], 'details': {'received_by': 'اختبار'}}, expected=400)
            returned = self.returned(bill, '.5', '2031-02-01')
            self.assertEqual(returned['total_cents'], 5700)
        finally:
            current = self.request('accounting-periods?year=2031')['periods'][0]
            if current['status'] == 'closed':
                self.period_action('2031-01', 'reopen', current['revision'])
        self.assertEqual(self.request('accounting-periods?year=2031')['periods'][0]['status'], 'open')

    def test_period_preview_pending_draft_blocks_close(self):
        supplier, product = self.fixture(terms=0)
        bill, _ = self.bill(supplier, product, None, ds='2032-02-01', post=False)
        preview = self.request('period-preview?month=2032-02')
        self.assertFalse(preview['can_close'])
        self.assertTrue(any(row['code'] == bill['code'] for row in preview['pending_drafts']))
        data = {'request_key': uuid.uuid4().hex, 'revision': preview['period']['revision'],
                'reference': 'TEST', 'reason': 'يجب رفض وجود مسودة'}
        self.request('accounting-periods/2032-02/close', 'POST', data, expected=400)
        self.request(f"invoices/{bill['id']}", 'PUT', {'revision': bill['revision'], 'action': 'cancel'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
