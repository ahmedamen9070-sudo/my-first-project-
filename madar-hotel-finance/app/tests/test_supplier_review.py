"""Review center keeps dated money, current work, and diagnostics distinct."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting_periods
import payables
import supplier_adjustments
import supplier_payments
import supplier_review
import supplier_settlements
import withholding
import withholding_remittances
import test_supplier_payments as fixtures


class SupplierReview(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SupplierPayments('runTest')
        self.fixture.setUp()
        self.db = self.fixture.db
        supplier_settlements.initialize(self.db)
        supplier_adjustments.initialize(self.db)
        accounting_periods.initialize(self.db)
        withholding_remittances.initialize(self.db)

    def tearDown(self):
        self.fixture.tearDown()

    def report(self, day='2026-10-20', horizon=7):
        return supplier_review.snapshot(self.db, day, horizon)

    def test_empty_report_is_read_only_without_inventing_activity_or_tax(self):
        before = self.db.total_changes
        report = self.report()
        self.assertEqual(self.db.total_changes, before)
        self.assertTrue(all(value == 0 for value in report['position'].values()))
        self.assertEqual(report['tax_position']['net_due_cents'], 0)
        self.assertTrue(report['checks']['suppliers']['matched'])
        self.assertTrue(report['checks']['withholding']['matched'])
        self.assertTrue(report['checks']['trial_balance']['balanced'])
        self.assertEqual(report['queues']['financial_issues'], [])
        self.assertEqual(report['queues']['drafts_current'], [])
        self.assertIn('الحالية', report['notes'][1])
        self.assertIn('الافتتاحية', report['notes'][3])

    def test_due_queues_are_positive_historical_balances_with_inclusive_horizon(self):
        past = self.fixture.invoice(10000, due_date='2026-10-19')
        today = self.fixture.invoice(20000, due_date='2026-10-20')
        soon = self.fixture.invoice(30000, due_date='2026-10-27')
        later = self.fixture.invoice(40000, due_date='2026-10-28')
        self.fixture.invoice(50000, business_date='2026-10-21', due_date='2026-10-22')
        self.fixture.post(self.fixture.payload(amount='25', payment_date='2026-10-20',
                           allocation_mode='invoice', target_invoice_id=past['id']))
        report = self.report()
        self.assertEqual(report['position']['open_invoice_cents'], 97500)
        self.assertEqual(report['position']['open_invoice_count'], 4)
        self.assertEqual(report['position']['overdue_cents'], 7500)
        self.assertEqual(report['position']['due_today_cents'], 20000)
        self.assertEqual(report['position']['due_soon_cents'], 30000)
        self.assertEqual(report['queues']['overdue'][0]['id'], past['id'])
        self.assertEqual(report['queues']['due_today'][0]['id'], today['id'])
        self.assertEqual(report['queues']['due_soon'][0]['id'], soon['id'])
        self.assertEqual(report['queues']['overdue'][0]['amount_cents'], 10000)
        self.assertEqual(report['queues']['overdue'][0]['outstanding_cents'], 7500)
        self.assertEqual(report['counts']['due_later_count'], 1)
        self.assertEqual(self.report(horizon=0)['position']['due_soon_count'], 0)
        self.assertEqual(self.report(horizon=8)['queues']['due_soon'][-1]['id'], later['id'])
        self.assertEqual(self.report('2026-10-19')['position']['open_invoice_cents'], 100000)

    def test_paid_return_credit_and_unapplied_advance_stay_separate(self):
        invoice = self.fixture.invoice()
        self.fixture.post(self.fixture.payload(amount='140', allocation_mode='invoice',
                           target_invoice_id=invoice['id'], allow_advance=True))
        self.fixture.returned(invoice, quantity='0.2')
        before = self.report('2026-10-19')
        after = self.report('2026-10-20')
        self.assertEqual(before['position']['return_credit_cents'], 0)
        self.assertEqual(before['position']['advance_cents'], 4000)
        self.assertEqual(after['position']['return_credit_cents'], 2000)
        self.assertEqual(after['position']['advance_cents'], 4000)
        self.assertEqual(after['position']['net_payable_cents'], -6000)
        self.assertEqual(after['position']['open_invoice_cents'], 0)
        self.assertEqual({row['kind'] for row in after['queues']['credits']}, {'advance', 'return_credit'})
        self.assertTrue(after['checks']['suppliers']['matched'])
        self.assertEqual(after['position']['net_payable_cents'], after['checks']['suppliers']['totals']['subledger_net_cents'])
        self.assertTrue(all(row['action'] == {'type': 'settlement_new', 'supplier_id': 1}
                            for row in after['queues']['credits']))

    def test_cheques_use_saved_net_and_historical_stop_resume_clearance_state(self):
        invoice = self.fixture.invoice()
        code = withholding.create_code(self.db, {'name': 'خصم مراجعة', 'rate': '3', 'active': True, 'confirmed': True})
        payment = self.fixture.post(self.fixture.payload('cheque', amount='100',
                    allocation_mode='invoice', target_invoice_id=invoice['id'], withholding_code_id=code['id']))
        stopped = supplier_adjustments.cheque_event(self.db, payment['id'], {
            'request_key': 'review-stop', 'revision': payment['revision'], 'event_revision': 0,
            'action': 'stop', 'business_date': '2026-10-19', 'reference': 'STOP-REVIEW', 'reason': 'إيقاف اختبار المراجعة'})
        resumed = supplier_adjustments.cheque_event(self.db, payment['id'], {
            'request_key': 'review-resume', 'revision': stopped['revision'], 'event_revision': 1,
            'action': 'resume', 'business_date': '2026-10-20', 'reference': 'RESUME-REVIEW', 'reason': 'استئناف اختبار المراجعة'})
        supplier_payments.clear_cheque(self.db, payment['id'], {
            'revision': resumed['revision'], 'event_revision': 2, 'clearance_date': '2026-10-22',
            'bank_account_id': self.fixture.bank, 'reference': 'CLEAR-REVIEW'})
        before_changes = self.db.total_changes
        before = self.report('2026-10-18')
        self.assertEqual(before['position']['open_cheque_net_cents'], 9700)
        row = before['queues']['cheques'][0]
        self.assertEqual((row['gross_cents'], row['amount_cents'], row['net_cash_cents']), (10000, 9700, 9700))
        self.assertEqual(row['effective_cheque_status'], 'issued')
        self.assertEqual(self.report('2026-10-19')['queues']['cheques'][0]['effective_cheque_status'], 'stopped')
        self.assertEqual(self.report('2026-10-21')['queues']['cheques'][0]['effective_cheque_status'], 'issued')
        self.assertEqual(self.report('2026-10-22')['queues']['cheques'], [])
        self.assertEqual(self.report('2026-10-15')['queues']['cheques'], [])
        self.assertEqual(self.db.total_changes, before_changes)

    def test_cancelled_cheque_is_open_only_before_effective_cancellation(self):
        invoice = self.fixture.invoice()
        payment = self.fixture.post(self.fixture.payload('cheque', amount='100',
                    allocation_mode='invoice', target_invoice_id=invoice['id']))
        supplier_adjustments.cheque_event(self.db, payment['id'], {
            'request_key': 'review-cancel', 'revision': payment['revision'], 'event_revision': 0,
            'action': 'cancel', 'business_date': '2026-10-21', 'reference': 'CANCEL-REVIEW', 'reason': 'إلغاء اختبار المراجعة'})
        self.assertEqual(self.report('2026-10-20')['position']['open_cheque_net_cents'], 10000)
        self.assertEqual(self.report('2026-10-21')['position']['open_cheque_count'], 0)
        self.assertEqual(self.report('2026-10-21')['position']['open_invoice_cents'], 10000)

    def test_current_future_draft_is_not_mistaken_for_historical_posting(self):
        invoice = self.fixture.invoice()
        draft = supplier_payments.save_payment(self.db, self.fixture.payload(amount='10',
            payment_date='2026-10-25', allocation_mode='invoice', target_invoice_id=invoice['id']))
        report = self.report('2026-10-20')
        rows = report['queues']['drafts_current']
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['id'], rows[0]['kind'], rows[0]['future_dated']), (draft['id'], 'payment', True))
        self.assertEqual(report['position']['open_invoice_cents'], 10000)
        self.assertEqual(report['counts']['future_drafts'], 1)
        self.assertEqual(self.report('2026-10-01')['queues']['drafts_current'][0]['id'], draft['id'])

    def test_net_zero_supplier_dimension_damage_remains_a_review_issue(self):
        self.fixture.invoice()
        journal = payables.create_journal(self.db, 'review_shift', 1, '2026-10-21', [
            {'account_id': self.fixture.ap, 'debit_cents': 700, 'supplier_id': 1},
            {'account_id': self.fixture.ap, 'credit_cents': 700, 'supplier_id': 2}])
        report = self.report('2026-10-21')
        check = report['checks']['suppliers']
        self.assertFalse(check['matched'])
        self.assertEqual(check['totals']['net_difference_cents'], 0)
        problems = [row for row in report['queues']['financial_issues']
                    if row['type'] == 'supplier_balance_difference']
        self.assertEqual({row['supplier_id'] for row in problems}, {1, 2})
        self.assertTrue(all(journal in row['journal_ids'] for row in problems))
        self.assertEqual(self.report('2026-10-20')['queues']['financial_issues'], [])

    def test_unsealed_journal_is_not_hidden_by_zero_ledger_difference(self):
        journal = self.db.execute("""INSERT INTO journal_entries(code,source_type,source_id,entry_date,created_at,sealed,request_fingerprint)
            VALUES('REVIEW-DRAFT-J','review_import',1,'2026-10-20','now',0,'review')""").lastrowid
        report = self.report()
        self.assertFalse(report['checks']['trial_balance']['balanced'])
        self.assertEqual(report['checks']['trial_balance']['totals']['closing_difference_cents'], 0)
        self.assertTrue(any(row['type'] == 'unsealed_journal' and row['journal_id'] == journal
                            for row in report['queues']['financial_issues']))

    def test_tax_overpayment_credit_is_not_netted_against_another_historical_account(self):
        invoice = self.fixture.invoice()
        code = withholding.create_code(self.db, {'name': 'خصم مراجعة ضريبية', 'rate': '3',
                                                  'active': True, 'confirmed': True})
        first = self.fixture.post(self.fixture.payload(amount='100', allocation_mode='invoice',
                    target_invoice_id=invoice['id'], withholding_code_id=code['id']))
        draft = withholding_remittances.save(self.db, {
            'request_key': 'review-tax-remit', 'business_date': '2026-10-20',
            'period_from': '2026-10-01', 'period_to': '2026-10-20',
            'authority_name': 'جهة ضريبية للاختبار', 'reference': 'REVIEW-TAX-RECEIPT',
            'method': 'cash', 'funding_account_id': self.fixture.cash,
            'details': {'received_by': 'مندوب الجهة'},
            'allocations': [{'payment_id': first['id'], 'amount': '3'}],
        })
        withholding_remittances.approve(self.db, draft['id'], {'revision': draft['revision']})
        supplier_adjustments.reverse_payment(self.db, first['id'], {
            'request_key': 'review-tax-reverse', 'business_date': '2026-10-21',
            'revision': first['revision'], 'event_revision': 0,
            'reference': 'REVIEW-TAX-CORRECTION', 'reason': 'تصحيح بعد تسجيل توريد اختباري'})
        new_account = self.fixture.account('213005')
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الضرائب' AND role='withholding_payable'", (new_account,))
        second_invoice = self.fixture.invoice(supplier_id=2)
        self.fixture.post(self.fixture.payload(amount='100', supplier_id=2,
            payment_date='2026-10-22', allocation_mode='invoice',
            target_invoice_id=second_invoice['id'], withholding_code_id=code['id']))
        report = self.report('2026-10-22')
        self.assertEqual(report['tax_position']['remaining_cents'], 0)
        self.assertEqual(report['tax_position']['net_due_cents'], 300)
        self.assertEqual(report['tax_position']['account_credit_cents'], 300)
        self.assertTrue(report['checks']['withholding']['matched'])
        historical = self.report('2026-10-20')
        self.assertEqual(historical['tax_position']['net_due_cents'], 0)
        self.assertEqual(historical['tax_position']['account_credit_cents'], 0)

    def test_negative_funding_is_recorded_movement_even_for_now_inactive_account(self):
        invoice = self.fixture.invoice()
        self.fixture.post(self.fixture.payload(amount='20', allocation_mode='invoice', target_invoice_id=invoice['id']))
        self.db.execute('UPDATE accounts SET active=0 WHERE id=?', (self.fixture.cash,))
        report = self.report()
        self.assertEqual(len(report['queues']['negative_funding']), 1)
        row = report['queues']['negative_funding'][0]
        self.assertEqual((row['id'], row['amount_cents'], row['active']), (self.fixture.cash, -2000, False))
        self.assertIn('الافتتاحية', row['message'])
        self.assertEqual(row['action']['type'], 'ledger')

    def test_damaged_advance_journal_does_not_hide_independent_totals_or_checks(self):
        # Deliberately simulate an imported posted header without its source
        # journal. Normal approval creates the journal before marking it posted.
        self.db.execute("""INSERT INTO supplier_payments(code,request_key,request_fingerprint,
            supplier_id,payment_date,amount_cents,method,status,details,funding_account_id,
            allocation_mode,allow_advance,advance_cents,created_at,updated_at)
            VALUES('IMPORTED-ADV','imported-adv','imported',1,'2026-10-16',4000,
                   'cash','posted','{}',?,'manual',1,4000,'now','now')""", (self.fixture.cash,))
        before = self.db.total_changes
        report = self.report()
        self.assertFalse(report['credits_complete'])
        self.assertEqual(report['position']['advance_cents'], 4000)
        self.assertEqual(report['position']['net_payable_cents'], -4000)
        self.assertEqual(report['queues']['credits'], [])
        self.assertFalse(report['checks']['suppliers']['matched'])
        issue = next(row for row in report['queues']['financial_issues']
                     if row['type'] == 'unavailable_credit_sources')
        self.assertEqual(issue['supplier_id'], 1)
        self.assertEqual(issue['action']['type'], 'reconciliation')
        self.assertIn('غير مكتملة', report['notes'][0])
        self.assertEqual(self.db.total_changes, before)

    def test_more_than_five_hundred_due_invoices_are_not_truncated(self):
        for _ in range(501):
            self.fixture.invoice(cents=100)
        before = self.db.total_changes
        report = self.report()
        self.assertEqual(len(report['queues']['overdue']), 501)
        self.assertEqual(report['position']['overdue_cents'], 50100)
        self.assertEqual(report['position']['open_invoice_count'], 501)
        self.assertEqual(self.db.total_changes, before)

    def test_strict_dates_and_integer_horizon_and_maximum_date_boundary(self):
        for value in ('', '2026-1-01', '2026-02-30', '20261020', True, 1):
            with self.subTest(as_of=value), self.assertRaises(ValueError):
                supplier_review.snapshot(self.db, value)
        for value in (-1, 91, True, '7', 7.0, None):
            with self.subTest(horizon=value), self.assertRaises(ValueError):
                supplier_review.snapshot(self.db, '2026-10-20', value)
        self.assertEqual(self.report('9999-12-31', 90)['as_of'], '9999-12-31')
        self.assertEqual(supplier_review.snapshot(self.db)['horizon_days'], 7)


if __name__ == '__main__':
    unittest.main()
