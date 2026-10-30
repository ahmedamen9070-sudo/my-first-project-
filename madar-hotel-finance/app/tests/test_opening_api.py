"""Settings openings through real HTTP transactions and downstream subledgers."""
import sqlite3
import unittest
import uuid
from pathlib import Path

import test_payables_api as base


class OpeningAPI(unittest.TestCase):
    request = classmethod(base.PayablesAPI.request.__func__)
    fixture = base.PayablesAPI.fixture
    invoice = base.PayablesAPI.invoice
    payment = base.PayablesAPI.payment

    def setUp(self):
        base.PayablesAPI.setUpClass.__func__(self.__class__)
        self.chart = self.request('accounting')
        self.accounts = {a['code']: a['id'] for a in self.chart['accounts']}
        self.cash, self.bank = self.accounts['111002'], self.accounts['112001']
        self.supplier, self.product = self.fixture()
        self.person = self.request('custodians', 'POST', {'name': 'صاحب عهدة افتتاحية اختبار'})
        for m in self.chart['mappings']:
            if (m['module'],m['role']) in {('الإعدادات','opening_clearing'),('الخزينة','operating_custodies'),('الضرائب','withholding_payable')}:
                self.request('account-mappings/'+str(m['id']), 'PUT', {'revision':m['revision'],'status':'confirmed'})
        self.capital = next(a['id'] for a in self.request('opening-config')['general_accounts'] if a['code'].startswith('3'))

    def tearDown(self):
        base.PayablesAPI.tearDownClass.__func__(self.__class__)

    def payload(self):
        supplier=self.supplier['id']
        return {'request_key':uuid.uuid4().hex,'business_date':'2026-01-01','reference':'OPEN-'+uuid.uuid4().hex[:10], 'lines':[
            {'kind':'general','account_id':self.cash,'side':'debit','amount':'1000'},
            {'kind':'general','account_id':self.bank,'side':'debit','amount':'2000'},
            {'kind':'supplier_advance','supplier_id':supplier,'reference':'ADV-OLD','source_date':'2025-11-01','amount':'200'},
            {'kind':'custody','custodian_id':self.person['id'],'reference':'CUST-OLD','source_date':'2025-12-01','amount':'100'},
            {'kind':'supplier_credit','supplier_id':supplier,'reference':'CREDIT-OLD','source_date':'2025-12-01','due_date':'2025-12-10','amount':'100'},
            {'kind':'supplier_invoice','supplier_id':supplier,'reference':'INV-OLD','source_date':'2025-10-01','due_date':'2025-11-01','amount':'500','remaining_net_amount':'450'},
            {'kind':'withholding','supplier_id':supplier,'reference':'WHT-OLD','source_date':'2025-12-01','amount':'100'},
            {'kind':'issued_cheque','supplier_id':supplier,'reference':'CHQ-OLD','source_date':'2025-12-15','due_date':'2026-01-15','amount':'300','extra':{'bank_account_id':self.bank,'bank_name':'بنك اختبار','cheque_number':'OLD-001','beneficiary':'المورد المستفيد'}},
            {'kind':'general','account_id':self.capital,'side':'credit','amount':'2500'}]}

    def post(self, path, data):
        draft=self.request(path,'POST',data)
        return self.request(f"{path}/{draft['id']}/post",'POST',{'revision':draft['revision']})

    def assert_matches(self, day='2026-02-28'):
        for path in ('supplier-reconciliation','custody-balances','withholding-balances'):
            report=self.request(path+'?as_of='+day)
            self.assertTrue(report['matched'], (path,report.get('exceptions')))
        trial=self.request('trial-balance?from_date=2026-01-01&to_date='+day)
        self.assertTrue(trial['balanced'],trial['exceptions'])
        return trial

    def test_balanced_settings_openings_feed_existing_modules_without_fake_cash(self):
        payload=self.payload()
        self.request('opening-balances','POST',payload,expected=403,csrf=False)
        preview=self.request('opening-preview','POST',payload)
        self.assertTrue(preview['can_approve'],preview['blockers'])
        self.assertEqual(preview['totals']['debit_cents'],340000)
        draft=self.request('opening-balances','POST',payload)
        self.assertEqual(self.request('opening-balances','POST',payload)['id'],draft['id'])
        self.assertEqual(self.request('treasury-position')['totals']['balance_cents'],0)
        self.assertEqual(self.request('period-preview?month=2026-01')['draft_counts']['opening_balance'],1)
        posted=self.request(f"opening-balances/{draft['id']}/post",'POST',{'revision':draft['revision']})
        self.assertEqual(posted['status'],'posted')
        self.assertEqual(self.request(f"opening-balances/{draft['id']}/post",'POST',{'revision':draft['revision']})['id'],draft['id'])
        self.assertEqual(self.request('treasury-position?as_of=2026-01-01')['totals']['balance_cents'],100000)
        self.assertEqual(self.request(f'bank-book?account_id={self.bank}&from_date=2026-01-01&to_date=2026-01-01')['closing_balance_cents'],200000)
        trial=self.assert_matches('2026-01-01')
        clearing=self.request('opening-config')['clearing_mapping']['account_id']
        self.assertTrue(all(r['closing_balance_cents']==0 for r in trial['rows'] if r['account_id']==clearing))
        self.assertTrue(trial['opening_status']['included'])
        with sqlite3.connect(Path(self.tmp.name)/'api.sqlite') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM supplier_payments').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM invoice_lines').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM payment_withholding').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM journal_entries').fetchone()[0],9)
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
        bills=self.request(f"suppliers/{self.supplier['id']}/open-invoices?as_of=2026-01-01")
        bill=next(i for i in bills if i['total_cents']>0)
        payment=self.payment(self.supplier,'100','2026-01-03',mode='invoice',target=bill['id'])
        self.assertEqual(payment['net_cash_cents'],10000)
        sources=self.request(f"supplier-credit-sources?supplier_id={self.supplier['id']}&as_of=2026-01-04")['sources']
        advance=next(x for x in sources if x['source_type']=='advance')
        self.post('supplier-settlements',{'request_key':uuid.uuid4().hex,'reference':'USE-OLD-ADV','supplier_id':self.supplier['id'],'business_date':'2026-01-04','source_type':'advance','source_id':advance['source_id'],'source_origin':'opening','allocations':[{'invoice_id':bill['id'],'amount_cents':10000}]})
        custody=self.request('custody-documents')[0]
        self.assertTrue(custody['is_opening'])
        self.post('custody-documents',{'request_key':uuid.uuid4().hex,'kind':'settlement','reference':'SET-CUST-OLD','business_date':'2026-01-05','source_issue_id':custody['id'],'custodian_id':self.person['id'],'lines':[{'expense_account_id':self.expense,'department_id':self.department,'reference':'ACTUAL-EXP','amount':'40'}]})
        tax=self.request('withholding-balances?as_of=2026-01-05')['sources'][0]
        self.post('withholding-remittances',{'request_key':uuid.uuid4().hex,'business_date':'2026-01-06','period_from':'2025-01-01','period_to':'2025-12-31','authority_name':'جهة اختبار','reference':'WHT-REMIT-OLD','method':'cash','funding_account_id':self.cash,'details':{'received_by':'ممثل الجهة','receipt_reference':'OLD-WHT-REC'},'allocations':[{'source_type':'opening_withholding','source_id':tax['source_id'],'amount':'30'}]})
        cheque=self.request('opening-bank-cheques')[0]
        clear={'request_key':uuid.uuid4().hex,'revision':cheque['revision'],'business_date':'2026-01-16','value_date':'2026-01-16','reference':'CLEARED-OLD','bank_reference':'BANK-OLD-CHQ'}
        self.request(f"opening-bank-cheques/{cheque['id']}/clear",'POST',clear)
        self.request(f"opening-bank-cheques/{cheque['id']}/clear",'POST',clear)
        bank=self.request(f'bank-book?account_id={self.bank}&from_date=2026-01-01&to_date=2026-01-31')
        self.assertEqual(bank['closing_balance_cents'],170000)
        self.assertEqual(len([r for r in bank['rows'] if r['source_type']=='opening_cheque_clearance']),1)
        self.assertEqual(self.request('custody-balances')['totals']['remaining_cents'],6000)
        self.assertEqual(self.request('withholding-balances')['all_totals']['remaining_cents'],7000)
        self.assert_matches()

    def test_unbalanced_drafts_and_cutoff_and_immutability(self):
        data=self.payload();data['lines']=data['lines'][:1]
        preview=self.request('opening-preview','POST',data)
        self.assertFalse(preview['can_approve'])
        draft=self.request('opening-balances','POST',data)
        self.request(f"opening-balances/{draft['id']}/post",'POST',{'revision':draft['revision']},expected=400)
        full={**self.payload(),'request_key':data['request_key'],'revision':draft['revision']}
        fixed=self.request('opening-balances/'+str(draft['id']),'PUT',full)
        posted=self.request(f"opening-balances/{fixed['id']}/post",'POST',{'revision':fixed['revision']})
        self.request('opening-balances','POST',self.payload(),expected=400)
        self.request('opening-balances/'+str(posted['id']),'PUT',{**full,'revision':posted['revision']},expected=400)
        bad={'request_key':uuid.uuid4().hex,'supplier_id':self.supplier['id'],'department_id':self.department,'reference':'BEFORE-BASE','business_date':'2025-12-31','kind':'service','tax_rate':'0','lines':[{'product_id':self.product['id'],'quantity':'1','price':'100'}]}
        self.request('invoices','POST',bad,expected=400)
        self.assertFalse(self.request('treasury-position?as_of=2025-12-31')['opening_status']['included'])
        self.assert_matches()

    def test_bank_master_metadata_has_no_opening_amount_and_reads_without_posting(self):
        self.assertEqual(self.request('bank-accounts'),[])
        master=self.request('bank-accounts','POST',{'chart_account_id':self.bank,'bank_name':'بنك اختبار','branch':'القاهرة','account_number':'TEST-123'})
        self.assertEqual(self.request('bank-accounts')[0]['id'],master['id'])
        self.assertEqual(self.request('bank-accounts')[0]['account_number'],'TEST-123')
        self.assertEqual(self.request('bank-position')['totals']['balance_cents'],0)
        self.assertEqual(self.request('bank-documents'),[])
        self.assertEqual(self.request('opening-balances'),[])

    def test_bank_transfer_is_one_journal_seen_once_on_each_account_and_lock_blocks_new(self):
        self.post('opening-balances',self.payload())
        data={'request_key':uuid.uuid4().hex,'kind':'transfer','business_date':'2026-01-03','value_date':'2026-01-03','reference':'BANK-CASH-TRANSFER','bank_reference':'BANK-REF','amount':'200','account_id':self.bank,'destination_account_id':self.cash}
        doc=self.post('bank-documents',data)
        cash=self.request(f'cash-book?account_id={self.cash}&from_date=2026-01-01&to_date=2026-01-31')
        bank=self.request(f'bank-book?account_id={self.bank}&from_date=2026-01-01&to_date=2026-01-31')
        self.assertEqual(cash['closing_balance_cents'],120000)
        self.assertEqual(bank['closing_balance_cents'],180000)
        self.assertEqual(len([r for r in cash['rows'] if r['journal_id']==doc['journal_id']]),1)
        self.assert_matches()
        review=self.request('period-preview?month=2026-01')
        self.assertTrue(review['can_close'],review['blockers'])
        self.request('accounting-periods/2026-01/close','POST',{'request_key':uuid.uuid4().hex,'revision':1,'reference':'MONTH-CLOSE','reason':'تمت المراجعة'})
        self.assertEqual(self.request('bank-documents','POST',data)['id'],doc['id'])
        self.request('bank-documents','POST',{**data,'request_key':uuid.uuid4().hex,'reference':'NEW-CLOSED'},expected=400)


if __name__=='__main__':unittest.main()
