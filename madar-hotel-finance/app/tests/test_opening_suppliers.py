"""Opening AP sources reconcile without repeating purchases or cash movement."""
import sqlite3
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import opening_suppliers as opening
import payables
import supplier_payments as payments
import supplier_settlements as settlements
import supplier_adjustments as adjustments
import supplier_reconciliation as reconciliation
import withholding
import test_supplier_payments as fixtures


class OpeningSuppliers(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SupplierPayments('runTest')
        self.fixture.setUp()
        self.db = self.fixture.db
        settlements.initialize(self.db)
        adjustments.initialize(self.db)
        withholding.initialize(self.db)
        # Adapter contract fixture: the batch engine has separate balance,
        # period, permission, atomic approval and immutable-draft tests.
        self.db.executescript('''CREATE TABLE opening_batches(id INTEGER PRIMARY KEY,status TEXT,business_date TEXT,clearing_account_id INTEGER);
            INSERT INTO opening_batches VALUES(1,'posted','2026-01-01',NULL);
            CREATE TABLE opening_lines(id INTEGER PRIMARY KEY,batch_id INTEGER,business_date TEXT,
            kind TEXT,account_id INTEGER,side TEXT,amount_cents INTEGER,supplier_id INTEGER,
            custodian_id INTEGER,reference TEXT,due_date TEXT,source_date TEXT,department_id INTEGER,
            journal_id INTEGER,document_type TEXT,document_id INTEGER,extra_json TEXT NOT NULL DEFAULT '{}');''')
        opening.initialize(self.db)
        self.clearing = self.db.execute("SELECT id FROM accounts WHERE code LIKE '3%' AND posting=1 ORDER BY code LIMIT 1").fetchone()[0]
        self.db.execute('UPDATE opening_batches SET clearing_account_id=?',(self.clearing,))
        self.count = 0
        self.db.commit()

    def tearDown(self):
        self.fixture.tearDown()

    def source(self, kind='supplier_invoice', cents=10000, **extra):
        self.count += 1
        line = opening.prepare(self.db, {'kind': kind, 'supplier_id': 1, 'amount_cents': cents,
            'reference': 'PRIOR-'+str(self.count), 'source_date': '2025-11-01',
            'due_date': '2025-12-01', **extra}, '2026-01-01')
        keys = ('batch_id','business_date','kind','account_id','side','amount_cents','supplier_id','custodian_id','reference','due_date','source_date','department_id')
        line.update(batch_id=1,business_date='2026-01-01')
        line['id'] = self.db.execute('INSERT INTO opening_lines('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')', tuple(line.get(k) for k in keys)).lastrowid
        side, contra = ('debit_cents','credit_cents') if line['side']=='debit' else ('credit_cents','debit_cents')
        journal = payables.create_journal(self.db, 'opening_balance', line['id'], line['business_date'], [
            {'account_id': line['account_id'],side:cents,'supplier_id':1,'department_id':line.get('department_id'),'source_line_id':line['id']},
            {'account_id': self.clearing,contra:cents}])
        self.db.execute('UPDATE opening_lines SET journal_id=? WHERE id=?',(journal,line['id']))
        metadata=opening.post_line(self.db,line,journal)
        self.db.execute('UPDATE opening_lines SET document_type=?,document_id=?,extra_json=? WHERE id=?',
                        (metadata['document_type'],metadata['document_id'],json.dumps({**line['extra'],**metadata}),line['id']))
        return metadata

    def invoice(self, amount=10000, **extra):
        doc = self.source(cents=amount, **extra)
        return payables._invoice_detail(self.db,doc['invoice_id'])

    def settlement(self, source, target, amount, origin='opening', day='2026-03-01'):
        self.count += 1
        payload = {'request_key':'SET-'+str(self.count),'reference':'SET-'+str(self.count),
            'supplier_id':1,'source_type':'advance','source_id':source['advance_id'],
            'source_origin':origin,'business_date':day,
            'allocations':[{'invoice_id':target['id'],'amount_cents':amount}]}
        draft = settlements.save(self.db,payload)
        return settlements.approve(self.db,draft['id'],{'revision':draft['revision']})

    def refund_payload(self, source, amount='10', origin='opening', day='2026-03-02'):
        self.count += 1
        return {'request_key':'REF-'+str(self.count),'reference':'REF-'+str(self.count),'supplier_id':1,
            'source_type':'advance','source_id':source['advance_id'],'source_origin':origin,
            'business_date':day,'amount':amount,'reason':'استرداد رصيد سابق','method':'cash',
            'funding_account_id':self.fixture.cash,'details':{'received_by':'أمين الخزينة'}}

    def refund(self, payload):
        draft = adjustments.save_refund(self.db,payload)
        return adjustments.approve_refund(self.db,draft['id'],{'revision':draft['revision']})

    def available(self, ident, origin='opening', day='9999-12-31'):
        return next(s for s in settlements.source_balances(self.db,1,day)['sources']
                    if s['source_type']=='advance' and s['source_id']==ident and s['source_origin']==origin)['available_cents']

    def assert_matched(self, day='9999-12-31'):
        result = reconciliation.report(self.db,day)
        self.assertTrue(result['matched'],result['exceptions'])
        self.assertEqual(result['totals']['net_difference_cents'],0)
        return result

    def test_old_invoice_credit_and_advance_age_without_purchase_cash_or_tax(self):
        invoice = self.invoice(10000)
        credit = self.source('supplier_credit',2000)
        advance = self.source('supplier_advance',3000)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM invoice_lines').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM supplier_payments').fetchone()[0],0)
        self.assertEqual(payments.account_balance(self.db,self.fixture.cash),0)
        self.assertEqual(payments.account_balance(self.db,self.fixture.expense),0)
        self.assertEqual(invoice['total_cents'],10000)
        self.assertTrue(invoice['is_opening'])
        self.assertEqual(invoice['opening_source_date'],'2025-11-01')
        self.assertEqual(invoice['lines'],[])
        report = payables.aging(self.db,'2026-03-05')['totals']
        self.assertEqual((report['days_over_90_cents'],report['credit_cents'],report['advance_cents'],report['net_payable_cents']),(10000,2000,3000,5000))
        self.assertEqual(payables.supplier_balances(self.db,1,'2025-12-31')['net_payable_cents'],0)
        self.assertEqual(payables.ledger_summary(self.db)['posted_invoices_cents'],0)
        self.assertEqual(payables.ledger_summary(self.db)['opening_payables_cents'],8000)
        self.assert_matched()
        statement = payables.statement(self.db,1)['rows']
        self.assertTrue(all(row['description']=='رصيد افتتاحي للمورد' for row in statement))
        self.assertEqual({row['code'] for row in statement},{invoice['code'],credit['code'],advance['code']})

    def test_partial_invoice_payment_and_general_fifo_allocate_opening_dues(self):
        one,two = self.invoice(10000),self.invoice(8000,reference='PRIOR-SECOND',due_date='2025-12-15')
        first = self.fixture.post(self.fixture.payload(amount='30',payment_date='2026-02-01',allocation_mode='invoice',target_invoice_id=one['id']))
        self.assertEqual(first['allocations'][0]['invoice_id'],one['id'])
        general = self.fixture.post(self.fixture.payload(amount='90',payment_date='2026-02-02',allocation_mode='fifo'))
        self.assertEqual([(r['invoice_id'],r['amount_cents']) for r in general['allocations']],[(one['id'],7000),(two['id'],2000)])
        self.assertEqual(payables.supplier_balances(self.db,1)['net_payable_cents'],6000)
        self.assert_matched()

    def test_opening_credit_settles_and_refunds_as_credit_not_physical_return(self):
        target = self.invoice()
        credit = self.source('supplier_credit',4000)
        payload={'request_key':'credit-set','reference':'CS','supplier_id':1,'source_type':'return_credit',
            'source_id':credit['invoice_id'],'business_date':'2026-02-01','allocations':[{'invoice_id':target['id'],'amount_cents':2500}]}
        draft=settlements.save(self.db,payload)
        settlements.approve(self.db,draft['id'],{'revision':draft['revision']})
        data=self.refund_payload({'advance_id':credit['invoice_id']},amount='15',origin='operating')
        data['source_type']='return_credit'
        self.refund(data)
        self.assertEqual(payables.supplier_balances(self.db,1)['net_payable_cents'],7500)
        with self.assertRaisesRegex(ValueError,'افتتاحي'):
            payables._return_payload(self.db,{'invoice_id':target['id']})
        self.assert_matched()

    def test_opening_advance_partial_use_refund_reversals_restore_dated_capacity(self):
        invoice=self.invoice()
        advance=self.source('supplier_advance',10000)
        settlement=self.settlement(advance,invoice,4000)
        refund=self.refund(self.refund_payload(advance,amount='20'))
        self.assertEqual(self.available(advance['advance_id']),4000)
        self.assertEqual(refund['source_origin'],'opening')
        self.assertEqual(refund['source_code'],advance['code'])
        adjustments.reverse_refund(self.db,refund['id'],{'request_key':'refund-reverse','revision':refund['revision'],
            'event_revision':0,'business_date':'2026-04-01','reference':'RR','reason':'تصحيح الاسترداد'})
        settlements.reverse(self.db,settlement['id'],{'revision':settlement['revision'],
            'business_date':'2026-04-02','reference':'RS','reason':'تصحيح التوزيع'})
        self.assertEqual(self.available(advance['advance_id'],day='2026-03-31'),4000)
        self.assertEqual(self.available(advance['advance_id'],day='2026-04-01'),6000)
        self.assertEqual(self.available(advance['advance_id'],day='2026-04-02'),10000)
        self.assert_matched('2026-03-31');self.assert_matched()
        with self.assertRaisesRegex(ValueError,'يتجاوز'):
            adjustments.refund_preview(self.db,self.refund_payload(advance,amount='80',day='2026-02-01'))

    def test_colliding_operating_and_opening_ids_are_independent_and_retries_compatible(self):
        target=self.invoice(20000)
        advance=self.source('supplier_advance',10000)
        payment=self.fixture.post(self.fixture.payload(amount='80',payment_date='2026-02-01',allow_advance=True))
        self.assertEqual(payment['id'],advance['advance_id'])
        self.settlement(advance,target,3000)
        self.refund(self.refund_payload(advance,amount='10'))
        self.assertEqual((self.available(advance['advance_id']),self.available(payment['id'],'operating')),(6000,8000))
        data=self.refund_payload({'advance_id':payment['id']},amount='20',origin='operating')
        old=dict(data);old.pop('source_origin')
        saved=adjustments.save_refund(self.db,old)
        self.assertEqual(adjustments.save_refund(self.db,data)['id'],saved['id'])
        adjustments.approve_refund(self.db,saved['id'],{'revision':saved['revision']})
        self.assertEqual((self.available(advance['advance_id']),self.available(payment['id'],'operating')),(6000,6000))
        data={'request_key':'legacy-set','reference':'LEG','supplier_id':1,'source_type':'advance','source_id':payment['id'],
            'business_date':'2026-03-05','allocations':[{'invoice_id':target['id'],'amount_cents':1000}]}
        saved=settlements.save(self.db,data)
        self.assertEqual(settlements.save(self.db,{**data,'source_origin':'operating'})['id'],saved['id'])
        settlements.approve(self.db,saved['id'],{'revision':saved['revision']})
        self.assertEqual(self.available(advance['advance_id']),6000)
        self.assert_matched()

    def test_opening_advance_uses_original_account_after_remapping(self):
        invoice=self.invoice()
        advance=self.source('supplier_advance',5000)
        self.db.execute("UPDATE account_mappings SET account_id=? WHERE module='الموردون' AND role='supplier_advances'",(self.fixture.account('115003'),))
        settlement=self.settlement(advance,invoice,2000)
        refund=self.refund(self.refund_payload(advance,amount='10'))
        self.assertEqual(settlement['source_account_id'],self.fixture.advance)
        self.assertEqual(next(line['account_id'] for line in refund['journal']['lines'] if line['credit_cents']),self.fixture.advance)
        self.assertEqual(payables.supplier_balances(self.db,1)['advance_cents'],2000)
        self.assert_matched()

    def test_opening_source_id_does_not_block_reversal_of_unrelated_payment(self):
        target=self.invoice()
        advance=self.source('supplier_advance',5000)
        operating=self.fixture.post(self.fixture.payload(amount='80',payment_date='2026-02-01',allow_advance=True))
        self.assertEqual(advance['advance_id'],operating['id'])
        self.settlement(advance,target,2000)
        self.refund(self.refund_payload(advance,amount='10'))
        adjustments.reverse_payment(self.db,operating['id'],{'request_key':'independent-reverse',
            'revision':operating['revision'],'event_revision':0,'business_date':'2026-04-01',
            'reference':'CORRECT-PAY','reason':'عكس دفعة تشغيل مستقلة عن الافتتاح'})
        self.assertEqual(self.available(advance['advance_id']),2000)
        self.assertEqual(payables.supplier_balances(self.db,1)['advance_cents'],2000)
        self.assert_matched()

    def test_actual_balanced_batch_posts_independent_sources_and_zero_clearing(self):
        import opening_balances as core
        fixture=fixtures.SupplierPayments('runTest')
        fixture.setUp()
        db=fixture.db
        try:
            settlements.initialize(db);adjustments.initialize(db);core.initialize(db)
            db.execute("UPDATE account_mappings SET status='confirmed'")
            payload={'request_key':'full-engine','business_date':'2026-01-01','reference':'افتتاح فعلي','lines':[
                {'kind':'supplier_invoice','supplier_id':1,'reference':'OLD-1','source_date':'2025-11-01','due_date':'2025-12-01','amount':'100','remaining_net_amount':'90'},
                {'kind':'supplier_credit','supplier_id':1,'reference':'OLD-CREDIT','amount':'20'},
                {'kind':'supplier_advance','supplier_id':1,'reference':'OLD-ADVANCE','amount':'30'},
                {'kind':'general','account_id':fixture.cash,'side':'debit','amount':'50'}]}
            draft=core.save(db,payload)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM opening_supplier_advances').fetchone()[0],0)
            posted=core.approve(db,draft['id'],{'revision':draft['revision']})
            retry=core.approve(db,draft['id'],{'revision':draft['revision']})
            self.assertEqual([j['id'] for j in posted['journals']],[j['id'] for j in retry['journals']])
            self.assertEqual(len(posted['journals']),4)
            self.assertEqual(payments.account_balance(db,posted['clearing_account_id']),0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM supplier_payments').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM invoice_lines').fetchone()[0],0)
            self.assertEqual(payables.supplier_balances(db,1)['net_payable_cents'],5000)
            self.assertTrue(reconciliation.report(db)['matched'])
        finally:
            fixture.tearDown()

    def test_opening_withholding_needs_explicit_remaining_net_no_vat_inference(self):
        unknown=self.invoice()
        known=self.invoice(11400,remaining_net_amount='100')
        code=withholding.create_code(self.db,{'name':'خصم صريح للاختبار','rate':'3','active':True,'confirmed':True})
        value={'supplier_id':1,'payment_date':'2026-02-01','amount_cents':5700,
               'funding_account_id':self.fixture.cash,'withholding_code_id':code['id']}
        with self.assertRaisesRegex(ValueError,'صافي الوعاء'):
            withholding.prepare(self.db,value,[{'invoice_id':unknown['id'],'amount_cents':5700}])
        plan=withholding.prepare(self.db,value,[{'invoice_id':known['id'],'amount_cents':5700}])
        self.assertEqual((plan['withholding_base_cents'],plan['withholding_cents']),(5000,150))
        self.assertEqual(known['tax_cents'],0)
        self.assertEqual(payments.account_balance(self.db,self.fixture.expense),0)

    def test_metadata_is_immutable_and_control_source_must_match(self):
        invoice=self.invoice()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE opening_supplier_invoices SET remaining_net_cents=1 WHERE invoice_id=?',(invoice['id'],))
        self.assert_matched()
        for field,value in [('reference','wrong'),('department_id',1),('source_date','2025-10-01'),
                            ('due_date','2025-12-02'),('document_id',999),('extra_json','{"remaining_net_cents":1}')]:
            with self.subTest(field=field):
                self.db.execute('SAVEPOINT damaged_source')
                self.db.execute('UPDATE opening_lines SET '+field+'=? WHERE id=?',(value,invoice['opening_line_id']))
                report=reconciliation.report(self.db)
                self.assertFalse(report['matched'])
                self.assertTrue(any(e['type']=='supplier_source_journal_mismatch' for e in report['exceptions']))
                self.db.execute('ROLLBACK TO damaged_source');self.db.execute('RELEASE damaged_source')
        self.db.execute('DROP TRIGGER ap_immutable_journal_lines_update')
        self.db.execute('UPDATE journal_lines SET source_line_id=NULL WHERE journal_id=? AND supplier_id=1',(invoice['journal_id'],))
        self.assertFalse(reconciliation.report(self.db)['matched'])

    def test_opening_invoice_requires_original_invoice_and_due_dates(self):
        value={'kind':'supplier_invoice','supplier_id':1,'amount_cents':10000,'reference':'ORIGINAL'}
        with self.assertRaisesRegex(ValueError,'تاريخ الفاتورة الأصلي'):
            opening.prepare(self.db,{**value,'due_date':'2025-12-01'},'2026-01-01')
        with self.assertRaisesRegex(ValueError,'تاريخ الاستحقاق الأصلي'):
            opening.prepare(self.db,{**value,'source_date':'2025-11-01'},'2026-01-01')
        # A prior supplier credit can be an account-level credit without aging.
        credit=opening.prepare(self.db,{**value,'kind':'supplier_credit'},'2026-01-01')
        self.assertEqual(credit['due_date'],'2026-01-01')


if __name__=='__main__':
    unittest.main()
