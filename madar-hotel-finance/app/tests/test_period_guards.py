"""Direct AP module boundaries honor monthly locks, independently of HTTP routes."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting_periods as periods
import supplier_payments as payments
import supplier_settlements as settlements
import supplier_adjustments as adjustments
import test_supplier_adjustments as fixtures


class DirectPeriodGuards(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SupplierAdjustments()
        self.f.setUp()
        self.db = self.f.db
        settlements.initialize(self.db)
        periods.initialize(self.db)
        self.db.commit()
        self.seq = 0

    def tearDown(self):
        self.f.tearDown()

    def event(self, revision=1):
        self.seq += 1
        return {'request_key': 'period-' + str(self.seq), 'revision': revision,
                'reference': 'MONTH-REVIEW', 'reason': 'مراجعة الشهر وتوثيق المطابقة'}

    def close(self, month='2026-10'):
        return periods.close(self.db, month, self.event())

    def force_closed(self, month='2026-10'):
        # A legacy/migrated draft must not evade a lock already on disk. Normal
        # period.close separately refuses pending drafts before reaching this state.
        self.db.execute("INSERT INTO accounting_periods(month,status,updated_at) VALUES(?,'closed','fixture') ON CONFLICT(month) DO UPDATE SET status='closed'", (month,))

    def advance(self, amount='100.00', method='cash'):
        return self.f.advance_payment(amount, method)

    def settlement_data(self, advance, invoice, amount=1000, day='2026-10-21'):
        self.seq += 1
        return {'request_key': 'settle-' + str(self.seq), 'supplier_id': 1,
                'source_type': 'advance', 'source_id': advance['id'],
                'business_date': day, 'reference': 'SET-' + str(self.seq), 'notes': '',
                'allocations': [{'invoice_id': invoice['id'], 'amount_cents': amount}]}

    def post_settlement(self, data):
        saved = settlements.save(self.db, data)
        return settlements.approve(self.db, saved['id'], {'revision': saved['revision']})

    def assert_locked(self, function, *args):
        with self.assertRaisesRegex(ValueError, 'مقفولة'):
            function(*args)

    def test_new_payment_settlement_and_refund_drafts_reject_closed_dates(self):
        advance = self.advance()
        invoice = self.f.invoice(20000)
        self.close()
        payment = self.f.payload(amount='10', allocation_mode='invoice', target_invoice_id=invoice['id'])
        settlement = self.settlement_data(advance, invoice)
        refund = self.f.refund_data(advance['id'], amount='10')
        self.assert_locked(payments.save_payment, self.db, payment)
        self.assert_locked(settlements.save, self.db, settlement)
        self.assert_locked(adjustments.save_refund, self.db, refund)
        # Read-only calculation remains possible for review of a closed month.
        self.assertEqual(settlements.preview(self.db, settlement)['amount_cents'], 1000)
        self.assertEqual(adjustments.refund_preview(self.db, refund)['amount_cents'], 1000)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM supplier_refunds').fetchone()[0], 0)

    def test_payment_draft_cannot_edit_cancel_or_move_out_of_locked_month(self):
        invoice = self.f.invoice()
        data = self.f.payload(allocation_mode='invoice', target_invoice_id=invoice['id'])
        saved = payments.save_payment(self.db, data)
        self.force_closed()
        for changes in ({'action': 'cancel'}, {'payment_date': '2026-11-01'}, {'amount': '20'}):
            self.assert_locked(payments.update_payment, self.db, saved['id'], {**data, **changes, 'revision': saved['revision']})
        self.assertEqual(payments.get_payment(self.db, saved['id'])['revision'], 1)
        self.assert_locked(payments.approve_payment, self.db, saved['id'], {'revision': 1})

    def test_settlement_draft_cannot_edit_cancel_or_move_out_of_locked_month(self):
        advance = self.advance()
        invoice = self.f.invoice(20000)
        data = self.settlement_data(advance, invoice)
        saved = settlements.save(self.db, data)
        self.force_closed()
        for changes in ({'action': 'cancel'}, {'business_date': '2026-11-01'}, {'notes': 'تعديل'}):
            self.assert_locked(settlements.save, self.db, {**data, **changes, 'revision': saved['revision']}, saved['id'])
        self.assertEqual(settlements.detail(self.db, saved['id'])['revision'], 1)
        self.assert_locked(settlements.approve, self.db, saved['id'], {'revision': 1})

    def test_refund_draft_cannot_edit_cancel_or_move_out_of_locked_month(self):
        advance = self.advance()
        data = self.f.refund_data(advance['id'])
        saved = adjustments.save_refund(self.db, data)
        self.force_closed()
        for changes in ({'action': 'cancel'}, {'business_date': '2026-11-01'}, {'amount': '20'}):
            self.assert_locked(adjustments.save_refund, self.db, {**data, **changes, 'revision': saved['revision']}, saved['id'])
        self.assertEqual(adjustments.get_refund(self.db, saved['id'])['revision'], 1)
        self.assert_locked(adjustments.approve_refund, self.db, saved['id'], {'revision': 1})

    def test_new_closed_date_rejected_when_moving_open_drafts_backward(self):
        advance = self.advance()
        invoice = self.f.invoice(20000)
        self.close()
        pdata = self.f.payload(payment_date='2026-11-01', amount='10', allocation_mode='invoice', target_invoice_id=invoice['id'])
        payment = payments.save_payment(self.db, pdata)
        sdata = self.settlement_data(advance, invoice, day='2026-11-01')
        settlement = settlements.save(self.db, sdata)
        rdata = self.f.refund_data(advance['id'], amount='10', business_date='2026-11-01')
        refund = adjustments.save_refund(self.db, rdata)
        self.assert_locked(payments.update_payment, self.db, payment['id'], {**pdata, 'revision': 1, 'payment_date': '2026-10-25'})
        self.assert_locked(settlements.save, self.db, {**sdata, 'revision': 1, 'business_date': '2026-10-25'}, settlement['id'])
        self.assert_locked(adjustments.save_refund, self.db, {**rdata, 'revision': 1, 'business_date': '2026-10-25'}, refund['id'])

    def test_existing_save_and_approval_retries_are_allowed_after_close(self):
        pdata = self.f.payload(amount='100', allow_advance=True)
        advance = self.f.post(pdata)
        invoice = self.f.invoice(20000)
        sdata = self.settlement_data(advance, invoice)
        settlement = self.post_settlement(sdata)
        rdata = self.f.refund_data(advance['id'], amount='10')
        refund = self.f.refund(rdata)
        self.close()
        journal_count = self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0]
        self.assertEqual(payments.save_payment(self.db, pdata)['id'], advance['id'])
        self.assertEqual(payments.approve_payment(self.db, advance['id'], {'revision': 1})['id'], advance['id'])
        self.assertEqual(settlements.save(self.db, sdata)['id'], settlement['id'])
        self.assertEqual(settlements.approve(self.db, settlement['id'], {'revision': 1})['id'], settlement['id'])
        self.assertEqual(adjustments.save_refund(self.db, rdata)['id'], refund['id'])
        self.assertEqual(adjustments.approve_refund(self.db, refund['id'], {'revision': 1})['id'], refund['id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], journal_count)

    def test_payment_correction_of_closed_source_uses_open_new_date_and_retries(self):
        payment = self.advance()
        self.close()
        blocked = self.f.reverse_data(payment)
        self.assert_locked(adjustments.reverse_payment, self.db, payment['id'], blocked)
        data = self.f.reverse_data(payment, business_date='2026-11-01')
        reversed_payment = adjustments.reverse_payment(self.db, payment['id'], data)
        self.assertEqual(reversed_payment['effective_status'], 'reversed')
        self.close('2026-11')
        self.assertEqual(adjustments.reverse_payment(self.db, payment['id'], data)['reversal']['id'], reversed_payment['reversal']['id'])

    def test_settlement_correction_of_closed_source_uses_open_new_date_and_retries(self):
        advance = self.advance()
        invoice = self.f.invoice(20000)
        settlement = self.post_settlement(self.settlement_data(advance, invoice))
        self.close()
        data = {'revision': settlement['revision'], 'reference': 'UNDO', 'reason': 'تصحيح التوزيع', 'business_date': '2026-10-25'}
        self.assert_locked(settlements.reverse, self.db, settlement['id'], data)
        data['business_date'] = '2026-11-01'
        result = settlements.reverse(self.db, settlement['id'], data)
        self.close('2026-11')
        self.assertEqual(settlements.reverse(self.db, settlement['id'], data)['reversal']['journal_id'], result['reversal']['journal_id'])

    def test_operational_stop_and_resume_are_locked_without_creating_journals(self):
        cheque = self.advance(method='cheque')
        self.close()
        count = self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0]
        self.assert_locked(adjustments.cheque_event, self.db, cheque['id'], self.f.event_data(cheque, 'stop'))
        stop_data = self.f.event_data(cheque, 'stop', business_date='2026-11-01')
        stop = adjustments.cheque_event(self.db, cheque['id'], stop_data)
        self.assertEqual(stop['effective_cheque_status'], 'stopped')
        self.close('2026-11')
        self.assertEqual(adjustments.cheque_event(self.db, cheque['id'], stop_data)['event_history'][-1]['id'], stop['event_history'][-1]['id'])
        self.assert_locked(adjustments.cheque_event, self.db, cheque['id'], self.f.event_data(stop, 'resume', business_date='2026-11-02', event_revision=1))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0], count)
        periods.reopen(self.db, '2026-11', self.event(revision=2))
        resume = adjustments.cheque_event(self.db, cheque['id'], self.f.event_data(stop, 'resume', business_date='2026-11-02', event_revision=1))
        self.assertEqual(resume['effective_cheque_status'], 'issued')

    def test_clearance_of_closed_source_requires_open_clearance_date_and_is_idempotent(self):
        cheque = self.advance(method='cheque')
        self.close()
        data = {'revision': cheque['revision'], 'event_revision': 0, 'reference': 'BANK-CLEAR', 'bank_account_id': self.f.bank, 'clearance_date': '2026-10-25'}
        self.assert_locked(payments.clear_cheque, self.db, cheque['id'], data)
        data['clearance_date'] = '2026-11-01'
        cleared = payments.clear_cheque(self.db, cheque['id'], data)
        self.close('2026-11')
        self.assertEqual(payments.clear_cheque(self.db, cheque['id'], data)['clearance_journal_entry_id'], cleared['clearance_journal_entry_id'])

    def test_cancel_and_bounce_cheques_guard_new_event_date_only(self):
        first = self.advance(method='cheque')
        pdata = self.f.payload('cheque', amount='20', allow_advance=True)
        pdata['details'] = {**pdata['details'], 'cheque_number': 'SECOND'}
        second = self.f.post(pdata)
        second = self.f.clear(second)
        self.close()
        self.assert_locked(adjustments.cheque_event, self.db, first['id'], self.f.event_data(first, 'cancel'))
        self.assert_locked(adjustments.cheque_event, self.db, second['id'], self.f.event_data(second, 'bounce'))
        cancelled = adjustments.cheque_event(self.db, first['id'], self.f.event_data(first, 'cancel', business_date='2026-11-01'))
        bounced = adjustments.cheque_event(self.db, second['id'], self.f.event_data(second, 'bounce', business_date='2026-11-02'))
        self.assertEqual(cancelled['effective_cheque_status'], 'cancelled')
        self.assertEqual(bounced['effective_cheque_status'], 'bounced')

    def test_later_closed_month_blocks_earlier_open_month_direct_mutations(self):
        advance = self.advance()
        invoice = self.f.invoice(20000)
        self.close('2026-11')
        for function, data in ((payments.save_payment, self.f.payload(amount='10', allocation_mode='invoice', target_invoice_id=invoice['id'])),
                               (settlements.save, self.settlement_data(advance, invoice)),
                               (adjustments.save_refund, self.f.refund_data(advance['id'], amount='10'))):
            self.assert_locked(function, self.db, data)
        self.assertEqual(periods.snapshot(self.db, 2026)['periods'][9]['status'], 'open')
        self.assertTrue(periods.snapshot(self.db, 2026)['periods'][9]['effective_locked'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
