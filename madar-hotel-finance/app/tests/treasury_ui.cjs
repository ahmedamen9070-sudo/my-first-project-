/** Main cash and operating custodies acceptance. Disposable fresh MADAR_DB only. */
'use strict';
const assert=require('node:assert/strict');
const path=require('node:path');
const {chromium}=require('playwright');
const base=process.env.AM_TEST_URL||'http://127.0.0.1:8140';
const W='#treasury-window';
const A=name=>`${W} [data-treasury-action="${name}"]`;
const F=name=>`${W} [data-treasury-field="${name}"]`;
const stamp=Date.now().toString(36);let sequence=0;
const key=label=>`CASH-TEST-${stamp}-${label}-${++sequence}`;
const money=cents=>new Intl.NumberFormat('ar-EG',{minimumFractionDigits:2,maximumFractionDigits:2}).format(cents/100);

async function api(page,route,method='GET',data,expected=200){
  const result=await page.evaluate(async({route,method,data})=>{
    const boot=await(await fetch('/api/bootstrap')).json();
    const response=await fetch('/api/'+route,{method,headers:{'Content-Type':'application/json','X-Madar-CSRF':boot.csrf},body:data===undefined?undefined:JSON.stringify(data)});
    return{status:response.status,body:await response.json()};
  },{route,method,data});
  assert.equal(result.status,expected,`${method} ${route}: ${JSON.stringify(result.body)}`);return result.body;
}
async function clickResponse(page,selector,route,method='POST'){
  const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/'+route&&r.request().method()===method);
  await page.locator(selector).click();const response=await pending,result=await response.json();
  assert.equal(response.status(),200,`${method} ${route}: ${JSON.stringify(result)}`);return result;
}
async function choose(page,name,id,line=null){
  const selector=line===null?`${A('pick')}[data-treasury-pick="${name}"]`:`${A('line-pick')}[data-line-index="${line}"][data-treasury-pick="${name}"]`;
  await page.locator(selector).click();await page.locator(`#picker-window [data-pick="${id}"]`).click();
}
async function print(page,words){
  const before=await page.evaluate(()=>window.__cashPrints);await page.locator(await page.locator(A('print')).count()?A('print'):A('print-report')).click();
  await page.waitForFunction(before=>window.__cashPrints>before,before);
  const content=await page.locator('#print-document').textContent();
  for(const word of words)assert.ok(content.includes(word),'Print contains '+word);
  assert.match(content,/راجعه|اعتمده|توقيع|استلم/);
  await page.emulateMedia({media:'print'});assert.equal(await page.locator('dialog:visible').count(),0);assert.ok(await page.locator('#print-document').isVisible());await page.emulateMedia({media:'screen'});
}
async function close(page){await page.locator(A('close')).first().click();await page.waitForFunction(()=>!document.querySelector('#treasury-window').open);}
async function openCash(page,kind){await page.evaluate(kind=>Treasury.newDocument(kind),kind);await page.waitForSelector(F('business_date'));}
async function cash(page,kind,date,amount,accounts,department){
  await openCash(page,kind);
  await page.locator(F('business_date')).fill(date);await page.locator(F('reference')).fill(key(kind));await page.locator(F('amount')).fill(String(amount));
  await choose(page,'cash_account_id',accounts.cash);
  if(kind==='transfer'){
    await choose(page,'destination_cash_account_id',accounts.destination);
    assert.equal(await page.locator(F('party_name')).count(),0,'Transfer does not require an external recipient');
  }else{
    await page.locator(F('party_name')).fill(kind==='receipt'?'تمويل فعلي للاختبار فقط':'مستلم مصروف اختبار');
    await choose(page,'counter_account_id',accounts.counter);
    if(kind==='payment')await choose(page,'department_id',department);
    assert.equal(await page.locator(`${A('pick')}[data-treasury-pick="destination_cash_account_id"]`).count(),0,'Cash vouchers show no transfer destination');
  }
  await clickResponse(page,A('preview'),'treasury-preview');
  const saved=await clickResponse(page,A('save'),'treasury-documents');
  const posted=await clickResponse(page,A('post'),`treasury-documents/${saved.id}/post`);
  assert.equal(posted.status,'posted');assert.equal(posted.amount_cents,amount*100);
  assert.equal(posted.journal.debit_cents,amount*100);assert.equal(posted.journal.credit_cents,amount*100);
  await print(page,[posted.code,money(amount*100),'نيو بانوراما']);
  assert.equal(await page.locator(A('post')).count(),0,'Posted document has no approve-again control');
  await close(page);return posted;
}
async function custody(page,kind,date,amount,context){
  const preset=kind==='issue'?{custodian_id:context.person.id,cash_account_id:context.cash}:{source_issue_id:context.issue.id,cash_account_id:context.cash};
  await page.evaluate(({kind,preset})=>Treasury.newCustody(kind,preset),{kind,preset});await page.waitForSelector(F('business_date'));
  await page.locator(F('business_date')).fill(date);await page.locator(F('reference')).fill(key(kind));
  if(kind==='issue'){
    await choose(page,'custodian_id',context.person.id);await choose(page,'cash_account_id',context.cash);
    await page.locator(F('purpose')).fill('مشتريات تشغيلية صغيرة بمستندات اختبار');await page.locator(F('amount')).fill(String(amount));
  }else{
    // The original issue determines the person and custody account automatically.
    assert.ok((await page.locator(W).textContent()).includes(context.issue.code));
    if(kind==='return'){await choose(page,'cash_account_id',context.cash);await page.locator(F('amount')).fill(String(amount));}
    else{
      if(!await page.locator(`${W} [data-treasury-line-field="amount"]`).count())await page.locator(A('add-line')).click();
      await choose(page,'expense_account_id',context.expense,0);await choose(page,'department_id',context.department,0);
      await page.locator(`${W} [data-treasury-line-field="reference"]`).first().fill(key('EXP-RECEIPT'));
      await page.locator(`${W} [data-treasury-line-field="amount"]`).first().fill(String(amount));
      assert.equal(await page.locator(F('amount')).count(),0,'Expense settlement total is derived from its lines');
    }
  }
  await clickResponse(page,A('preview'),'custody-preview');
  const saved=await clickResponse(page,A('save'),'custody-documents');
  const posted=await clickResponse(page,A('post'),`custody-documents/${saved.id}/post`);
  assert.equal(posted.status,'posted');assert.equal(posted.amount_cents,amount*100);
  assert.equal(posted.journal.debit_cents,amount*100);assert.equal(posted.journal.credit_cents,amount*100);
  await print(page,[posted.code,money(amount*100),context.person.name]);
  await close(page);return posted;
}
async function reverse(page,document,kind,date){
  await page.evaluate(({id,kind})=>kind==='cash'?Treasury.openDocument(id):Treasury.openCustody(id),{id:document.id,kind});await page.waitForSelector(A('reverse'));
  await page.locator(A('reverse')).click();await page.waitForSelector(F('reason'));
  await page.locator(F('business_date')).fill(date);await page.locator(F('reference')).fill(key('REV'));await page.locator(F('reason')).fill('تصحيح موثق في قاعدة اختبار معزولة');
  const result=await clickResponse(page,A('confirm-reverse'),`${kind==='cash'?'treasury':'custody'}-documents/${document.id}/reverse`);
  assert.equal(result.effective_status,'reversed');assert.ok(result.reversal?.journal);
  await print(page,[document.code,'تصحيح موثق']);await close(page);return result;
}
async function book(page,account,ds='2026-01-31'){return api(page,`cash-book?account_id=${account}&from_date=2026-01-01&to_date=${ds}`);}

(async()=>{
  const browser=await chromium.launch({executablePath:process.env.AM_CHROMIUM||'/usr/bin/chromium',args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:1080}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));await page.addInitScript(()=>{window.__cashPrints=0;window.print=()=>window.__cashPrints++;});
  try{
    await page.goto(base);await page.waitForSelector('#loading[hidden]',{state:'attached'});
    const boot=await api(page,'bootstrap'),chart=await api(page,'accounting');
    await page.evaluate(()=>Treasury.custodies());await page.waitForSelector(A('mappings'));await page.locator(A('mappings')).click();
    await page.waitForSelector('#account-mappings-panel:not([hidden])');assert.equal(await page.locator(W).evaluate(e=>e.open),false,'Initial custody setup opens mapping review');
    await page.evaluate(()=>page('dashboard'));
    for(const mapping of chart.mappings.filter(m=>['operating_custodies','payable_control','supplier_advances','issued_cheques','withholding_payable'].includes(m.role)))
      await api(page,`account-mappings/${mapping.id}`,'PUT',{revision:mapping.revision,status:'confirmed',account_id:mapping.account_id});
    const config=await api(page,'treasury-config'),main=config.cash_accounts.find(a=>a.code==='111002'),petty=config.cash_accounts.find(a=>a.code==='111003');
    const equity=config.counter_accounts_by_kind.receipt.find(a=>a.code.startsWith('3'))||config.counter_accounts_by_kind.receipt[0];
    const expense=config.counter_accounts_by_kind.payment.find(a=>a.code.startsWith('6'))||config.counter_accounts_by_kind.payment[0];
    assert.ok(main&&petty&&equity&&expense);assert.ok(config.cash_accounts.some(a=>a.code==='111004'),'Physical POS cash stays included');assert.ok(!config.cash_accounts.some(a=>a.code==='111005'),'Transit is excluded from physical cash');
    assert.equal((await book(page,main.id)).closing_balance_cents,0,'No cash or opening balances are invented');
    await page.locator('[data-treasury-open]').first().click();await page.waitForSelector(`${W}[open]`);await close(page);
    assert.equal(await page.locator('select').count(),0);assert.equal(await page.locator('aside:visible').count(),0);
    const receipt=await cash(page,'receipt','2026-01-01',1000,{cash:main.id,counter:equity.id});
    const payment=await cash(page,'payment','2026-01-02',100,{cash:main.id,counter:expense.id},boot.departments[0].id);
    const transfer=await cash(page,'transfer','2026-01-03',200,{cash:main.id,destination:petty.id});
    assert.equal((await book(page,main.id)).closing_balance_cents,70000);assert.equal((await book(page,petty.id)).closing_balance_cents,20000);
    assert.equal((await api(page,'treasury-position?as_of=2026-01-03')).totals.balance_cents,90000,'Internal cash transfer preserves total physical cash');

    // Operating custodian is a separate master, independent of payroll or suppliers.
    await page.evaluate(()=>Treasury.custodianMaster());await page.waitForSelector(F('name'));await page.locator(F('name')).fill('أمين عهدة اختبار '+stamp);await page.locator(F('phone')).fill('TEST-010');
    const person=await clickResponse(page,A('save-master'),'custodians');await close(page);
    const custodyConfig=await api(page,'custody-config'),custodyExpense=custodyConfig.expense_accounts.find(a=>a.id===expense.id)||custodyConfig.expense_accounts[0];
    const context={person,cash:main.id,expense:custodyExpense.id,department:boot.departments[0].id};
    const issue=await custody(page,'issue','2026-01-04',150,context);context.issue=issue;
    const settled=await custody(page,'settlement','2026-01-05',60,context);
    const returned=await custody(page,'return','2026-01-06',90,context);
    let balances=await api(page,'custody-balances?as_of=2026-01-04');assert.ok(balances.matched);assert.equal(balances.totals.remaining_cents,15000);
    balances=await api(page,'custody-balances?as_of=2026-01-06');assert.ok(balances.matched);assert.equal(balances.totals.remaining_cents,0);
    let cashbook=await book(page,main.id);assert.equal(cashbook.closing_balance_cents,64000);assert.equal(cashbook.rows.filter(r=>r.source_type==='custody_settlement').length,0,'Expense settlement does not pay cash again');
    assert.equal(cashbook.rows.filter(r=>r.source_type==='custody_issue').length,1);assert.equal(cashbook.rows.filter(r=>r.source_type==='custody_return').length,1);

    // Existing supplier cash payment already posts its NET movement; treasury reads it once.
    const supplier=await api(page,'suppliers','POST',{name:'مورد ربط الخزينة '+stamp,terms_days:0});
    const product=await api(page,'products','POST',{supplier_id:supplier.id,department_id:boot.departments[0].id,name:'خدمة ربط الخزينة '+stamp,unit:'خدمة',price:'100',expense_account_id:expense.id});
    const invDraft=await api(page,'invoices','POST',{request_key:key('INV'),supplier_id:supplier.id,department_id:boot.departments[0].id,reference:key('INV-REF'),business_date:'2026-01-09',due_date:'2026-01-09',kind:'service',lines:[{product_id:product.id,quantity:'1',price:'100'}]});
    const invoice=await api(page,`invoices/${invDraft.id}/post`,'POST',{revision:invDraft.revision});
    const wh=await api(page,'withholding-codes','POST',{name:'TEST CASH WHT '+stamp,rate:'3',active:true,confirmed:true});
    const apDraft=await api(page,'supplier-payments','POST',{request_key:key('AP'),supplier_id:supplier.id,payment_date:'2026-01-10',amount:'100',method:'cash',funding_account_id:main.id,allocation_mode:'invoice',target_invoice_id:invoice.id,withholding_code_id:wh.id,details:{received_by:'مندوب المورد في الاختبار'}});
    const ap=await api(page,`supplier-payments/${apDraft.id}/post`,'POST',{revision:apDraft.revision});assert.equal(ap.net_cash_cents,9700);
    cashbook=await book(page,main.id);assert.equal(cashbook.closing_balance_cents,54300);const apRows=cashbook.rows.filter(r=>r.source_type==='supplier_payment'&&r.source_id===ap.id);
    assert.equal(apRows.length,1);assert.equal(apRows[0].credit_cents,9700);assert.equal((await api(page,'treasury-documents')).length,3,'AP payment creates no duplicate generic treasury document');
    assert.ok((await api(page,'supplier-reconciliation?as_of=2026-01-31')).matched);assert.ok((await api(page,'trial-balance?to_date=2026-01-31')).balanced);

    await reverse(page,settled,'custody','2026-01-11');assert.equal((await api(page,'custody-balances?as_of=2026-01-10')).totals.remaining_cents,0);
    assert.equal((await api(page,'custody-balances?as_of=2026-01-11')).totals.remaining_cents,6000);
    await reverse(page,returned,'custody','2026-01-12');await reverse(page,issue,'custody','2026-01-13');
    assert.equal((await api(page,'custody-balances?as_of=2026-01-13')).totals.remaining_cents,0);assert.equal((await book(page,main.id)).closing_balance_cents,60300);
    await reverse(page,payment,'cash','2026-01-14');assert.equal((await book(page,main.id)).closing_balance_cents,70300);
    assert.equal((await api(page,`treasury-documents/${payment.id}?as_of=2026-01-06`)).effective_status,'posted','Correction changes the effective position only on its date');

    const beforeAudit=await api(page,'audit');
    await page.evaluate(id=>Treasury.cashBook(id,{from_date:'2026-01-01',to_date:'2026-01-31'}),main.id);await page.waitForSelector(A('print-report'));
    await page.locator(F('to_date')).fill('2026-02-01');assert.ok(await page.locator(A('print-report')).isDisabled(),'Edited dates cannot print stale balances');
    await page.locator(A('reload-book')).click();await page.waitForSelector(`${A('print-report')}:not([disabled])`);
    assert.equal(await page.locator(F('to_date')).inputValue(),'2026-02-01');
    await page.locator(F('to_date')).fill('2026-01-31');await page.locator(A('reload-book')).click();await page.waitForSelector(`${A('print-report')}:not([disabled])`);
    await print(page,[main.code,ap.code,money(9700),'2026-01-31']);await page.locator(W).screenshot({path:path.join(__dirname,'treasury-cash-book.png')});await close(page);
    await page.evaluate(()=>Treasury.balances('2026-01-31'));await page.waitForSelector(A('print-report'));await print(page,[person.name,'نيو بانوراما']);await page.locator(W).screenshot({path:path.join(__dirname,'treasury-custodies.png')});await close(page);
    assert.deepEqual(await api(page,'audit'),beforeAudit,'Reports and printing do not write financial or audit records');

    // Dirty close preserves the form unless the operator accepts discarding it.
    await openCash(page,'payment');await page.locator(F('reference')).fill(key('UNSAVED'));page.once('dialog',d=>d.dismiss());await page.keyboard.press('Escape');assert.ok(await page.locator(W).evaluate(e=>e.open));
    page.once('dialog',d=>d.accept());await page.keyboard.press('Escape');assert.equal(await page.locator(W).evaluate(e=>e.open),false);
    await page.evaluate(()=>Treasury.open());await page.waitForSelector(A('new-receipt'));await page.setViewportSize({width:392,height:850});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.equal(await page.locator(W).evaluate(e=>e.scrollWidth>e.clientWidth),false,'Treasury fits the phone width');await page.keyboard.press('Escape');
    let delayed=false;await page.route('**/api/treasury-config',async route=>{delayed=true;await new Promise(resolve=>setTimeout(resolve,250));await route.continue();});
    await page.evaluate(()=>{Treasury.open();});await page.waitForSelector(`${W}[open]`);await page.keyboard.press('Escape');
    await page.waitForTimeout(500);assert.ok(delayed);assert.equal(await page.locator(W).evaluate(e=>e.open),false,'Late reads cannot reopen a closed workspace');await page.unroute('**/api/treasury-config');
    const inactive=await api(page,'custodians/'+person.id,'PUT',{name:person.name,phone:person.phone,active:false,revision:person.revision});
    await page.evaluate(id=>Treasury.custodianMaster(id),inactive.id);await page.waitForSelector(A('toggle-active'));assert.equal(await page.locator(A('toggle-active')).textContent(),'موقوف');await close(page);
    await page.evaluate(()=>Treasury.newCustody('issue'));await page.waitForSelector(F('business_date'));await page.locator(`${A('pick')}[data-treasury-pick="custodian_id"]`).click();
    assert.equal(await page.locator(`#picker-window [data-pick="${inactive.id}"]`).count(),0,'Inactive custodians cannot be selected for a new issue');await page.keyboard.press('Escape');await close(page);
    assert.equal(errors.length,0,errors.join('\n'));
    console.log('PASS treasury UI: cash vouchers/transfers, person-linked partial custody expenses/returns, immutable dated corrections, AP NET cash once, historical balances, journal reports/prints, dirty guards and mobile.');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
