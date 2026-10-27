"""HTTP proof for dated supplier settlements, refunds and correction documents."""
import unittest
import uuid
import test_payables_api as base_api


class SettlementAPI(unittest.TestCase):
    # Reuse the isolated HTTP harness, without inheriting/rerunning its test cases.
    setUpClass = classmethod(base_api.PayablesAPI.setUpClass.__func__)
    tearDownClass = classmethod(base_api.PayablesAPI.tearDownClass.__func__)
    request = classmethod(base_api.PayablesAPI.request.__func__)
    fixture = base_api.PayablesAPI.fixture
    invoice = base_api.PayablesAPI.invoice
    payment = base_api.PayablesAPI.payment
    statement = base_api.PayablesAPI.statement

    def settlement(self, supplier, kind, source_id, allocations, ds, post=True):
        data = {'request_key': uuid.uuid4().hex, 'supplier_id': supplier['id'],
                'source_type': kind, 'source_id': source_id, 'business_date': ds,
                'reference': uuid.uuid4().hex, 'allocations': allocations}
        saved = self.request('supplier-settlements', 'POST', data)
        self.assertEqual(self.request('supplier-settlements', 'POST', data)['id'], saved['id'])
        return self.request(f"supplier-settlements/{saved['id']}/post", 'POST', {'revision': saved['revision']}) if post else saved

    def refund(self, supplier, kind, source_id, amount, ds, post=True):
        data = {'request_key': uuid.uuid4().hex, 'supplier_id': supplier['id'],
                'source_type': kind, 'source_id': source_id, 'business_date': ds,
                'reference': uuid.uuid4().hex, 'reason': 'استرداد جزء من الرصيد',
                'method': 'cash', 'funding_account_id': self.cash, 'amount': amount,
                'details': {'received_by': 'أمين الخزينة'}}
        saved = self.request('supplier-refunds', 'POST', data)
        return self.request(f"supplier-refunds/{saved['id']}/post", 'POST', {'revision': saved['revision']}) if post else saved

    def reversal(self, payment, ds):
        return self.request(f"supplier-payments/{payment['id']}/reverse", 'POST', {
            'request_key': uuid.uuid4().hex, 'business_date': ds,
            'reason': 'تصحيح سداد سجل بالخطأ', 'revision': payment['revision'],
            'event_revision': payment.get('adjustment_revision', 0)})

    def reconciled(self, ds):
        report = self.request('supplier-reconciliation?as_of=' + ds)
        self.assertTrue(report['matched'], report.get('exceptions'))
        self.assertEqual(report['totals']['net_difference_cents'], 0)
        return report

    def test_advance_apply_across_invoices_and_reverse_keeps_dated_history(self):
        supplier, product = self.fixture(terms=0)
        advance = self.payment(supplier, '100', '2026-01-01', mode='manual', allow_advance=True)
        first = self.invoice(supplier, product, '100', '2026-01-02')
        second = self.invoice(supplier, product, '100', '2026-01-03')
        doc = self.settlement(supplier, 'advance', advance['id'], [
            {'invoice_id': first['id'], 'amount': '60'},
            {'invoice_id': second['id'], 'amount': '20'}], '2026-01-05', post=False)
        before = self.statement(supplier, '2026-01-05')['totals']
        self.assertEqual((before['payable_balance_cents'], before['advance_cents']), (20000, 10000))
        self.request(f"supplier-settlements/{doc['id']}/post", 'POST', {'revision': doc['revision']}, expected=403, csrf=False)
        posted = self.request(f"supplier-settlements/{doc['id']}/post", 'POST', {'revision': doc['revision']})
        after = self.statement(supplier, '2026-01-05')['totals']
        self.assertEqual((after['payable_balance_cents'], after['advance_cents'], after['net_payable_cents']), (12000, 2000, 10000))
        sources = self.request(f"supplier-credit-sources?supplier_id={supplier['id']}&as_of=2026-01-05")
        self.assertEqual(next(s['available_cents'] for s in sources['sources'] if s['source_type'] == 'advance'), 2000)
        self.reconciled('2026-01-05')
        reverse = {'revision': posted['revision'], 'business_date': '2026-01-10', 'reference': uuid.uuid4().hex, 'reason': 'إلغاء التوزيع لإعادة المراجعة'}
        self.request(f"supplier-settlements/{doc['id']}/reverse", 'POST', reverse)
        self.assertEqual(self.statement(supplier, '2026-01-09')['totals']['advance_cents'], 2000)
        restored = self.statement(supplier, '2026-01-10')['totals']
        self.assertEqual((restored['payable_balance_cents'], restored['advance_cents']), (20000, 10000))
        self.reconciled('2026-01-10')

    def test_paid_return_credit_refund_then_apply_has_no_lost_credit(self):
        supplier, product = self.fixture(terms=0)
        original = self.invoice(supplier, product, '100', '2026-02-01')
        paid = self.payment(supplier, '100', '2026-02-02', mode='invoice', target=original['id'])
        returned = self.request('supplier-returns', 'POST', {'request_key': uuid.uuid4().hex,
            'invoice_id': original['id'], 'business_date': '2026-02-03', 'reference': uuid.uuid4().hex,
            'lines': [{'invoice_line_id': original['lines'][0]['id'], 'quantity': '.4'}]})
        self.request(f"supplier-returns/{returned['id']}/post", 'POST', {'revision': returned['revision']})
        target = self.invoice(supplier, product, '80', '2026-02-04')
        self.refund(supplier, 'return_credit', original['id'], '10', '2026-02-05')
        self.settlement(supplier, 'return_credit', original['id'], [{'invoice_id': target['id'], 'amount': '30'}], '2026-02-06')
        balances = self.statement(supplier, '2026-02-06')['totals']
        self.assertEqual((balances['payable_balance_cents'], balances['unapplied_return_credit_cents']), (5000, 0))
        invoices = self.request(f"suppliers/{supplier['id']}/open-invoices?as_of=2026-02-06")
        source = next(i for i in invoices if i['id'] == original['id'])
        self.assertEqual((source['credit_used_cents'], source['credit_refunded_cents'], source['raw_outstanding_cents']), (3000, 1000, 0))
        self.assertEqual(self.statement(supplier, '2026-02-04')['totals']['net_payable_cents'], 4000)
        self.request(f"supplier-payments/{paid['id']}/reverse", 'POST', {'request_key': uuid.uuid4().hex,
            'business_date': '2026-02-07', 'reason': 'عكس ممنوع بسبب استهلاك الرصيد', 'revision': paid['revision']}, expected=400)
        self.reconciled('2026-02-06')

    def test_payment_reversal_restores_invoice_without_editing_original(self):
        supplier, product = self.fixture(terms=0)
        invoice = self.invoice(supplier, product, '100', '2026-03-01')
        paid = self.payment(supplier, '40', '2026-03-02', mode='invoice', target=invoice['id'])
        reversed_doc = self.reversal(paid, '2026-03-03')
        self.assertEqual((reversed_doc['status'], reversed_doc['effective_status']), ('posted', 'reversed'))
        self.assertEqual(self.statement(supplier, '2026-03-02')['totals']['payable_balance_cents'], 6000)
        self.assertEqual(self.statement(supplier, '2026-03-03')['totals']['payable_balance_cents'], 10000)
        self.assertEqual(reversed_doc['allocated_cents'], 4000)  # Original evidence remains intact.
        self.reconciled('2026-03-03')

    def test_cheque_stop_resume_clear_and_bounce_have_separate_history(self):
        supplier, product = self.fixture(terms=0)
        invoice = self.invoice(supplier, product, '100', '2026-04-01')
        cheque = self.payment(supplier, '100', '2026-04-02', mode='invoice', target=invoice['id'],
            method='cheque', funding_account_id=self.bank, details={'bank_name': 'بنك الاختبار',
            'cheque_number': uuid.uuid4().hex, 'cheque_book': 'دفتر', 'beneficiary': supplier['name'],
            'cheque_date': '2026-04-02', 'due_date': '2026-04-05'})
        event = lambda action, ds, ev: self.request(f"supplier-payments/{cheque['id']}/cheque-event", 'POST', {
            'request_key': uuid.uuid4().hex, 'action': action, 'business_date': ds, 'reference': uuid.uuid4().hex,
            'reason': 'إجراء موثق من البنك', 'revision': cheque['revision'], 'event_revision': ev})
        stopped = event('stop', '2026-04-03', 0)
        self.assertEqual(stopped['effective_cheque_status'], 'stopped')
        self.request(f"supplier-payments/{cheque['id']}/clear", 'POST', {'revision': cheque['revision'],
            'clearance_date': '2026-04-05', 'reference': 'CLEAR', 'bank_account_id': self.bank}, expected=400)
        resumed = event('resume', '2026-04-04', 1)
        self.assertEqual(resumed['effective_cheque_status'], 'issued')
        cheque = self.request(f"supplier-payments/{cheque['id']}/clear", 'POST', {'revision': cheque['revision'],
            'event_revision': 2, 'clearance_date': '2026-04-05', 'reference': 'CLEAR', 'bank_account_id': self.bank})
        bounced = event('bounce', '2026-04-06', 2)
        self.assertEqual((bounced['effective_status'], bounced['effective_cheque_status']), ('reversed', 'bounced'))
        self.assertEqual(self.statement(supplier, '2026-04-05')['totals']['payable_balance_cents'], 0)
        self.assertEqual(self.statement(supplier, '2026-04-06')['totals']['payable_balance_cents'], 10000)
        self.assertEqual(len(self.request(f"supplier-payments/{cheque['id']}/cheque-events?as_of=2026-04-06")), 3)
        self.reconciled('2026-04-06')

    def test_backdated_payment_cannot_reuse_intermediate_future_payment_capacity(self):
        supplier, product = self.fixture(terms=0)
        invoice = self.invoice(supplier, product, '100', '2026-05-01')
        future = self.payment(supplier, '80', '2026-07-01', mode='invoice', target=invoice['id'])
        self.reversal(future, '2026-08-01')
        data = {'request_key': uuid.uuid4().hex, 'supplier_id': supplier['id'], 'payment_date': '2026-06-01',
                'amount': '80', 'method': 'cash', 'funding_account_id': self.cash, 'allocation_mode': 'manual',
                'allocations': [{'invoice_id': invoice['id'], 'amount': '80'}], 'details': {'received_by': 'مستلم'}}
        count = len(self.request('supplier-payments?supplier_id=' + str(supplier['id'])))
        self.request('supplier-payments', 'POST', data, expected=400)
        self.assertEqual(len(self.request('supplier-payments?supplier_id=' + str(supplier['id']))), count)
        self.payment(supplier, '20', '2026-06-01', mode='invoice', target=invoice['id'])
        self.assertEqual(self.statement(supplier, '2026-07-01')['totals']['payable_balance_cents'], 0)
        self.assertEqual(self.statement(supplier, '2026-08-01')['totals']['payable_balance_cents'], 8000)
        self.reconciled('2026-07-01')
        self.reconciled('2026-08-01')


if __name__ == '__main__':
    unittest.main(verbosity=2)
