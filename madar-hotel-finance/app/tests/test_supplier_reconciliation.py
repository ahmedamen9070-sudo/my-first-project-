"""Dated supplier control reconciliation, including deliberately inconsistent data."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting
import payables
import supplier_payments
import supplier_reconciliation
import supplier_settlements
import supplier_adjustments
import test_supplier_payments as fixtures


class SupplierReconciliation(unittest.TestCase):
    def setUp(self):
        # Reuse the realistic isolated database builder without inheriting or
        # re-running its payment tests in this module.
        self.fixture = fixtures.SupplierPayments('runTest')
        self.fixture.setUp()
        self.db = self.fixture.db
        supplier_settlements.initialize(self.db)
        supplier_adjustments.initialize(self.db)

    def tearDown(self):
        self.fixture.tearDown()

    def report(self, day='2026-12-31'):
        return supplier_reconciliation.report(self.db, day)

    def assert_matched(self, report):
        self.assertTrue(report['matched'], report)
        self.assertEqual(report['totals']['payable_difference_cents'], 0)
        self.assertEqual(report['totals']['advance_difference_cents'], 0)
        self.assertEqual(report['totals']['net_difference_cents'], 0)
        self.assertEqual(report['exceptions'], [])
        self.assertTrue(all(row['matched'] for row in report['suppliers']))

    def test_empty_report_has_no_invented_balances_and_is_read_only(self):
        changes = self.db.total_changes
        report = self.report()
        self.assert_matched(report)
        self.assertEqual(report['counts']['supplier_count'], 0)
        self.assertEqual(report['counts']['journal_count'], 0)
        self.assertTrue(all(value == 0 for value in report['totals'].values()))
        self.assertEqual(self.db.total_changes, changes)

    def test_invoice_payment_advance_and_paid_return_match_on_each_historical_date(self):
        invoice = self.fixture.invoice()
        self.fixture.post(self.fixture.payload(amount='140.00', allocation_mode='invoice', target_invoice_id=invoice['id'], allow_advance=True))
        self.fixture.returned(invoice, quantity='0.2')
        before = self.report('2026-10-15')
        self.assert_matched(before)
        self.assertEqual(before['totals']['ledger_payable_cents'], 10000)
        self.assertEqual(before['totals']['ledger_advance_cents'], 0)
        paid = self.report('2026-10-16')
        self.assert_matched(paid)
        self.assertEqual(paid['totals']['ledger_payable_cents'], 0)
        self.assertEqual(paid['totals']['ledger_advance_cents'], 4000)
        returned = self.report('2026-10-20')
        self.assert_matched(returned)
        self.assertEqual(returned['totals']['ledger_payable_cents'], -2000)
        self.assertEqual(returned['totals']['ledger_net_cents'], -6000)
        self.assertEqual(returned['totals']['payable_credit_cents'], 10000)
        self.assertEqual(returned['totals']['payable_debit_cents'], 12000)
        self.assertEqual(returned['counts']['source_document_count'], 3)

    def test_old_and_new_control_accounts_survive_mapping_changes(self):
        first = self.fixture.invoice(cents=5000)
        self.fixture.post(self.fixture.payload(amount='20', allow_advance=True))
        old_ap, old_advance = self.fixture.ap, self.fixture.advance
        new_ap, new_advance = self.fixture.account('211002'), self.fixture.account('115003')
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الموردون' AND role='payable_control'", (new_ap,))
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الموردون' AND role='supplier_advances'", (new_advance,))
        second = self.fixture.invoice(cents=6000, business_date='2026-10-17', due_date='2026-10-20')
        self.fixture.post(self.fixture.payload(amount='10', payment_date='2026-10-18', allow_advance=True))
        # Even another mapping change cannot relabel old source journals.
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الموردون' AND role='supplier_advances'", (self.fixture.account('115001'),))
        report = self.report()
        self.assert_matched(report)
        actual = {a['id']: a for a in report['accounts']}
        self.assertEqual(actual[old_ap]['balance_cents'], 5000)
        self.assertEqual(actual[new_ap]['balance_cents'], 6000)
        self.assertEqual(actual[old_advance]['balance_cents'], 2000)
        self.assertEqual(actual[new_advance]['balance_cents'], 1000)
        self.assertEqual(report['totals']['ledger_net_cents'], 8000)
        self.assertIn(first['journal_id'], report['suppliers'][0]['journal_ids'])
        self.assertIn(second['journal_id'], report['suppliers'][0]['journal_ids'])

    def test_offsetting_wrong_supplier_balances_do_not_pass_on_grand_total(self):
        self.fixture.invoice()
        journal = payables.create_journal(self.db, 'review_shift', 1, '2026-10-21', [
            {'account_id': self.fixture.ap, 'debit_cents': 700, 'supplier_id': 1},
            {'account_id': self.fixture.ap, 'credit_cents': 700, 'supplier_id': 2},
        ])
        report = self.report()
        self.assertFalse(report['matched'])
        self.assertEqual(report['totals']['payable_difference_cents'], 0)
        self.assertEqual(report['counts']['mismatched_suppliers'], 2)
        rows = {row['supplier_id']: row for row in report['suppliers']}
        self.assertEqual(rows[1]['payable_difference_cents'], -700)
        self.assertEqual(rows[2]['payable_difference_cents'], 700)
        self.assertIn(journal, rows[1]['journal_ids'])
        self.assertIn(journal, rows[2]['journal_ids'])

    def test_wrong_control_account_transfer_cannot_hide_inside_one_supplier_total(self):
        self.fixture.invoice()
        alternate = self.fixture.account('211002')
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الموردون' AND role='payable_control'", (alternate,))
        self.fixture.invoice(cents=5000)
        payables.create_journal(self.db, 'review_wrong_control', 1, '2026-10-21', [
            {'account_id': self.fixture.ap, 'debit_cents': 1000, 'supplier_id': 1},
            {'account_id': alternate, 'credit_cents': 1000, 'supplier_id': 1},
        ])
        report = self.report()
        self.assertFalse(report['matched'])
        self.assertEqual(report['totals']['payable_difference_cents'], 0)
        self.assertEqual(report['suppliers'][0]['payable_difference_cents'], 0)
        self.assertFalse(report['suppliers'][0]['matched'])
        differences = {a['id']: a['difference_cents'] for a in report['accounts']}
        self.assertEqual(differences[self.fixture.ap], -1000)
        self.assertEqual(differences[alternate], 1000)
        self.assertEqual(report['counts']['mismatched_accounts'], 2)

    def test_wrong_advance_account_transfer_cannot_hide_inside_one_supplier_total(self):
        self.fixture.post(self.fixture.payload(amount='50', allow_advance=True))
        alternate = self.fixture.account('115003')
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الموردون' AND role='supplier_advances'", (alternate,))
        payables.create_journal(self.db, 'review_wrong_advance', 1, '2026-10-21', [
            {'account_id': self.fixture.advance, 'credit_cents': 1000, 'supplier_id': 1},
            {'account_id': alternate, 'debit_cents': 1000, 'supplier_id': 1},
        ])
        report = self.report()
        self.assertFalse(report['matched'])
        self.assertEqual(report['totals']['advance_difference_cents'], 0)
        self.assertEqual(report['suppliers'][0]['advance_difference_cents'], 0)
        self.assertFalse(report['suppliers'][0]['matched'])
        self.assertEqual(report['counts']['mismatched_accounts'], 2)

    def test_unassigned_control_lines_fail_even_when_their_net_is_zero(self):
        payables.create_journal(self.db, 'review_orphan', 1, '2026-10-21', [
            {'account_id': self.fixture.ap, 'debit_cents': 700},
            {'account_id': self.fixture.ap, 'credit_cents': 700},
        ])
        report = self.report()
        self.assertFalse(report['matched'])
        self.assertEqual(report['totals']['payable_difference_cents'], 0)
        self.assertEqual(report['totals']['unassigned_payable_cents'], 0)
        self.assertEqual([row['type'] for row in report['exceptions']], ['missing_supplier', 'missing_supplier'])
        self.assertTrue(all(row['journal_id'] for row in report['exceptions']))
        self.assert_matched(self.report('2026-10-20'))

    def test_source_supplier_dimension_is_checked_even_for_a_zero_net_journal(self):
        self.fixture.counter += 1
        draft_id = self.db.execute("""INSERT INTO invoices(code,request_key,supplier_id,reference,business_date,department_id,status,subtotal_cents,total_cents,created_at,updated_at)
            VALUES('draft-check','draft-check',1,'check','2026-10-01',1,'draft',100,100,'now','now')""").lastrowid
        payables.create_journal(self.db, 'supplier_invoice', draft_id, '2026-10-21', [
            {'account_id': self.fixture.ap, 'debit_cents': 100, 'supplier_id': 2},
            {'account_id': self.fixture.ap, 'credit_cents': 100, 'supplier_id': 2},
        ])
        report = self.report()
        self.assertFalse(report['matched'])
        self.assertEqual(report['totals']['payable_difference_cents'], 0)
        self.assertEqual(report['counts']['mismatched_suppliers'], 1)
        self.assertTrue(all(row['type'] == 'source_supplier_mismatch' for row in report['exceptions']))
        self.assertTrue(all(row['expected_supplier_id'] == 1 for row in report['exceptions']))

    def test_unbalanced_sealed_journal_is_an_exception_despite_matching_control_net(self):
        # Simulate an imported/damaged sealed journal independently of the normal
        # balanced journal helper, which correctly refuses this transaction.
        journal = self.db.execute("""INSERT INTO journal_entries(code,source_type,source_id,entry_date,created_at,sealed,request_fingerprint)
            VALUES('IMPORT-BAD','review_import',1,'2026-10-21','now',0,'review')""").lastrowid
        self.db.executemany('INSERT INTO journal_lines(journal_id,account_id,debit_cents,credit_cents,supplier_id) VALUES(?,?,?,?,?)', [
            (journal, self.fixture.ap, 100, 0, 1),
            (journal, self.fixture.ap, 0, 100, 1),
            (journal, self.fixture.cash, 1, 0, 1),
        ])
        self.db.execute('UPDATE journal_entries SET sealed=1 WHERE id=?', (journal,))
        report = self.report()
        self.assertFalse(report['matched'])
        self.assertEqual(report['totals']['payable_difference_cents'], 0)
        self.assertEqual(report['exceptions'][0]['type'], 'unbalanced_journal')
        self.assertEqual(report['exceptions'][0]['journal_id'], journal)


    def settlement(self, source_type, source_id, invoice, amount, day):
        saved = supplier_settlements.save(self.db, {'request_key': 'set-' + day + '-' + str(source_id),
            'supplier_id': 1, 'source_type': source_type, 'source_id': source_id,
            'business_date': day, 'reference': 'SET-' + day,
            'allocations': [{'invoice_id': invoice['id'], 'amount': amount}]})
        return supplier_settlements.approve(self.db, saved['id'], {'revision': saved['revision']})

    def refund(self, source_type, source_id, amount, day):
        saved = supplier_adjustments.save_refund(self.db, {'request_key': 'ref-' + day + '-' + str(source_id),
            'supplier_id': 1, 'source_type': source_type, 'source_id': source_id,
            'business_date': day, 'reference': 'REF-' + day, 'reason': 'استرداد رصيد غير مستخدم',
            'amount': amount, 'method': 'cash', 'funding_account_id': self.fixture.cash,
            'details': {'received_by': 'أمين الخزينة'}})
        return supplier_adjustments.approve_refund(self.db, saved['id'], {'revision': saved['revision']})

    def test_advance_application_its_reversal_and_refund_match_each_dated_ledger(self):
        advance = self.fixture.post(self.fixture.payload(amount='100', allow_advance=True))
        invoice = self.fixture.invoice(cents=12000, business_date='2026-10-17', due_date='2026-10-31')
        applied = self.settlement('advance', advance['id'], invoice, '60', '2026-10-18')
        supplier_settlements.reverse(self.db, applied['id'], {'revision': applied['revision'],
            'business_date': '2026-10-20', 'reference': 'REV-SET', 'reason': 'تصحيح توزيع التسوية'})
        self.refund('advance', advance['id'], '25', '2026-10-22')
        expected = [('2026-10-16', 0, 10000), ('2026-10-18', 6000, 4000),
                    ('2026-10-20', 12000, 10000), ('2026-10-22', 12000, 7500)]
        for day, payable, advance_balance in expected:
            with self.subTest(day=day):
                report = self.report(day)
                self.assert_matched(report)
                self.assertEqual(report['totals']['ledger_payable_cents'], payable)
                self.assertEqual(report['totals']['ledger_advance_cents'], advance_balance)

    def test_paid_return_credit_settlement_and_refund_reconcile_without_losing_credit(self):
        original = self.fixture.invoice()
        self.fixture.post(self.fixture.payload(amount='100', allocation_mode='invoice', target_invoice_id=original['id']))
        self.fixture.returned(original, quantity='0.8')
        target = self.fixture.invoice(business_date='2026-10-21', due_date='2026-10-31')
        self.settlement('return_credit', original['id'], target, '30', '2026-10-22')
        self.refund('return_credit', original['id'], '20', '2026-10-23')
        for day, payable in [('2026-10-20', -8000), ('2026-10-21', 2000), ('2026-10-22', 2000), ('2026-10-23', 4000)]:
            with self.subTest(day=day):
                report = self.report(day)
                self.assert_matched(report)
                self.assertEqual(report['totals']['ledger_payable_cents'], payable)
        balances = payables.supplier_balances(self.db, 1, '2026-10-23')
        self.assertEqual(balances['unapplied_return_credit_cents'], 3000)
        self.assertEqual(balances['outstanding_cents'], 7000)

    def test_reversed_cash_payment_restores_original_supplier_on_reversal_date(self):
        invoice = self.fixture.invoice()
        payment = self.fixture.post(self.fixture.payload(allocation_mode='invoice', target_invoice_id=invoice['id']))
        supplier_adjustments.reverse_payment(self.db, payment['id'], {'request_key': 'reverse-cash',
            'revision': payment['revision'], 'business_date': '2026-10-18', 'reason': 'تصحيح مستند السداد'})
        for day, payable in [('2026-10-17', 7000), ('2026-10-18', 10000)]:
            report = self.report(day)
            self.assert_matched(report)
            self.assertEqual(report['totals']['ledger_payable_cents'], payable)

    def test_cheque_stop_resume_clearance_and_bounce_remain_reconciled(self):
        invoice = self.fixture.invoice()
        payment = self.fixture.post(self.fixture.payload('cheque', allocation_mode='invoice', target_invoice_id=invoice['id']))
        for index, (action, day) in enumerate([('stop', '2026-10-17'), ('resume', '2026-10-18')]):
            supplier_adjustments.cheque_event(self.db, payment['id'], {'request_key': 'event-' + action,
                'revision': payment['revision'], 'event_revision': index, 'action': action,
                'business_date': day, 'reference': 'BANK-' + action, 'reason': 'تعليمات البنك'})
        payment = supplier_payments.clear_cheque(self.db, payment['id'], {'revision': payment['revision'],
            'clearance_date': '2026-10-20', 'event_revision': 2, 'bank_account_id': self.fixture.bank, 'reference': 'CLEAR-1'})
        supplier_adjustments.cheque_event(self.db, payment['id'], {'request_key': 'event-bounce',
            'revision': payment['revision'], 'event_revision': 2, 'action': 'bounce',
            'business_date': '2026-10-22', 'reference': 'BANK-RETURN', 'reason': 'ارتداد الشيك'})
        for day, payable in [('2026-10-17', 7000), ('2026-10-20', 7000), ('2026-10-22', 10000)]:
            report = self.report(day)
            self.assert_matched(report)
            self.assertEqual(report['totals']['ledger_payable_cents'], payable)

    def test_invalid_report_dates_are_rejected(self):
        for invalid in ('2026-02-30', '20261027', 'not a date', 20261027):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    supplier_reconciliation.report(self.db, invalid)


if __name__ == '__main__':
    unittest.main(verbosity=2)
