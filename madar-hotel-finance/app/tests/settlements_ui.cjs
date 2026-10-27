/**
 * Supplier settlement acceptance checks against a disposable MADAR_DB only.
 * AM_TEST_URL defaults to http://127.0.0.1:8101; requires Playwright + Chromium.
 * Old AP documents are fixtures. New settlement, refund, reversal and cheque
 * event mutations run through their real UI, including printing and reports.
 */
'use strict';
const assert = require('node:assert/strict');
const path = require('node:path');
const { chromium } = require('playwright');
const url = process.env.AM_TEST_URL || 'http://127.0.0.1:8101';
const SS = '#supplier-settlement-window';
const action = name => `${SS} [data-ss-action="${name}"]`;
const field = name => `${SS} [data-ss-field="${name}"]`;
const stamp = Date.now().toString(36);
let serial = 0;
const key = label => `settlement-ui-${stamp}-${label}-${++serial}`;
const fmt = cents => new Intl.NumberFormat('ar-EG',{minimumFractionDigits:2,maximumFractionDigits:2}).format(cents/100);

async function request(page, route, method='GET', data, expected=200) {
  const result = await page.evaluate(async ({route,method,data}) => {
    const boot = await (await fetch('/api/bootstrap')).json();
    const response = await fetch('/api/'+route,{method,headers:{'Content-Type':'application/json','X-Madar-CSRF':boot.csrf},body:data===undefined?undefined:JSON.stringify(data)});
    return {status:response.status,body:await response.json()};
  },{route,method,data});
  assert.equal(result.status,expected,`${method} ${route}: ${result.status} ${result.body.error||''}`);
  return result.body;
}
async function clickResponse(page, selector, route, method='POST', expected=200) {
  const pending = page.waitForResponse(r=>new URL(r.url()).pathname==='/api/'+route&&r.request().method()===method);
  await page.locator(selector).click();
  const response = await pending, body = await response.json();
  assert.equal(response.status(),expected,`${method} ${route}: ${response.status()} ${body.error||''}`);
  return body;
}
async function closeWindow(page) {
  if (!await page.locator(SS).evaluate(e=>e.open)) return;
  await page.locator(action('close')).last().click();
  await page.waitForFunction(()=>!document.querySelector('#supplier-settlement-window').open);
}
async function openWindow(page, method, ...args) {
  await page.evaluate(({method,args})=>SupplierSettlement[method](...args),{method,args});
  await page.waitForSelector(`${SS}[open]`);
}
async function printDocument(page, selector, required) {
  const before = await page.evaluate(()=>window.__settlementPrints||0);
  await page.locator(selector).click();
  await page.waitForFunction(n=>(window.__settlementPrints||0)>n,before);
  const document = await page.locator('#print-document').textContent();
  for(const text of required) assert.ok(document.includes(text),'Printed document includes '+text);
  assert.match(document,/توقيع|اعتمده|المستلم|استلم|راجعه|اعتماد/,'Printable signatures');
  await page.emulateMedia({media:'print'});
  assert.equal(await page.locator('dialog:visible').count(),0,'Printing hides stacked input and supplier dialogs');
  assert.ok(await page.locator('#print-document').isVisible(),'Saved print document is visible in print media');
  await page.emulateMedia({media:'screen'});
}
async function invoice(page,fixture,amount,date='2026-01-02') {
  const draft=await request(page,'invoices','POST',{request_key:key('invoice'),supplier_id:fixture.supplier.id,department_id:fixture.department,reference:key('INV'),business_date:date,due_date:date,kind:'service',tax_rate:'0',lines:[{product_id:fixture.product.id,quantity:'1',price:String(amount)}]});
  return request(page,`invoices/${draft.id}/post`,'POST',{revision:draft.revision});
}
async function payment(page,fixture,amount,date,options={}) {
  const data={request_key:key('payment'),supplier_id:fixture.supplier.id,payment_date:date,amount:String(amount),method:'cash',funding_account_id:fixture.cash,allocation_mode:'manual',allocations:[],allow_advance:true,details:{received_by:'مندوب الاختبار'},...options};
  const draft=await request(page,'supplier-payments','POST',data);
  return request(page,`supplier-payments/${draft.id}/post`,'POST',{revision:draft.revision});
}
async function returned(page,invoice,quantity,date) {
  const draft=await request(page,'supplier-returns','POST',{request_key:key('return'),invoice_id:invoice.id,business_date:date,reference:key('RET'),lines:[{invoice_line_id:invoice.lines[0].id,quantity:String(quantity)}]});
  return request(page,`supplier-returns/${draft.id}/post`,'POST',{revision:draft.revision});
}
async function balances(page,supplierId,date='2026-12-31') {
  return (await request(page,`suppliers/${supplierId}/statement?as_of=${date}`)).totals;
}
async function outstanding(page,supplierId,invoiceId,date='2026-12-31') {
  const rows=await request(page,`suppliers/${supplierId}/open-invoices?as_of=${date}`);
  return rows.find(r=>r.id===invoiceId||r.invoice_id===invoiceId);
}
async function fixture(page,setup,label) {
  const supplier=await request(page,'suppliers','POST',{name:`مورد اختبار ${label} ${stamp}`,terms_days:0,category_ids:[setup.category]});
  const product=await request(page,'products','POST',{supplier_id:supplier.id,department_id:setup.department,name:'خدمة '+label,unit:'وحدة',price:'100',category_id:setup.category,expense_account_id:setup.expense});
  return {...setup,supplier,product};
}
async function chooseSource(page,type,id) {
  await page.locator(`${action('source')}[data-source-type="${type}"][data-source-id="${id}"]`).click();
}
async function fillDocument(page,date,reference,reason) {
  await page.locator(field('business_date')).fill(date);
  if(await page.locator(field('reference')).count())await page.locator(field('reference')).fill(reference);
  if(reason&&await page.locator(field('reason')).count())await page.locator(field('reason')).fill(reason);
}
async function chequeEvent(page,paymentId,event,date,reference) {
  await openWindow(page,'chequeEvents',paymentId);
  await page.locator(`${action('choose-cheque-action')}[data-event-action="${event}"]`).click();
  await fillDocument(page,date,reference,'سبب اختباري موثق للحدث '+event);
  const result=await clickResponse(page,action('confirm-cheque-event'),`supplier-payments/${paymentId}/cheque-event`);
  await closeWindow(page);
  return result;
}

(async()=>{
  const browser=await chromium.launch({executablePath:process.env.AM_CHROMIUM||'/usr/bin/chromium',args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:1080}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{window.__settlementPrints=0;window.print=()=>{window.__settlementPrints++}});
  try{
    await page.goto(url);await page.waitForSelector('#loading[hidden]',{state:'attached'});
    const boot=await request(page,'bootstrap'),chart=await request(page,'accounting');
    const setup={department:boot.departments[0].id,category:boot.categories[0].id,
      expense:chart.accounts.find(a=>a.active&&a.posting&&a.normal_side==='debit'&&/^[568]/.test(a.code)).id,
      cash:chart.accounts.find(a=>a.active&&a.posting&&a.code==='111002').id,
      bank:chart.accounts.find(a=>a.active&&a.posting&&a.code==='112001').id};
    for(const m of chart.mappings.filter(m=>m.module==='الموردون'&&['payable_control','supplier_advances','issued_cheques'].includes(m.role)))
      await request(page,'account-mappings/'+m.id,'PUT',{revision:m.revision,status:'confirmed',account_id:m.account_id});

    // Advance: source remains a separate asset until an explicit posted settlement.
    const adv=await fixture(page,setup,'دفعة مقدمة');
    const advance=await payment(page,adv,100,'2026-01-01');
    const target=await invoice(page,adv,200);
    await page.evaluate(()=>refresh());
    await openWindow(page,'newSettlement',adv.supplier.id);
    await chooseSource(page,'advance',advance.id);
    await fillDocument(page,'2026-01-03',key('SET'));
    await page.locator(`[data-ss-allocation="${target.id}"]`).fill('60');
    const preview=await clickResponse(page,action('preview-settlement'),'settlement-preview');
    const draft=await clickResponse(page,action('save-settlement'),'supplier-settlements');
    assert.equal(draft.status,'draft');
    assert.equal((await balances(page,adv.supplier.id)).advance_cents,10000,'Draft settlement does not consume advance');
    const settled=await clickResponse(page,action('post-settlement'),`supplier-settlements/${draft.id}/post`);
    assert.equal(settled.status,'posted');
    let b=await balances(page,adv.supplier.id);
    assert.equal(b.payable_balance_cents,14000);assert.equal(b.advance_cents,4000);assert.equal(b.net_payable_cents,10000);
    assert.equal((await outstanding(page,adv.supplier.id,target.id)).outstanding_cents,14000);
    assert.equal((await balances(page,adv.supplier.id,'2026-01-02')).advance_cents,10000,'Historical source availability stays intact');
    await printDocument(page,action('print-settlement'),[settled.code,adv.supplier.name,target.code]);
    await closeWindow(page);

    // A consumed payment cannot be reversed by bypassing its source dependencies.
    await openWindow(page,'reversePayment',advance.id);
    await fillDocument(page,'2026-01-04',key('BLOCKED'),'اختبار منع عكس دفعة مستخدمة');
    await clickResponse(page,action('confirm-payment-reversal'),`supplier-payments/${advance.id}/reverse`,'POST',400);
    assert.ok((await page.locator(`${SS} #ss-error`).textContent()).trim(),'Dependency error is visible');
    page.once('dialog',d=>d.accept());await closeWindow(page);

    // The remaining advance is refunded in cash, with a receipt rather than a disbursement.
    await openWindow(page,'refund',adv.supplier.id);await chooseSource(page,'advance',advance.id);
    await fillDocument(page,'2026-01-04',key('REFUND'),'استرداد الجزء المتبقي من الدفعة المقدمة');
    await page.locator(field('amount')).fill('40');
    await page.locator(`${action('refund-method')}[data-method="cash"]`).click();
    await page.locator(`${action('funding')}[data-account-id="${setup.cash}"]`).click();
    await page.locator(field('details.received_by')).fill('أمين خزينة الاختبار');
    const refundDraft=await clickResponse(page,action('save-refund'),'supplier-refunds');
    assert.equal((await balances(page,adv.supplier.id)).advance_cents,4000,'Draft refund has no ledger effect');
    const refund=await clickResponse(page,action('post-refund'),`supplier-refunds/${refundDraft.id}/post`);
    b=await balances(page,adv.supplier.id);assert.equal(b.advance_cents,0);assert.equal(b.net_payable_cents,14000);
    await printDocument(page,action('print-refund'),[refund.code,adv.supplier.name,'قبض']);
    await closeWindow(page);

    // A settlement correction restores the source and target using a linked reversal.
    await openWindow(page,'openSettlement',settled.id);
    await page.locator(action('reverse-settlement')).click();
    await fillDocument(page,'2026-01-05',key('SET-REV'),'إعادة توزيع الدفعة على فاتورة أخرى لاحقًا');
    const reversedSettlement=await clickResponse(page,action('confirm-reverse-settlement'),`supplier-settlements/${settled.id}/reverse`);
    assert.equal(reversedSettlement.status,'reversed');
    b=await balances(page,adv.supplier.id);assert.equal(b.advance_cents,6000);assert.equal(b.payable_balance_cents,20000);assert.equal(b.net_payable_cents,14000);
    assert.equal((await balances(page,adv.supplier.id,'2026-01-04')).advance_cents,0,'Settlement reversal does not rewrite prior reports');
    await printDocument(page,action('print-settlement'),[settled.code,'عكس','2026-01-05']);
    await closeWindow(page);

    // A paid invoice return creates a reusable credit. Split it across two invoices.
    const credit=await fixture(page,setup,'رصيد مرتجع');
    const original=await invoice(page,credit,100,'2026-02-01');
    await payment(page,credit,100,'2026-02-02',{allocation_mode:'invoice',target_invoice_id:original.id,allow_advance:false});
    await returned(page,original,'.6','2026-02-03');
    const left=await invoice(page,credit,70,'2026-02-04'),right=await invoice(page,credit,80,'2026-02-04');
    await page.evaluate(()=>refresh());
    await openWindow(page,'newSettlement',credit.supplier.id);await chooseSource(page,'return_credit',original.id);
    await fillDocument(page,'2026-02-05',key('CREDIT'));
    await page.locator(`[data-ss-allocation="${left.id}"]`).fill('20');await page.locator(`[data-ss-allocation="${right.id}"]`).fill('20');
    const creditDraft=await clickResponse(page,action('save-settlement'),'supplier-settlements');
    const creditSettled=await clickResponse(page,action('post-settlement'),`supplier-settlements/${creditDraft.id}/post`);
    b=await balances(page,credit.supplier.id);assert.equal(b.payable_balance_cents,9000);assert.equal(b.unapplied_return_credit_cents,2000);
    assert.equal((await outstanding(page,credit.supplier.id,left.id)).outstanding_cents,5000);
    assert.equal((await outstanding(page,credit.supplier.id,right.id)).outstanding_cents,6000);
    await printDocument(page,action('print-settlement'),[creditSettled.code,original.code,left.code,right.code]);await closeWindow(page);

    // Cash received by bank removes the remaining credit, preserving invoice balances.
    await openWindow(page,'refund',credit.supplier.id);await chooseSource(page,'return_credit',original.id);
    await fillDocument(page,'2026-02-06',key('BANKREF'),'استرداد رصيد المرتجع بتحويل بنكي');
    await page.locator(field('amount')).fill('20');await page.locator(`${action('refund-method')}[data-method="bank_transfer"]`).click();
    await page.locator(`${action('funding')}[data-account-id="${setup.bank}"]`).click();
    await page.locator(field('details.bank_name')).fill('بنك اختبار');
    await page.locator(field('details.transaction_reference')).fill(key('BANK-REFERENCE'));
    await page.locator(field('details.value_date')).fill('2026-02-06');
    const bankRefundDraft=await clickResponse(page,action('save-refund'),'supplier-refunds');
    const bankRefund=await clickResponse(page,action('post-refund'),`supplier-refunds/${bankRefundDraft.id}/post`);
    b=await balances(page,credit.supplier.id);assert.equal(b.unapplied_return_credit_cents,0);assert.equal(b.net_payable_cents,11000);
    await printDocument(page,action('print-refund'),[bankRefund.code,credit.supplier.name]);await closeWindow(page);

    // Correcting an unused approved cash payment restores the original invoice amount.
    const rev=await fixture(page,setup,'عكس سداد');const revInvoice=await invoice(page,rev,100,'2026-03-01');
    const paid=await payment(page,rev,30,'2026-03-02',{allocation_mode:'invoice',target_invoice_id:revInvoice.id,allow_advance:false});
    await page.evaluate(()=>refresh());await openWindow(page,'reversePayment',paid.id);
    const reversalReference=key('REV');await fillDocument(page,'2026-03-03',reversalReference,'تصحيح مستند سداد سجل بالخطأ');
    const reversedPayment=await clickResponse(page,action('confirm-payment-reversal'),`supplier-payments/${paid.id}/reverse`);
    assert.equal(reversedPayment.reversal.reference,reversalReference,'Reversal reference is persisted');
    await printDocument(page,action('print-adjustment'),[paid.code,reversalReference,'تصحيح مستند سداد سجل بالخطأ']);
    assert.equal((await balances(page,rev.supplier.id)).payable_balance_cents,10000);
    assert.equal((await balances(page,rev.supplier.id,'2026-03-02')).payable_balance_cents,7000);
    await closeWindow(page);

    // Operational stop/resume has no money movement; cancel/bounce append reversals.
    const ch=await fixture(page,setup,'حالات شيك');const chInvoice=await invoice(page,ch,100,'2026-04-01');
    const chequeOptions={method:'cheque',funding_account_id:setup.bank,allocation_mode:'invoice',target_invoice_id:chInvoice.id,allow_advance:false,
      details:{bank_name:'بنك اختبار',cheque_number:key('CH'),cheque_book:'دفتر اختبار',beneficiary:ch.supplier.name,cheque_date:'2026-04-02',due_date:'2026-04-02'}};
    const cheque=await payment(page,ch,40,'2026-04-02',chequeOptions);
    await page.evaluate(()=>refresh());await chequeEvent(page,cheque.id,'stop','2026-04-03',key('STOP'));
    await request(page,`supplier-payments/${cheque.id}/clear`,'POST',{revision:cheque.revision,clearance_date:'2026-04-04',bank_account_id:setup.bank,reference:key('BAD-CLEAR')},400);
    assert.equal((await balances(page,ch.supplier.id)).payable_balance_cents,6000);
    await chequeEvent(page,cheque.id,'resume','2026-04-04',key('RESUME'));
    await chequeEvent(page,cheque.id,'cancel','2026-04-05',key('CANCEL'));
    assert.equal((await balances(page,ch.supplier.id)).payable_balance_cents,10000);
    const cheque2=await payment(page,ch,50,'2026-04-06',{...chequeOptions,details:{...chequeOptions.details,cheque_number:key('CH2'),cheque_date:'2026-04-06',due_date:'2026-04-06'}});
    await request(page,`supplier-payments/${cheque2.id}/clear`,'POST',{revision:cheque2.revision,clearance_date:'2026-04-07',bank_account_id:setup.bank,reference:key('CLEAR')});
    await chequeEvent(page,cheque2.id,'bounce','2026-04-08',key('BOUNCE'));
    assert.equal((await balances(page,ch.supplier.id)).payable_balance_cents,10000);
    assert.equal((await balances(page,ch.supplier.id,'2026-04-07')).payable_balance_cents,5000);
    await openWindow(page,'chequeEvents',cheque2.id);
    const chequeHistory=await page.locator(`${SS} #ss-body`).textContent();
    assert.ok(chequeHistory.includes('عكس السداد')&&chequeHistory.includes('عكس صرف البنك'),'Bounce history shows both financial reversal journals');
    await printDocument(page,action('print-cheque-history'),[cheque2.code,cheque2.details.cheque_number,'مرتد']);await closeWindow(page);

    // Reconciliation must use the exact dated report rather than display unconditional success.
    await openWindow(page,'reconciliation','2026-12-31');
    assert.ok(await page.locator(`${SS} #ss-reconciliation-table`).isVisible());
    assert.ok(await page.locator(`${SS} .ss-account-reconciliation`).isVisible(),'Historical account-level reconciliation is visible');
    assert.ok(await page.locator(`${SS} .ss-result.matched`).isVisible(),'Actual reconciliation reports a match');
    assert.ok((await page.locator(`${SS} #ss-body`).textContent()).includes(adv.supplier.name));
    await printDocument(page,action('print-reconciliation'),['2026-12-31',adv.supplier.name]);
    await page.locator(`${SS} [data-ss-report-date]`).fill('2026-01-02');
    const historical=await clickResponse(page,action('reload-reconciliation'),'supplier-reconciliation','GET');
    const historicalSupplier=historical.suppliers.find(s=>s.supplier_id===adv.supplier.id);
    assert.equal(historicalSupplier.ledger_payable_cents,20000);assert.equal(historicalSupplier.ledger_advance_cents,10000);assert.equal(historical.matched,true);
    await closeWindow(page);

    // A difference returned by the report must remain visible; the UI never forces success.
    await page.route('**/api/supplier-reconciliation?as_of=2026-12-30',async route=>{
      const response=await route.fetch(),body=await response.json();
      body.matched=false;body.totals.payable_difference_cents=100;
      if(body.accounts.length){body.accounts[0].difference_cents=100;body.accounts[0].matched=false;}
      body.exceptions=[{type:'test_discrepancy',message:'فرق اختباري للمراجعة دون قيد تلقائي',journal_id:target.journal_id,journal_code:target.journal_code}];
      await route.fulfill({response,json:body});
    });
    await openWindow(page,'reconciliation','2026-12-30');
    assert.ok(await page.locator(`${SS} .ss-result.unmatched`).isVisible(),'A discrepant report cannot show a match');
    assert.ok((await page.locator(`${SS} #ss-body`).textContent()).includes('فرق اختباري للمراجعة دون قيد تلقائي'));
    assert.ok((await page.locator(`${SS} .ss-account-reconciliation tbody tr`).first().innerText()).includes(fmt(100)),'Account discrepancy is visible');
    await clickResponse(page,`${action('journal')}[data-id="${target.journal_id}"]`,`journals/${target.journal_id}`,'GET');
    assert.ok((await page.locator(`${SS} #ss-title`).textContent()).includes('القيد'));
    await page.locator(action('back-reconciliation')).click();assert.ok(await page.locator(`${SS} .ss-result.unmatched`).isVisible(),'Returning from a journal keeps the discrepant report');
    await closeWindow(page);await page.unroute('**/api/supplier-reconciliation?as_of=2026-12-30');

    // Dirty drafts are kept on decline, discarded on consent, and fit a small screen.
    const mobile=await fixture(page,setup,'نافذة هاتف');const mobileAdvance=await payment(page,mobile,10,'2026-05-01');await invoice(page,mobile,20,'2026-05-02');await page.evaluate(()=>refresh());
    await page.setViewportSize({width:390,height:844});await openWindow(page,'newSettlement',mobile.supplier.id);await chooseSource(page,'advance',mobileAdvance.id);
    await page.locator(field('reference')).fill(key('DIRTY'));
    assert.equal(await page.locator(SS).evaluate(e=>e.scrollWidth>e.clientWidth),false,'No dialog mobile overflow');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'No page mobile overflow');
    page.once('dialog',d=>d.dismiss());await page.keyboard.press('Escape');assert.ok(await page.locator(SS).evaluate(e=>e.open));
    page.once('dialog',d=>d.accept());await page.keyboard.press('Escape');await page.waitForFunction(()=>!document.querySelector('#supplier-settlement-window').open);
    assert.equal(await page.locator('select').count(),0,'No dropdown selects');assert.equal(await page.locator('aside:visible').count(),0,'No sidebar');
    assert.deepEqual(errors,[],'No JavaScript errors');
    console.log('PASS settlement UI: advance and multi-invoice credit application, cash/bank refunds, dependency protection, payment reversal, cheque stop/resume/cancel/bounce, dated reconciliation/prints, dirty Escape and mobile.');
  }catch(error){await page.screenshot({path:path.join(__dirname,'settlements-failure.png'),fullPage:true}).catch(()=>{});if(errors.length)console.error('PAGE_ERRORS',errors);throw error}
  finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
