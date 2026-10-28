"""Historical tax source capacity, over-remittance credits and independent GL."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting
import payables
import supplier_adjustments as adjustments
import supplier_payments as payments
import withholding
import withholding_balances as balances
import withholding_remittances as remittances
import test_supplier_payments as fixtures


class WithholdingBalances(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SupplierPayments('runTest')
        self.fixture.setUp()
        self.db = self.fixture.db
        adjustments.initialize(self.db)
        self.counter = 0
        self.wh = self.fixture.account('213004')

    def tearDown(self):
        self.fixture.tearDown()

    def key(self, prefix):
        self.counter += 1
        return prefix + '-' + str(self.counter)

    def payment(self, rate='3', day='2026-10-16', supplier_id=1):
        code = withholding.create_code(self.db, {'name': self.key('خصم اختبار'), 'rate': rate,
                                                 'active': True, 'confirmed': True})
        invoice = self.fixture.invoice(supplier_id=supplier_id)
        return self.fixture.post(self.fixture.payload(supplier_id=supplier_id, amount='100',
            payment_date=day, allocation_mode='invoice', target_invoice_id=invoice['id'],
            withholding_code_id=code['id']))

    def remit(self, payment, amount='1', day='2026-10-20'):
        remittances.initialize(self.db)
        data = {'request_key': self.key('remit'), 'business_date': day,
                'period_from': '2026-10-01', 'period_to': day,
                'authority_name': 'جهة ضريبية للاختبار', 'reference': self.key('REC'),
                'method': 'cash', 'funding_account_id': self.fixture.cash,
                'details': {'received_by': 'مندوب الجهة الضريبية'},
                'allocations': [{'payment_id': payment['id'], 'amount': amount}]}
        saved = remittances.save(self.db, data)
        return remittances.approve(self.db, saved['id'], {'revision': saved['revision']})

    def reverse_payment(self, payment, day='2026-10-22'):
        return adjustments.reverse_payment(self.db, payment['id'], {
            'request_key': self.key('reverse-payment'), 'revision': payment['revision'], 'event_revision': 0,
            'business_date': day, 'reference': self.key('CORR'), 'reason': 'تصحيح سداد اختبار'})

    def reverse_remit(self, remittance, day='2026-10-25'):
        return remittances.reverse(self.db, remittance['id'], {
            'request_key': self.key('reverse-remit'), 'revision': remittance['revision'], 'event_revision': 0,
            'business_date': day, 'reference': self.key('RCORR'), 'reason': 'تصحيح توريد اختبار'})

    def report(self, day='2026-10-31', **filters):
        return balances.balance_report(self.db, day, **filters)

    def matched(self, day='2026-10-31'):
        result = self.report(day)
        self.assertTrue(result['matched'], result['exceptions'])
        self.assertTrue(all(account['matched'] for account in result['accounts']))
        return result

    def test_chart_only_database_is_empty_read_only_and_does_not_need_remittance_tables(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        try:
            accounting.initialize(db)
            before = db.total_changes
            report = balances.balance_report(db, '2026-10-31')
            self.assertTrue(report['matched'])
            self.assertEqual(report['sources'], [])
            self.assertEqual(report['all_totals']['remaining_cents'], 0)
            self.assertEqual(report['all_totals']['net_due_cents'], 0)
            self.assertEqual(db.total_changes, before)
        finally:
            db.close()

    def test_existing_withholding_without_remittance_schema_is_reconciled(self):
        payment = self.payment()
        self.assertFalse(payables._has_table(self.db, 'withholding_remittances'))
        before = self.db.total_changes
        report = self.matched()
        self.assertEqual((report['sources'][0]['original_cents'], report['sources'][0]['withheld_cents'],
                          report['sources'][0]['remitted_cents'], report['sources'][0]['remaining_cents']), (300, 300, 0, 300))
        self.assertEqual(balances.capacity(self.db, payment['id'], '2026-10-16'), 300)
        self.assertEqual(balances.account_capacity(self.db, self.wh, '2026-10-16'), 300)
        self.assertEqual(self.db.total_changes, before)

    def test_partial_remittances_and_reversal_preserve_every_historical_balance(self):
        payment = self.payment('10')
        first = self.remit(payment, '4', '2026-10-20')
        self.remit(payment, '2', '2026-10-22')
        self.reverse_remit(first)
        for day, remaining, remitted in [('2026-10-19', 1000, 0), ('2026-10-20', 600, 400),
                                          ('2026-10-22', 400, 600), ('2026-10-24', 400, 600),
                                          ('2026-10-25', 800, 200)]:
            result = self.matched(day)
            source = result['sources'][0]
            self.assertEqual((source['remaining_cents'], source['remitted_cents']), (remaining, remitted))
            self.assertEqual(result['accounts'][0]['ledger_balance_cents'], remaining)

    def test_source_reversal_after_actual_remittance_keeps_overpaid_credit(self):
        payment = self.payment()
        self.remit(payment, '3')
        self.reverse_payment(payment)
        before = self.matched('2026-10-21')['sources'][0]
        after = self.matched('2026-10-22')['sources'][0]
        self.assertEqual((before['withheld_cents'], before['remitted_cents'], before['remaining_cents']), (300, 300, 0))
        self.assertEqual((after['withheld_cents'], after['remitted_cents'], after['remaining_cents'], after['overpaid_cents']), (0, 300, -300, 300))
        self.assertTrue(after['source_reversed'])
        self.assertEqual(self.report()['all_totals']['account_credit_cents'], 300)
        self.assertEqual(balances.account_capacity(self.db, self.wh, '2026-10-22'), -300)

    def test_prior_period_credit_caps_later_source_but_filter_does_not_hide_it(self):
        first = self.payment()
        self.remit(first, '3')
        self.reverse_payment(first)
        second = self.payment('5', '2026-10-23')
        report = self.report(period_from='2026-10-23', period_to='2026-10-23')
        self.assertEqual([row['payment_id'] for row in report['sources']], [second['id']])
        self.assertEqual(report['totals']['remaining_cents'], 500)
        self.assertEqual(report['all_totals']['remaining_cents'], 200)
        self.assertEqual(report['all_totals']['due_cents'], 500)
        self.assertEqual(report['all_totals']['overpaid_cents'], 300)
        self.assertEqual(report['all_totals']['net_due_cents'], 200)
        self.assertEqual(balances.capacity(self.db, second['id'], '2026-10-23'), 500)
        self.assertEqual(balances.account_capacity(self.db, self.wh, '2026-10-23'), 200)
        self.remit(second, '2', '2026-10-24')
        self.assertEqual(self.matched()['all_totals']['net_due_cents'], 0)
        self.assertEqual(self.matched()['all_totals']['remaining_cents'], 0)

    def test_credits_and_liabilities_on_different_accounts_never_net_capacity(self):
        first = self.payment()
        self.remit(first, '3')
        self.reverse_payment(first)
        second_account = self.fixture.account('213005')
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE role='withholding_payable'", (second_account,))
        second = self.payment('5', '2026-10-23')
        report = self.matched()
        self.assertEqual(report['all_totals']['remaining_cents'], 200)
        self.assertEqual((report['all_totals']['net_due_cents'], report['all_totals']['account_credit_cents']), (500, 300))
        self.assertEqual(balances.account_capacity(self.db, second_account, '2026-10-23'), 500)
        self.assertEqual(balances.account_capacity(self.db, self.wh, '2026-10-23'), -300)
        self.assertEqual(next(row for row in report['sources'] if row['payment_id'] == second['id'])['account_id'], second_account)

    def test_intermediate_future_remittance_cannot_be_hidden_by_later_reversal(self):
        payment = self.payment()
        remittance = self.remit(payment, '2', '2026-10-20')
        self.reverse_remit(remittance)
        self.assertEqual(self.matched('2026-10-17')['all_totals']['remaining_cents'], 300)
        self.assertEqual(self.matched()['all_totals']['remaining_cents'], 300)
        self.assertEqual(balances.capacity(self.db, payment['id'], '2026-10-17'), 100)
        self.assertEqual(balances.account_capacity(self.db, self.wh, '2026-10-17'), 100)
        self.assertEqual(balances.capacity(self.db, payment['id'], '2026-10-17', remittance['id']), 300)
        self.assertEqual(balances.account_capacity(self.db, self.wh, '2026-10-17', remittance['id']), 300)

    def test_known_future_source_cancellation_cannot_fund_new_backdated_remittance(self):
        payment = self.payment()
        self.reverse_payment(payment, '2026-10-30')
        self.assertEqual(self.matched('2026-10-20')['all_totals']['net_due_cents'], 300)
        self.assertEqual(balances.capacity(self.db, payment['id'], '2026-10-20'), 0)
        self.assertEqual(balances.account_capacity(self.db, self.wh, '2026-10-20'), 0)

    def test_original_supplier_identity_and_account_survive_master_mapping_changes(self):
        self.db.execute("UPDATE suppliers SET tax_number='ORIGINAL-TAX' WHERE id=1")
        payment = self.payment()
        self.db.execute("UPDATE suppliers SET name='اسم جديد',tax_number='NEW-TAX' WHERE id=1")
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE role='withholding_payable'", (self.fixture.account('213005'),))
        report = self.matched()
        row = report['sources'][0]
        self.assertEqual(row['supplier_tax_number'], 'ORIGINAL-TAX')
        self.assertEqual(row['supplier_name'], payment['withholding']['supplier_name'])
        self.assertEqual(row['account_id'], self.wh)
        self.assertEqual(len(report['accounts']), 2)

    def test_offsetting_wrong_supplier_journal_does_not_pass_reconciliation(self):
        self.payment()
        payables.create_journal(self.db, 'external_test', 1, '2026-10-20', [
            {'account_id': self.wh, 'debit_cents': 50, 'supplier_id': 1},
            {'account_id': self.wh, 'credit_cents': 50, 'supplier_id': 2}])
        result = self.report()
        self.assertEqual(result['all_totals']['difference_cents'], 0)
        self.assertFalse(result['matched'])
        self.assertFalse(result['accounts'][0]['matched'])
        self.assertTrue(any(not row['matched'] for row in result['accounts'][0]['supplier_differences']))

    def test_unassigned_offsetting_control_lines_do_not_pass_zero_difference(self):
        self.payment()
        payables.create_journal(self.db, 'external_test', 1, '2026-10-20', [
            {'account_id': self.wh, 'debit_cents': 50}, {'account_id': self.wh, 'credit_cents': 50}])
        result = self.report()
        self.assertEqual(result['all_totals']['difference_cents'], 0)
        self.assertFalse(result['matched'])
        self.assertFalse(result['accounts'][0]['matched'])
        self.assertTrue(any(row['type'] == 'missing_ledger_supplier' for row in result['exceptions']))

    def test_corrupt_journals_cannot_cancel_their_differences_at_grand_total(self):
        first = self.payment()
        second = self.payment()
        self.db.execute('DROP TRIGGER ap_immutable_journal_lines_update')
        self.db.execute('UPDATE journal_lines SET credit_cents=credit_cents+1 WHERE journal_id=? AND account_id=?', (first['journal_entry_id'], self.wh))
        self.db.execute('UPDATE journal_lines SET credit_cents=credit_cents-1 WHERE journal_id=? AND account_id=?', (second['journal_entry_id'], self.wh))
        result = self.report()
        self.assertEqual(result['all_totals']['difference_cents'], 0)
        self.assertFalse(result['matched'])
        self.assertFalse(result['accounts'][0]['matched'])
        self.assertEqual(sum(row['type'] == 'invalid_source_journal' for row in result['exceptions']), 2)
        self.assertTrue(any(row['type'] == 'source_journal_difference' for row in result['exceptions']))

    def test_invalid_dates_and_ids_rejected_without_writes(self):
        payment = self.payment()
        before = self.db.total_changes
        for as_of in ('2026-02-30', '20261020', True):
            with self.subTest(as_of=as_of), self.assertRaises(ValueError):
                self.report(as_of)
        with self.assertRaises(ValueError):
            self.report(period_from='2026-11-01', period_to='2026-10-01')
        for ident in (True, 0, -1, '1', 999999):
            with self.subTest(ident=ident), self.assertRaises(ValueError):
                balances.capacity(self.db, ident, '2026-10-20')
        with self.assertRaises(ValueError):
            balances.account_capacity(self.db, self.wh, '2026-10-20', True)
        self.assertEqual(self.db.total_changes, before)
        self.assertEqual(balances.capacity(self.db, payment['id'], '2026-10-20'), 300)


if __name__ == '__main__':
    unittest.main(verbosity=2)
