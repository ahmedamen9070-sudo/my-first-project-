/** EGP bank movements and opening cheque clearance. Disposable fresh MADAR_DB. */
'use strict';
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const base=process.env.AM_TEST_URL||'http://127.0.0.1:8157',W='#bank-window';
const A=name=>`${W} [data-bank-action="${name}"]`,F=name=>`${W} [data-bank-field="${name}"]`;
const stamp=Date.now().toString(36);let serial=0;
const key=name=>`BANK-UI-${stamp}-${name}-${++serial}`;
const money=cents=>new Intl.NumberFormat('ar-EG',{minimumFractionDigits:2,maximumFractionDigits:2}).format(cents/100);
async function api(page,route,method='GET',data,expected=200){const r=await page.evaluate(async({route,method,data})=>{const b=await(await fetch('/api/bootstrap')).json(),r=await fetch('/api/'+route,{method,headers:{'Content-Type':'application/json','X-Madar-CSRF':b.csrf},body:data===undefined?undefined:JSON.stringify(data)});return{status:r.status,body:await r.json()};},{route,method,data});assert.equal(r.status,expected,`${method} ${route}: ${JSON.stringify(r.body)}`);return r.body;}
async function clicked(page,selector,route,method='POST'){const waiting=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/'+route&&r.request().method()===method);await page.locator(selector).click();const r=await waiting,body=await r.json();assert.equal(r.status(),200,`${method} ${route}: ${JSON.stringify(body)}`);return body;}
async function choose(page,name,id){await page.locator(`${A('pick')}[data-bank-pick="${name}"]`).click();await page.locator(`#picker-window [data-pick="${id}"]`).click();}
async function close(page){await page.locator(A('close')).first().click();await page.waitForFunction(()=>!document.querySelector('#bank-window').open);}
async function printed(page,words,action='print'){const before=await page.evaluate(()=>window.__bankPrints);await page.locator(A(action)).click();await page.waitForFunction(n=>window.__bankPrints>n,before);const content=await page.locator('#print-document').textContent();for(const word of words)assert.ok(content.includes(word),'Print includes '+word);assert.match(content,/راجعه|اعتمده|توقيع/);await page.emulateMedia({media:'print'});assert.equal(await page.locator('dialog:visible').count(),0);assert.ok(await page.locator('#print-document').isVisible());await page.emulateMedia({media:'screen'});}
async function movement(page,kind,amount,accounts,date='2026-01-05'){
  await page.evaluate(kind=>Banking.newDocument(kind),kind);await page.waitForSelector(F('business_date'));
  await page.locator(F('business_date')).fill(date);await page.locator(F('value_date')).fill(date);
  await page.locator(F('reference')).fill(key('REF'));await page.locator(F('bank_reference')).fill(key('BANK-REF'));await page.locator(F('amount')).fill(String(amount));
  await choose(page,'account_id',accounts.source);
  if(kind==='transfer'){await choose(page,'destination_account_id',accounts.destination);assert.equal(await page.locator(F('party_name')).count(),0);}
  else{await choose(page,'counter_account_id',accounts.counter);await page.locator(F('party_name')).fill('مستفيد موثق لاختبار البنك');if(['payment','fee'].includes(kind))await choose(page,'department_id',accounts.department);}
  await clicked(page,A('preview'),'bank-preview');const saved=await clicked(page,A('save'),'bank-documents');
  assert.equal(saved.status,'draft');const result=await clicked(page,A('post'),`bank-documents/${saved.id}/post`);
  assert.equal(result.journal.debit_cents,Math.round(amount*100));assert.equal(result.journal.credit_cents,Math.round(amount*100));
  await printed(page,[result.code,money(Math.round(amount*100)),'نيو بانوراما']);await close(page);return result;
}
async function bankBook(page,account,to='2026-01-31'){return api(page,`bank-book?account_id=${account}&from_date=2026-01-01&to_date=${to}`);}
(async()=>{
  const browser=await chromium.launch({executablePath:process.env.AM_CHROMIUM||'/usr/bin/chromium',args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:1080}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));await page.addInitScript(()=>{window.__bankPrints=0;window.print=()=>window.__bankPrints++;});
  try{
    await page.goto(base);await page.waitForSelector('#loading[hidden]',{state:'attached'});
    const boot=await api(page,'bootstrap'),chart=await api(page,'accounting');
    for(const m of chart.mappings)if(m.status!=='confirmed')await api(page,`account-mappings/${m.id}`,'PUT',{revision:m.revision,status:'confirmed',account_id:m.account_id});
    const c=await api(page,'bank-config'),bank=c.bank_accounts.find(a=>a.code==='112001'),other=c.bank_accounts.find(a=>a.code==='112003'),cash=c.cash_accounts.find(a=>a.code==='111002');
    const equity=c.counter_accounts_by_kind.receipt.find(a=>a.code.startsWith('3')),expense=c.counter_accounts_by_kind.payment.find(a=>a.code.startsWith('6')),department=boot.departments[0].id;
    assert.ok(bank&&other&&cash&&equity&&expense);assert.ok(!c.bank_accounts.some(a=>a.code==='112002'));
    assert.equal((await bankBook(page,bank.id)).closing_balance_cents,0);assert.equal((await api(page,'bank-documents')).length,0);
    await page.locator('[data-banks-open]').first().click();await page.waitForSelector(A('new-master'));
    assert.equal(await page.locator('select').count(),0);assert.equal(await page.locator('aside:visible').count(),0);await close(page);
    await page.evaluate(()=>Banking.accountMaster());await page.waitForSelector(F('bank_name'));await choose(page,'chart_account_id',bank.id);
    await page.locator(F('bank_name')).fill('بنك مصر • ملف اختبار');await page.locator(F('branch')).fill('القاهرة');await page.locator(F('account_number')).fill('TEST-1234');
    assert.equal(await page.locator(F('opening_balance')).count(),0,'Bank master has no opening money input');
    const master=await clicked(page,A('save-master'),'bank-accounts');assert.equal(master.chart_account_id,bank.id);await close(page);
    assert.equal((await bankBook(page,bank.id)).closing_balance_cents,0,'Master creates no funds');

    // A real balanced initial position, including the independently clearable NET cheque.
    const initial=await api(page,'opening-balances','POST',{request_key:key('BASELINE'),business_date:'2026-01-01',reference:key('OPEN'),lines:[
      {kind:'general',account_id:bank.id,side:'debit',amount:'5000',reference:'BANK-OPEN'},
      {kind:'issued_cheque',side:'credit',amount:'200',reference:'CHEQUE-OPEN',source_date:'2025-12-01',due_date:'2026-01-20',extra:{bank_account_id:bank.id,bank_name:'بنك مصر',beneficiary:'مستفيد سابق للتشغيل',cheque_number:'OLD-'+stamp}},
      {kind:'general',account_id:equity.id,side:'credit',amount:'4800',reference:'EQUITY-OPEN'}]});
    const baseline=await api(page,`opening-balances/${initial.id}/post`,'POST',{revision:initial.revision});assert.equal(baseline.status,'posted');
    assert.equal((await bankBook(page,bank.id)).closing_balance_cents,500000);
    const a={source:bank.id,counter:equity.id,department};
    const receipt=await movement(page,'receipt',1000.01,a,'2026-01-02');
    const payment=await movement(page,'payment',20.01,{...a,counter:expense.id},'2026-01-03');
    await movement(page,'fee',10,{...a,counter:expense.id},'2026-01-04');
    await movement(page,'transfer',200,{source:bank.id,destination:other.id},'2026-01-05');
    await movement(page,'transfer',100,{source:bank.id,destination:cash.id},'2026-01-06');
    await movement(page,'transfer',40,{source:cash.id,destination:bank.id},'2026-01-07');
    assert.equal((await bankBook(page,bank.id)).closing_balance_cents,571000);assert.equal((await bankBook(page,other.id)).closing_balance_cents,20000);
    const cashBook=await api(page,`cash-book?account_id=${cash.id}&from_date=2026-01-01&to_date=2026-01-31`);
    assert.equal(cashBook.closing_balance_cents,6000);assert.equal(cashBook.rows.length,2);assert.ok(cashBook.rows.every(r=>r.action.type==='bank_document'));
    await page.evaluate(({id})=>Banking.openDocument(id),{id:payment.id});await page.waitForSelector(A('reverse'));
    assert.equal(await page.locator(A('post')).count(),0);assert.equal(await page.locator(F('amount')).count(),0);
    await page.locator(A('reverse')).click();await page.locator(F('business_date')).fill('2026-01-10');await page.locator(F('reference')).fill(key('REVERSE'));await page.locator(F('reason')).fill('تصحيح مستند اختبار موثق');
    const reversal=await clicked(page,A('confirm-reverse'),`bank-documents/${payment.id}/reverse`);assert.equal(reversal.effective_status,'reversed');
    await printed(page,[payment.code,'تصحيح مستند اختبار موثق']);await close(page);
    assert.equal((await bankBook(page,bank.id,'2026-01-09')).closing_balance_cents,571000);assert.equal((await bankBook(page,bank.id)).closing_balance_cents,573001);

    const cheque=(await api(page,'opening-bank-cheques'))[0],apBefore=await api(page,'supplier-payments');
    await page.evaluate(id=>Banking.openingCheque(id),cheque.id);await page.waitForSelector(A('clear-cheque'));await page.locator(A('clear-cheque')).click();
    await page.locator(F('business_date')).fill('2026-01-21');await page.locator(F('value_date')).fill('2026-01-22');await page.locator(F('reference')).fill(key('CLEAR'));await page.locator(F('bank_reference')).fill(key('CLEAR-BANK'));
    const cleared=await clicked(page,A('confirm-clear'),`opening-bank-cheques/${cheque.id}/clear`);assert.equal(cleared.effective_status,'cleared');assert.equal(cleared.clearance.amount_cents,20000);
    assert.equal(await page.locator(A('clear-cheque')).count(),0);await printed(page,[cheque.cheque_number,'مستفيد سابق للتشغيل',money(20000)],'print-cheque');await close(page);
    assert.deepEqual(await api(page,'supplier-payments'),apBefore,'Clearing opening cheque creates no dummy AP payment');
    const report=await bankBook(page,bank.id);assert.equal(report.closing_balance_cents,553001);assert.equal(report.rows.filter(r=>r.source_type==='opening_cheque_clearance').length,1);

    await page.evaluate(id=>Banking.book(id,{from_date:'2026-01-01',to_date:'2026-01-31'}),bank.id);await page.waitForSelector(A('print-report'));
    await printed(page,[bank.code,money(553001),'كشف حركة البنك'],'print-report');
    await page.locator(F('to_date')).fill('2026-01-09');assert.ok(await page.locator(A('print-report')).isDisabled());
    await page.locator(A('reload-book')).click();await page.waitForFunction(()=>document.querySelector('#bank-window [data-bank-action="print-report"]')?.disabled===false);
    assert.ok((await page.locator(W).textContent()).includes(money(571000)));await close(page);
    await page.evaluate(()=>Banking.newDocument('receipt'));await page.waitForSelector(F('reference'));await page.locator(F('reference')).fill('لم يحفظ');
    page.once('dialog',d=>d.dismiss());await page.keyboard.press('Escape');assert.ok(await page.locator(W).evaluate(e=>e.open));
    page.once('dialog',d=>d.accept());await page.keyboard.press('Escape');assert.equal(await page.locator(W).evaluate(e=>e.open),false);

    // A late GET cannot reopen a window the operator has already closed.
    await page.route('**/api/bank-config',async route=>{await new Promise(r=>setTimeout(r,250));await route.continue();});
    await page.evaluate(()=>{Banking.newDocument('fee');});await page.waitForSelector(`${W}[open]`);await page.locator(A('close')).first().click();await page.waitForTimeout(400);
    assert.equal(await page.locator(W).evaluate(e=>e.open),false);await page.unroute('**/api/bank-config');
    await page.evaluate(()=>Banking.open());await page.waitForSelector(A('new-fee'));await page.locator(W).screenshot({path:'/tmp/am-banks-workspace.png'});
    for(const width of [1024,768,390]){await page.setViewportSize({width,height:900});const size=await page.locator(W).boundingBox();assert.ok(size.width<=width+1,`Bank dialog fits ${width}px`);}
    await close(page);assert.deepEqual(errors,[]);assert.ok((await api(page,'trial-balance?from_date=2026-01-01&to_date=2026-01-31')).balanced);
    console.log('Banks UI passed: opening NET cheque, four movements, both cash directions, immutable reversal, reports, print, dirty/Escape/late GET/mobile.');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
