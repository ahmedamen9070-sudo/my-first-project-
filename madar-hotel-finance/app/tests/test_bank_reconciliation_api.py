"""External statement certification, locks and CSV through real HTTP transactions."""
import sqlite3
import unittest
import uuid
from pathlib import Path

import test_payables_api as base


class BankReconciliationAPI(unittest.TestCase):
    request = classmethod(base.PayablesAPI.request.__func__)
    fixture = base.PayablesAPI.fixture
    invoice = base.PayablesAPI.invoice
    payment = base.PayablesAPI.payment

    def setUp(self):
        base.PayablesAPI.setUpClass.__func__(self.__class__)
        chart=self.request('accounting')
        ids={a['code']:a['id'] for a in chart['accounts']}
        self.bank,self.other,self.cash=ids['112001'],ids['112003'],ids['111002']
        cfg=self.request('bank-config');self.contra=next(a['id'] for a in cfg['counter_accounts_by_kind']['receipt'] if a['code'].startswith('3'))
        self.fee=cfg['counter_accounts_by_kind']['fee'][0]['id']
        m=self.request('opening-config')['clearing_mapping']
        self.request('account-mappings/'+str(m['id']),'PUT',{'revision':m['revision'],'status':'confirmed'})
        self.post('opening-balances',{'request_key':uuid.uuid4().hex,'business_date':'2026-01-01','reference':'ACTUAL-OPEN','lines':[
            {'kind':'general','account_id':self.bank,'side':'debit','amount':'1000'},
            {'kind':'general','account_id':self.cash,'side':'debit','amount':'100'},
            {'kind':'general','account_id':self.contra,'side':'credit','amount':'1100'}]})

    def tearDown(self):base.PayablesAPI.tearDownClass.__func__(self.__class__)

    def post(self,path,data):
        d=self.request(path,'POST',data)
        return self.request(f"{path}/{d['id']}/post",'POST',{'revision':d['revision']})

    def movement(self,kind,amount,day,account=None):
        return self.post('bank-documents',{'request_key':uuid.uuid4().hex,'kind':kind,'account_id':account or self.bank,
            'amount':amount,'business_date':day,'value_date':day,'reference':uuid.uuid4().hex,'bank_reference':uuid.uuid4().hex,
            'counter_account_id':self.contra if kind=='receipt' else self.fee,'party_name':'طرف فعلي لاختبار المطابقة',
            'department_id':self.department if kind!='receipt' else None})

    def book_line(self,doc):return next(l['id'] for l in doc['journal']['lines'] if l['account_id']==self.bank)

    def payload(self,start='2026-01-01',end='2026-01-31',opening='1000',closing='1000',lines=None,matches=None,**extra):
        return {'request_key':uuid.uuid4().hex,'account_id':self.bank,'from_date':start,'to_date':end,'reference':uuid.uuid4().hex,
            'opening_balance':opening,'closing_balance':closing,'prepared_by':'محاسب البنك','reviewed_by':'مراجع البنك',
            'lines':lines or [],'matches':matches or [],'notes':'كشف فعلي في قاعدة اختبار معزولة',**extra}

    def certify(self,data):
        d=self.request('bank-reconciliations','POST',data)
        return self.request(f"bank-reconciliations/{d['id']}/confirm",'POST',{'revision':d['revision']})

    def count(self,table):
        with sqlite3.connect(Path(self.tmp.name)/'api.sqlite') as db:return db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]

    def void_data(self,doc,day='2026-03-01'):
        return {'request_key':uuid.uuid4().hex,'revision':doc['revision'],'business_date':day,'reference':uuid.uuid4().hex,'reason':'إبطال المطابقة لمراجعة مستند مصحح'}

    def test_nonposting_certificate_carry_chain_latest_void_and_account_specific_locks(self):
        r=self.movement('receipt','200','2026-01-05');p=self.movement('payment','50','2026-01-06');transit=self.movement('receipt','100','2026-01-07')
        data=self.payload(closing='1150',lines=[{'date':'2026-01-05','reference':'BANK-IN','amount':'200'}, {'date':'2026-01-06','reference':'BANK-OUT','amount':'-50'}],matches=[
            {'statement_line_ids':[1],'book_line_ids':[self.book_line(r)]},{'statement_line_ids':[2],'book_line_ids':[self.book_line(p)]}])
        self.request('bank-reconciliations','POST',data,expected=403,csrf=False)
        preview=self.request('bank-reconciliation-preview','POST',data);self.assertTrue(preview['can_confirm'],preview['blockers'])
        self.assertEqual(len(preview['unmatched_book_lines']),1)
        baseline=self.count('journal_entries');cert=self.certify(data)
        self.assertEqual(cert['status'],'confirmed');self.assertEqual(self.count('journal_entries'),baseline)
        self.assertEqual(self.request('bank-reconciliations','POST',data)['id'],cert['id'])
        self.assertEqual(self.request(f"bank-reconciliations/{cert['id']}/confirm",'POST',{'revision':cert['revision']})['id'],cert['id'])
        # Financial replay remains readable after certification; a new old-dated journal cannot alter it.
        self.assertEqual(self.request(f"bank-documents/{r['id']}/post",'POST',{'revision':r['revision']})['id'],r['id'])
        denied={'request_key':uuid.uuid4().hex,'kind':'fee','account_id':self.bank,'amount':'1','business_date':'2026-01-10','value_date':'2026-01-10','reference':'BLOCKED-BANK','bank_reference':'BLOCKED-BANK','counter_account_id':self.fee,'department_id':self.department,'party_name':'البنك'}
        draft=self.request('bank-documents','POST',denied)
        self.request(f"bank-documents/{draft['id']}/post",'POST',{'revision':draft['revision']},expected=400)
        self.request(f"bank-documents/{draft['id']}",'PUT',{'revision':draft['revision'],'action':'cancel'})
        self.assertEqual(self.count('journal_entries'),baseline)
        self.movement('receipt','1','2026-01-10',self.other)
        feb=self.movement('receipt','10','2026-02-03')
        second=self.payload('2026-02-01','2026-02-28','1150','1260',lines=[{'date':'2026-02-02','amount':'100'},{'date':'2026-02-03','amount':'10'}],matches=[
            {'statement_line_ids':[1],'book_line_ids':[self.book_line(transit)]},{'statement_line_ids':[2],'book_line_ids':[self.book_line(feb)]}])
        nxt=self.certify(second)
        self.request(f"bank-reconciliations/{cert['id']}/void",'POST',self.void_data(cert),expected=400)
        voided=self.request(f"bank-reconciliations/{nxt['id']}/void",'POST',self.void_data(nxt))
        self.assertEqual(voided['effective_status'],'voided')
        self.movement('fee','5','2026-02-04')
        frozen=self.request(f"bank-reconciliations/{cert['id']}")
        self.assertFalse(frozen['book_changed'])
        self.assertTrue(self.request('trial-balance?to_date=2026-03-01')['balanced'])

    def test_bank_only_fee_must_be_recorded_and_matched_no_synthetic_journals(self):
        data=self.payload(closing='990',lines=[{'date':'2026-01-04','reference':'BANK-FEE','amount':'-10'}])
        before=self.count('journal_entries');draft=self.request('bank-reconciliations','POST',data)
        self.request(f"bank-reconciliations/{draft['id']}/confirm",'POST',{'revision':draft['revision']},expected=400)
        self.assertEqual(self.count('journal_entries'),before)
        fee=self.movement('fee','10','2026-01-04')
        saved=self.request('bank-reconciliations/'+str(draft['id']),'PUT',{**data,'revision':draft['revision'],'matches':[{'statement_line_ids':[1],'book_line_ids':[self.book_line(fee)]}]})
        confirmed=self.request(f"bank-reconciliations/{saved['id']}/confirm",'POST',{'revision':saved['revision']})
        self.assertEqual(self.count('journal_entries'),before+1)
        month=self.request('period-preview?month=2026-01');self.assertTrue(month['can_close'],month['blockers'])
        self.request('accounting-periods/2026-01/close','POST',{'request_key':uuid.uuid4().hex,'revision':1,'reference':'RECON-CLOSE','reason':'مراجعة المستندات والمطابقة'})
        self.request(f"bank-reconciliations/{saved['id']}/confirm",'POST',{'revision':saved['revision']})
        self.request(f"bank-reconciliations/{saved['id']}/void",'POST',self.void_data(confirmed,'2026-01-31'),expected=400)

    def test_zero_net_financial_changes_stale_saved_review_and_nonposting_draft_close_block(self):
        data=self.payload();d=self.request('bank-reconciliations','POST',data)
        self.assertEqual(self.request('period-preview?month=2026-01')['draft_counts']['bank_reconciliation'],1)
        self.movement('receipt','5','2026-01-10');self.movement('payment','5','2026-01-10')
        detail=self.request('bank-reconciliations/'+str(d['id']));self.assertTrue(detail['book_changed'])
        self.request(f"bank-reconciliations/{d['id']}/confirm",'POST',{'revision':d['revision']},expected=409)
        saved=self.request('bank-reconciliations/'+str(d['id']),'PUT',{**data,'revision':d['revision']})
        self.assertEqual(len(saved['snapshot']['unmatched_book_lines']),2)
        self.request(f"bank-reconciliations/{saved['id']}/confirm",'POST',{'revision':saved['revision']})

    def test_opening_bridge_and_many_to_one_match_keep_gross_movements_honest(self):
        old=self.movement('receipt','100','2026-01-05')
        first=self.payload('2026-02-01','2026-02-28','1000','1000')
        preview=self.request('bank-reconciliation-preview','POST',first);self.assertFalse(preview['can_confirm'])
        first['opening_outstanding_line_ids']=[self.book_line(old)]
        self.assertTrue(self.request('bank-reconciliation-preview','POST',first)['can_confirm'])
        self.certify(first)
        a=self.movement('receipt','20','2026-03-02');b=self.movement('receipt','30','2026-03-02')
        third=self.payload('2026-03-01','2026-03-31','1000','1150',lines=[{'date':'2026-03-02','amount':'150'}],matches=[{'statement_line_ids':[1],'book_line_ids':[self.book_line(old),self.book_line(a),self.book_line(b)]}])
        self.assertTrue(self.request('bank-reconciliation-preview','POST',third)['can_confirm'])
        self.certify(third)

    def test_reconciled_account_lock_reaches_ap_but_cash_and_other_accounts_remain_available(self):
        supplier,product=self.fixture();bill=self.invoice(supplier,product,'100','2026-01-02')
        details={'bank_name':'بنك اختبار','beneficiary':supplier['name'],'beneficiary_account':'TEST-ACCOUNT','transaction_reference':'BANK-AP-PART','value_date':'2026-01-03'}
        paid=self.payment(supplier,'20','2026-01-03',mode='invoice',target=bill['id'],method='bank_transfer',funding_account_id=self.bank,details=details)
        book=self.request(f'bank-book?account_id={self.bank}&from_date=2026-01-01&to_date=2026-01-31')
        line=next(r['line_id'] for r in book['rows'] if r['source_type']=='supplier_payment' and r['source_id']==paid['id'])
        cert=self.certify(self.payload(closing='980',lines=[{'date':'2026-01-03','amount':'-20'}],matches=[{'statement_line_ids':[1],'book_line_ids':[line]}]))
        pending=self.payment(supplier,'20','2026-01-04',mode='invoice',target=bill['id'],post=False,method='bank_transfer',funding_account_id=self.bank,details={**details,'transaction_reference':'NEW-AP-OLD'})
        before=self.count('journal_entries')
        self.request(f"supplier-payments/{pending['id']}/post",'POST',{'revision':pending['revision']},expected=400)
        self.assertEqual(self.count('journal_entries'),before)
        self.assertEqual(next(row['outstanding_cents'] for row in self.request(f"suppliers/{supplier['id']}/open-invoices") if row['id']==bill['id']),8000)
        self.payment(supplier,'20','2026-01-04',mode='invoice',target=bill['id'])
        self.assertFalse(self.request('bank-reconciliations/'+str(cert['id']))['book_changed'])
        self.assertEqual(next(row['outstanding_cents'] for row in self.request(f"suppliers/{supplier['id']}/open-invoices") if row['id']==bill['id']),6000)

    def test_csv_import_and_inspection_are_read_only_and_bad_rows_are_atomic(self):
        before=(self.count('journal_entries'),self.count('bank_reconciliations'),self.count('audit'))
        data={'csv_text':'Date;Reference;Deposit;Withdrawal;Balance\n2026-01-04;0001;10;;1010\n2026-01-05;0002;;2;1008\n','delimiter':'semicolon','date_format':'YYYY-MM-DD','amount_mode':'columns','decimal_separator':'point','columns':{'date':0,'reference':1,'deposit':2,'withdrawal':3,'balance':4}}
        info=self.request('bank-statement-inspect','POST',data);self.assertEqual(info['row_count'],2)
        parsed=self.request('bank-statement-import','POST',data);self.assertEqual([l['amount_cents'] for l in parsed['lines']],[1000,-200]);self.assertEqual(parsed['lines'][0]['reference'],'0001')
        bad={**data,'csv_text':data['csv_text'].replace(';2;1008',';2.001;1008')}
        self.request('bank-statement-import','POST',bad,expected=400)
        self.request('bank-statement-import','POST',data,expected=403,csrf=False)
        self.assertEqual((self.count('journal_entries'),self.count('bank_reconciliations'),self.count('audit')),before)


if __name__=='__main__':unittest.main()
