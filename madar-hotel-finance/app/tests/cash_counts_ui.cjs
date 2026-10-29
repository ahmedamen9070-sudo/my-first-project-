/** Physical cash count certificates against real, isolated cash journals. */
'use strict';
const assert=require('node:assert/strict');
const path=require('node:path');
const {chromium}=require('playwright');
const base=process.env.AM_TEST_URL||'http://127.0.0.1:8141';
const W='#cash-count-window';
const A=name=>`${W} [data-cash-count-action="${name}"]`;
const F=name=>`${W} [data-cash-count-field="${name}"]`;
const Q=denomination=>`${W} [data-cash-count-quantity="${denomination}"]`;
const stamp=Date.now().toString(36);let sequence=0;
const key=label=>`COUNT-UI-${stamp}-${label}-${++sequence}`;
const money=cents=>new Intl.NumberFormat('ar-EG',{minimumFractionDigits:2,maximumFractionDigits:2}).format(cents/100);

async function api(page,route,method='GET',data,expected=200){
  const response=await page.evaluate(async({route,method,data})=>{
    const boot=await(await fetch('/api/bootstrap')).json();
    const r=await fetch('/api/'+route,{method,headers:{'Content-Type':'application/json','X-Madar-CSRF':boot.csrf},body:data===undefined?undefined:JSON.stringify(data)});
    return{status:r.status,body:await r.json()};
  },{route,method,data});
  assert.equal(response.status,expected,`${method} ${route}: ${JSON.stringify(response.body)}`);return response.body;
}
async function clickResponse(page,selector,route,method='POST',expected=200){
  const waiting=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/'+route&&r.request().method()===method);
  await page.locator(selector).click();const response=await waiting,body=await response.json();
  assert.equal(response.status(),expected,`${method} ${route}: ${JSON.stringify(body)}`);return body;
}
async function treasury(page,kind,amount,accounts,date='2026-01-05'){
  const payload={request_key:key(kind),kind,business_date:date,reference:key('cash-ref'),amount:String(amount),
    cash_account_id:accounts.cash,counter_account_id:kind==='receipt'?accounts.equity:accounts.expense,
    party_name:'طرف مستند اختبار الجرد',department_id:accounts.department};
  const draft=await api(page,'treasury-documents','POST',payload);
  return api(page,`treasury-documents/${draft.id}/post`,'POST',{revision:draft.revision});
}
async function close(page){await page.locator(A('close')).first().click();await page.waitForFunction(()=>!document.querySelector('#cash-count-window').open);}
async function printed(page,words){
  const before=await page.evaluate(()=>window.__countPrints);await page.locator(A('print')).click();
  await page.waitForFunction(n=>window.__countPrints>n,before);
  const text=await page.locator('#print-document').textContent();
  for(const word of words)assert.ok(text.includes(word),'Printed count contains '+word);
  assert.match(text,/توقيع|راجعه|القائم بالجرد|المراجع/);
  await page.emulateMedia({media:'print'});assert.equal(await page.locator('dialog:visible').count(),0);assert.ok(await page.locator('#print-document').isVisible());await page.emulateMedia({media:'screen'});
  return text;
}
async function financialState(page,cash){
  return{trial:await api(page,'trial-balance?from_date=2026-01-01&to_date=2026-02-28&include_zero=1'),
    book:await api(page,`cash-book?account_id=${cash}&from_date=2026-01-01&to_date=2026-02-28`)};
}

(async()=>{
  const browser=await chromium.launch({executablePath:process.env.AM_CHROMIUM||'/usr/bin/chromium',args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:1080}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));await page.addInitScript(()=>{window.__countPrints=0;window.print=()=>window.__countPrints++;});
  try{
    await page.goto(base);await page.waitForSelector('#loading[hidden]',{state:'attached'});
    const boot=await api(page,'bootstrap'),config=await api(page,'treasury-config');
    const main=config.cash_accounts.find(a=>a.code==='111002');
    const accounts={cash:main.id,equity:config.counter_accounts_by_kind.receipt.find(a=>a.code.startsWith('3')).id,
      expense:config.counter_accounts_by_kind.payment.find(a=>a.code.startsWith('6')).id,department:boot.departments[0].id};
    await treasury(page,'receipt',1000,accounts,'2026-01-01');await treasury(page,'payment',100,accounts,'2026-01-02');
    assert.equal((await financialState(page,main.id)).book.closing_balance_cents,90000);
    const initial=await financialState(page,main.id);
    const countConfig=await api(page,'cash-count-config');assert.ok(countConfig.denominations_cents.includes(20000));assert.ok(countConfig.cash_accounts.some(a=>a.id===main.id));
    assert.equal((await api(page,'cash-counts')).length,0,'Fresh database has no sample counts');
    await page.evaluate(()=>Treasury.open());await page.waitForSelector('#treasury-window [data-treasury-action="cash-counts"]');
    await page.locator('#treasury-window [data-treasury-action="cash-counts"]').click();await page.waitForSelector(A('new'));
    assert.equal(await page.locator('#treasury-window').evaluate(e=>e.open),false,'Cash count entry closes the treasury workspace cleanly');await close(page);
    await page.evaluate(id=>CashCounts.newDocument({cash_account_id:id,business_date:'2026-01-31'}),main.id);await page.waitForSelector(Q(20000));
    await page.locator(A('cash-account')).click();await page.locator(`#picker-window [data-pick="${main.id}"]`).click();
    await page.locator(F('business_date')).fill('2026-01-31');await page.locator(F('reference')).fill(key('COUNT-REF'));
    await page.locator(F('counted_by')).fill('أمين الخزينة في اختبار الجرد');await page.locator(F('reviewed_by')).fill('مراجع الجرد في الاختبار');
    await page.locator(F('notes')).fill('عد فعلي اختباري؛ فرق للمراجعة دون قيد مالي');
    for(const[denomination,quantity]of [[20000,4],[5000,1],[2000,1],[1000,1]])await page.locator(Q(denomination)).fill(String(quantity));
    assert.ok((await page.locator(`${W} [data-cash-count-actual]`).textContent()).includes(money(88000)),'Quantity changes recompute actual cash');
    await page.locator(F('custom_denomination')).fill('2.00');await page.locator(A('add-denomination')).click();await page.locator(Q(200)).fill('2');
    assert.ok((await page.locator(`${W} [data-cash-count-actual]`).textContent()).includes(money(88400)));
    await page.locator(`${A('remove-denomination')}[data-denomination-cents="200"]`).click();assert.equal(await page.locator(Q(200)).count(),0);
    let preview=await clickResponse(page,A('preview'),'cash-count-preview');
    assert.equal(preview.actual_cents,88000);assert.equal(preview.book_cents,90000);assert.equal(preview.variance_cents,-2000);
    assert.ok(preview.warnings.length,'Opening-balance qualification is explicit');
    assert.equal(await page.locator('select').count(),0);assert.equal(await page.locator('aside:visible').count(),0);
    const saved=await clickResponse(page,A('save'),'cash-counts');assert.equal(saved.status,'draft');
    assert.deepEqual(await financialState(page,main.id),initial,'Saving/previewing a count creates no cash movement or journal');
    await treasury(page,'receipt',10,accounts);await treasury(page,'payment',10,accounts);
    assert.equal((await financialState(page,main.id)).book.closing_balance_cents,90000,'Balanced later entries leave net cash unchanged');
    await clickResponse(page,A('confirm'),`cash-counts/${saved.id}/confirm`,'POST',409);
    assert.match(await page.locator(W).textContent(),/حفظ|تغير|تغيّر|أعد/,'Even net-zero book changes require a fresh saved snapshot');
    const resaved=await clickResponse(page,A('save'),`cash-counts/${saved.id}`,'PUT');
    const beforeConfirm=await financialState(page,main.id);
    const confirmed=await clickResponse(page,A('confirm'),`cash-counts/${saved.id}/confirm`);
    assert.equal(confirmed.status,'confirmed');assert.equal(confirmed.book_cents,90000);assert.equal(confirmed.actual_cents,88000);assert.equal(confirmed.variance_cents,-2000);
    assert.ok(confirmed.confirmed_at);assert.equal(confirmed.revision>resaved.revision,true);
    assert.deepEqual(await financialState(page,main.id),beforeConfirm,'Confirming a physical count is nonposting');
    assert.equal(await page.locator(A('confirm')).count(),0,'Confirmed count has no repeat approval action');
    const snapshotPrint=await printed(page,[confirmed.code,'نيو بانوراما',money(90000),money(88000),'2026-01-31']);
    assert.match(snapshotPrint,/افتتاح|مراجعة|قيد/);
    await page.locator(`${W} .cc-scroll`).evaluate(e=>{e.scrollTop=0;});
    await page.locator(W).screenshot({path:path.join(__dirname,'cash-count-certificate.png')});
    await page.locator('#cash-count-preview').scrollIntoViewIfNeeded();
    await page.locator(W).screenshot({path:path.join(__dirname,'cash-count-comparison.png')});await close(page);

    await treasury(page,'receipt',30,accounts,'2026-01-07');
    const reloaded=await api(page,'cash-counts/'+confirmed.id);
    assert.equal(reloaded.book_cents,90000);assert.equal(reloaded.variance_cents,-2000);
    assert.equal(reloaded.live_book_cents,93000);assert.equal(reloaded.live_variance_cents,-5000);assert.equal(reloaded.book_changed,true);
    const beforeRead=await api(page,'audit'),beforeVoid=await financialState(page,main.id);
    await page.evaluate(id=>CashCounts.openDocument(id),confirmed.id);await page.waitForSelector(A('void'));
    assert.ok((await page.locator(`${W} [data-cash-count-book]`).textContent()).includes(money(90000)),'The main saved comparison keeps its original book value');
    assert.match(await page.locator(W).textContent(),/تغير|تغيّر|أحدث|الحالي|لاحق/,'Later book activity is labelled separately from frozen evidence');
    const laterPrint=await printed(page,[confirmed.code,money(90000),money(88000)]);
    assert.ok(laterPrint.includes('2026-01-31'),'Original count date is preserved in print');
    assert.deepEqual(await api(page,'audit'),beforeRead,'Reading and printing recorded evidence does not append events');
    await page.locator(A('void')).click();await page.waitForSelector(F('reason'));
    await page.locator(F('business_date')).fill('2026-02-01');await page.locator(F('reference')).fill(key('VOID'));
    await page.locator(F('reason')).fill('إلغاء محضر مكرر بعد المراجعة دون تعديل النقدية');
    const voided=await clickResponse(page,A('confirm-void'),`cash-counts/${confirmed.id}/void`);
    assert.equal(voided.status,'confirmed');assert.equal(voided.effective_status,'voided');assert.ok(voided.void);
    assert.equal(voided.book_cents,90000);assert.equal(voided.actual_cents,88000);assert.equal(voided.variance_cents,-2000);
    assert.deepEqual(await financialState(page,main.id),beforeVoid,'Voiding the certificate is not a cash adjustment');
    await printed(page,[confirmed.code,voided.void.code,'إلغاء محضر مكرر']);await close(page);

    // Dirty cancellation remains under the operator's control.
    await page.evaluate(()=>CashCounts.newDocument());await page.waitForSelector(Q(20000));await page.locator(F('reference')).fill(key('DIRTY'));
    page.once('dialog',dialog=>dialog.dismiss());await page.keyboard.press('Escape');assert.equal(await page.locator(W).evaluate(e=>e.open),true);
    page.once('dialog',dialog=>dialog.accept());await page.keyboard.press('Escape');assert.equal(await page.locator(W).evaluate(e=>e.open),false);
    await page.evaluate(()=>CashCounts.open());await page.waitForSelector(A('new'));await page.locator(F('search')).fill(confirmed.code);
    assert.ok((await page.locator(W).textContent()).includes(confirmed.code));
    await page.setViewportSize({width:392,height:850});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.equal(await page.locator(W).evaluate(e=>e.scrollWidth>e.clientWidth),false,'Count list fits the phone viewport');
    await page.evaluate(id=>CashCounts.openDocument(id),confirmed.id);await page.waitForSelector(A('print'));
    assert.equal(await page.locator(W).evaluate(e=>e.scrollWidth>e.clientWidth),false,'Count certificate and denomination table fit the phone viewport');await close(page);
    let delayed=false;await page.route('**/api/cash-counts',async route=>{if(route.request().method()==='GET'){delayed=true;await new Promise(resolve=>setTimeout(resolve,250));}await route.continue();});
    await page.evaluate(()=>{CashCounts.open();});await page.waitForSelector(`${W}[open]`);await page.keyboard.press('Escape');await page.waitForTimeout(500);
    assert.ok(delayed);assert.equal(await page.locator(W).evaluate(e=>e.open),false,'Late report responses cannot reopen a closed count window');
    await page.unroute('**/api/cash-counts');assert.deepEqual(errors,[],errors.join('\n'));
    console.log('PASS cash count UI: exact denomination counts, frozen nonposting evidence, net-zero stale-book guard, later book comparison, certificate print/void, dirty guards, mobile and late reads.');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
