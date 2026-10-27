"""Financial invariants for dated supplier refunds and immutable corrections."""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_supplier_payments as fixtures
import accounting
import payables
import supplier_payments as payments
import supplier_adjustments as adjustments


class SupplierAdjustments(unittest.TestCase):
    account = fixtures.SupplierPayments.account
    invoice = fixtures.SupplierPayments.invoice
    details = fixtures.SupplierPayments.details
    payload = fixtures.SupplierPayments.payload
    post = fixtures.SupplierPayments.post
    returned = fixtures.SupplierPayments.returned
    assert_balanced = fixtures.SupplierPayments.assert_balanced

    def setUp(self):
        fixtures.SupplierPayments.setUp(self)
        adjustments.initialize(self.db)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def advance_payment(self, amount='100.00', method='cash'):
        return self.post(self.payload(method, amount=amount, allow_advance=True))

    def refund_data(self, source_id, source_type='advance', amount='30.00', method='cash', **changes):
        self.counter += 1
        result = {'request_key': f'refund-{self.counter}', 'supplier_id': 1, 'source_type': source_type,
                  'source_id': source_id, 'amount': amount, 'business_date': '2026-10-22', 'reference': f'REFUND-{self.counter}',
                  'reason': 'استرداد رصيد زائد موثق', 'method': method, 'funding_account_id': self.cash if method=='cash' else self.bank,
                  'details': {'received_by': 'أمين الخزينة', 'receipt_reference': 'RC-1'} if method=='cash' else {'bank_name': 'بنك الفندق', 'transaction_reference': 'REF-TR-1', 'value_date': '2026-10-22'}}
        result.update(changes)
        return result

    def refund(self, data):
        draft = adjustments.save_refund(self.db, data)
        return adjustments.approve_refund(self.db, draft['id'], {'revision': draft['revision']})

    def reverse_data(self, payment, **changes):
        self.counter += 1
        value = {'request_key': f'reverse-{self.counter}', 'business_date': '2026-10-25', 'reason': 'تصحيح سداد سجل بطريق الخطأ',
                 'revision': payment['revision'], 'event_revision': 0}
        value.update(changes)
        return value

    def event_data(self, payment, action, **changes):
        value = {**self.reverse_data(payment), 'action': action, 'reference': 'BANK-ACTION-1'}
        value.update(changes)
        return value

    def clear(self, payment):
        return payments.clear_cheque(self.db, payment['id'], {'revision': payment['revision'], 'clearance_date': '2026-10-21', 'bank_account_id': self.bank, 'reference': 'CLEAR-1', 'event_revision': self.db.execute('SELECT COUNT(*) FROM supplier_cheque_events WHERE payment_id=?',(payment['id'],)).fetchone()[0]})

    def test_cash_advance_refund_is_receipt_and_preserves_original_source(self):
        payment = self.advance_payment()
        before = payments.account_balance(self.db, self.cash)
        data = self.refund_data(payment['id'])
        preview = adjustments.refund_preview(self.db, data)
        self.assertEqual(preview['available_cents'], 10000)
        self.assertEqual(preview['remaining_cents'], 7000)
        self.assertEqual(preview['funding_balance_after_cents'], before+3000)
        saved = adjustments.save_refund(self.db, data)
        self.assertEqual(payments.account_balance(self.db, self.cash), before)
        result = adjustments.approve_refund(self.db, saved['id'], {'revision': saved['revision']})
        self.assertIn('قبض', result['voucher_title'])
        self.assert_balanced(result['journal'])
        self.assertEqual(payments.account_balance(self.db, self.cash), before+3000)
        self.assertEqual(adjustments.refund_consumption(self.db, 'advance', payment['id'], '2026-10-21'), 0)
        self.assertEqual(adjustments.refund_consumption(self.db, 'advance', payment['id'], '2026-10-22'), 3000)
        self.assertEqual(self.db.execute('SELECT amount_cents FROM supplier_payments WHERE id=?', (payment['id'],)).fetchone()[0], 10000)

    def test_bank_refund_uses_original_advance_account_after_mapping_changes(self):
        payment = self.advance_payment()
        new_account = self.account('115003')
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الموردون' AND role='supplier_advances'", (new_account,))
        result = self.refund(self.refund_data(payment['id'], method='bank_transfer'))
        credit = next(l for l in result['journal']['lines'] if l['credit_cents'])
        debit = next(l for l in result['journal']['lines'] if l['debit_cents'])
        self.assertEqual(credit['account_id'], self.advance)
        self.assertEqual(debit['account_id'], self.bank)
        self.assertEqual(payments.account_balance(self.db, self.advance), 7000)

    def test_bank_value_date_is_independent_of_accounting_business_date(self):
        payment=self.advance_payment()
        data=self.refund_data(payment['id'],method='bank_transfer',details={'bank_name':'بنك الفندق','transaction_reference':'BANK-DIFFERENT-DATE','value_date':'2026-10-24'})
        result=self.refund(data)
        self.assertEqual(result['details']['value_date'],'2026-10-24')
        self.assertEqual(result['journal']['entry_date'],'2026-10-22')
        self.assertEqual(payments.account_balance(self.db,self.bank,'2026-10-21'),0)
        self.assertEqual(payments.account_balance(self.db,self.bank,'2026-10-22'),3000)

    def test_refund_save_retry_and_approval_idempotence_and_immutable_posted_rows(self):
        payment = self.advance_payment()
        data = self.refund_data(payment['id'])
        first = adjustments.save_refund(self.db, data)
        self.assertEqual(adjustments.save_refund(self.db, data)['id'], first['id'])
        with self.assertRaises(accounting.ConflictError): adjustments.save_refund(self.db, {**data,'amount':'31'})
        result = adjustments.approve_refund(self.db, first['id'], {'revision': first['revision']})
        self.assertEqual(adjustments.approve_refund(self.db, first['id'], {'revision':1})['journal_id'], result['journal_id'])
        with self.assertRaises(sqlite3.IntegrityError): self.db.execute('UPDATE supplier_refunds SET amount_cents=1 WHERE id=?', (first['id'],))
        with self.assertRaises(sqlite3.IntegrityError): self.db.execute('DELETE FROM supplier_refunds WHERE id=?', (first['id'],))

    def test_refund_drafts_recheck_source_before_approval(self):
        payment = self.advance_payment()
        one = adjustments.save_refund(self.db, self.refund_data(payment['id'], amount='70'))
        two = adjustments.save_refund(self.db, self.refund_data(payment['id'], amount='70'))
        adjustments.approve_refund(self.db, one['id'], {'revision':1})
        with self.assertRaises(ValueError): adjustments.approve_refund(self.db, two['id'], {'revision':1})
        self.assertEqual(adjustments.get_refund(self.db, two['id'])['status'], 'draft')
        self.assertEqual(adjustments.refund_consumption(self.db, 'advance', payment['id'], '9999-12-31'), 7000)

    def test_refund_rejects_foreign_supplier_dates_source_and_irrelevant_details(self):
        payment = self.advance_payment()
        for changes in [dict(supplier_id=2),dict(business_date='2026-10-01'),dict(source_type='unknown'),dict(amount='100.01'),dict(details={'card_number':'123456789'}),dict(method='cheque'),dict(funding_account_id=self.ap)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                adjustments.save_refund(self.db, self.refund_data(payment['id'], **changes))
        with self.assertRaises(ValueError): adjustments.save_refund(self.db, self.refund_data(payment['id'], method='bank_transfer',details={'bank_name':'bank','transaction_reference':'x','value_date':'not-a-date'}))

    def test_future_refund_reserves_source_against_backdated_second_refund(self):
        payment = self.advance_payment()
        self.refund(self.refund_data(payment['id'], amount='70', business_date='2026-11-01'))
        with self.assertRaises(ValueError): adjustments.save_refund(self.db, self.refund_data(payment['id'], amount='40'))
        self.assertEqual(adjustments.refund_preview(self.db, self.refund_data(payment['id'], amount='30'))['available_cents'], 3000)

    def test_return_credit_refund_restores_cash_and_reduces_credit_without_repricing(self):
        invoice = self.invoice()
        self.post(self.payload(amount='100',allocation_mode='invoice',target_invoice_id=invoice['id']))
        self.returned(invoice, quantity='0.5')
        result = self.refund(self.refund_data(invoice['id'], source_type='return_credit', amount='20'))
        credit = next(l for l in result['journal']['lines'] if l['credit_cents'])
        self.assertEqual(credit['account_id'], self.ap)
        self.assertEqual(payables.supplier_balances(self.db, 1, '2026-10-21')['unapplied_return_credit_cents'], 5000)
        self.assertEqual(payables.supplier_balances(self.db, 1, '2026-10-22')['unapplied_return_credit_cents'], 3000)
        self.assertEqual(payables.enrich_product(self.db, dict(self.db.execute('SELECT * FROM products WHERE id=1').fetchone()))['last_purchase_price_cents'], 10000)
        with self.assertRaises(ValueError): adjustments.save_refund(self.db, self.refund_data(invoice['id'],source_type='return_credit',amount='31'))

    def test_reverse_payment_restores_dated_invoice_balance_and_uses_original_accounts(self):
        invoice = self.invoice()
        payment = self.post(self.payload(amount='40',allocation_mode='invoice',target_invoice_id=invoice['id']))
        data = self.reverse_data(payment)
        result = adjustments.reverse_payment(self.db, payment['id'], data)
        self.assertEqual(result['effective_status'], 'reversed')
        self.assertEqual(payables.invoice_outstanding(self.db, invoice['id'], '2026-10-24'), 6000)
        self.assertEqual(payables.invoice_outstanding(self.db, invoice['id'], '2026-10-25'), 10000)
        self.assertEqual(payments.account_balance(self.db, self.cash), 0)
        self.assertEqual(self.db.execute('SELECT status FROM supplier_payments WHERE id=?',(payment['id'],)).fetchone()[0], 'posted')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM payment_allocations WHERE payment_id=?',(payment['id'],)).fetchone()[0], 1)
        self.assertEqual(adjustments.reverse_payment(self.db,payment['id'],data)['reversal']['id'],result['reversal']['id'])
        with self.assertRaises(accounting.ConflictError): adjustments.reverse_payment(self.db,payment['id'],self.reverse_data(payment))

    def test_reverse_advance_removes_available_source_and_blocks_refunded_dependency(self):
        payment = self.advance_payment()
        self.refund(self.refund_data(payment['id']))
        with self.assertRaises(ValueError): adjustments.reverse_payment(self.db,payment['id'],self.reverse_data(payment))
        other = self.advance_payment('40')
        adjustments.reverse_payment(self.db,other['id'],self.reverse_data(other))
        self.assertTrue(adjustments.payment_reversed(self.db,other['id'],'2026-10-25'))
        with self.assertRaises(ValueError): adjustments.refund_preview(self.db,self.refund_data(other['id'], business_date='2026-10-26'))

    def test_reverse_payment_blocks_used_credit_on_previously_allocated_invoice(self):
        invoice = self.invoice()
        payment = self.post(self.payload(amount='100',allocation_mode='invoice',target_invoice_id=invoice['id']))
        self.returned(invoice,quantity='0.5')
        self.refund(self.refund_data(invoice['id'],source_type='return_credit',amount='10'))
        with self.assertRaises(ValueError): adjustments.reverse_payment(self.db,payment['id'],self.reverse_data(payment))

    def test_reverse_payment_blocks_active_settlement_of_advance_or_related_credit(self):
        self.db.execute('CREATE TABLE IF NOT EXISTS supplier_settlements(id INTEGER PRIMARY KEY,source_type TEXT,source_id INTEGER,business_date TEXT,amount_cents INTEGER,status TEXT)')
        payment = self.advance_payment()
        self.db.execute("INSERT INTO supplier_settlements(source_type,source_id,business_date,amount_cents,status) VALUES('advance',?,'2026-10-20',2000,'posted')",(payment['id'],))
        with self.assertRaises(ValueError): adjustments.reverse_payment(self.db,payment['id'],self.reverse_data(payment))

    def test_reverse_requires_reason_current_revision_and_noncheque_source(self):
        invoice=self.invoice()
        payment=self.post(self.payload(allocation_mode='invoice',target_invoice_id=invoice['id']))
        with self.assertRaises(ValueError): adjustments.reverse_payment(self.db,payment['id'],self.reverse_data(payment,reason=''))
        with self.assertRaises(accounting.ConflictError): adjustments.reverse_payment(self.db,payment['id'],self.reverse_data(payment,revision=1))
        with self.assertRaises(ValueError): adjustments.reverse_payment(self.db,payment['id'],self.reverse_data(payment,business_date='2026-10-01'))
        cheque=self.post(self.payload('cheque',allocation_mode='invoice',target_invoice_id=invoice['id']))
        with self.assertRaises(ValueError): adjustments.reverse_payment(self.db,cheque['id'],self.reverse_data(cheque))

    def test_stop_and_resume_are_operational_and_block_clearance_while_stopped(self):
        invoice=self.invoice()
        cheque=self.post(self.payload('cheque',allocation_mode='invoice',target_invoice_id=invoice['id']))
        journals=self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0]
        stop=adjustments.cheque_event(self.db,cheque['id'],self.event_data(cheque,'stop',business_date='2026-10-20'))
        self.assertEqual(stop['effective_cheque_status'],'stopped')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0],journals)
        with self.assertRaises(ValueError): self.clear(cheque)
        with self.assertRaises(accounting.ConflictError): adjustments.cheque_event(self.db,cheque['id'],self.event_data(cheque,'resume',business_date='2026-10-21'))
        resumed=adjustments.cheque_event(self.db,cheque['id'],self.event_data(cheque,'resume',business_date='2026-10-21',event_revision=1))
        self.assertEqual(resumed['effective_cheque_status'],'issued')
        self.clear(cheque)
        self.assertEqual(adjustments.payment_overlay(self.db,dict(self.db.execute('SELECT * FROM supplier_payments WHERE id=?',(cheque['id'],)).fetchone()),'9999-12-31')['effective_cheque_status'],'cleared')

    def test_cancel_unpresented_cheque_reopens_invoice_without_bank_movement(self):
        invoice=self.invoice()
        cheque=self.post(self.payload('cheque',allocation_mode='invoice',target_invoice_id=invoice['id']))
        data=self.event_data(cheque,'cancel',business_date='2026-10-22')
        result=adjustments.cheque_event(self.db,cheque['id'],data)
        self.assertEqual(result['effective_cheque_status'],'cancelled')
        self.assertEqual(payments.account_balance(self.db,self.bank),0)
        self.assertEqual(payments.account_balance(self.db,self.cheques),0)
        self.assertEqual(payables.invoice_outstanding(self.db,invoice['id'],'2026-10-22'),10000)
        with self.assertRaises(ValueError): self.clear(cheque)
        self.assertEqual(adjustments.cheque_event(self.db,cheque['id'],data)['adjustment_revision'],1)

    def test_bounced_cleared_cheque_reverses_bank_and_issue_once(self):
        invoice=self.invoice()
        cheque=self.post(self.payload('cheque',allocation_mode='invoice',target_invoice_id=invoice['id']))
        cleared=self.clear(cheque)
        data=self.event_data(cleared,'bounce',business_date='2026-10-23')
        result=adjustments.cheque_event(self.db,cheque['id'],data)
        self.assertEqual(result['effective_cheque_status'],'bounced')
        self.assertEqual(payments.account_balance(self.db,self.bank),0)
        self.assertEqual(payments.account_balance(self.db,self.cheques),0)
        self.assertEqual(payables.invoice_outstanding(self.db,invoice['id'],'2026-10-22'),7000)
        self.assertEqual(payables.invoice_outstanding(self.db,invoice['id'],'2026-10-23'),10000)
        self.assertIsNotNone(result['reversal']['clearance_journal_id'])
        self.assertEqual(adjustments.cheque_event(self.db,cheque['id'],data)['reversal']['id'],result['reversal']['id'])
        with self.assertRaises(ValueError): adjustments.cheque_event(self.db,cheque['id'],self.event_data(cleared,'cancel'))

    def test_operational_events_are_immutable_and_reject_backdated_or_changed_retries(self):
        invoice=self.invoice()
        cheque=self.post(self.payload('cheque',allocation_mode='invoice',target_invoice_id=invoice['id']))
        data=self.event_data(cheque,'stop',business_date='2026-10-20')
        adjustments.cheque_event(self.db,cheque['id'],data)
        with self.assertRaises(accounting.ConflictError): adjustments.cheque_event(self.db,cheque['id'],{**data,'reason':'سبب آخر'})
        with self.assertRaises(ValueError): adjustments.cheque_event(self.db,cheque['id'],self.event_data(cheque,'resume',event_revision=1,business_date='2026-10-19'))
        with self.assertRaises(sqlite3.IntegrityError): self.db.execute('DELETE FROM supplier_cheque_events')
        with self.assertRaises(sqlite3.IntegrityError): self.db.execute("UPDATE supplier_cheque_events SET reason='changed'")

    def make_settlement_cycle(self, source_type, source_id, target, cents, day='2026-03-01', reverse_day='2026-04-01'):
        import supplier_settlements as settlements
        settlements.initialize(self.db)
        self.counter += 1
        saved = settlements.save(self.db, {'request_key': f'set-cycle-{self.counter}', 'supplier_id':1,
            'source_type':source_type,'source_id':source_id,'business_date':day,'reference':f'SET-CYCLE-{self.counter}',
            'allocations':[{'invoice_id':target['id'],'amount_cents':cents}]})
        posted = settlements.approve(self.db,saved['id'],{'revision':saved['revision']})
        return settlements.reverse(self.db,posted['id'],{'revision':posted['revision'],'business_date':reverse_day,
            'reference':f'REV-SET-{self.counter}','reason':'عكس تسوية اختبار زمني'})

    def test_backdated_advance_refund_checks_temporary_future_consumption(self):
        # Move the source and invoice into the earlier part of the financial year.
        # Sources are generated directly at their proper dates, never edited after approval.
        payment=self.post(self.payload(amount='100',allow_advance=True,payment_date='2026-01-10'))
        target=self.invoice(cents=20000,business_date='2026-01-15',due_date='2026-01-31')
        self.make_settlement_cycle('advance',payment['id'],target,8000)
        with self.assertRaises(ValueError):
            adjustments.refund_preview(self.db,self.refund_data(payment['id'],amount='80',business_date='2026-02-01'))
        self.assertEqual(adjustments.refund_preview(self.db,self.refund_data(payment['id'],amount='20',business_date='2026-02-01'))['available_cents'],2000)

    def test_backdated_credit_refund_checks_temporary_future_consumption(self):
        original=self.invoice(business_date='2026-01-10',due_date='2026-01-31')
        self.post(self.payload(amount='100',allocation_mode='invoice',target_invoice_id=original['id'],payment_date='2026-01-11'))
        self.returned(original,quantity='0.5',day='2026-01-20')
        target=self.invoice(cents=20000,business_date='2026-01-21',due_date='2026-01-31')
        self.make_settlement_cycle('return_credit',original['id'],target,4000)
        with self.assertRaises(ValueError):
            adjustments.refund_preview(self.db,self.refund_data(original['id'],source_type='return_credit',amount='20',business_date='2026-02-01'))
        self.assertEqual(adjustments.refund_preview(self.db,self.refund_data(original['id'],source_type='return_credit',amount='10',business_date='2026-02-01'))['available_cents'],1000)

    def test_payment_reversal_waits_until_future_dependency_has_been_reversed(self):
        payment=self.post(self.payload(amount='100',allow_advance=True,payment_date='2026-01-10'))
        target=self.invoice(cents=20000,business_date='2026-01-15',due_date='2026-01-31')
        self.make_settlement_cycle('advance',payment['id'],target,8000)
        with self.assertRaises(ValueError):
            adjustments.reverse_payment(self.db,payment['id'],self.reverse_data(payment,business_date='2026-02-01'))
        result=adjustments.reverse_payment(self.db,payment['id'],self.reverse_data(payment,business_date='2026-04-02',reference='CORRECT-REF'))
        self.assertEqual(result['reversal']['reference'],'CORRECT-REF')
        self.assertTrue(result['reversal']['code'].startswith('SPR-'))
        self.assertEqual(payments.account_balance(self.db,self.advance,'2026-04-02'),0)

    def test_caller_can_rollback_refund_or_reversal_with_its_journal(self):
        payment=self.advance_payment()
        self.db.commit()
        before=self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0]
        try:
            with self.db:
                self.refund(self.refund_data(payment['id']))
                raise RuntimeError('simulatecallerfailure')
        except RuntimeError: pass
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM supplier_refunds').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0],before)
        try:
            with self.db:
                adjustments.reverse_payment(self.db,payment['id'],self.reverse_data(payment))
                raise RuntimeError('simulatecallerfailure')
        except RuntimeError: pass
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM supplier_payment_reversals').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0],before)


if __name__=='__main__':
    unittest.main(verbosity=2)
