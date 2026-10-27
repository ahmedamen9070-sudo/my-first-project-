"""Cross-module HTTP contracts against an isolated application database."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid


SERVER = Path(__file__).resolve().parents[1] / 'server.py'


class PayablesAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        cls.url = f'http://127.0.0.1:{port}/api/'
        cls.proc = subprocess.Popen(
            ['python3', str(SERVER), '--port', str(port)],
            env={**os.environ, 'MADAR_DB': str(Path(cls.tmp.name) / 'api.sqlite')},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(100):
                try:
                    bootstrap = cls.request('bootstrap')
                    break
                except (OSError, urllib.error.URLError):
                    if cls.proc.poll() is not None:
                        raise RuntimeError(cls.proc.stderr.read())
                    time.sleep(.05)
            else:
                raise RuntimeError('Isolated API server did not start')
            cls.token = bootstrap['csrf']
            cls.categories = bootstrap['categories']
            cls.department = bootstrap['departments'][0]['id']
            chart = cls.request('accounting')
            cls.expense = next(a['id'] for a in chart['accounts'] if a['posting'] and a['normal_side'] == 'debit' and a['code'].startswith('6'))
            cls.cash = next(a['id'] for a in chart['accounts'] if a['posting'] and a['normal_side'] == 'debit' and a['code'].startswith('111'))
            cls.bank = next(a['id'] for a in chart['accounts'] if a['posting'] and a['normal_side'] == 'debit' and a['code'].startswith('112'))
            for mapping in chart['mappings']:
                if mapping['module'] == 'الموردون' and mapping['role'] in ('payable_control', 'supplier_advances', 'issued_cheques'):
                    cls.request(f"account-mappings/{mapping['id']}", 'PUT', {'revision': mapping['revision'], 'status': 'confirmed'})
        except Exception:
            cls.proc.terminate()
            cls.proc.wait(timeout=5)
            cls.proc.stderr.close()
            cls.tmp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        cls.proc.stderr.close()
        cls.tmp.cleanup()

    @classmethod
    def request(cls, path, method='GET', data=None, expected=200, csrf=True):
        headers = {'Content-Type': 'application/json'}
        if csrf:
            headers['X-Madar-CSRF'] = getattr(cls, 'token', '')
        request = urllib.request.Request(cls.url + path, method=method, headers=headers,
                                         data=json.dumps(data).encode() if data is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status, result = response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                status, result = error.code, json.load(error)
        if status != expected:
            raise AssertionError(f'{method} {path}: expected {expected}, got {status}: {result}')
        return result

    def fixture(self, terms=15):
        suffix = uuid.uuid4().hex[:12]
        category_ids = [c['id'] for c in self.categories[:2]]
        supplier = self.request('suppliers', 'POST', {'name': 'مورد API ' + suffix, 'terms_days': terms, 'category_ids': category_ids})
        product = self.request('products', 'POST', {
            'supplier_id': supplier['id'], 'department_id': self.department,
            'name': 'خدمة API ' + suffix, 'unit': 'وحدة', 'price': '100',
            'category_id': category_ids[0], 'expense_account_id': self.expense})
        return supplier, product

    def invoice(self, supplier, product, amount='100', business_date='2026-01-01', quantity='1', due_date=None, post=True):
        data = {'request_key': uuid.uuid4().hex, 'supplier_id': supplier['id'], 'department_id': self.department,
                'reference': uuid.uuid4().hex, 'business_date': business_date, 'kind': 'service', 'tax_rate': '0',
                'lines': [{'product_id': product['id'], 'quantity': quantity, 'price': amount}]}
        if due_date is not None:
            data['due_date'] = due_date
        result = self.request('invoices', 'POST', data)
        return self.request(f"invoices/{result['id']}/post", 'POST', {'revision': result['revision']}) if post else result

    def payment(self, supplier, amount, business_date='2026-01-20', mode='fifo', target=None, allocations=None, post=True, **extras):
        data = {'request_key': uuid.uuid4().hex, 'supplier_id': supplier['id'], 'payment_date': business_date,
                'currency': 'EGP', 'amount': amount, 'method': 'cash', 'funding_account_id': self.cash,
                'allocation_mode': mode, 'allow_advance': False, 'details': {'received_by': 'مستلم اختبار'}}
        if target is not None:
            data['target_invoice_id'] = target
        if allocations is not None:
            data['allocations'] = allocations
        data.update(extras)
        result = self.request('supplier-payments', 'POST', data)
        return self.request(f"supplier-payments/{result['id']}/post", 'POST', {'revision': result['revision']}) if post else result

    def statement(self, supplier, as_of='2026-12-31'):
        return self.request(f"suppliers/{supplier['id']}/statement?as_of={as_of}")

    def test_master_validation_price_history_and_posted_overlay(self):
        supplier, product = self.fixture()
        self.assertEqual(len(supplier['category_ids']), 2)
        before = len(self.request('bootstrap')['products'])
        outsider = next(c['id'] for c in self.categories if c['id'] not in supplier['category_ids'])
        self.request('products', 'POST', {'supplier_id': supplier['id'], 'department_id': self.department,
            'name': 'نوع توريد غير مسموح', 'unit': 'وحدة', 'price': '10', 'category_id': outsider}, expected=400)
        self.assertEqual(len(self.request('bootstrap')['products']), before, 'Failed configuration must roll back the inserted master')
        self.request(f"suppliers/{supplier['id']}", 'PUT', {'category_ids': []}, expected=400)
        self.assertEqual(self.request('bootstrap')['suppliers'][0]['category_ids'], supplier['category_ids'])
        first = self.invoice(supplier, product, '100', '2026-01-01')
        self.assertEqual(first['due_date'], '2026-01-16')
        self.assertEqual(first['status'], 'posted')
        self.assertTrue(first['journal_code'])
        later = self.invoice(supplier, product, '120', '2026-01-05')
        backdated = self.invoice(supplier, product, '110', '2026-01-03')
        self.assertEqual(later['lines'][0]['previous_price_cents'], 10000)
        self.assertEqual(backdated['lines'][0]['previous_price_cents'], 10000)
        self.invoice(supplier, product, '999', '2026-01-06', post=False)
        current = next(p for p in self.request('bootstrap')['products'] if p['id'] == product['id'])
        self.assertEqual(current['last_purchase_price_cents'], 12000)
        self.assertEqual(current['last_purchase_date'], '2026-01-05')
        self.assertEqual(current['price_cents'], 12000, 'Backdated posting and drafts must preserve latest approved price')
        self.request(f"products/{product['id']}", 'PUT', {'price': '119'}, expected=400)
        self.request(f"products/{product['id']}", 'PUT', {'price': '120', 'name': 'اسم معدل بعد الاعتماد'})
        self.assertEqual(self.request('invoices/' + str(first['id']))['lines'][0]['name'], product['name'])
        self.request(f"invoices/{first['id']}", 'PUT', {'revision': first['revision'], 'action': 'cancel'}, expected=400)
        rows = self.request('invoices?supplier_id=' + str(supplier['id']))
        self.assertEqual([r['status'] for r in rows].count('posted'), 3)
        self.assertEqual([r['status'] for r in rows].count('draft'), 1)
        journal = self.request('journals/' + str(first['journal_id']))
        self.assertEqual(journal['debit_cents'], journal['credit_cents'])
        self.assertEqual(journal['debit_cents'], 10000)
        self.assertNotIn('request_fingerprint', journal)
        retried = self.request(f"invoices/{first['id']}/post", 'POST', {'revision': 1})
        self.assertEqual(retried['journal_id'], first['journal_id'])
        self.request('journals/not-a-number', expected=400)
        self.request('supplier-aging?as_of=2026-02-30', expected=400)

    def test_partial_and_general_payments_and_transaction_rollback(self):
        supplier, product = self.fixture()
        first = self.invoice(supplier, product, '100', '2026-02-01', due_date='2026-02-10')
        second = self.invoice(supplier, product, '200', '2026-02-02', due_date='2026-02-20')
        partial = self.payment(supplier, '30', '2026-02-05', mode='invoice', target=first['id'])
        self.assertEqual(partial['allocated_cents'], 3000)
        general = self.payment(supplier, '100', '2026-02-10')
        self.assertEqual([(a['invoice_id'], a['amount_cents']) for a in general['allocations']], [(first['id'], 7000), (second['id'], 3000)])
        self.assertEqual(self.statement(supplier)['totals']['balance_cents'], 17000)
        self.assertEqual(self.statement(supplier, '2026-02-04')['totals']['balance_cents'], 30000)
        retried = self.request(f"supplier-payments/{general['id']}/post", 'POST', {'revision': 1})
        self.assertEqual(retried['journal_entry_id'], general['journal_entry_id'])
        bad = self.payment(supplier, '170', '2026-02-11', mode='manual',
                           allocations=[{'invoice_id': second['id'], 'amount': '170'}], post=False)
        self.payment(supplier, '10', '2026-02-11', mode='invoice', target=second['id'])
        self.request(f"supplier-payments/{bad['id']}/post", 'POST', {'revision': bad['revision']}, expected=400)
        self.assertEqual(self.request('supplier-payments/' + str(bad['id']))['status'], 'draft')
        self.assertEqual(self.statement(supplier)['totals']['balance_cents'], 16000)
        self.request(f"supplier-payments/{general['id']}", 'PUT', {'revision': general['revision'], 'action': 'cancel'}, expected=400)
        self.assertEqual(len(self.request('supplier-payments?supplier_id=' + str(supplier['id']))), 4)
        report = self.request('supplier-aging?as_of=2026-03-23')
        row = next(s for s in report['suppliers'] if s['supplier_id'] == supplier['id'])
        self.assertEqual(row['net_payable_cents'], 16000)
        self.assertEqual(row['total_cents'], 16000)
        self.assertEqual(row['days_31_60_cents'], 16000)

    def test_advance_is_separate_from_payable_balance(self):
        supplier, product = self.fixture()
        self.invoice(supplier, product, '100', '2026-05-01')
        payment = self.payment(supplier, '150', '2026-05-02', allow_advance=True)
        self.assertEqual(payment['allocated_cents'], 10000)
        self.assertEqual(payment['advance_cents'], 5000)
        statement = self.statement(supplier, '2026-05-02')
        self.assertEqual(statement['totals']['balance_cents'], 0)
        self.assertEqual(statement['totals']['advance_cents'], 5000)
        self.assertEqual(statement['totals']['net_payable_cents'], -5000)
        self.assertEqual(statement['rows'][-1]['advance_cents'], 5000)
        self.assertEqual(self.statement(supplier, '2026-05-01')['totals']['net_payable_cents'], 10000)
        report = self.request('supplier-aging?as_of=2026-05-02')
        row = next(s for s in report['suppliers'] if s['supplier_id'] == supplier['id'])
        self.assertEqual(row['total_cents'], 0)
        self.assertEqual(row['advance_cents'], 5000)
        self.assertEqual(row['net_payable_cents'], -5000)

    def test_legacy_draft_needs_configuration_before_posting(self):
        suffix = uuid.uuid4().hex
        supplier = self.request('suppliers', 'POST', {'name': 'مورد دون إعداد ' + suffix})
        product = self.request('products', 'POST', {'supplier_id': supplier['id'], 'department_id': self.department,
            'name': 'بند دون ربط', 'unit': 'وحدة', 'price': '50'})
        draft = self.invoice(supplier, product, '50', '2026-06-01', post=False)
        self.assertIsNone(draft['due_date'])
        self.assertIsNone(draft['lines'][0]['expense_account_id'])
        self.request(f"invoices/{draft['id']}/post", 'POST', {'revision': draft['revision']}, expected=400)
        self.assertEqual(self.request('invoices/' + str(draft['id']))['status'], 'draft')
        self.assertFalse(self.statement(supplier)['rows'])
        data = {'revision': draft['revision'], 'request_key': suffix, 'supplier_id': supplier['id'],
                'department_id': self.department, 'reference': draft['reference'], 'business_date': '2026-06-01',
                'due_date': '2026-05-01', 'lines': [{'product_id': product['id'], 'quantity': '2', 'price': '50'}]}
        self.request('invoices/' + str(draft['id']), 'PUT', data, expected=400)
        unchanged = self.request('invoices/' + str(draft['id']))
        self.assertEqual(unchanged['revision'], draft['revision'])
        self.assertEqual(unchanged['total_cents'], 5000)
        self.assertEqual(unchanged['lines'][0]['quantity'], '1')
        self.request('invoices/' + str(draft['id']), 'PUT', {**data, 'due_date': '2026-06-01', 'revision': 0}, expected=409)

    def test_invoice_retry_compares_semantics_and_survives_master_edits(self):
        supplier, product = self.fixture(terms=30)
        data = {'request_key': uuid.uuid4().hex, 'supplier_id': supplier['id'], 'department_id': self.department,
                'reference': uuid.uuid4().hex, 'business_date': '2026-07-01', 'kind': 'service', 'tax_rate': '0',
                'lines': [{'product_id': product['id'], 'quantity': '1.500', 'price': '100.00'}]}
        first = self.request('invoices', 'POST', data)
        posted = self.request(f"invoices/{first['id']}/post", 'POST', {'revision': first['revision']})
        self.request(f"products/{product['id']}", 'PUT', {'name': 'اسم آخر للبند', 'unit': 'زيارة'})
        self.request(f"suppliers/{supplier['id']}", 'PUT', {'terms_days': 60})
        repeated = self.request('invoices', 'POST', {**data, 'lines': [{'product_id': product['id'], 'quantity': '1.5', 'price': '100'}]})
        self.assertEqual(repeated['id'], first['id'])
        self.assertEqual(repeated['status'], 'posted')
        self.assertEqual(repeated['journal_id'], posted['journal_id'])
        self.assertEqual(repeated['due_date'], '2026-07-31')
        changed = {**data, 'lines': [{'product_id': product['id'], 'quantity': '1.5', 'price': '101'}]}
        self.request('invoices', 'POST', changed, expected=409)
        self.request('invoices', 'POST', {**data, 'due_date': '2026-08-30'}, expected=409)
        self.assertEqual(len(self.request('invoices?supplier_id=' + str(supplier['id']))), 1)
        self.assertEqual(self.statement(supplier)['totals']['balance_cents'], 15000)

    def test_return_after_payment_is_explicit_credit_and_as_of_is_reproducible(self):
        supplier, product = self.fixture(terms=0)
        invoice = self.invoice(supplier, product, '100', '2026-03-01')
        self.payment(supplier, '100', '2026-03-02', mode='invoice', target=invoice['id'])
        data = {'request_key': uuid.uuid4().hex, 'invoice_id': invoice['id'], 'business_date': '2026-03-03',
                'reference': uuid.uuid4().hex, 'lines': [{'invoice_line_id': invoice['lines'][0]['id'], 'quantity': '.25'}]}
        draft = self.request('supplier-returns', 'POST', data)
        self.assertEqual(self.request('supplier-returns', 'POST', data)['id'], draft['id'])
        self.request(f"supplier-returns/{draft['id']}/post", 'POST', {'revision': draft['revision']}, expected=403, csrf=False)
        self.assertEqual(self.request('supplier-returns/' + str(draft['id']))['status'], 'draft')
        posted = self.request(f"supplier-returns/{draft['id']}/post", 'POST', {'revision': draft['revision']})
        self.assertEqual(posted['total_cents'], 2500)
        self.assertEqual(self.request(f"supplier-returns/{draft['id']}/post", 'POST', {'revision': 1})['journal_id'], posted['journal_id'])
        self.request(f"supplier-returns/{draft['id']}", 'PUT', {**data, 'revision': posted['revision']}, expected=400)
        self.assertEqual(self.statement(supplier, '2026-03-01')['totals']['balance_cents'], 10000)
        self.assertEqual(self.statement(supplier, '2026-03-02')['totals']['balance_cents'], 0)
        statement = self.statement(supplier, '2026-03-03')
        self.assertEqual(statement['totals']['balance_cents'], -2500)
        self.assertEqual(statement['totals']['unapplied_return_credit_cents'], 2500)
        row = next(s for s in self.request('supplier-aging?as_of=2026-03-03')['suppliers'] if s['supplier_id'] == supplier['id'])
        self.assertEqual(row['total_cents'], 0)
        self.assertEqual(row['credit_cents'], 2500)
        self.assertEqual(row['net_payable_cents'], -2500)
        detail = self.request('invoices/' + str(invoice['id']))
        self.assertEqual(detail['lines'][0]['returnable_quantity'], '0.75')
        self.assertEqual(len(self.request('supplier-returns?supplier_id=' + str(supplier['id']))), 1)

    def test_cheque_issue_and_clearance_have_separate_journals(self):
        supplier, product = self.fixture()
        invoice = self.invoice(supplier, product, '80', '2026-04-01')
        payment = self.payment(supplier, '80', '2026-04-02', mode='invoice', target=invoice['id'],
            method='cheque', funding_account_id=self.bank,
            details={'bank_name': 'بنك اختبار', 'cheque_number': uuid.uuid4().hex, 'cheque_book': 'دفتر 1',
                     'beneficiary': supplier['name'], 'cheque_date': '2026-04-02', 'due_date': '2026-04-10'})
        self.assertEqual(payment['cheque_status'], 'issued')
        issue = self.request('journals/' + str(payment['journal_entry_id']))
        self.assertNotIn(self.bank, [line['account_id'] for line in issue['lines']])
        clearance = {'revision': payment['revision'], 'clearance_date': '2026-04-10',
                     'bank_account_id': self.bank, 'reference': 'CLR-' + uuid.uuid4().hex}
        cleared = self.request(f"supplier-payments/{payment['id']}/clear", 'POST', clearance)
        self.assertEqual(cleared['cheque_status'], 'cleared')
        self.assertNotEqual(cleared['journal_entry_id'], cleared['clearance_journal_entry_id'])
        journal = self.request('journals/' + str(cleared['clearance_journal_entry_id']))
        self.assertEqual(journal['debit_cents'], 8000)
        self.assertEqual(journal['credit_cents'], 8000)
        self.assertIn(self.bank, [line['account_id'] for line in journal['lines'] if line['credit_cents']])
        self.assertEqual(self.request(f"supplier-payments/{payment['id']}/clear", 'POST', clearance)['clearance_journal_entry_id'], cleared['clearance_journal_entry_id'])
        self.assertEqual(self.statement(supplier)['totals']['balance_cents'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
