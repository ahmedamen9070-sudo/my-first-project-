"""Opening subledgers remain dated, usable and independently matched to GL."""
import sqlite3
import unittest

import test_supplier_payments as fixtures
import accounting_periods
import custodies
import opening_balances
import opening_controls
import payables
import supplier_adjustments
import supplier_payments
import supplier_reconciliation
import treasury
import withholding
import withholding_balances
import withholding_remittances


class OpeningControls(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SupplierPayments('runTest')
        self.fixture.setUp()
        self.db = self.fixture.db
        accounting_periods.initialize(self.db)
        custodies.initialize(self.db)
        treasury.initialize(self.db)
        supplier_adjustments.initialize(self.db)
        withholding_remittances.initialize(self.db)
        opening_balances.initialize(self.db)
        self.db.execute("UPDATE account_mappings SET status='confirmed'")
        self.person = custodies.save_custodian(self.db, {'name': 'أمين عهدة افتتاحية'})
        self.custody_expense = custodies.config(self.db)['expense_accounts'][0]['id']
        self.serial = 0

    def tearDown(self):
        self.fixture.tearDown()

    def key(self):
        self.serial += 1
        return 'opening-control-' + str(self.serial)

    def batch(self, lines, post=True):
        lines = [dict(row) for row in lines]
        sides = {'custody': 'debit', 'withholding': 'credit', 'supplier_invoice': 'credit'}
        difference = sum(int(float(row['amount']) * 100) * (1 if row.get('side', sides.get(row['kind'])) == 'debit' else -1) for row in lines)
        if difference:
            lines.append({'kind': 'general', 'account_id': self.fixture.account('300001'),
                          'side': 'credit' if difference > 0 else 'debit', 'amount_cents': abs(difference)})
        doc = opening_balances.save(self.db, {'request_key': self.key(), 'business_date': '2026-01-01',
            'reference': self.key(), 'lines': lines})
        if post:
            doc = opening_balances.approve(self.db, doc['id'], {'revision': doc['revision']})
        return doc

    def custody_line(self, **changes):
        return {'kind': 'custody', 'custodian_id': self.person['id'], 'amount': '150',
                'reference': 'CUSTODY-2025', 'source_date': '2025-12-01', **changes}

    def wh_line(self, **changes):
        return {'kind': 'withholding', 'supplier_id': 1, 'amount': '30',
                'reference': 'WH-2025', 'source_date': '2025-12-15', **changes}

    def opening_source(self):
        return dict(self.db.execute('SELECT * FROM opening_withholding_sources').fetchone())

    def remit_data(self, source, amount='10', day='2026-01-10', **changes):
        return {'request_key': self.key(), 'business_date': day, 'period_from': '2025-01-01',
                'period_to': day, 'authority_name': 'الجهة الضريبية', 'reference': self.key(),
                'method': 'cash', 'funding_account_id': self.fixture.cash,
                'details': {'received_by': 'مندوب الجهة'},
                'allocations': [{'source_type': 'opening_withholding', 'source_id': source['id'], 'amount': amount}], **changes}

    def remit(self, data):
        doc = withholding_remittances.save(self.db, data)
        return withholding_remittances.approve(self.db, doc['id'], {'revision': doc['revision']})

    def reverse_remit(self, doc, day='2026-01-25'):
        return withholding_remittances.reverse(self.db, doc['id'], {'revision': doc['revision'], 'event_revision': 0,
            'request_key': self.key(), 'business_date': day, 'reference': self.key(), 'reason': 'تصحيح التسجيل'})

    def custody_post(self, kind, source, amount, day):
        data = {'request_key': self.key(), 'kind': kind, 'source_issue_id': source['id'],
                'business_date': day, 'reference': self.key()}
        if kind == 'settlement':
            data['lines'] = [{'expense_account_id': self.custody_expense, 'department_id': 1,
                              'amount': amount, 'reference': self.key()}]
        else:
            data.update(cash_account_id=self.fixture.cash, amount=amount)
        doc = custodies.save(self.db, data)
        return custodies.approve(self.db, doc['id'], {'revision': doc['revision']})

    def test_opening_custody_is_no_cash_issue_and_partial_settlement_and_return_match(self):
        self.batch([self.custody_line()])
        source = custodies.list_documents(self.db)[0]
        self.assertTrue(source['is_opening'])
        self.assertIsNone(source['cash_account_id'])
        self.assertEqual(source['journal']['source_type'], 'opening_balance')
        self.assertNotIn(self.fixture.cash, [row['account_id'] for row in source['journal']['lines']])
        self.assertEqual(custodies.balance_report(self.db, '2025-12-31')['totals']['remaining_cents'], 0)
        self.assertEqual(custodies.balance_report(self.db, '2026-01-01')['totals']['opening_cents'], 15000)
        self.custody_post('settlement', source, '60', '2026-01-02')
        self.custody_post('return', source, '90', '2026-01-03')
        report = custodies.balance_report(self.db, '2026-01-03')
        self.assertTrue(report['matched'], report['exceptions'])
        self.assertEqual((report['totals']['issued_cents'], report['totals']['remaining_cents']), (0, 0))
        self.assertEqual(supplier_payments.account_balance(self.db, self.fixture.cash), 9000)
        with self.assertRaisesRegex(ValueError, 'الافتتاح'):
            custodies.reverse(self.db, source['id'], {})

    def test_opening_custody_capacity_includes_future_consumption_and_dated_reversal(self):
        self.batch([self.custody_line(amount='100')])
        source = custodies.list_documents(self.db)[0]
        child = self.custody_post('settlement', source, '80', '2026-01-20')
        custodies.reverse(self.db, child['id'], {'revision': child['revision'], 'event_revision': 0,
            'request_key': self.key(), 'business_date': '2026-01-25', 'reference': self.key(), 'reason': 'تصحيح'})
        self.assertEqual(custodies.capacity(self.db, source['id'], '2026-01-10'), 2000)
        with self.assertRaisesRegex(ValueError, 'المتاح'):
            self.custody_post('return', source, '30', '2026-01-10')
        self.custody_post('return', source, '20', '2026-01-10')
        self.assertEqual(custodies.balance_report(self.db, '2026-01-20')['totals']['remaining_cents'], 0)
        self.assertEqual(custodies.balance_report(self.db, '2026-01-25')['totals']['remaining_cents'], 8000)

    def test_opening_withholding_is_prior_source_not_payment_or_certificate(self):
        self.batch([self.wh_line()])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM supplier_payments').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM payment_withholding').fetchone()[0], 0)
        self.assertEqual(withholding_balances.balance_report(self.db, '2025-12-31')['sources'], [])
        report = withholding_balances.balance_report(self.db, '2026-01-01', '2025-12-01', '2025-12-31')
        self.assertTrue(report['matched'], report['exceptions'])
        source = report['sources'][0]
        self.assertEqual((source['source_type'], source['payment_id'], source['rate'], source['remaining_cents']),
                         ('opening_withholding', None, None, 3000))
        self.assertEqual(withholding_balances.balance_report(self.db, '2026-01-31', '2026-01-01', '2026-01-31')['sources'], [])

    def test_withholding_source_requires_actual_prior_date_and_reference(self):
        for changes in ({'source_date': None}, {'source_date': '2026-01-02'}, {'reference': ''}, {'side': 'debit'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.batch([self.wh_line(**changes)])
        with self.assertRaises(ValueError):
            self.batch([self.custody_line(source_date='2026-01-02')])

    def test_partial_opening_remittance_has_dedicated_allocations_and_dated_reversal(self):
        self.batch([self.wh_line()])
        source = self.opening_source()
        original_ap = payables.supplier_balances(self.db, 1)
        doc = self.remit(self.remit_data(source))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM withholding_remittance_allocations').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM opening_remittance_allocations').fetchone()[0], 1)
        self.assertIsNone(doc['allocations'][0]['rate'])
        self.assertEqual(doc['allocations'][0]['source_id'], source['id'])
        self.assertEqual(payables.supplier_balances(self.db, 1), original_ap)
        self.reverse_remit(doc)
        for day, expected in [('2026-01-09', 3000), ('2026-01-10', 2000), ('2026-01-25', 3000)]:
            report = withholding_balances.balance_report(self.db, day)
            self.assertTrue(report['matched'], report['exceptions'])
            self.assertEqual(report['all_totals']['remaining_cents'], expected)

    def test_future_remittance_capacity_prevents_intermediate_overpayment(self):
        self.batch([self.wh_line()])
        source = self.opening_source()
        future = self.remit(self.remit_data(source, '20', '2026-01-20'))
        self.reverse_remit(future)
        self.assertEqual(withholding_balances.opening_capacity(self.db, source['id'], '2026-01-10'), 1000)
        with self.assertRaisesRegex(ValueError, 'المتاح'):
            self.remit(self.remit_data(source, '11'))
        self.remit(self.remit_data(source, '10'))
        self.assertTrue(withholding_balances.balance_report(self.db, '2026-01-20')['matched'])

    def test_mixed_opening_and_current_sources_with_same_id_are_independent(self):
        self.batch([self.wh_line()])
        source = self.opening_source()
        code = withholding.create_code(self.db, {'name': 'خصم تشغيل', 'rate': '3', 'active': True, 'confirmed': True})
        invoice = self.fixture.invoice()
        payment = self.fixture.post(self.fixture.payload(amount='100', allocation_mode='invoice',
            target_invoice_id=invoice['id'], withholding_code_id=code['id']))
        self.assertEqual(payment['id'], source['id'])
        doc = self.remit(self.remit_data(source, day='2026-10-20', allocations=[
            {'payment_id': payment['id'], 'amount': '1'},
            {'source_type': 'opening_withholding', 'source_id': source['id'], 'amount': '10'}]))
        self.assertEqual(doc['amount_cents'], 1100)
        report = withholding_balances.balance_report(self.db, '2026-10-20')
        self.assertTrue(report['matched'], report['exceptions'])
        self.assertEqual({row['source_type']: row['remaining_cents'] for row in report['sources']},
                         {'opening_withholding': 2000, 'payment_withholding': 200})
        self.assertTrue(supplier_reconciliation.report(self.db)['matched'])

    def test_posted_sources_and_allocations_are_immutable_and_locks_apply(self):
        self.batch([self.wh_line()])
        source = self.opening_source()
        doc = self.remit(self.remit_data(source))
        for sql in ('UPDATE opening_withholding_sources SET amount_cents=1',
                    'DELETE FROM opening_withholding_sources',
                    'UPDATE opening_remittance_allocations SET amount_cents=1',
                    'DELETE FROM opening_remittance_allocations'):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql)
        self.db.execute("INSERT INTO accounting_periods(month,status,updated_at) VALUES('2026-02','closed','test')")
        with self.assertRaisesRegex(ValueError, 'مقفولة'):
            self.remit(self.remit_data(source, '1', '2026-02-01'))
        with self.assertRaisesRegex(ValueError, 'مقفولة'):
            self.reverse_remit(doc, '2026-02-01')

    def test_journal_dimensions_and_opening_identity_are_checked_before_use(self):
        self.batch([self.custody_line(), self.wh_line()])
        source = custodies.list_documents(self.db)[0]
        self.db.execute('DROP TRIGGER immutable_opening_line_update')
        self.db.execute('DROP TRIGGER immutable_opening_line_parent_update')
        self.db.execute("UPDATE opening_lines SET document_id=999 WHERE kind IN('custody','withholding')")
        self.assertFalse(custodies.balance_report(self.db, '2026-01-01')['matched'])
        self.assertFalse(withholding_balances.balance_report(self.db, '2026-01-01')['matched'])
        with self.assertRaisesRegex(ValueError, 'لا يطابق'):
            self.custody_post('return', source, '1', '2026-01-02')
        draft = withholding_remittances.save(self.db, self.remit_data(self.opening_source()))
        with self.assertRaisesRegex(ValueError, 'مطابقة'):
            withholding_remittances.approve(self.db, draft['id'], {'revision': draft['revision']})

    def test_opening_payable_withholding_uses_explicit_remaining_net_and_keeps_ap_match(self):
        self.batch([{'kind': 'supplier_invoice', 'supplier_id': 1, 'amount': '100',
                    'remaining_net_amount': '80', 'reference': 'OLD-INVOICE', 'source_date': '2025-12-01', 'due_date': '2025-12-31'}])
        invoice = self.db.execute('SELECT invoice_id FROM opening_supplier_invoices').fetchone()[0]
        code = withholding.create_code(self.db, {'name': 'خصم افتتاح المورد', 'rate': '3', 'active': True, 'confirmed': True})
        payment = self.fixture.post(self.fixture.payload(amount='50', payment_date='2026-01-10', allocation_mode='invoice',
            target_invoice_id=invoice, withholding_code_id=code['id']))
        self.assertEqual(payment['withholding_cents'], 120)
        self.assertTrue(supplier_reconciliation.report(self.db)['matched'])
        self.assertTrue(withholding_balances.balance_report(self.db, '2026-01-10')['matched'])

    def test_unknown_prior_invoice_net_does_not_invent_withholding_base(self):
        self.batch([{'kind': 'supplier_invoice', 'supplier_id': 1, 'amount': '100',
                    'reference': 'OLD-INVOICE', 'source_date': '2025-12-01', 'due_date': '2025-12-31'}])
        invoice = self.db.execute('SELECT invoice_id FROM opening_supplier_invoices').fetchone()[0]
        code = withholding.create_code(self.db, {'name': 'خصم يحتاج وعاء', 'rate': '3', 'active': True, 'confirmed': True})
        with self.assertRaisesRegex(ValueError, 'صافي الوعاء'):
            self.fixture.post(self.fixture.payload(amount='50', payment_date='2026-01-10', allocation_mode='invoice',
                target_invoice_id=invoice, withholding_code_id=code['id']))
