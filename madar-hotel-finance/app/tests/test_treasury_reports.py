"""Cash source movements are read exactly once and preserve dated corrections."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import accounting
import custodies
import ledger_reports
import payables
import supplier_adjustments
import supplier_settlements
import treasury
import treasury_reports as reports
import withholding
import withholding_remittances
import test_supplier_payments as fixtures


class TreasuryReports(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SupplierPayments('runTest')
        self.f.setUp()
        self.db = self.f.db
        supplier_settlements.initialize(self.db)
        supplier_adjustments.initialize(self.db)
        withholding_remittances.initialize(self.db)
        treasury.initialize(self.db)
        custodies.initialize(self.db)
        self.serial = 0
        self.cash = self.f.cash
        self.petty = self.f.account('111003')
        self.outlet = self.f.account('111004')
        config = treasury.config(self.db)
        self.receipt_account = config['counter_accounts_by_kind']['receipt'][0]['id']
        self.expense = config['counter_accounts_by_kind']['payment'][0]['id']
        self.person = custodies.save_custodian(self.db, {'name': 'صاحب عهدة اختبار', 'phone': 'TEST'})

    def tearDown(self):
        self.f.tearDown()

    def key(self):
        self.serial += 1
        return 'cash-reports-' + str(self.serial)

    def book(self, start='2026-10-01', end='2026-10-31', account=None):
        return reports.cash_book(self.db, self.cash if account is None else account, start, end)

    def voucher_data(self, kind='receipt', amount='1000', day='2026-10-01', **extra):
        data = {'kind': kind, 'request_key': self.key(), 'business_date': day,
                'reference': self.key(), 'amount': amount, 'cash_account_id': self.cash,
                'party_name': 'طرف مستند اختبار', 'counter_account_id': self.receipt_account}
        if kind == 'payment':
            data.update(counter_account_id=self.expense, department_id=1)
        elif kind == 'transfer':
            data.pop('counter_account_id')
            data['destination_cash_account_id'] = self.petty
        return {**data, **extra}

    def voucher(self, **kw):
        saved = treasury.save(self.db, self.voucher_data(**kw))
        return treasury.approve(self.db, saved['id'], {'revision': saved['revision']})

    def custody(self, kind='issue', amount='100', day='2026-10-05', source=None, **extra):
        data = {'kind': kind, 'request_key': self.key(), 'business_date': day,
                'reference': self.key(), 'amount': amount, 'cash_account_id': self.cash}
        if kind == 'issue':
            data.update(custodian_id=self.person['id'], purpose='مشتريات تشغيلية موثقة')
        else:
            data['source_issue_id'] = source['id']
        if kind == 'settlement':
            data['lines'] = [{'expense_account_id': self.expense, 'department_id': 1,
                              'reference': self.key(), 'amount': amount}]
        saved = custodies.save(self.db, {**data, **extra})
        return custodies.approve(self.db, saved['id'], {'revision': saved['revision']})

    def reverse_data(self, doc, day='2026-10-25'):
        return {'request_key': self.key(), 'revision': doc['revision'], 'event_revision': 0,
                'business_date': day, 'reference': self.key(), 'reason': 'تصحيح اختبار مؤرخ'}

    def test_empty_position_is_read_only_with_outlet_and_without_in_transit(self):
        before = self.db.total_changes
        position = reports.position(self.db, '2026-10-31')
        self.assertEqual(position['totals']['balance_cents'], 0)
        self.assertTrue(position['complete'])
        self.assertIn('111004', {a['code'] for a in position['accounts']})
        self.assertNotIn('111005', {a['code'] for a in position['accounts']})
        self.assertEqual(self.book()['rows'], [])
        self.assertEqual(self.book()['opening_balance_cents'], 0)
        self.assertEqual(self.db.total_changes, before)

    def test_ap_cash_net_is_counted_once_without_new_treasury_document(self):
        invoice = self.f.invoice()
        code = withholding.create_code(self.db, {'name': 'خصم اختبار صريح', 'rate': '3',
                                               'active': True, 'confirmed': True})
        payment = self.f.post(self.f.payload(amount='100', allocation_mode='invoice',
                                            target_invoice_id=invoice['id'], withholding_code_id=code['id']))
        book = self.book()
        self.assertEqual((book['debit_cents'], book['credit_cents'], book['closing_balance_cents']), (0, 9700, -9700))
        self.assertEqual(len(book['rows']), 1)
        self.assertEqual(book['rows'][0]['document_code'], payment['code'])
        self.assertEqual(book['rows'][0]['party_name'], 'مورد أغذية')
        self.assertEqual(book['rows'][0]['action'], {'type': 'payment', 'id': payment['id']})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM treasury_documents').fetchone()[0], 0)

    def test_cash_tax_remittance_and_dated_reversal_are_not_supplier_payment_again(self):
        invoice = self.f.invoice()
        code = withholding.create_code(self.db, {'name': 'خصم توريد اختبار', 'rate': '3',
                                               'active': True, 'confirmed': True})
        payment = self.f.post(self.f.payload(amount='100', allocation_mode='invoice',
                                            target_invoice_id=invoice['id'], withholding_code_id=code['id']))
        data = {'request_key': self.key(), 'business_date': '2026-10-20',
                'period_from': '2026-10-01', 'period_to': '2026-10-16',
                'authority_name': 'جهة اختبار التوريد', 'reference': self.key(), 'method': 'cash',
                'funding_account_id': self.cash, 'details': {'received_by': 'مندوب الجهة'},
                'allocations': [{'payment_id': payment['id'], 'amount': '3'}]}
        draft = withholding_remittances.save(self.db, data)
        remit = withholding_remittances.approve(self.db, draft['id'], {'revision': draft['revision']})
        self.assertEqual(self.book(end='2026-10-20')['closing_balance_cents'], -10000)
        reverse = withholding_remittances.reverse(self.db, remit['id'], self.reverse_data(remit))
        book = self.book()
        self.assertEqual(book['closing_balance_cents'], -9700)
        row = next(r for r in book['rows'] if r['source_type'] == 'withholding_remittance_reversal')
        self.assertEqual(row['document_code'], reverse['reversal']['code'])
        self.assertEqual(row['party_name'], 'جهة اختبار التوريد')
        self.assertEqual(row['action'], {'type': 'remittance', 'id': remit['id']})

    def test_internal_transfer_preserves_total_and_moves_each_safe_once(self):
        receipt = self.voucher()
        transfer = self.voucher(kind='transfer', amount='200', day='2026-10-10')
        main, petty = self.book(), self.book(account=self.petty)
        self.assertEqual((main['closing_balance_cents'], petty['closing_balance_cents']), (80000, 20000))
        self.assertEqual(reports.position(self.db, '2026-10-31')['totals']['balance_cents'], 100000)
        self.assertEqual([r['document_code'] for r in main['rows']], [receipt['code'], transfer['code']])
        self.assertEqual([r['document_code'] for r in petty['rows']], [transfer['code']])
        self.assertEqual(petty['rows'][0]['action'], {'type': 'treasury_document', 'id': transfer['id']})

    def test_opening_period_and_treasury_correction_use_recorded_dates(self):
        self.voucher()
        payment = self.voucher(kind='payment', amount='250', day='2026-10-16')
        treasury.reverse(self.db, payment['id'], self.reverse_data(payment, '2026-10-22'))
        early = self.book('2026-10-16', '2026-10-21')
        late = self.book('2026-10-16', '2026-10-22')
        self.assertEqual((early['opening_balance_cents'], early['credit_cents'], early['closing_balance_cents']),
                         (100000, 25000, 75000))
        self.assertEqual(late['closing_balance_cents'], 100000)
        row = late['rows'][-1]
        self.assertRegex(row['document_code'], r'^TRV-')
        self.assertEqual(row['original_document_code'], payment['code'])
        self.assertEqual(row['action'], {'type': 'treasury_document', 'id': payment['id']})
        self.assertEqual(row['running_balance_cents'], 100000)

    def test_custody_settlement_is_not_a_second_cash_payment(self):
        issue = self.custody()
        self.custody('settlement', '60', '2026-10-10', issue)
        returned = self.custody('return', '40', '2026-10-12', issue)
        before = self.db.total_changes
        book = self.book()
        self.assertEqual(book['closing_balance_cents'], -6000)
        self.assertEqual({r['source_type'] for r in book['rows']}, {'custody_issue', 'custody_return'})
        self.assertEqual(len(book['rows']), 2)
        row = next(r for r in book['rows'] if r['source_type'] == 'custody_return')
        self.assertEqual(row['document_code'], returned['code'])
        self.assertEqual(row['action'], {'type': 'custody_document', 'id': returned['id']})
        self.assertEqual(row['custodian_name'], self.person['name'])
        self.assertEqual(self.db.total_changes, before)

    def test_custody_return_reversal_has_saved_code_and_identity_after_master_rename(self):
        issue = self.custody()
        returned = self.custody('return', '40', '2026-10-12', issue)
        reversed_doc = custodies.reverse(self.db, returned['id'], self.reverse_data(returned))
        custodies.save_custodian(self.db, {'revision': self.person['revision'], 'name': 'اسم حالي جديد', 'phone': ''}, self.person['id'])
        early = self.book(end='2026-10-24')
        late = self.book(end='2026-10-25')
        self.assertEqual((early['closing_balance_cents'], late['closing_balance_cents']), (-6000, -10000))
        row = late['rows'][-1]
        self.assertEqual(row['document_code'], reversed_doc['reversal']['code'])
        self.assertRegex(row['document_code'], '^CSR-')
        self.assertEqual(row['original_document_code'], returned['code'])
        self.assertEqual(row['custodian_name'], 'صاحب عهدة اختبار')
        self.assertEqual(row['action']['id'], returned['id'])

    def test_drafts_do_not_enter_cash_book_or_position(self):
        treasury.save(self.db, self.voucher_data())
        issue_data = {'kind': 'issue', 'request_key': self.key(), 'business_date': '2026-10-05',
                      'reference': self.key(), 'amount': '100', 'cash_account_id': self.cash,
                      'custodian_id': self.person['id'], 'purpose': 'عهدة مسودة'}
        custodies.save(self.db, issue_data)
        self.assertEqual(self.book()['rows'], [])
        self.assertEqual(reports.position(self.db)['totals']['balance_cents'], 0)

    def test_inactive_cash_and_default_mapping_preserve_historical_access(self):
        self.voucher(cash_account_id=self.outlet)
        self.db.execute('UPDATE accounts SET active=0 WHERE id=?', (self.outlet,))
        self.assertEqual(self.book(account=self.outlet)['closing_balance_cents'], 100000)
        account = next(a for a in reports.position(self.db, '2026-10-31')['accounts'] if a['id'] == self.outlet)
        self.assertFalse(account['active'])
        self.assertEqual(account['balance_cents'], 100000)
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الخزينة' AND role='main_expense_safe'", (self.petty,))
        self.assertEqual(reports.cash_book(self.db)['account_id'], self.petty)

    def test_clearing_mapping_and_bank_group_accounts_are_rejected(self):
        for ident in (True, 0, -1, '1', self.f.bank, self.f.account('111000'), self.f.account('111005')):
            with self.subTest(account=ident), self.assertRaises(ValueError):
                reports.cash_book(self.db, ident)
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الخزينة' AND role='cash_transfer_clearing'", (self.outlet,))
        self.assertNotIn(self.outlet, {a['id'] for a in reports.position(self.db)['accounts']})
        with self.assertRaises(ValueError):
            self.book(account=self.outlet)

    def test_strict_dates_and_chart_without_journals_stay_read_only(self):
        for start, end in (('20261001', '2026-10-31'), ('2026-02-30', '2026-10-31'),
                           ('2026-11-01', '2026-10-31'), (True, '2026-10-31')):
            with self.subTest(start=start), self.assertRaises(ValueError):
                reports.cash_book(self.db, self.cash, start, end)
        with self.assertRaises(ValueError):
            reports.position(self.db, '')
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        try:
            accounting.initialize(db)
            before = db.total_changes
            book = reports.cash_book(db, to_date='2024-02-29')
            self.assertEqual(book['from_date'], '2024-01-01')
            self.assertEqual(book['rows'], [])
            self.assertEqual(reports.position(db, '2024-02-29')['totals']['balance_cents'], 0)
            self.assertEqual(db.total_changes, before)
        finally:
            db.close()

    def test_all_501_cash_movements_return_without_hidden_cap(self):
        for _ in range(501):
            self.voucher(amount='0.01')
        before = self.db.total_changes
        book = self.book()
        self.assertEqual((len(book['rows']), book['debit_cents'], book['closing_balance_cents']), (501, 501, 501))
        self.assertEqual(book['rows'][-1]['running_balance_cents'], 501)
        self.assertEqual(self.db.total_changes, before)

    def raw_journal(self, ident, sealed=False, debit_account=None):
        journal = self.db.execute('''INSERT INTO journal_entries(code,source_type,source_id,
            entry_date,created_at,sealed,request_fingerprint)
            VALUES(?,'cash-import-test',?,'2026-10-17','test',0,?)''',
            ('CASH-IMPORT-' + str(ident), ident, 'cash-import-' + str(ident))).lastrowid
        self.db.execute('INSERT INTO journal_lines(journal_id,account_id,debit_cents) VALUES(?,?,100)',
                        (journal, self.cash if debit_account is None else debit_account))
        self.db.execute('INSERT INTO journal_lines(journal_id,account_id,credit_cents) VALUES(?,?,100)',
                        (journal, self.f.bank))
        if sealed:
            self.db.execute('UPDATE journal_entries SET sealed=1 WHERE id=?', (journal,))
        return journal

    def test_unsealed_and_bad_group_cash_journals_are_disclosed(self):
        pending = self.raw_journal(1)
        book = self.book()
        self.assertEqual(book['rows'], [])
        self.assertFalse(book['complete'])
        self.assertEqual(book['exceptions'][0]['journal_id'], pending)
        bad = self.raw_journal(2, True, self.f.account('111000'))
        position = reports.position(self.db, '2026-10-31')
        self.assertFalse(position['complete'])
        self.assertTrue(any(e['type'] == 'nonposting_account' and e['journal_id'] == bad for e in position['exceptions']))
        self.assertEqual(position['totals']['balance_cents'], 0)


if __name__ == '__main__':
    unittest.main()
