"""Financial invariants on an isolated legacy-schema database."""
import sqlite3
import sys
import unittest
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting
import payables


class PayablesAccounting(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE sequences(kind TEXT PRIMARY KEY,value INTEGER NOT NULL);
        CREATE TABLE suppliers(id INTEGER PRIMARY KEY,code TEXT UNIQUE,name TEXT,phone TEXT DEFAULT '',tax_number TEXT DEFAULT '',notes TEXT DEFAULT '',created_at TEXT);
        CREATE TABLE departments(id INTEGER PRIMARY KEY,code TEXT,name TEXT,cost_code TEXT);
        CREATE TABLE products(id INTEGER PRIMARY KEY,code TEXT,supplier_id INTEGER REFERENCES suppliers(id),name TEXT,unit TEXT,price_cents INTEGER,department_id INTEGER REFERENCES departments(id));
        CREATE TABLE invoices(id INTEGER PRIMARY KEY,code TEXT UNIQUE,request_key TEXT UNIQUE,supplier_id INTEGER REFERENCES suppliers(id),reference TEXT,business_date TEXT,department_id INTEGER REFERENCES departments(id),notes TEXT,status TEXT CHECK(status IN('draft','cancelled')),subtotal_cents INTEGER,tax_basis INTEGER,tax_cents INTEGER,total_cents INTEGER,revision INTEGER DEFAULT 1,created_at TEXT,updated_at TEXT,kind TEXT DEFAULT 'expense');
        CREATE TABLE invoice_lines(id INTEGER PRIMARY KEY,invoice_id INTEGER REFERENCES invoices(id),product_id INTEGER REFERENCES products(id),name TEXT,unit TEXT,quantity TEXT,price_cents INTEGER,total_cents INTEGER);
        CREATE TABLE audit(id INTEGER PRIMARY KEY,entity TEXT,entity_id INTEGER,action TEXT,details TEXT,created_at TEXT);
        INSERT INTO suppliers(id,code,name,created_at) VALUES(1,'SUP-1','مورد أغذية ومشروبات','2026-01-01'),(2,'SUP-2','مورد آخر','2026-01-01');
        INSERT INTO departments(id,code,name,cost_code) VALUES(1,'DEP-1','المطبخ','KIT'),(2,'DEP-2','المطعم','REST');
        INSERT INTO products(id,code,supplier_id,name,unit,price_cents,department_id) VALUES(1,'P-1',1,'أرز','كجم',900,1),(2,'P-2',1,'عصير','عبوة',500,2);
        ''')
        accounting.initialize(self.db)
        payables.initialize(self.db)
        self.expense = self.db.execute("SELECT id FROM accounts WHERE posting=1 AND normal_side='debit' AND code LIKE '5%' LIMIT 1").fetchone()[0]
        self.alternate = self.db.execute("SELECT id FROM accounts WHERE posting=1 AND normal_side='debit' AND code LIKE '6%' LIMIT 1").fetchone()[0]
        self.payable = self.db.execute("SELECT account_id FROM account_mappings WHERE module='الموردون' AND role='payable_control'").fetchone()[0]
        self.funding = self.db.execute("SELECT id FROM accounts WHERE code='111002'").fetchone()[0]
        self.advance = self.db.execute("SELECT id FROM accounts WHERE code='115004'").fetchone()[0]
        self.db.execute("UPDATE account_mappings SET status='confirmed' WHERE module='الموردون' AND role='payable_control'")
        payables.configure_product(self.db, 1, {'expense_account_id': self.expense})
        payables.configure_product(self.db, 2, {'expense_account_id': self.alternate})
        self.db.commit()
        self.n = 0

    def tearDown(self):
        self.db.close()

    def make_invoice(self, price=1000, qty='10', business_date='2026-01-01', due='2026-01-31', product=1, approve=True):
        self.n += 1
        total = int((Decimal(qty) * price).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        ident = self.db.execute("INSERT INTO invoices(code,request_key,supplier_id,reference,business_date,department_id,notes,status,subtotal_cents,tax_basis,tax_cents,total_cents,created_at,updated_at) VALUES(?,?,?,?,?,1,'','draft',?,0,0,?,'now','now')", (f'PIN-{self.n}', f'key-{self.n}', 1, f'ref-{self.n}', business_date, total, total)).lastrowid
        self.db.execute('INSERT INTO invoice_lines(invoice_id,product_id,name,unit,quantity,price_cents,total_cents) VALUES(?,?,?,?,?,?,?)', (ident, product, 'الصنف', 'كجم', qty, price, total))
        payables.set_invoice_terms(self.db, ident, {'due_date': due})
        if approve:
            return payables.post_invoice(self.db, ident, {'revision': 1})
        return payables.enrich_invoice(self.db, dict(self.db.execute('SELECT * FROM invoices WHERE id=?', (ident,)).fetchone()))

    def make_return(self, invoice, quantity, business_date='2026-02-01', approve=True):
        self.n += 1
        draft = payables.save_return(self.db, {'invoice_id': invoice['id'], 'reference': f'RET-{self.n}', 'business_date': business_date, 'lines': [{'invoice_line_id': invoice['lines'][0]['id'], 'quantity': quantity}]})
        return payables.approve_return(self.db, draft['id'], {'revision': draft['revision']}) if approve else draft

    def payment_fixture(self, invoice, cents, payment_date='2026-01-15', advance=0):
        self.db.execute('CREATE TABLE IF NOT EXISTS supplier_payments(id INTEGER PRIMARY KEY,code TEXT,supplier_id INTEGER,status TEXT,payment_date TEXT,amount_cents INTEGER)')
        self.db.execute('CREATE TABLE IF NOT EXISTS payment_allocations(payment_id INTEGER,invoice_id INTEGER,amount_cents INTEGER)')
        payment_id = self.db.execute("INSERT INTO supplier_payments(code,supplier_id,status,payment_date,amount_cents) VALUES('SPAY',1,'posted',?,?)", (payment_date, cents + advance)).lastrowid
        if cents:
            self.db.execute('INSERT INTO payment_allocations VALUES(?,?,?)', (payment_id, invoice['id'], cents))
        lines = [{'account_id': self.funding, 'credit_cents': cents + advance, 'supplier_id': 1}]
        if cents:
            lines.append({'account_id': self.payable, 'debit_cents': cents, 'supplier_id': 1})
        if advance:
            lines.append({'account_id': self.advance, 'debit_cents': advance, 'supplier_id': 1})
        payables.create_journal(self.db, 'supplier_payment', payment_id, payment_date, lines)
        return payment_id

    def test_draft_has_no_journal_and_approval_is_balanced_immutable_idempotent(self):
        with self.db:
            draft = self.make_invoice(approve=False)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT price_cents FROM products WHERE id=1').fetchone()[0], 900)
        with self.db:
            posted = payables.post_invoice(self.db, draft['id'], {'revision': 1})
            repeat = payables.post_invoice(self.db, draft['id'], {'revision': 1})
        self.assertEqual(posted['journal_id'], repeat['journal_id'])
        self.assertEqual(posted['status'], 'posted')
        self.assertEqual(posted['revision'], 2)
        self.assertEqual(self.db.execute('SELECT status FROM invoices WHERE id=?', (posted['id'],)).fetchone()[0], 'draft')
        self.assertEqual(tuple(self.db.execute('SELECT SUM(debit_cents),SUM(credit_cents) FROM journal_lines').fetchone()), (10000, 10000))
        with self.assertRaises(ValueError):
            payables.assert_invoice_editable(self.db, posted['id'])
        for sql, args in [('UPDATE invoices SET total_cents=1 WHERE id=?', (posted['id'],)), ('DELETE FROM invoice_lines WHERE invoice_id=?', (posted['id'],)), ('UPDATE journal_lines SET debit_cents=1 WHERE journal_id=?', (posted['journal_id'],)), ('INSERT INTO invoice_lines(invoice_id,product_id) VALUES(?,1)', (posted['id'],))]:
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql, args)

    def test_due_date_expense_mapping_tax_and_revision_are_approval_prerequisites(self):
        with self.db:
            draft = self.make_invoice(due=None, approve=False)
        with self.assertRaisesRegex(ValueError, 'الاستحقاق'):
            with self.db:
                payables.post_invoice(self.db, draft['id'], {'revision': 1})
        with self.assertRaises(ValueError):
            payables.set_invoice_terms(self.db, draft['id'], {'due_date': '2025-12-31'})
        with self.db:
            payables.set_invoice_terms(self.db, draft['id'], {'due_date': '2026-01-31'})
            self.db.execute('UPDATE products SET expense_account_id=NULL WHERE id=1')
        with self.assertRaisesRegex(ValueError, 'للصنف'):
            with self.db:
                payables.post_invoice(self.db, draft['id'], {'revision': 1})
        with self.db:
            payables.configure_product(self.db, 1, {'expense_account_id': self.expense})
            self.db.execute("UPDATE account_mappings SET status='proposed' WHERE role='payable_control'")
        with self.assertRaisesRegex(ValueError, 'تأكيد'):
            with self.db:
                payables.post_invoice(self.db, draft['id'], {'revision': 1})
        with self.db:
            self.db.execute("UPDATE account_mappings SET status='confirmed' WHERE role='payable_control'")
            self.db.execute('UPDATE invoices SET tax_cents=1 WHERE id=?', (draft['id'],))
        with self.assertRaisesRegex(ValueError, 'الضريبية'):
            with self.db:
                payables.post_invoice(self.db, draft['id'], {'revision': 1})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], 0)
        self.db.execute('UPDATE invoices SET tax_cents=0 WHERE id=?', (draft['id'],))
        with self.assertRaises(accounting.ConflictError):
            payables.post_invoice(self.db, draft['id'], {'revision': 0})

    def test_last_purchase_price_uses_posted_business_chronology_and_variance_snapshot(self):
        with self.db:
            first = self.make_invoice(price=1000, business_date='2026-01-01')
            newer = self.make_invoice(price=1200, business_date='2026-03-01', due='2026-03-31')
            self.make_invoice(price=7777, business_date='2026-04-01', due='2026-04-30', approve=False)
            self.make_invoice(price=1100, business_date='2026-02-01', due='2026-02-28')
        product = payables.enrich_product(self.db, dict(self.db.execute('SELECT * FROM products WHERE id=1').fetchone()))
        self.assertEqual((product['price_cents'], product['last_purchase_price_cents'], product['last_purchase_date']), (1200, 1200, '2026-03-01'))
        line = payables.enrich_invoice(self.db, newer)['lines'][0]
        self.assertEqual((line['previous_price_cents'], line['price_difference_cents']), (1000, 200))
        with self.db:
            self.make_return(first, '2', business_date='2026-04-02')
        self.assertEqual(self.db.execute('SELECT price_cents FROM products WHERE id=1').fetchone()[0], 1200)

    def test_return_uses_original_price_account_department_and_cumulative_quantity_caps(self):
        with self.db:
            invoice = self.make_invoice(price=1000)
            payables.configure_product(self.db, 1, {'expense_account_id': self.alternate})
            self.db.execute('UPDATE products SET department_id=2 WHERE id=1')
            first = self.make_return(invoice, '4')
        self.assertEqual(first['total_cents'], 4000)
        credited = self.db.execute('SELECT account_id,department_id,credit_cents FROM journal_lines WHERE journal_id=? AND credit_cents>0', (first['journal_id'],)).fetchone()
        self.assertEqual(tuple(credited), (self.expense, 1, 4000))
        with self.assertRaisesRegex(ValueError, 'كمية'):
            payables.save_return(self.db, {'invoice_id': invoice['id'], 'reference': 'excess', 'business_date': '2026-02-01', 'lines': [{'invoice_line_id': invoice['lines'][0]['id'], 'quantity': '6.001'}]})
        with self.db:
            final = self.make_return(invoice, '6')
        detail = payables.enrich_invoice(self.db, invoice)
        self.assertEqual(detail['lines'][0]['returnable_quantity'], '0')
        self.assertEqual(payables.invoice_outstanding(self.db, invoice['id'], '2026-02-01'), 0)
        self.assertEqual(payables.approve_return(self.db, final['id'], {'revision': 0})['journal_id'], final['journal_id'])
        with self.assertRaises(ValueError):
            payables.save_return(self.db, {**final, 'revision': final['revision']})

    def test_competing_return_drafts_revalidate_on_approval_and_round_to_original_cents(self):
        with self.db:
            invoice = self.make_invoice(price=3, qty='0.5')  # 1.5 cents rounds to two.
            a = self.make_return(invoice, '0.25', approve=False)
            b = self.make_return(invoice, '0.5', approve=False)
            a = payables.approve_return(self.db, a['id'], {'revision': 1})
        self.assertEqual(a['total_cents'], 1)
        with self.assertRaisesRegex(ValueError, 'كمية'):
            with self.db:
                payables.approve_return(self.db, b['id'], {'revision': 1})
        with self.db:
            b = payables.save_return(self.db, {**b, 'lines': [{'invoice_line_id': invoice['lines'][0]['id'], 'quantity': '0.25'}]})
            b = payables.approve_return(self.db, b['id'], {'revision': b['revision']})
        self.assertEqual(b['total_cents'], 1)
        self.assertEqual(self.db.execute("SELECT SUM(total_cents) FROM supplier_returns WHERE status='posted'").fetchone()[0], 2)

    def test_repeated_fractional_returns_reconcile_quantity_and_rounding_without_zero_journals(self):
        with self.db:
            invoice = self.make_invoice(price=1, qty='3')
            returns = [self.make_return(invoice, '0.5') for _ in range(6)]
        self.assertEqual([row['total_cents'] for row in returns], [1, 0, 1, 0, 1, 0])
        self.assertEqual(sum(row['total_cents'] for row in returns), invoice['total_cents'])
        self.assertEqual(payables.enrich_invoice(self.db, invoice)['lines'][0]['returnable_quantity'], '0')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], 4)
        self.assertIsNone(returns[-1]['journal_id'])

    def test_aging_uses_due_date_boundaries_and_excludes_later_documents(self):
        due_dates = ['2026-06-01', '2026-05-31', '2026-05-02', '2026-05-01', '2026-04-02', '2026-04-01', '2026-03-03', '2026-03-02']
        with self.db:
            invoices = [self.make_invoice(business_date='2026-01-01', due=due) for due in due_dates]
            self.make_invoice(business_date='2026-06-02', due='2026-06-02')
            self.payment_fixture(invoices[0], 2000, payment_date='2026-06-02')
            self.make_return(invoices[0], '1', business_date='2026-06-02')
        historical = payables.aging(self.db, '2026-06-01')
        self.assertEqual([historical['totals'][key] for key in payables.BUCKETS], [10000, 20000, 20000, 20000, 10000])
        self.assertEqual(historical['totals']['net_payable_cents'], 80000)
        current = payables.aging(self.db, '2026-06-02')
        self.assertEqual(current['totals']['net_payable_cents'], 87000)

    def test_return_after_payment_preserves_supplier_credit_and_statement_reconciles(self):
        with self.db:
            invoice = self.make_invoice(price=10000)
            self.payment_fixture(invoice, 100000, advance=20000)
            self.make_return(invoice, '4')
        report = payables.aging(self.db, '2026-02-01')
        self.assertEqual(report['totals']['total_cents'], 0)
        self.assertEqual(report['totals']['credit_cents'], 40000)
        self.assertEqual(report['totals']['payable_balance_cents'], -40000)
        self.assertEqual(report['totals']['net_payable_cents'], -60000)
        self.assertEqual(report['totals']['advance_cents'], 20000)
        statement = payables.statement(self.db, 1, '2026-02-01')
        self.assertEqual(statement['totals']['balance_cents'], -40000)
        self.assertEqual(statement['totals']['net_payable_cents'], -60000)
        self.assertEqual(statement['totals']['advance_cents'], 20000)
        self.assertEqual(payables.statement(self.db, 1, '2026-01-14')['totals']['balance_cents'], 100000)

    def test_existing_advances_reduce_net_cash_due_without_silently_allocating(self):
        with self.db:
            invoice = self.make_invoice(price=5000)
            self.payment_fixture(invoice, 0, advance=10000)
        balances = payables.supplier_balances(self.db, 1, '2026-01-31')
        self.assertEqual(balances['payable_balance_cents'], 50000)
        self.assertEqual(balances['advance_cents'], 10000)
        self.assertEqual(balances['net_payable_cents'], 40000)
        # The advance is still a separate receivable; no fabricated AP application.
        self.assertEqual(payables.invoice_outstanding(self.db, invoice['id'], '2026-01-31'), 50000)
        statement = payables.statement(self.db, 1, '2026-01-31')
        self.assertEqual(statement['totals']['balance_cents'], 50000)
        self.assertEqual(statement['rows'][-1]['advance_cents'], 10000)
        report = payables.aging(self.db, '2026-01-31')
        self.assertEqual(report['totals']['net_payable_cents'], 40000)
        self.assertEqual(payables.journal_detail(self.db, invoice['journal_id'])['debit_cents'], 50000)

    def test_many_supply_categories_and_optional_credit_terms_are_validated(self):
        categories = payables.master_data(self.db)['categories']
        ids = [category['id'] for category in categories[:2]]
        with self.db:
            supplier = payables.configure_supplier(self.db, 1, {'category_ids': ids, 'terms_days': 30})
            product = payables.configure_product(self.db, 1, {'category_id': ids[0]})
            invoice = self.make_invoice(due=None, approve=False)
            payables.set_invoice_terms(self.db, invoice['id'], {})
        self.assertEqual(supplier['category_ids'], ids)
        self.assertEqual(product['category_id'], ids[0])
        self.assertEqual(payables.enrich_invoice(self.db, invoice)['due_date'], '2026-01-31')
        with self.assertRaisesRegex(ValueError, 'إزالة'):
            payables.configure_supplier(self.db, 1, {'category_ids': [ids[1]]})
        with self.assertRaisesRegex(ValueError, 'أنواع'):
            payables.configure_product(self.db, 2, {'category_id': categories[2]['id']})
        with self.assertRaises(ValueError):
            payables.configure_supplier(self.db, 1, {'terms_days': True})
        with self.assertRaises(ValueError):
            payables.configure_product(self.db, 1, {'expense_account_id': self.payable})

    def test_journal_rejects_unbalanced_and_changed_retry_and_rolls_back_as_unit(self):
        lines = [{'account_id': self.expense, 'debit_cents': 100}, {'account_id': self.payable, 'credit_cents': 100}]
        with self.db:
            journal = payables.create_journal(self.db, 'test', 1, '2026-01-01', lines)
            self.assertEqual(journal, payables.create_journal(self.db, 'test', 1, '2026-01-01', list(reversed(lines))))
        with self.assertRaises(accounting.ConflictError):
            payables.create_journal(self.db, 'test', 1, '2026-01-02', lines)
        with self.assertRaises(ValueError):
            payables.create_journal(self.db, 'test', 2, '2026-01-01', [lines[0], {'account_id': self.payable, 'credit_cents': 99}])
        before = self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0]
        with self.assertRaises(RuntimeError):
            with self.db:
                payables.create_journal(self.db, 'test', 3, '2026-01-01', lines)
                raise RuntimeError('simulate failure after journal')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
