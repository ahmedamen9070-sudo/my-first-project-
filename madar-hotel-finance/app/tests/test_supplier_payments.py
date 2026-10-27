"""Financial invariants for supplier payments, without touching an operator DB."""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting
import payables
import supplier_payments as payments


class SupplierPayments(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE sequences(kind TEXT PRIMARY KEY,value INTEGER NOT NULL);
            CREATE TABLE suppliers(id INTEGER PRIMARY KEY,code TEXT UNIQUE,name TEXT,phone TEXT DEFAULT '',tax_number TEXT DEFAULT '',notes TEXT DEFAULT '',created_at TEXT);
            CREATE TABLE departments(id INTEGER PRIMARY KEY,code TEXT,name TEXT,cost_code TEXT);
            CREATE TABLE products(id INTEGER PRIMARY KEY,code TEXT,supplier_id INTEGER REFERENCES suppliers(id),name TEXT,unit TEXT,price_cents INTEGER,department_id INTEGER REFERENCES departments(id));
            CREATE TABLE invoices(id INTEGER PRIMARY KEY,code TEXT UNIQUE,request_key TEXT UNIQUE,supplier_id INTEGER REFERENCES suppliers(id),reference TEXT,business_date TEXT,department_id INTEGER REFERENCES departments(id),notes TEXT DEFAULT '',status TEXT CHECK(status IN('draft','cancelled')),subtotal_cents INTEGER,tax_basis INTEGER DEFAULT 0,tax_cents INTEGER DEFAULT 0,total_cents INTEGER,revision INTEGER DEFAULT 1,created_at TEXT,updated_at TEXT,kind TEXT DEFAULT 'expense');
            CREATE TABLE invoice_lines(id INTEGER PRIMARY KEY,invoice_id INTEGER REFERENCES invoices(id),product_id INTEGER REFERENCES products(id),name TEXT,unit TEXT,quantity TEXT,price_cents INTEGER,total_cents INTEGER);
            CREATE TABLE audit(id INTEGER PRIMARY KEY,entity TEXT,entity_id INTEGER,action TEXT,details TEXT,created_at TEXT);
            INSERT INTO suppliers(id,code,name,created_at) VALUES(1,'SUP-1','مورد أغذية','2026-01-01'),(2,'SUP-2','مورد آخر','2026-01-01');
            INSERT INTO departments(id,code,name,cost_code) VALUES(1,'D1','المطبخ','C1');
        ''')
        accounting.initialize(self.db)
        payables.initialize(self.db)
        payments.initialize(self.db)
        self.db.execute("UPDATE account_mappings SET status='confirmed'")
        self.cash = self.account('111002')
        self.bank = self.account('112001')
        self.ap = self.account('211001')
        self.advance = self.account('115004')
        self.cheques = self.account('217004')
        self.expense = self.account('680003')
        for supplier_id in (1, 2):
            self.db.execute('INSERT INTO products(id,code,supplier_id,name,unit,price_cents,department_id,expense_account_id) VALUES(?,?,?,?,?,10000,1,?)',
                            (supplier_id, f'P{supplier_id}', supplier_id, 'صنف اختبار', 'وحدة', self.expense))
        self.counter = 0
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def account(self, code):
        return self.db.execute('SELECT id FROM accounts WHERE code=?', (code,)).fetchone()[0]

    def invoice(self, cents=10000, business_date='2026-10-01', due_date='2026-10-15', supplier_id=1):
        self.counter += 1
        pk = self.db.execute('''INSERT INTO invoices(code,request_key,supplier_id,reference,business_date,department_id,status,subtotal_cents,total_cents,created_at,updated_at)
            VALUES(?,?,?,?,?,1,'draft',?,?,?,?)''',
            (f'INV-{self.counter}', f'invoice-key-{self.counter}', supplier_id, f'REF-{self.counter}', business_date, cents, cents, business_date, business_date)).lastrowid
        self.db.execute('INSERT INTO invoice_lines(invoice_id,product_id,name,unit,quantity,price_cents,total_cents) VALUES(?,?,?,?,?,?,?)',
                        (pk, supplier_id, 'صنف اختبار', 'وحدة', '1', cents, cents))
        return payables.post_invoice(self.db, pk, {'revision': 1, 'due_date': due_date})

    def details(self, method):
        return {
            'cash': {'received_by': 'مندوب المورد', 'receipt_reference': 'ACK-1'},
            'cheque': {'bank_name': 'بنك مصر', 'cheque_number': '00125', 'cheque_book': 'دفتر 1',
                       'beneficiary': 'مورد أغذية', 'cheque_date': '2026-10-16', 'due_date': '2026-10-20'},
            'bank_transfer': {'bank_name': 'بنك المورد', 'beneficiary': 'مورد أغذية',
                              'beneficiary_account': 'EG-TEST-ACCOUNT', 'transaction_reference': 'TR-1', 'value_date': '2026-10-16'},
            'bank_deposit': {'bank_name': 'بنك المورد', 'beneficiary_account': 'TEST-ACCOUNT',
                             'deposit_reference': 'DEP-1', 'value_date': '2026-10-16'},
            'instapay': {'recipient': 'supplier@instapay', 'transaction_reference': 'IP-1', 'value_date': '2026-10-16'},
            'bank_card': {'card_type': 'Visa', 'last_four': '1234', 'transaction_reference': 'CARD-1', 'value_date': '2026-10-16'},
        }[method]

    def payload(self, method='cash', **changes):
        self.counter += 1
        value = {'request_key': f'payment-key-{self.counter}', 'supplier_id': 1, 'payment_date': '2026-10-16',
                 'amount': '30.00', 'currency': 'EGP', 'method': method,
                 'funding_account_id': self.cash if method == 'cash' else self.bank,
                 'allocation_mode': 'manual', 'allocations': [], 'allow_advance': False,
                 'details': self.details(method)}
        value.update(changes)
        return value

    def post(self, payload):
        saved = payments.save_payment(self.db, payload)
        return payments.approve_payment(self.db, saved['id'], {'revision': saved['revision']})

    def returned(self, invoice, quantity='0.2', day='2026-10-20'):
        self.counter += 1
        saved = payables.save_return(self.db, {'request_key': f'return-{self.counter}', 'invoice_id': invoice['id'],
            'business_date': day, 'reference': f'RET-{self.counter}',
            'lines': [{'invoice_line_id': invoice['lines'][0]['id'], 'quantity': quantity}]})
        return payables.approve_return(self.db, saved['id'], {'revision': saved['revision']})

    def assert_balanced(self, journal):
        self.assertIsNotNone(journal)
        self.assertEqual(sum(x['debit_cents'] for x in journal['lines']), sum(x['credit_cents'] for x in journal['lines']))

    def test_draft_does_not_move_ledger_and_can_be_updated_or_cancelled(self):
        invoice = self.invoice()
        payload = self.payload(allocation_mode='invoice', target_invoice_id=invoice['id'])
        draft = payments.save_payment(self.db, payload)
        self.assertEqual(draft['status'], 'draft')
        self.assertEqual(draft['allocations'], [])
        self.assertEqual(payables.invoice_outstanding(self.db, invoice['id']), 10000)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], 1)
        updated = payments.update_payment(self.db, draft['id'], {**payload, 'revision': 1, 'amount': '20.00'})
        self.assertEqual(updated['revision'], 2)
        with self.assertRaises(accounting.ConflictError):
            payments.update_payment(self.db, draft['id'], {**payload, 'revision': 1})
        cancelled = payments.update_payment(self.db, draft['id'], {'revision': 2, 'action': 'cancel'})
        self.assertEqual(cancelled['status'], 'cancelled')
        with self.assertRaises(ValueError):
            payments.approve_payment(self.db, draft['id'], {'revision': 3})

    def test_all_six_methods_and_cash_supplier_acknowledgement(self):
        for method in ('cash', 'cheque', 'bank_transfer', 'bank_deposit', 'instapay', 'bank_card'):
            with self.subTest(method=method):
                invoice = self.invoice()
                result = self.post(self.payload(method, allocation_mode='invoice', target_invoice_id=invoice['id']))
                self.assertEqual(result['status'], 'posted')
                self.assertEqual(result['allocated_cents'], 3000)
                self.assertEqual(result['advance_cents'], 0)
                self.assertEqual(result['allocations'][0]['code'], invoice['code'])
                self.assert_balanced(result['journal'])
                credit = next(line for line in result['journal']['lines'] if line['credit_cents'])
                self.assertEqual(credit['account_id'], self.cash if method == 'cash' else self.cheques if method == 'cheque' else self.bank)
                if method == 'cash':
                    self.assertIn('صرف', result['voucher_title'])
                    self.assertEqual(result['details']['received_by'], 'مندوب المورد')

    def test_required_details_per_method_and_no_card_secrets(self):
        self.invoice()
        for method in (item['id'] for item in payments.payment_methods()):
            with self.subTest(method=method):
                with self.assertRaises(ValueError):
                    payments.save_payment(self.db, self.payload(method, details={}))
        for bad_details in ({**self.details('bank_card'), 'cvv': '123'},
                            {**self.details('bank_card'), 'card_number': '4111111111111111'},
                            {**self.details('bank_card'), 'last_four': '4111111111111111'},
                            {**self.details('bank_card'), 'card_type': 'unknown'}):
            with self.assertRaises(ValueError):
                payments.save_payment(self.db, self.payload('bank_card', details=bad_details))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM supplier_payments').fetchone()[0], 0)

    def test_wrong_funding_account_and_currency_rejected(self):
        self.invoice()
        for value in (self.payload('cash', funding_account_id=self.bank),
                      self.payload('bank_transfer', funding_account_id=self.cash),
                      self.payload('bank_card', funding_account_id=self.account('113001')),
                      self.payload('cash', currency='USD')):
            with self.assertRaises(ValueError):
                payments.save_payment(self.db, value)
        self.db.execute('UPDATE accounts SET active=0 WHERE id=?', (self.cash,))
        with self.assertRaises(ValueError):
            payments.save_payment(self.db, self.payload())

    def test_cash_funded_deposit_credits_cash_rather_than_bank(self):
        invoice = self.invoice()
        result = self.post(self.payload('bank_deposit', funding_account_id=self.cash,
            allocation_mode='invoice', target_invoice_id=invoice['id']))
        credit = next(line for line in result['journal']['lines'] if line['credit_cents'])
        self.assertEqual(credit['account_id'], self.cash)
        self.assertEqual(payments.account_balance(self.db, self.cash), -3000)
        self.assertEqual(payments.account_balance(self.db, self.bank), 0)
        self.assertEqual(next(item for item in payments.payment_methods() if item['id'] == 'bank_deposit')['funding_prefixes'], ['111', '112'])

    def test_specific_invoice_partial_payment_and_general_manual_split(self):
        first = self.invoice()
        second = self.invoice(20000)
        paid = self.post(self.payload(allocation_mode='invoice', target_invoice_id=first['id'], amount='25.00'))
        self.assertEqual(payables.invoice_outstanding(self.db, first['id']), 7500)
        self.assertEqual(paid['advance_cents'], 0)
        general = self.post(self.payload(amount='80.00', allocations=[{'invoice_id': first['id'], 'amount': '50.00'},
            {'invoice_id': second['id'], 'amount': '30.00'}]))
        self.assertEqual(general['allocated_cents'], 8000)
        self.assertEqual(payables.invoice_outstanding(self.db, first['id']), 2500)
        self.assertEqual(payables.invoice_outstanding(self.db, second['id']), 17000)

    def test_fifo_uses_oldest_due_and_excess_requires_explicit_advance(self):
        later = self.invoice(7000, due_date='2026-10-14')
        earlier = self.invoice(5000, due_date='2026-10-05')
        data = self.payload(amount='150.00', allocation_mode='fifo')
        preview = payments.allocation_preview(self.db, data)
        self.assertEqual([x['invoice_id'] for x in preview['allocations']], [earlier['id'], later['id']])
        self.assertEqual(preview['advance_cents'], 3000)
        self.assertFalse(preview['can_approve_allocation'])
        saved = payments.save_payment(self.db, data)
        with self.assertRaises(ValueError):
            payments.approve_payment(self.db, saved['id'], {'revision': 1})
        saved = payments.update_payment(self.db, saved['id'], {**data, 'revision': 1, 'allow_advance': True})
        result = payments.approve_payment(self.db, saved['id'], {'revision': saved['revision']})
        self.assertEqual(result['advance_cents'], 3000)
        self.assertEqual(payables.supplier_balances(self.db, 1)['advance_cents'], 3000)
        self.assertEqual(next(line['debit_cents'] for line in result['journal']['lines'] if line['account_id'] == self.advance), 3000)

    def test_no_implicit_fifo_for_general_payment(self):
        self.invoice()
        data = self.payload()
        data.pop('allocation_mode')
        preview = payments.allocation_preview(self.db, data)
        self.assertEqual(preview['allocations'], [])
        self.assertEqual(preview['advance_cents'], 3000)
        self.assertFalse(preview['can_approve_allocation'])

    def test_existing_advance_reduces_new_cash_ceiling_without_implicit_application(self):
        advance = self.post(self.payload(amount='100.00', allow_advance=True))
        self.assertEqual(advance['advance_cents'], 10000)
        invoice = self.invoice(50000)
        data = self.payload(amount='500.00', allocation_mode='fifo')
        preview = payments.allocation_preview(self.db, data)
        self.assertEqual(preview['existing_advance_cents'], 10000)
        self.assertEqual(preview['net_payable_cents'], 40000)
        self.assertEqual(preview['allocated_cents'], 40000)
        self.assertEqual(preview['advance_cents'], 10000)
        self.assertEqual(payables.invoice_outstanding(self.db, invoice['id']), 50000)
        self.assertEqual(payables.supplier_balances(self.db, 1)['advance_cents'], 10000)

    def test_idempotent_save_and_post_with_wrong_retry_payload_conflict(self):
        invoice = self.invoice()
        data = self.payload(allocation_mode='invoice', target_invoice_id=invoice['id'])
        draft = payments.save_payment(self.db, data)
        self.assertEqual(payments.save_payment(self.db, data)['id'], draft['id'])
        with self.assertRaises(accounting.ConflictError):
            payments.save_payment(self.db, {**data, 'amount': '31.00'})
        posted = payments.approve_payment(self.db, draft['id'], {'revision': 1})
        self.assertEqual(payments.approve_payment(self.db, draft['id'], {'revision': 1})['journal_entry_id'], posted['journal_entry_id'])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM journal_entries WHERE source_type='supplier_payment'").fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM payment_allocations').fetchone()[0], 1)
        with self.assertRaises(ValueError):
            payments.update_payment(self.db, posted['id'], {**data, 'revision': posted['revision']})

    def test_mapping_confirmation_and_revision_required_before_approval(self):
        invoice = self.invoice()
        draft = payments.save_payment(self.db, self.payload(allocation_mode='invoice', target_invoice_id=invoice['id']))
        for revision in (0, True, None):
            with self.assertRaises(accounting.ConflictError):
                payments.approve_payment(self.db, draft['id'], {'revision': revision})
        self.db.execute("UPDATE account_mappings SET status='proposed' WHERE role='payable_control'")
        with self.assertRaises(ValueError):
            payments.approve_payment(self.db, draft['id'], {'revision': 1})
        self.assertEqual(payments.get_payment(self.db, draft['id'])['status'], 'draft')

    def test_duplicate_cheque_including_drafts_and_separate_clearance(self):
        invoice = self.invoice()
        data = self.payload('cheque', allocation_mode='invoice', target_invoice_id=invoice['id'])
        draft = payments.save_payment(self.db, data)
        with self.assertRaises(ValueError):
            payments.save_payment(self.db, {**data, 'request_key': 'another-key'})
        bank_before = payments.account_balance(self.db, self.bank)
        posted = payments.approve_payment(self.db, draft['id'], {'revision': 1})
        self.assertEqual(posted['cheque_status'], 'issued')
        self.assertEqual(payments.account_balance(self.db, self.bank), bank_before)
        self.assertEqual(payments.account_balance(self.db, self.cheques), -3000)
        clearance = {'revision': posted['revision'], 'clearance_date': '2026-10-21', 'bank_account_id': self.bank, 'reference': 'CLEAR-1'}
        cleared = payments.clear_cheque(self.db, posted['id'], clearance)
        self.assertEqual(cleared['cheque_status'], 'cleared')
        self.assertEqual(payments.account_balance(self.db, self.bank), bank_before - 3000)
        self.assertEqual(payments.account_balance(self.db, self.cheques), 0)
        self.assert_balanced(cleared['clearance_journal'])
        self.assertEqual(payments.clear_cheque(self.db, posted['id'], clearance)['clearance_journal_entry_id'], cleared['clearance_journal_entry_id'])
        with self.assertRaises(accounting.ConflictError):
            payments.clear_cheque(self.db, posted['id'], {**clearance, 'reference': 'DIFFERENT'})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM journal_entries WHERE source_type='supplier_cheque_clearance'").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE supplier_payments SET clearance_reference=? WHERE id=?', ('tampered', posted['id']))

    def test_cheque_liability_cannot_be_the_supplier_control_account(self):
        invoice = self.invoice()
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE role='issued_cheques'", (self.ap,))
        draft = payments.save_payment(self.db, self.payload('cheque', allocation_mode='invoice', target_invoice_id=invoice['id']))
        with self.assertRaises(ValueError):
            payments.approve_payment(self.db, draft['id'], {'revision': 1})
        self.assertEqual(payables.invoice_outstanding(self.db, invoice['id']), 10000)

    def test_cheque_cancel_frees_number_and_invalid_dates_or_clearance_rejected(self):
        invoice = self.invoice()
        data = self.payload('cheque', allocation_mode='invoice', target_invoice_id=invoice['id'])
        draft = payments.save_payment(self.db, data)
        with self.assertRaises(ValueError):
            payments.clear_cheque(self.db, draft['id'], {})
        payments.update_payment(self.db, draft['id'], {'revision': 1, 'action': 'cancel'})
        new = payments.save_payment(self.db, {**data, 'request_key': 'replacement'})
        result = payments.approve_payment(self.db, new['id'], {'revision': 1})
        with self.assertRaises(accounting.ConflictError):
            payments.clear_cheque(self.db, result['id'], {'revision': 1, 'clearance_date': '2026-10-21', 'bank_account_id': self.bank, 'reference': 'x'})
        with self.assertRaises(ValueError):
            payments.clear_cheque(self.db, result['id'], {'revision': result['revision'], 'clearance_date': '2026-10-10', 'bank_account_id': self.bank, 'reference': 'x'})

    def test_wrong_supplier_duplicate_or_excess_allocations_rejected(self):
        invoice = self.invoice()
        foreign = self.invoice(supplier_id=2)
        for lines in ([{'invoice_id': foreign['id'], 'amount': '30.00'}],
                      [{'invoice_id': invoice['id'], 'amount': '101.00'}],
                      [{'invoice_id': invoice['id'], 'amount': '30.00'}, {'invoice_id': invoice['id'], 'amount': '1.00'}],
                      [{'invoice_id': invoice['id'], 'amount': '31.00'}]):
            with self.assertRaises(ValueError):
                payments.save_payment(self.db, self.payload(allocations=lines))

    def test_saved_draft_rechecks_outstanding_after_another_payment(self):
        invoice = self.invoice()
        first = payments.save_payment(self.db, self.payload(amount='70.00', allocations=[{'invoice_id': invoice['id'], 'amount': '70.00'}]))
        second = self.post(self.payload(amount='60.00', allocation_mode='invoice', target_invoice_id=invoice['id']))
        self.assertEqual(second['allocated_cents'], 6000)
        with self.assertRaises(ValueError):
            payments.approve_payment(self.db, first['id'], {'revision': 1})
        self.assertEqual(payables.invoice_outstanding(self.db, invoice['id']), 4000)

    def test_return_credit_is_not_silently_spent_again(self):
        settled = self.invoice()
        self.post(self.payload(amount='100.00', allocation_mode='invoice', target_invoice_id=settled['id']))
        other = self.invoice()
        self.returned(settled, quantity='0.2')
        data = self.payload(amount='100.00', payment_date='2026-10-21', allocation_mode='fifo')
        preview = payments.allocation_preview(self.db, data)
        self.assertEqual(preview['unapplied_return_credit_cents'], 2000)
        self.assertEqual(preview['net_payable_cents'], 8000)
        self.assertEqual(preview['allocated_cents'], 8000)
        self.assertEqual(preview['advance_cents'], 2000)
        with self.assertRaises(ValueError):
            payments.save_payment(self.db, self.payload(amount='100.00', payment_date='2026-10-21',
                allocations=[{'invoice_id': other['id'], 'amount': '100.00'}]))

    def test_historical_outstanding_and_future_approved_reservations(self):
        invoice = self.invoice()
        self.post(self.payload(amount='70.00', allocation_mode='invoice', target_invoice_id=invoice['id'], payment_date='2026-11-01'))
        self.assertEqual(payables.invoice_outstanding(self.db, invoice['id'], '2026-10-25'), 10000)
        self.assertEqual(payables.invoice_outstanding(self.db, invoice['id'], '2026-11-02'), 3000)
        with self.assertRaises(ValueError):
            payments.save_payment(self.db, self.payload(amount='50.00', payment_date='2026-10-20', allocations=[{'invoice_id': invoice['id'], 'amount': '50.00'}]))
        preview = payments.allocation_preview(self.db, self.payload(amount='50.00', payment_date='2026-10-20', allocation_mode='invoice', target_invoice_id=invoice['id']))
        self.assertEqual(preview['allocated_cents'], 3000)
        self.assertEqual(preview['advance_cents'], 2000)

    def test_signed_funding_preview_warns_without_inventing_openings(self):
        invoice = self.invoice()
        preview = payments.allocation_preview(self.db, self.payload(allocation_mode='invoice', target_invoice_id=invoice['id']))
        self.assertEqual(preview['funding_balance_before_cents'], 0)
        self.assertEqual(preview['funding_balance_after_cents'], -3000)
        self.assertTrue(preview['warnings'])
        self.assertEqual(payments.account_balance(self.db, self.cash), 0)
        self.assertTrue(any(row['id'] == self.cash and row['balance_cents'] == 0 for row in payments.funding_balances(self.db)))

    def test_posted_financial_data_and_allocations_are_immutable(self):
        invoice = self.invoice()
        posted = self.post(self.payload(allocation_mode='invoice', target_invoice_id=invoice['id']))
        for sql, args in [('UPDATE supplier_payments SET amount_cents=1 WHERE id=?', (posted['id'],)),
                          ('DELETE FROM supplier_payments WHERE id=?', (posted['id'],)),
                          ('UPDATE payment_allocations SET amount_cents=1 WHERE payment_id=?', (posted['id'],)),
                          ('DELETE FROM payment_allocations WHERE payment_id=?', (posted['id'],))]:
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql, args)
        self.assertEqual(payments.get_payment(self.db, posted['id'])['amount_cents'], 3000)

    def test_caller_transaction_can_rollback_save_and_approval(self):
        invoice = self.invoice()
        self.db.commit()
        self.db.execute('BEGIN IMMEDIATE')
        result = self.post(self.payload(allocation_mode='invoice', target_invoice_id=invoice['id']))
        self.assertEqual(result['status'], 'posted')
        self.db.rollback()
        self.assertEqual(payments.list_payments(self.db), [])
        self.assertEqual(payables.invoice_outstanding(self.db, invoice['id']), 10000)


if __name__ == '__main__':
    unittest.main(verbosity=2)
