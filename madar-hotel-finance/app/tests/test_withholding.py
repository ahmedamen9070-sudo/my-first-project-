"""Explicit supplier withholding snapshots and exact historical report invariants."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting
import payables
import supplier_adjustments as adjustments
import supplier_payments as payments
import supplier_settlements as settlements
import withholding as wh
import test_supplier_payments as fixtures


class Withholding(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SupplierPayments('runTest')
        self.fixture.setUp()
        self.db = self.fixture.db
        wh.initialize(self.db)
        adjustments.initialize(self.db)
        settlements.initialize(self.db)
        self.account = self.fixture.account('213004')
        self.count = 0

    def tearDown(self):
        self.fixture.tearDown()

    def code(self, rate='3', **extra):
        self.count += 1
        return wh.create_code(self.db, {'name':'كود اختبار خصم '+str(self.count),'rate':rate,'active':True,'confirmed':True,**extra})

    def draft_invoice(self, net, gross, supplier=1):
        self.fixture.counter += 1
        ident = self.db.execute('''INSERT INTO invoices(code,request_key,supplier_id,reference,business_date,department_id,
            status,subtotal_cents,tax_basis,tax_cents,total_cents,created_at,updated_at)
            VALUES(?,?,?,?,?,1,'draft',?,0,?,?,?,?)''',
            ('INV-WH-'+str(self.fixture.counter),'invoice-'+str(self.fixture.counter),supplier,'REF-'+str(self.fixture.counter),
             '2026-10-01',net,gross-net,gross,'2026-10-01','2026-10-01')).lastrowid
        # No fake tax transaction: this fixture posts original net and VAT as a
        # single expense for the purpose of testing payment's saved NET ratio.
        journal = payables.create_journal(self.db,'supplier_invoice',ident,'2026-10-01',[
            {'account_id':self.fixture.expense,'debit_cents':gross,'supplier_id':supplier},
            {'account_id':self.fixture.ap,'credit_cents':gross,'supplier_id':supplier}])
        self.db.execute('INSERT INTO invoice_terms(invoice_id,due_date) VALUES(?,?)',(ident,'2026-10-01'))
        self.db.execute('INSERT INTO invoice_postings(invoice_id,journal_id,payable_account_id,posted_at) VALUES(?,?,?,?)',
                        (ident,journal,self.fixture.ap,'2026-10-01'))
        return ident

    def value(self, amount, code, **changes):
        result = {'supplier_id':1,'payment_date':'2026-10-16','amount_cents':amount,
                  'funding_account_id':self.fixture.cash,'withholding_code_id':code['id'] if code else None}
        result.update(changes)
        return result

    def plan(self, invoice, amount, code, **changes):
        return wh.prepare(self.db,self.value(amount,code,**changes),[{'invoice_id':invoice,'amount_cents':amount}])

    def payment(self, invoice, code, amount='114', day='2026-10-16', post=True):
        payload = self.fixture.payload(amount=amount,allocation_mode='invoice',target_invoice_id=invoice,
                                       withholding_code_id=code['id'] if code else None,payment_date=day)
        saved = payments.save_payment(self.db,payload)
        return payments.approve_payment(self.db,saved['id'],{'revision':saved['revision']}) if post else saved

    def reverse(self, payment, day='2026-10-20'):
        self.count += 1
        return adjustments.reverse_payment(self.db,payment['id'],{'request_key':'reverse-wh-'+str(self.count),
            'revision':payment['revision'],'event_revision':0,'business_date':day,'reference':'CORR-'+str(self.count),
            'reason':'اختبار عكس كامل لحسابات المصدر الأصلية'})

    def test_pristine_config_has_no_assumed_rates_and_none_selection_preserves_cash(self):
        self.assertEqual(wh.snapshot(self.db)['codes'],[])
        before = self.db.total_changes
        prepared = wh.prepare(self.db,{'amount_cents':11400},[])
        self.assertEqual((prepared['withholding_cents'],prepared['net_cash_cents']),(0,11400))
        self.assertIsNone(prepared['withholding'])
        self.assertEqual(self.db.total_changes,before)
        wh.initialize(self.db)
        self.assertEqual(wh.snapshot(self.db)['codes'],[])
        self.assertEqual(wh.report(self.db,'2026-01-01','2026-12-31')['rows'],[])

    def test_explicit_rate_codes_validation_duplicate_and_revision(self):
        for rate in ('0','-1','100.01','3.001','NaN',True):
            with self.subTest(rate=rate),self.assertRaises(ValueError):
                self.code(rate)
        with self.assertRaises(ValueError):
            wh.create_code(self.db,{'name':'بدون نسبة'})
        code = self.code('3.25')
        self.assertEqual(code['rate_basis'],325)
        self.assertTrue(code['code'].startswith('WHT-'))
        self.assertEqual(wh.snapshot(self.db)['codes'][0]['rate'],'3.25')
        with self.assertRaises(ValueError):
            wh.create_code(self.db,{'name':code['name'],'rate':'2'})
        with self.assertRaises(accounting.ConflictError):
            wh.update_code(self.db,code['id'],{'revision':True,'rate':'4'})
        changed = wh.update_code(self.db,code['id'],{'revision':code['revision'],'rate':'4','confirmed':False})
        self.assertEqual(changed['revision'],2)
        self.assertFalse(changed['confirmed'])
        invoice = self.draft_invoice(10000,11400)
        with self.assertRaises(ValueError):
            self.plan(invoice,11400,changed)

    def test_net_excludes_vat_and_each_partial_allocation_has_its_own_basis(self):
        code = self.code()
        invoice = self.draft_invoice(10000,11400)
        whole = self.plan(invoice,11400,code)
        half = self.plan(invoice,5700,code)
        self.assertEqual((whole['withholding_base_cents'],whole['withholding_cents'],whole['net_cash_cents']),(10000,300,11100))
        self.assertEqual((half['withholding_base_cents'],half['withholding_cents'],half['net_cash_cents']),(5000,150,5550))
        self.assertEqual(half['withholding']['lines'][0]['allocated_cents'],5700)
        self.assertEqual(half['withholding']['lines'][0]['invoice_net_cents'],10000)

    def test_one_code_multiple_invoices_rounds_each_line_without_taxing_advance(self):
        code = self.code('3')
        first,second = self.draft_invoice(10000,11400),self.draft_invoice(5000,5700)
        result = wh.prepare(self.db,self.value(8550,code),[
            {'invoice_id':first,'amount_cents':5700},{'invoice_id':second,'amount_cents':2850}])
        self.assertEqual((result['withholding_base_cents'],result['withholding_cents'],result['net_cash_cents']),(7500,225,8325))
        for allocations in ([],[{'invoice_id':first,'amount_cents':8500}]):
            with self.assertRaises(ValueError):
                wh.prepare(self.db,self.value(8550,code),allocations)
        with self.assertRaises(ValueError):
            wh.prepare(self.db,self.value(11400,code),[{'invoice_id':first,'amount_cents':5700}]*2)

    def test_confirmed_distinct_liability_account_is_required(self):
        code = self.code()
        invoice = self.draft_invoice(10000,11400)
        mapping = self.db.execute("SELECT id FROM account_mappings WHERE role='withholding_payable'").fetchone()[0]
        self.db.execute("UPDATE account_mappings SET status='proposed' WHERE id=?",(mapping,))
        with self.assertRaisesRegex(ValueError,'ربط'):
            self.plan(invoice,11400,code)
        self.db.execute("UPDATE account_mappings SET status='confirmed',account_id=? WHERE id=?",(self.fixture.ap,mapping))
        with self.assertRaisesRegex(ValueError,'مستقل'):
            self.plan(invoice,11400,code)
        self.db.execute('UPDATE account_mappings SET account_id=? WHERE id=?',(self.account,mapping))
        self.assertEqual(self.plan(invoice,11400,code)['withholding']['account_id'],self.account)

    def test_tiny_payments_round_base_without_exceeding_original_net(self):
        code = self.code('3')
        invoice = self.draft_invoice(2,3)
        first = self.payment(invoice,code,'.01')
        second = self.payment(invoice,code,'.01','2026-10-17')
        third = self.payment(invoice,code,'.01','2026-10-18')
        self.assertEqual([p['withholding_base_cents'] for p in (first,second,third)],[1,1,0])
        self.assertEqual(sum(p['withholding_base_cents'] for p in (first,second,third)),2)

    def test_base_cap_checks_intermediate_future_use_before_its_later_reversal(self):
        code = self.code('3')
        invoice = self.draft_invoice(2,3)
        future = self.payment(invoice,code,'.01','2026-11-01')
        self.reverse(future,'2026-12-01')
        first = self.payment(invoice,code,'.01','2026-10-16')
        second = self.payment(invoice,code,'.01','2026-10-17')
        self.assertEqual((future['withholding_base_cents'],first['withholding_base_cents'],second['withholding_base_cents']),(1,1,0))
        self.assertEqual(wh._used_base(self.db,invoice,'2026-11-01',None),2)
        self.assertEqual(wh._used_base(self.db,invoice,'2026-12-01',None),1)

    def test_positive_supplier_cash_is_required_even_with_explicit_hundred_percent_code(self):
        code = self.code('100')
        invoice = self.draft_invoice(100,100)
        with self.assertRaisesRegex(ValueError,'أكبر من صفر'):
            self.plan(invoice,100,code)

    def test_snapshot_stale_config_and_supplier_details_need_resave(self):
        code = self.code()
        invoice = self.draft_invoice(10000,11400)
        draft = self.payment(invoice,code,post=False)
        plan = self.plan(invoice,11400,code)
        wh.validate_for_post(self.db,draft['id'],plan)
        self.db.execute("UPDATE suppliers SET tax_number='CHANGED' WHERE id=1")
        current = self.plan(invoice,11400,code)
        with self.assertRaises(accounting.ConflictError):
            wh.validate_for_post(self.db,draft['id'],current)
        wh.save_snapshot(self.db,draft['id'],current)
        wh.validate_for_post(self.db,draft['id'],current)
        changed = wh.update_code(self.db,code['id'],{'revision':code['revision'],'rate':'4'})
        with self.assertRaises(accounting.ConflictError):
            wh.validate_for_post(self.db,draft['id'],self.plan(invoice,11400,changed))

    def test_posted_snapshot_immutable_old_and_new_parent(self):
        code = self.code()
        invoice = self.draft_invoice(10000,11400)
        posted = self.payment(invoice,code,'57')
        draft = self.payment(invoice,code,'57','2026-10-17',post=False)
        for sql,args in (
            ('UPDATE payment_withholding SET rate_basis=500 WHERE payment_id=?',(posted['id'],)),
            ('DELETE FROM payment_withholding WHERE payment_id=?',(posted['id'],)),
            ('UPDATE payment_withholding_lines SET base_cents=1 WHERE payment_id=?',(posted['id'],)),
            ('DELETE FROM payment_withholding_lines WHERE payment_id=?',(posted['id'],)),
            ('UPDATE payment_withholding SET payment_id=? WHERE payment_id=?',(posted['id'],draft['id'])),
            ('UPDATE payment_withholding_lines SET payment_id=? WHERE payment_id=?',(posted['id'],draft['id'])),
        ):
            with self.subTest(sql=sql),self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql,args)
        with self.assertRaises(ValueError):
            wh.save_snapshot(self.db,posted['id'],self.plan(invoice,5700,code))

    def test_saved_certificate_and_signed_report_keep_original_supplier_rate_and_account(self):
        code = self.code()
        self.db.execute("UPDATE suppliers SET tax_number='ORIGINAL-TAX' WHERE id=1")
        invoice = self.draft_invoice(10000,11400)
        posted = self.payment(invoice,code)
        original_name = posted['withholding']['supplier_name']
        original_account = posted['withholding']['account_id']
        self.reverse(posted)
        self.db.execute("UPDATE suppliers SET name='اسم معدل',tax_number='NEW-TAX' WHERE id=1")
        wh.update_code(self.db,code['id'],{'revision':code['revision'],'rate':'7'})
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE role='withholding_payable'",(self.fixture.cheques,))
        saved = payments.get_payment(self.db,posted['id'])
        self.assertEqual(saved['withholding']['rate'],'3.00')
        self.assertEqual(saved['withholding']['supplier_name'],original_name)
        self.assertEqual(saved['withholding']['supplier_tax_number'],'ORIGINAL-TAX')
        self.assertEqual(saved['withholding']['certificate_code'],'WHT-'+posted['code'])
        before = wh.report(self.db,'2026-10-01','2026-10-19')
        after = wh.report(self.db,'2026-10-01','2026-10-20')
        correction = wh.report(self.db,'2026-10-20','2026-10-20')
        self.assertEqual(before['totals']['withholding_cents'],300)
        self.assertEqual(after['totals']['withholding_cents'],0)
        self.assertEqual(correction['totals']['withholding_cents'],-300)
        self.assertEqual(correction['rows'][0]['account_id'],original_account)
        self.assertEqual(correction['rows'][0]['supplier_tax_number'],'ORIGINAL-TAX')
        self.assertEqual(wh.net_cash(self.db,posted['id'],11400),11100)

    def test_report_is_readonly_and_filters_supplier_and_dates(self):
        code = self.code()
        first = self.draft_invoice(10000,11400)
        second = self.draft_invoice(5000,5700,2)
        self.payment(first,code)
        payload = self.fixture.payload(supplier_id=2,amount='57',allocation_mode='invoice',target_invoice_id=second,withholding_code_id=code['id'])
        self.fixture.post(payload)
        before = self.db.total_changes
        report = wh.report(self.db,'2026-10-16','2026-10-16',2)
        self.assertEqual(report['totals']['withholding_cents'],150)
        self.assertTrue(all(row['supplier_id']==2 for row in report['rows']))
        self.assertEqual(self.db.total_changes,before)
        with self.assertRaises(ValueError):
            wh.report(self.db,'2026-10-17','2026-10-16')
        with self.assertRaises(ValueError):
            wh.report(self.db,supplier_id=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
