"""Actual bank statements, immutable nonposting certificates and account locks."""
import copy
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting
import accounting_periods
import bank_reconciliation as recon
import banks
import custodies
import opening_balances
import payables
import supplier_adjustments
import supplier_payments
import treasury
import withholding
import withholding_remittances
import test_supplier_payments as fixtures


class BankReconciliation(unittest.TestCase):
    def setUp(self):
        self.start()

    def start(self, opening_extra=None):
        self.f = fixtures.SupplierPayments('runTest'); self.f.setUp(); self.db = self.f.db
        supplier_adjustments.initialize(self.db)
        accounting_periods.initialize(self.db)
        treasury.initialize(self.db); custodies.initialize(self.db)
        withholding_remittances.initialize(self.db); banks.initialize(self.db)
        opening_balances.initialize(self.db); recon.initialize(self.db)
        self.db.execute("UPDATE account_mappings SET status='confirmed'")
        self.bank, self.other, self.cash = self.f.bank, self.f.account('112003'), self.f.cash
        self.capital = self.f.account('300001'); self.expense = next(a['id'] for a in banks.config(self.db)['counter_accounts_by_kind']['payment'] if a['code'].startswith('6'))
        self.serial = 0
        self.opening(opening_extra)
        self.db.commit()

    def tearDown(self):
        self.f.tearDown()

    def key(self, prefix='test'):
        self.serial += 1; return prefix + '-' + str(self.serial)

    def opening(self, extra=None):
        extra = extra or []
        capital = 100000 - sum(line['amount_cents'] for line in extra)
        draft = opening_balances.save(self.db, {'request_key': self.key('opening'), 'business_date':'2026-01-01', 'reference':'BASELINE', 'lines':[
            {'kind':'general','account_id':self.bank,'side':'debit','amount':'1000'},
            {'kind':'general','account_id':self.capital,'side':'credit','amount_cents':capital},*extra]})
        return opening_balances.approve(self.db,draft['id'],{'revision':draft['revision']})

    def movement(self, amount='100', kind='receipt', ds='2026-01-10', account=None):
        value = {'request_key':self.key('bank'), 'kind':kind, 'business_date':ds,'value_date':ds,
                 'reference':self.key('ref'),'bank_reference':self.key('bankref'), 'amount':amount,
                 'account_id':account or self.bank, 'counter_account_id':self.capital if kind=='receipt' else self.expense,
                 'department_id':1 if kind!='receipt' else None,'party_name':'طرف فعلي بمستند'}
        saved = banks.save(self.db,value)
        doc = banks.approve(self.db,saved['id'],{'revision':saved['revision']})
        doc['bank_line_id'] = next(l['id'] for l in doc['journal']['lines'] if l['account_id']==value['account_id'])
        return doc

    def payload(self, **changes):
        return {'request_key':self.key('recon'),'account_id':self.bank,'from_date':'2026-01-01','to_date':'2026-01-31',
                'reference':self.key('statement'),'opening_balance':'1000','closing_balance':'1000',
                'prepared_by':'معد التسوية','reviewed_by':'مراجع التسوية','notes':'كشف فعلي في قاعدة اختبار معزولة',
                'lines':[],'matches':[],'opening_outstanding_line_ids':[], **changes}

    def confirm(self, payload):
        saved = recon.save(self.db,payload)
        return recon.confirm(self.db,saved['id'],{'revision':saved['revision']})

    def period(self):
        a = self.movement('300',ds='2026-01-03'); b = self.movement('200',ds='2026-01-04')
        c = self.movement('100','payment','2026-01-05'); d = self.movement('10','fee','2026-01-06')
        pending = self.movement('50',ds='2026-01-07')
        payload = self.payload(closing_balance='1390',lines=[
            {'line_key':'deposit-batch','date':'2026-01-04','amount':'500','balance':'1500'},
            {'line_key':'withdrawal','date':'2026-01-05','amount':'-100','balance':'1400'},
            {'line_key':'fee','date':'2026-01-06','amount':'-10','balance':'1390'}],matches=[
            {'statement_line_ids':[1],'book_line_ids':[a['bank_line_id'],b['bank_line_id']]},
            {'statement_line_ids':[2,3],'book_line_ids':[c['bank_line_id'],d['bank_line_id']]}])
        return payload,pending,[a,b,c,d]

    def counts(self):
        return {table:self.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('journal_entries','journal_lines','bank_documents','supplier_payments')}

    def test_new_module_and_reads_do_not_create_bank_money_or_movements(self):
        before = self.counts(); audit = self.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
        self.assertEqual(recon.list_reconciliations(self.db),[])
        config = recon.config(self.db)
        self.assertEqual(config['latest_by_account'],{})
        self.assertEqual(config['currency'],'EGP')
        plan = recon.preview(self.db,self.payload())
        self.assertTrue(plan['can_confirm'],plan['blockers'])
        self.assertEqual(plan['totals']['book_opening_cents'],100000)
        self.assertEqual(plan['book_lines'],[])
        self.assertEqual(before,self.counts()); self.assertEqual(audit,self.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0])

    def test_many_to_many_matching_pending_deposit_and_contiguous_carry(self):
        payload,pending,docs = self.period(); before = self.counts()
        plan = recon.preview(self.db,payload)
        self.assertTrue(plan['can_confirm'],plan['blockers'])
        self.assertEqual(plan['totals']['book_closing_cents'],144000)
        self.assertEqual(plan['totals']['statement_closing_cents'],139000)
        self.assertEqual(plan['totals']['difference_cents'],0)
        self.assertEqual(plan['totals']['matched_statement_count'],3)
        self.assertEqual(plan['totals']['matched_book_count'],4)
        self.assertEqual(plan['totals']['pending_deposit_cents'],5000)
        first = self.confirm(payload)
        self.assertEqual(first['status'],'confirmed'); self.assertFalse(first['book_changed'])
        next_payload = self.payload(from_date='2026-02-01',to_date='2026-02-28',opening_balance='1390',closing_balance='1440',
                                   lines=[{'date':'2026-02-03','amount':'50'}],matches=[{'statement_line_ids':[1],'book_line_ids':[pending['bank_line_id']]}])
        next_plan = recon.preview(self.db,next_payload)
        self.assertTrue(next_plan['can_confirm'],next_plan['blockers'])
        self.assertEqual([l['line_id'] for l in next_plan['book_lines']],[pending['bank_line_id']])
        second = self.confirm(next_payload)
        self.assertEqual(second['snapshot']['unmatched_book_lines'],[])
        self.assertEqual(before,self.counts())
        self.assertTrue(recon.review(self.db)['matched'])
        self.assertEqual(recon.review(self.db)['certificate_count'],2)

    def test_first_opening_bridge_requires_explicit_valid_prior_ledger_lines(self):
        prior = self.movement('200',ds='2026-01-12')
        payload = self.payload(from_date='2026-02-01',to_date='2026-02-28',opening_balance='1000',closing_balance='1200',
                               lines=[{'date':'2026-02-02','amount':'200'}])
        plan = recon.preview(self.db,payload)
        self.assertIn('opening_difference',{b['code'] for b in plan['blockers']})
        self.assertEqual(plan['available_prior_lines'][0]['line_id'],prior['bank_line_id'])
        payload['opening_outstanding_line_ids']=[prior['bank_line_id']]
        payload['matches']=[{'statement_line_ids':[1],'book_line_ids':[prior['bank_line_id']]}]
        plan = recon.preview(self.db,payload)
        self.assertTrue(plan['can_confirm'],plan['blockers'])
        self.assertEqual(plan['totals']['opening_outstanding_cents'],20000)
        foreign = self.movement('200',ds='2026-01-13',account=self.other)
        payload['opening_outstanding_line_ids']=[foreign['bank_line_id']]
        self.assertIn('invalid_carry',{b['code'] for b in recon.preview(self.db,payload)['blockers']})

    def test_statement_arithmetic_running_balance_and_unmatched_bank_only_are_reviewed(self):
        data = self.payload(closing_balance='990',lines=[{'date':'2026-01-12','amount':'-10','balance':'991'}])
        plan = recon.preview(self.db,data)
        self.assertIn('running_balance',{b['code'] for b in plan['blockers']})
        self.assertIn('unmatched_statement',{b['code'] for b in plan['blockers']})
        saved = recon.save(self.db,data)
        with self.assertRaises(ValueError):recon.confirm(self.db,saved['id'],{'revision':saved['revision']})
        data['lines'][0]['balance']='990'; fee = self.movement('10','fee','2026-01-12')
        data['matches']=[{'statement_line_ids':[1],'book_line_ids':[fee['bank_line_id']]}]
        data['revision']=saved['revision']
        fixed = recon.save(self.db,data,saved['id'])
        self.assertTrue(fixed['preview']['can_confirm'])
        recon.confirm(self.db,fixed['id'],{'revision':fixed['revision']})
        self.assertEqual(self.counts()['bank_documents'],1)

    def test_same_net_journal_changes_force_resave_and_remain_visible_pending(self):
        data = self.payload(); saved = recon.save(self.db,data)
        self.movement('7',ds='2026-01-04'); self.movement('7','payment','2026-01-05')
        with self.assertRaises(accounting.ConflictError):recon.confirm(self.db,saved['id'],{'revision':saved['revision']})
        detail = recon.detail(self.db,saved['id']); self.assertTrue(detail['book_changed'])
        self.assertEqual(detail['snapshot']['totals']['book_closing_cents'],100000)
        self.assertEqual(detail['live_book_cents'],100000)
        fixed = recon.save(self.db,{**data,'revision':saved['revision']},saved['id'])
        confirmed = recon.confirm(self.db,fixed['id'],{'revision':fixed['revision']})
        self.assertEqual(confirmed['snapshot']['totals']['unmatched_book_count'],2)
        self.assertEqual(confirmed['snapshot']['totals']['unmatched_book_cents'],0)
        next_plan = recon.preview(self.db,self.payload(from_date='2026-02-01',to_date='2026-02-28'))
        self.assertEqual(len(next_plan['book_lines']),2)

    def test_gross_opposite_directions_cannot_disappear_in_net_zero_match(self):
        incoming = self.movement('1000'); outgoing = self.movement('1000','payment')
        data = self.payload(lines=[{'date':'2026-01-10','amount':'100'},{'date':'2026-01-10','amount':'-100'}],
            matches=[{'statement_line_ids':[1,2],'book_line_ids':[incoming['bank_line_id'],outgoing['bank_line_id']]}])
        plan = recon.preview(self.db,data)
        self.assertFalse(plan['can_confirm']); self.assertIn('match_difference',{b['code'] for b in plan['blockers']})

    def test_source_keys_aliases_preserve_stable_matching_and_reject_conflicting_ids(self):
        doc = self.movement('10')
        data = self.payload(closing_balance='1010',lines=[{'line_key':'source-key','date':'2026-01-10','amount_cents':1000}],
                            groups=[{'statement_line_keys':['source-key'],'ledger_line_ids':[doc['bank_line_id']]}])
        del data['matches']
        plan = recon.preview(self.db,data)
        self.assertTrue(plan['can_confirm'],plan['blockers'])
        self.assertEqual(plan['groups'][0]['statement_line_ids'],[1])
        saved = recon.save(self.db,data)
        self.assertEqual(recon.save(self.db,data)['id'],saved['id'])
        bad = copy.deepcopy(data);bad['groups'][0]['statement_line_ids']=[999]
        with self.assertRaises(ValueError):recon.preview(self.db,bad)

    def test_reused_statement_book_other_account_and_opening_entries_are_rejected(self):
        a=self.movement('10'); b=self.movement('10',account=self.other)
        data=self.payload(closing_balance='1010',lines=[{'date':'2026-01-10','amount':'10'}])
        data['matches']=[{'statement_line_ids':[1],'book_line_ids':[b['bank_line_id']]}]
        self.assertFalse(recon.preview(self.db,data)['can_confirm'])
        data['matches']=[{'statement_line_ids':[1],'book_line_ids':[a['bank_line_id']]},{'statement_line_ids':[1],'book_line_ids':[b['bank_line_id']]}]
        with self.assertRaises(ValueError):recon.preview(self.db,data)
        opening_id=self.db.execute("SELECT l.id FROM journal_lines l JOIN journal_entries j ON j.id=l.journal_id WHERE l.account_id=? AND j.source_type='opening_balance'",(self.bank,)).fetchone()[0]
        data['matches']=[{'statement_line_ids':[1],'book_line_ids':[opening_id]}]
        self.assertFalse(recon.preview(self.db,data)['can_confirm'])

    def test_certificate_immutability_safe_retries_and_single_draft(self):
        data=self.payload(); saved=recon.save(self.db,data)
        self.assertEqual(recon.save(self.db,data)['id'],saved['id'])
        with self.assertRaises(accounting.ConflictError):recon.save(self.db,self.payload())
        with self.assertRaises(accounting.ConflictError):recon.save(self.db,{**data,'revision':99},saved['id'])
        confirmed=recon.confirm(self.db,saved['id'],{'revision':saved['revision']})
        self.assertEqual(recon.confirm(self.db,saved['id'],{'revision':saved['revision']})['id'],saved['id'])
        with self.assertRaises(ValueError):recon.save(self.db,{**data,'revision':confirmed['revision']},saved['id'])
        with self.assertRaises(sqlite3.IntegrityError):self.db.execute('UPDATE bank_reconciliations SET reference=? WHERE id=?',('tamper',saved['id']))
        self.assertEqual(recon.list_reconciliations(self.db)[0]['effective_status'],'confirmed')

    def test_account_scoped_lock_allows_other_bank_later_correction_and_old_journal_replay(self):
        old=self.movement('10'); data=self.payload(closing_balance='1010',lines=[{'date':'2026-01-10','amount':'10'}],matches=[{'statement_line_ids':[1],'book_line_ids':[old['bank_line_id']]}])
        cert=self.confirm(data)
        with self.assertRaises(ValueError):self.movement('5',ds='2026-01-02')
        self.movement('5',ds='2026-02-01'); self.movement('5',ds='2026-01-02',account=self.other)
        old_journal=old['journal']
        lines=[{k:l[k] for k in ('account_id','debit_cents','credit_cents','supplier_id','department_id','source_line_id')} for l in old_journal['lines']]
        self.assertEqual(payables.create_journal(self.db,old_journal['source_type'],old_journal['source_id'],old_journal['entry_date'],lines),old_journal['id'])
        self.assertFalse(recon.detail(self.db,cert['id'])['book_changed'])

    def test_latest_only_void_preserves_original_and_releases_locks_without_journals(self):
        first=self.confirm(self.payload()); second=self.confirm(self.payload(from_date='2026-02-01',to_date='2026-02-28'))
        data={'request_key':self.key('void'),'business_date':'2026-03-01','reference':'VOID-1','reason':'إعادة مراجعة كشف البنك','revision':first['revision']}
        with self.assertRaises(ValueError):recon.void(self.db,first['id'],data)
        before=self.counts()
        second_data={**data,'request_key':self.key('void'),'revision':second['revision']}
        result=recon.void(self.db,second['id'],second_data)
        self.assertEqual(result['effective_status'],'voided');self.assertEqual(result['status'],'confirmed')
        self.assertEqual(recon.void(self.db,second['id'],second_data)['void']['id'],result['void']['id'])
        recon.void(self.db,first['id'],data)
        self.assertEqual(before,self.counts())
        self.movement('5',ds='2026-01-02')
        replacement=self.confirm(self.payload(closing_balance='1000'))
        self.assertEqual(replacement['snapshot']['totals']['pending_deposit_cents'],500)

    def test_continuity_opening_carry_and_overlap_cannot_be_bypassed(self):
        first=self.confirm(self.payload())
        for changes in ({'from_date':'2026-02-02','to_date':'2026-02-28'},
                        {'from_date':'2026-02-01','to_date':'2026-02-28','opening_balance':'999','closing_balance':'999'},
                        {'from_date':'2025-12-01','to_date':'2025-12-31'}):
            self.assertFalse(recon.preview(self.db,self.payload(**changes))['can_confirm'])
        with self.assertRaises(ValueError):recon.save(self.db,self.payload())
        after = self.payload(from_date='2026-02-01',to_date='2026-02-28')
        saved=recon.save(self.db,after)
        recon.void(self.db,first['id'],{'request_key':self.key('void'),'revision':first['revision'],'business_date':'2026-02-01','reference':'VOID-PREDECESSOR','reason':'إعادة تكوين الافتتاح'})
        with self.assertRaises(accounting.ConflictError):recon.confirm(self.db,saved['id'],{'revision':saved['revision']})

    def test_closed_month_guards_nonposting_documents_and_confirmed_replay(self):
        saved=recon.save(self.db,self.payload())
        self.db.execute("INSERT INTO accounting_periods(month,status,revision,updated_at) VALUES('2026-01','closed',1,'test')")
        with self.assertRaises(ValueError):recon.confirm(self.db,saved['id'],{'revision':saved['revision']})
        with self.assertRaises(ValueError):recon.save(self.db,{**self.payload(),'account_id':self.other})
        self.assertEqual(recon.save(self.db,{**json.loads(self.db.execute('SELECT payload_json FROM bank_reconciliations WHERE id=?',(saved['id'],)).fetchone()[0]),'request_key':saved['request_key']})['id'],saved['id'])

    def test_invalid_money_ids_dates_and_duplicate_rows_never_create_financial_data(self):
        before=self.counts()
        for changes in ({'opening_balance':True},{'closing_balance':'1.001'},{'opening_balance':float('nan')},
                        {'from_date':'2026-02-30'},{'account_id':self.cash},{'account_id':True},
                        {'lines':[{'date':'2026-02-01','amount':'1'}]},
                        {'lines':[{'date':'2026-01-01','amount':'0'}]},
                        {'lines':[{'line_key':'x','date':'2026-01-01','amount':'1'},{'line_key':'x','date':'2026-01-02','amount':'1'}]}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):recon.preview(self.db,self.payload(**changes))
        self.assertEqual(before,self.counts())

    def test_pending_opening_and_operating_net_cheques_are_information_not_bank_adjustments(self):
        bank = self.bank
        self.f.tearDown()
        self.start([{'kind':'issued_cheque','amount_cents':20000,'side':'credit','reference':'OLD-CHEQUE',
                     'source_date':'2025-12-01','due_date':'2026-01-20','extra':{'bank_account_id':bank,'bank_name':'بنك مصر','beneficiary':'المستفيد القديم','cheque_number':'OLD-CHQ-1'}}])
        invoice=self.f.invoice(10000,business_date='2026-01-02',due_date='2026-01-10')
        tax=withholding.create_code(self.db,{'name':'خصم اختبار','rate':'3','active':True,'confirmed':True})
        details={**self.f.details('cheque'),'cheque_date':'2026-01-10','due_date':'2026-02-03'}
        payment=self.f.post(self.f.payload('cheque',amount='100',payment_date='2026-01-10',details=details,
            allocation_mode='invoice',target_invoice_id=invoice['id'],withholding_code_id=tax['id']))
        plan=recon.preview(self.db,self.payload())
        self.assertTrue(plan['can_confirm'],plan['blockers'])
        self.assertEqual(plan['book_lines'],[])
        self.assertEqual(sorted(c['amount_cents'] for c in plan['pending_cheques']),[9700,20000])
        self.assertEqual(plan['totals']['adjusted_statement_cents'],100000)
        self.confirm(self.payload())
        cleared=supplier_payments.clear_cheque(self.db,payment['id'],{'revision':payment['revision'],'clearance_date':'2026-02-03','bank_account_id':self.bank,'reference':'OPERATING-CLEAR'})
        old=banks.list_opening_cheques(self.db)[0]
        cleared_old=banks.clear_opening_cheque(self.db,old['id'],{'request_key':self.key('clear'),'revision':old['revision'],
            'business_date':'2026-02-04','value_date':'2026-02-04','reference':'OLD-CLEAR','bank_reference':'BANK-CLEAR'})
        bank_ids=[next(l['id'] for l in cleared['clearance_journal']['lines'] if l['account_id']==self.bank),
                  next(l['id'] for l in cleared_old['clearance']['journal']['lines'] if l['account_id']==self.bank)]
        following=self.payload(from_date='2026-02-01',to_date='2026-02-28',closing_balance='703',
            lines=[{'date':'2026-02-03','amount':'-97'},{'date':'2026-02-04','amount':'-200'}],
            matches=[{'statement_line_ids':[1,2],'book_line_ids':bank_ids}])
        self.assertTrue(recon.preview(self.db,following)['can_confirm'])
        self.assertEqual(recon.preview(self.db,following)['pending_cheques'],[])
        jan=recon.preview(self.db,{**self.payload(),'reconciliation_id':1})
        self.assertEqual(sorted(c['amount_cents'] for c in jan['pending_cheques']),[9700,20000])

    def test_historical_date_and_gross_limits_validate_before_reading(self):
        saved=recon.save(self.db,self.payload())
        with self.assertRaises(ValueError):recon.detail(self.db,saved['id'],'invalid')
        with self.assertRaises(ValueError):recon.list_reconciliations(self.db,as_of='2026-02-30')
        data=self.payload(lines=[{'date':'2026-01-10','amount_cents':payables.MAX_CENTS},{'date':'2026-01-10','amount_cents':1}])
        with self.assertRaises(ValueError):recon.preview(self.db,data)

    def test_transaction_owner_can_roll_back_confirmation_completely(self):
        data,pending,docs=self.period();self.db.commit()
        with self.assertRaises(RuntimeError):
            with self.db:
                cert=self.confirm(data)
                self.assertEqual(cert['status'],'confirmed')
                raise RuntimeError('caller rollback')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM bank_reconciliations').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM bank_reconciliation_matches').fetchone()[0],0)


if __name__=='__main__':unittest.main()
