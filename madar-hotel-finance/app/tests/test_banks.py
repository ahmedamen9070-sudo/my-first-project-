"""Banks use exact sealed GL lines, with no duplicate supplier funding."""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting
import accounting_periods
import banks
import opening_balances
import payables
import supplier_payments
import treasury
import treasury_reports
import withholding
import withholding_remittances
import test_supplier_payments as fixtures


class Banks(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SupplierPayments('runTest')
        self.f.setUp()
        self.db = self.f.db
        treasury.initialize(self.db)
        banks.initialize(self.db)
        accounting_periods.initialize(self.db)
        self.bank = self.f.bank
        self.bank2 = self.f.account('112003')
        self.cash = self.f.cash
        self.config = banks.config(self.db)
        self.equity = next(a['id'] for a in self.config['counter_accounts_by_kind']['receipt'] if a['code'].startswith('3'))
        self.expense = self.config['counter_accounts_by_kind']['payment'][0]['id']
        self.serial = 0
        self.db.commit()

    def tearDown(self):
        self.f.tearDown()

    def payload(self, kind='receipt', **changes):
        self.serial += 1
        value = dict(kind=kind, request_key='bank-' + str(self.serial), business_date='2026-10-16',
                     value_date='2026-10-16', reference='REF-' + str(self.serial), bank_reference='BANK-' + str(self.serial),
                     currency='EGP', account_id=self.bank, amount='100.00', party_name='الطرف الفعلي',
                     counter_account_id=self.expense if kind in ('payment', 'fee') else self.equity,
                     department_id=1 if kind in ('payment', 'fee') else None)
        if kind == 'transfer':
            value.update(counter_account_id=None, destination_account_id=self.bank2, party_name='', department_id=None)
        return {**value, **changes}

    def post(self, value):
        draft = banks.save(self.db, value)
        return banks.approve(self.db, draft['id'], {'revision': draft['revision']})

    def balance(self, account, date=None):
        return supplier_payments.account_balance(self.db, account, date)

    def opening_cheque(self, **changes):
        self.serial += 1
        line = dict(id=self.serial, kind='issued_cheque', amount='200.00', side='credit',
                    reference='CHQ-OLD-' + str(self.serial), source_date='2026-09-10', due_date='2026-10-20',
                    extra=dict(bank_account_id=self.bank, bank_name='بنك مصر', beneficiary='مستفيد افتتاحي', cheque_number='OLD-' + str(self.serial)))
        line.update(changes)
        opening_balances.initialize(self.db)
        self.db.execute("UPDATE account_mappings SET status='confirmed'")
        value = banks.prepare_opening_cheque(self.db, line, '2026-10-01')
        saved = opening_balances.save(self.db, dict(request_key='OPEN-'+str(self.serial), business_date='2026-10-01',
            reference='OPEN-'+str(self.serial), lines=[value,
                dict(kind='general', account_id=self.equity, amount_cents=value['amount_cents'], side='debit', reference='CAPITAL-OPEN')]))
        posted = opening_balances.approve(self.db, saved['id'], dict(revision=saved['revision']))
        source = next(l for l in posted['lines'] if l['kind'] == 'issued_cheque')
        return banks.opening_cheque_detail(self.db, source['document_id'])

    def clear_payload(self, **changes):
        self.serial += 1
        return dict(request_key='CLEAR-' + str(self.serial), revision=1, business_date='2026-10-21',
                    value_date='2026-10-21', reference='CLEAR-REF-' + str(self.serial), bank_reference='BANK-CLEAR-' + str(self.serial), **changes)

    def test_exact_four_kinds_and_bank_cash_both_directions(self):
        docs = [self.post(self.payload(amount='1000.01')),
                self.post(self.payload('payment', amount='20.01')),
                self.post(self.payload('fee', amount='10.00')),
                self.post(self.payload('transfer', amount='200')),
                self.post(self.payload('transfer', amount='100', destination_account_id=self.cash)),
                self.post(self.payload('transfer', amount='40', account_id=self.cash, destination_account_id=self.bank))]
        self.assertEqual(self.balance(self.bank), 71000)
        self.assertEqual(self.balance(self.bank2), 20000)
        self.assertEqual(self.balance(self.cash), 6000)
        for doc in docs:
            self.assertEqual(doc['journal']['source_type'], 'bank_' + doc['kind'])
            self.assertEqual(doc['journal']['debit_cents'], doc['amount_cents'])
        cash_book = treasury_reports.cash_book(self.db, self.cash)
        self.assertEqual(len(cash_book['rows']), 2)
        self.assertEqual(cash_book['closing_balance_cents'], 6000)
        self.assertTrue(all(r['action']['type'] == 'bank_document' for r in cash_book['rows']))
        for doc in docs[1:3]:
            expense = next(l for l in doc['journal']['lines'] if l['account_id'] == self.expense)
            self.assertEqual(expense['department_id'], 1)

    def test_master_is_metadata_only_and_frozen_until_resave(self):
        master = banks.save_account(self.db, dict(chart_account_id=self.bank, bank_name='بنك مصر', account_number='123456', iban=''))
        self.assertEqual(self.balance(self.bank), 0)
        draft = banks.save(self.db, self.payload())
        banks.save_account(self.db, {**master, 'bank_name': 'اسم محدث'}, master['id'])
        with self.assertRaises(accounting.ConflictError):
            banks.approve(self.db, draft['id'], {'revision': draft['revision']})
        draft = banks.save(self.db, {**self.payload(), 'kind': draft['kind'], 'reference': draft['reference'], 'revision': draft['revision']}, draft['id'])
        posted = banks.approve(self.db, draft['id'], {'revision': draft['revision']})
        banks.save_account(self.db, {**banks.config(self.db)['masters'][0], 'bank_name': 'اسم آخر'}, master['id'])
        self.assertEqual(banks.detail(self.db, posted['id'])['bank_name'], 'اسم محدث')

    def test_no_seed_opening_and_master_new_chart_account(self):
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], 0)
        master = banks.save_account(self.db, dict(create_account=True, bank_name='بنك محلي جديد', account_number='4455'))
        self.assertTrue(master['account_code'].startswith('112'))
        self.assertEqual(self.balance(master['chart_account_id']), 0)
        with self.assertRaises(ValueError):
            banks.save_account(self.db, dict(chart_account_id=self.bank, bank_name='أجنبي', currency='USD'))

    def test_supplier_payment_shows_once_and_bank_draft_never_duplicates_it(self):
        self.f.invoice(10000)
        payment = self.f.post(self.f.payload('bank_transfer', amount='30', allocation_mode='fifo'))
        report = banks.book(self.db, self.bank)
        self.assertEqual(len(report['rows']), 1)
        self.assertEqual(report['credit_cents'], 3000)
        self.assertEqual(report['rows'][0]['action'], {'type': 'payment', 'id': payment['id']})
        self.assertEqual(banks.list_documents(self.db), [])
        self.assertEqual(report['rows'][0]['party_name'], 'مورد أغذية')

    def test_supplier_net_cash_cheque_clearance_and_tax_remittance_read_actual_bank_lines_once(self):
        withholding_remittances.initialize(self.db)
        code = withholding.create_code(self.db, dict(name='خصم اختبار البنك', rate='3', active=True, confirmed=True))
        first = self.f.invoice(10000)
        paid = self.f.post(self.f.payload('bank_transfer', amount='100', allocation_mode='invoice',
            target_invoice_id=first['id'], withholding_code_id=code['id']))
        second = self.f.invoice(10000)
        cheque = self.f.post(self.f.payload('cheque', amount='100', allocation_mode='invoice',
            target_invoice_id=second['id'], withholding_code_id=code['id']))
        self.assertEqual(self.balance(self.bank), -9700, 'Issuing a cheque does not reduce its bank')
        supplier_payments.clear_cheque(self.db, cheque['id'], dict(revision=cheque['revision'],
            clearance_date='2026-10-21', reference='CLEAR-AP', bank_account_id=self.bank))
        remit = withholding_remittances.save(self.db, dict(request_key='BANK-TAX', business_date='2026-10-23',
            period_from='2026-10-01', period_to='2026-10-23', authority_name='مصلحة الضرائب', reference='TAX-BANK',
            method='bank_transfer', funding_account_id=self.bank,
            details=dict(bank_name='بنك مصر', transaction_reference='TAX-BANK-TXN', value_date='2026-10-24'),
            allocations=[dict(payment_id=paid['id'], amount='3')]))
        withholding_remittances.approve(self.db, remit['id'], dict(revision=remit['revision']))
        report = banks.book(self.db, self.bank)
        self.assertEqual(len(report['rows']), 3)
        self.assertEqual([r['credit_cents'] for r in report['rows']], [9700, 9700, 300])
        self.assertEqual(report['closing_balance_cents'], -19700)
        self.assertEqual([r['action']['type'] for r in report['rows']], ['payment', 'payment', 'remittance'])
        self.assertEqual(banks.list_documents(self.db), [])

    def test_protected_control_accounts_never_direct_contras(self):
        for account in (self.f.ap, self.f.advance, self.f.cheques):
            with self.assertRaises(ValueError):
                banks.preview(self.db, self.payload(counter_account_id=account))
        mapped = self.db.execute("SELECT account_id FROM account_mappings WHERE module='الموردون' AND role='withholding_payable'").fetchone()
        if mapped:
            with self.assertRaises(ValueError):
                banks.preview(self.db, self.payload(counter_account_id=mapped[0]))

    def test_input_precision_currency_references_department_and_liquid_boundaries(self):
        for change in ({'amount': '0'}, {'amount': '1.001'}, {'amount': True}, {'currency': 'USD'},
                       {'bank_reference': ''}, {'value_date': ''}, {'account_id': self.cash},
                       {'account_id': self.f.account('112002')}):
            with self.assertRaises(ValueError, msg=str(change)):
                banks.preview(self.db, self.payload(**change))
        with self.assertRaises(ValueError):
            banks.preview(self.db, self.payload('fee', department_id=None))
        with self.assertRaises(ValueError):
            banks.preview(self.db, self.payload('transfer', account_id=self.cash, destination_account_id=self.f.account('111003')))
        with self.assertRaises(ValueError):
            banks.preview(self.db, self.payload('transfer', destination_account_id=self.f.account('111005')))

    def test_negative_funds_is_warning_and_never_invented_funding(self):
        plan = banks.preview(self.db, self.payload('payment', amount='200'))
        self.assertEqual(plan['balance_before_cents'], 0)
        self.assertEqual(plan['balance_after_cents'], -20000)
        self.assertTrue(plan['warnings'])
        self.post(self.payload('payment', amount='200'))
        self.assertEqual(self.balance(self.bank), -20000)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], 1)

    def test_save_revision_idempotence_draft_cancel_and_post_immutable(self):
        data = self.payload()
        draft = banks.save(self.db, data)
        self.assertEqual(draft['id'], banks.save(self.db, data)['id'])
        with self.assertRaises(accounting.ConflictError):
            banks.save(self.db, {**data, 'amount': '200'})
        newer = banks.save(self.db, {**data, 'revision': 1, 'amount': '22.15'}, draft['id'])
        with self.assertRaises(accounting.ConflictError):
            banks.save(self.db, {**data, 'revision': 1}, draft['id'])
        posted = banks.approve(self.db, newer['id'], {'revision': newer['revision']})
        self.assertEqual(banks.approve(self.db, newer['id'], {'revision': 1})['journal_id'], posted['journal_id'])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE bank_documents SET amount_cents=7 WHERE id=?', (posted['id'],))
        cancelled = banks.save(self.db, self.payload())
        banks.save(self.db, {'revision': cancelled['revision'], 'action': 'cancel'}, cancelled['id'])
        self.assertEqual(self.balance(self.bank), 2215)

    def test_dated_reversal_preserves_source_and_exact_dept_and_retries(self):
        doc = self.post(self.payload('payment', amount='37.11'))
        data = dict(request_key='REV-1', revision=doc['revision'], event_revision=0,
                    business_date='2026-10-20', reference='RV-1', reason='تصحيح مستند')
        result = banks.reverse(self.db, doc['id'], data)
        self.assertEqual(result['effective_status'], 'reversed')
        self.assertEqual(banks.detail(self.db, doc['id'], '2026-10-19')['effective_status'], 'posted')
        self.assertEqual(self.balance(self.bank, '2026-10-19'), -3711)
        self.assertEqual(self.balance(self.bank, '2026-10-20'), 0)
        self.assertEqual(banks.reverse(self.db, doc['id'], data)['reversal']['journal_id'], result['reversal']['journal_id'])
        self.assertEqual(len(banks.book(self.db, self.bank)['rows']), 2)
        self.assertTrue(banks.book(self.db, self.bank)['rows'][1]['document_code'].startswith('BRV-'))
        with self.assertRaises(accounting.ConflictError):
            banks.reverse(self.db, doc['id'], {**data, 'reason': 'سبب آخر'})

    def test_closed_period_blocks_save_post_and_reverse_but_safe_retries_work(self):
        data = self.payload()
        doc = self.post(data)
        self.db.execute("INSERT INTO accounting_periods(month,status,updated_at) VALUES('2026-10','closed','test')")
        self.assertEqual(banks.save(self.db, data)['id'], doc['id'])
        self.assertEqual(banks.approve(self.db, doc['id'], {})['journal_id'], doc['journal_id'])
        with self.assertRaises(ValueError):
            banks.save(self.db, self.payload())
        with self.assertRaises(ValueError):
            banks.reverse(self.db, doc['id'], dict(request_key='closed', revision=doc['revision'], business_date='2026-10-20', reference='R', reason='سبب'))

    def test_opening_cheque_is_net_liability_and_clearance_never_ap(self):
        cheque = self.opening_cheque(supplier_id=1)
        self.assertEqual(self.balance(self.bank), 0)
        self.assertEqual(self.balance(self.f.cheques), -20000)
        ap_before = payables.supplier_balances(self.db, 1)
        self.assertEqual(len(banks.list_opening_cheques(self.db)), 1)
        value = self.clear_payload()
        cleared = banks.clear_opening_cheque(self.db, cheque['id'], value)
        self.assertEqual(self.balance(self.bank), -20000)
        self.assertEqual(self.balance(self.f.cheques), 0)
        self.assertEqual(payables.supplier_balances(self.db, 1), ap_before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM supplier_payments').fetchone()[0], 0)
        self.assertEqual(cleared['clearance']['journal']['source_type'], 'opening_cheque_clearance')
        self.assertTrue(all(l['supplier_id'] is None for l in cleared['clearance']['journal']['lines']))
        self.assertEqual(banks.opening_cheque_detail(self.db, cheque['id'], '2026-10-20')['effective_status'], 'outstanding')
        self.assertEqual(banks.book(self.db, self.bank)['rows'][0]['action'], {'type': 'opening_bank_cheque', 'id': cheque['id']})
        self.assertEqual(banks.clear_opening_cheque(self.db, cheque['id'], value)['clearance']['id'], cleared['clearance']['id'])
        with self.assertRaises(accounting.ConflictError):
            banks.clear_opening_cheque(self.db, cheque['id'], {**value, 'bank_reference': 'آخر'})
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM bank_cheque_clearances WHERE id=?', (cleared['clearance']['id'],))

    def test_opening_cheque_clearance_validates_fixed_bank_net_date_and_locks(self):
        cheque = self.opening_cheque()
        for change in ({'bank_account_id': self.bank2}, {'amount': '100'}, {'business_date': '2026-09-30'},
                       {'revision': 0}, {'value_date': ''}, {'bank_reference': ''}):
            value = self.clear_payload()
            with self.assertRaises(ValueError):
                banks.clear_opening_cheque(self.db, cheque['id'], {**value, **change})
        self.db.execute("INSERT INTO accounting_periods(month,status,updated_at) VALUES('2026-10','closed','test')")
        with self.assertRaises(ValueError):
            banks.clear_opening_cheque(self.db, cheque['id'], self.clear_payload())

    def test_opening_cheque_duplicate_source_and_ap_number_are_rejected(self):
        cheque = self.opening_cheque()
        self.assertEqual(banks.post_opening_cheque(self.db, dict(id=cheque['opening_line_id']), cheque['journal_id'])['id'], cheque['id'])
        with self.assertRaises(accounting.ConflictError):
            self.opening_cheque(extra=dict(bank_account_id=self.bank, bank_name='بنك مصر', beneficiary='مستفيد', cheque_number=cheque['cheque_number']))
        self.f.invoice()
        payment = self.f.post(self.f.payload('cheque', allocation_mode='fifo'))
        with self.assertRaises(accounting.ConflictError):
            self.opening_cheque(extra=dict(bank_account_id=self.bank, bank_name='بنك مصر', beneficiary='مستفيد', cheque_number=payment['details']['cheque_number']))

    def test_ap_cheque_with_tax_never_promotes_wht_or_other_controls_to_issued_cheques(self):
        code = withholding.create_code(self.db, dict(name='خصم اختبار الفصل', rate='3', active=True, confirmed=True))
        invoice = self.f.invoice(10000)
        self.f.post(self.f.payload('cheque', amount='100', allocation_mode='invoice', target_invoice_id=invoice['id'], withholding_code_id=code['id']))
        tax = self.f.account('213004')
        self.assertNotIn(tax, banks.issued_cheque_account_ids(self.db))
        self.assertIn(self.f.cheques, banks.issued_cheque_account_ids(self.db))
        for account in (tax, self.f.ap, self.f.advance, self.f.account('115003')):
            with self.assertRaises(ValueError):
                banks.prepare_opening_cheque(self.db, dict(kind='issued_cheque', account_id=account, side='credit', amount='2',
                    source_date='2026-09-01', due_date='2026-10-01', extra=dict(bank_account_id=self.bank,
                    beneficiary='مستفيد', bank_name='بنك مصر', cheque_number='BAD-'+str(account))), '2026-10-01')
        # Even a later erroneous mapping cannot treat a saved tax control as a cheque liability.
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الموردون' AND role='issued_cheques'", (tax,))
        with self.assertRaises(ValueError):
            banks.prepare_opening_cheque(self.db, dict(amount='2', source_date='2026-09-01', due_date='2026-10-01',
                extra=dict(bank_account_id=self.bank, beneficiary='مستفيد', bank_name='بنك مصر', cheque_number='BAD-MAP')), '2026-10-01')

    def test_orphan_opening_journal_cannot_be_cleared_even_when_liability_and_source_match(self):
        line = banks.prepare_opening_cheque(self.db, dict(id=987, kind='issued_cheque', amount='200',
            reference='ORPHAN', source_date='2026-09-01', due_date='2026-10-20',
            extra=dict(bank_account_id=self.bank, bank_name='بنك مصر', beneficiary='مستفيد', cheque_number='ORPHAN')), '2026-10-01')
        journal = payables.create_journal(self.db, 'opening_balance', 987, '2026-10-01', [
            dict(account_id=self.f.cheques, credit_cents=20000), dict(account_id=self.equity, debit_cents=20000)])
        cheque = banks.post_opening_cheque(self.db, line, journal)
        with self.assertRaisesRegex(ValueError, 'افتتاح معتمد'):
            banks.clear_opening_cheque(self.db, cheque['id'], self.clear_payload())
        self.assertEqual(self.balance(self.bank), 0)

    def test_opening_cheque_clear_rejects_borrowed_line_or_mismatched_bank_identity(self):
        cheque = self.opening_cheque()
        # A manually imported malformed source cannot borrow a genuine batch/journal identity.
        row = dict(self.db.execute('SELECT * FROM opening_bank_cheques WHERE id=?', (cheque['id'],)).fetchone())
        row.update(id=99, code='OCH-MALFORMED', opening_line_id=988, bank_name='بنك آخر', cheque_number='MALFORMED')
        self.db.execute('INSERT INTO opening_bank_cheques('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')', tuple(row.values()))
        with self.assertRaises(ValueError):
            banks.clear_opening_cheque(self.db, row['id'], self.clear_payload())
        self.assertEqual(self.balance(self.bank), 0)
        original = dict(self.db.execute('SELECT * FROM opening_bank_cheques WHERE id=?', (cheque['id'],)).fetchone())
        for change in ({'beneficiary': 'مستفيد آخر'}, {'bank_name': 'بنك آخر'}, {'cheque_number': 'شيك آخر'},
                       {'bank_account_id': self.bank2}, {'amount_cents': 20001}, {'due_date': '2026-10-22'}):
            with self.assertRaises(ValueError, msg=str(change)):
                banks._validate_opening_cheque_source(self.db, {**original, **change})
        unsealed = {**payables.journal_detail(self.db, cheque['journal_id']), 'sealed': 0}
        with patch.object(payables, 'journal_detail', return_value=unsealed):
            with self.assertRaises(ValueError):
                banks.clear_opening_cheque(self.db, cheque['id'], self.clear_payload())
        self.assertEqual(self.balance(self.bank), 0)


if __name__ == '__main__':
    unittest.main()
