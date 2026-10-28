/** Refund corrections through the real UI; use only a disposable MADAR_DB. */
'use strict';
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const base = process.env.AM_TEST_URL || 'http://127.0.0.1:8111';
const W = '#supplier-settlement-window';
const A = name => `${W} [data-ss-action="${name}"]`;
const F = name => `${W} [data-ss-field="${name}"]`;
let serial = 0;
const stamp = Date.now().toString(36);
const key = label => `refund-reverse-ui-${stamp}-${label}-${++serial}`;
const money = cents => new Intl.NumberFormat('ar-EG', {minimumFractionDigits:2, maximumFractionDigits:2}).format(cents / 100);
async function api(page, route, method='GET', data, expected=200) {
  const result = await page.evaluate(async ({route,method,data}) => {
    const boot = await (await fetch('/api/bootstrap')).json();
    const response = await fetch('/api/'+route,{method,headers:{'Content-Type':'application/json','X-Madar-CSRF':boot.csrf},body:data===undefined?undefined:JSON.stringify(data)});
    return {status:response.status,body:await response.json()};
  }, {route,method,data});
  assert.equal(result.status,expected,`${method} ${route}: ${JSON.stringify(result.body)}`);
  return result.body;
}
async function clickResponse(page, selector, route, method='POST') {
  const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/'+route&&r.request().method()===method);
  await page.locator(selector).click();
  const response=await pending, body=await response.json();
  assert.equal(response.status(),200,`${method} ${route}: ${JSON.stringify(body)}`);
  return body;
}
async function printDocument(page, selector, required) {
  const before=await page.evaluate(()=>window.__refundPrints);
  await page.locator(selector).click();
  await page.waitForFunction(n=>window.__refundPrints>n,before);
  const doc=await page.locator('#print-document').textContent();
  for(const text of required)assert.ok(doc.includes(text),'Print contains '+text);
  assert.match(doc,/اعتمده|راجعه|توقيع/);
  await page.emulateMedia({media:'print'});
  assert.equal(await page.locator('dialog:visible').count(),0);
  assert.ok(await page.locator('#print-document').isVisible());
  await page.emulateMedia({media:'screen'});
}
async function close(page) {await page.locator(A('close')).last().click();await page.waitForFunction(()=>!document.querySelector('#supplier-settlement-window').open);}
async function fixture(page, chart, boot, suffix) {
  const supplier=await api(page,'suppliers','POST',{name:'مورد تصحيح الاسترداد '+stamp+' '+suffix,category_ids:[boot.categories[0].id]});
  const expense=chart.accounts.find(a=>a.active&&a.posting&&a.normal_side==='debit'&&a.code.startsWith('6'));
  const product=await api(page,'products','POST',{supplier_id:supplier.id,department_id:boot.departments[0].id,name:'خدمة تصحيح '+suffix,unit:'خدمة',price:'100',category_id:boot.categories[0].id,expense_account_id:expense.id});
  return {supplier, product, department:boot.departments[0].id,cash:chart.accounts.find(a=>a.code==='111002').id,bank:chart.accounts.find(a=>a.code==='112001').id};
}
async function payment(page, f, amount, date, options={}) {
  const d=await api(page,'supplier-payments','POST',{request_key:key('payment'),supplier_id:f.supplier.id,payment_date:date,method:'cash',funding_account_id:f.cash,amount:String(amount),allocation_mode:'manual',allocations:[],allow_advance:true,details:{received_by:'مندوب الاختبار'},...options});
  return api(page,`supplier-payments/${d.id}/post`,'POST',{revision:d.revision});
}
async function refund(page, f, type, sourceId, amount, date, method='cash') {
  const d=await api(page,'supplier-refunds','POST',{request_key:key('refund'),supplier_id:f.supplier.id,source_type:type,source_id:sourceId,business_date:date,reference:key('REFUND'),reason:'استرداد مسجل للاختبار',amount:String(amount),method,funding_account_id:method==='cash'?f.cash:f.bank,details:method==='cash'?{received_by:'أمين الخزينة'}:{bank_name:'بنك الاختبار',transaction_reference:key('BANK'),value_date:date}});
  return api(page,`supplier-refunds/${d.id}/post`,'POST',{revision:d.revision});
}
async function beginReverse(page, original) {
  await page.evaluate(id=>SupplierSettlement.openRefund(id), original.id);
  assert.ok((await page.locator('#ss-body').textContent()).includes(original.code));
  await page.locator(A('reverse-refund')).click();
  await page.waitForSelector(F('reason'));
  assert.equal(await page.locator(`${W} .ss-correction-lines tbody tr`).count(),2);
  assert.ok((await page.locator('#ss-body').textContent()).includes('معاينة القيد المقابل'));
}
async function fillReverse(page, date, reason) {
  await page.locator(F('business_date')).fill(date);
  await page.locator(F('reference')).fill(key('CORRECT'));
  await page.locator(F('reason')).fill(reason);
}

(async()=>{
  const browser=await chromium.launch({executablePath:process.env.AM_CHROMIUM||'/usr/bin/chromium',args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:1080}}), errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{window.__refundPrints=0;window.__refundUpdates=0;window.print=()=>window.__refundPrints++;document.addEventListener('supplier-accounting-updated',()=>window.__refundUpdates++)});
  try {
    await page.goto(base);await page.waitForSelector('#loading[hidden]',{state:'attached'});
    const boot=await api(page,'bootstrap'), chart=await api(page,'accounting');
    for(const m of chart.mappings.filter(m=>m.module==='الموردون'&&['payable_control','supplier_advances'].includes(m.role)))await api(page,`account-mappings/${m.id}`,'PUT',{revision:m.revision,status:'confirmed',account_id:m.account_id});
    const f=await fixture(page,chart,boot,'مقدم نقدي');
    const advance=await payment(page,f,100,'2026-08-01');
    const original=await refund(page,f,'advance',advance.id,40,'2026-08-02');
    await page.evaluate(()=>refresh());
    await beginReverse(page,original);

    // Required fields, unsaved Escape protection, and journal drill preserve the form.
    await page.locator(A('confirm-refund-reversal')).click();
    assert.match(await page.locator('#ss-error').textContent(),/التاريخ|المرجع|السبب/);
    const reason='تصحيح إثبات قبض نقدي غير صحيح';
    await fillReverse(page,'2026-08-04',reason);
    page.once('dialog',d=>d.dismiss());await page.keyboard.press('Escape');
    assert.ok(await page.locator(W).evaluate(e=>e.open));
    assert.equal(await page.locator(F('reason')).inputValue(),reason);
    await page.locator(`${W} [data-ss-action="journal"]`).click();
    await page.waitForSelector(A('back-document'));
    assert.ok((await page.locator('#ss-subtitle').textContent()).includes(original.journal.code));
    await page.locator(A('back-document')).click();
    assert.equal(await page.locator(F('reason')).inputValue(),reason);
    assert.equal(await page.locator(A('print-refund-reversal')).count(),0,'Unsaved reversal cannot be printed');

    const reversed=await clickResponse(page,A('confirm-refund-reversal'),`supplier-refunds/${original.id}/reverse`);
    await page.waitForSelector(A('print-refund-reversal'));
    const saved=await api(page,`supplier-refunds/${original.id}`);
    assert.equal(saved.effective_status,'reversed');assert.match(saved.reversal.code,/^SRR-/);
    assert.equal(saved.journal_id,original.journal_id,'Original journal remains linked');
    assert.equal(await page.locator(A('confirm-refund-reversal')).count(),0);
    const oldSource=(await api(page,`supplier-credit-sources?supplier_id=${f.supplier.id}&as_of=2026-08-03`)).sources.find(s=>s.source_type==='advance'&&s.source_id===advance.id);
    const newSource=(await api(page,`supplier-credit-sources?supplier_id=${f.supplier.id}&as_of=2026-08-04`)).sources.find(s=>s.source_type==='advance'&&s.source_id===advance.id);
    assert.equal(oldSource.available_cents,6000);assert.equal(newSource.available_cents,10000);
    assert.equal((await api(page,'supplier-reconciliation?as_of=2026-08-04')).matched,true);
    assert.ok(await page.evaluate(()=>window.__refundUpdates>0),'Underlying supplier reports refresh');
    await printDocument(page,A('print-refund-reversal'),[saved.reversal.code,original.code,f.supplier.name,reason,'2026-08-04',original.journal.code,saved.reversal.journal.code,money(4000)]);
    await page.locator(`${W} [data-ss-action="journal"]`).first().click();
    await page.waitForSelector(A('back-document'));await page.locator(A('back-document')).click();
    await page.locator(A('back-refund')).click();
    assert.equal(await page.locator(A('reverse-refund')).count(),0);
    assert.match(await page.locator(`${W} .ap-meta`).textContent(),/معكوس/);
    await printDocument(page,A('print-refund'),[original.code,'معكوس',saved.reversal.code,'2026-08-04']);
    await close(page);
    await page.evaluate(id=>SupplierSettlement.refunds(id),f.supplier.id);
    assert.match(await page.locator('#ss-body').textContent(),/معكوس/);
    await close(page);

    // A bank receipt from an existing return credit reverses the original bank/AP accounts.
    const b=await fixture(page,chart,boot,'رصيد مرتجع بنكي');
    const draft=await api(page,'invoices','POST',{request_key:key('invoice'),supplier_id:b.supplier.id,department_id:b.department,reference:key('INV'),business_date:'2026-09-01',due_date:'2026-09-01',kind:'service',tax_rate:'0',lines:[{product_id:b.product.id,quantity:'1',price:'100'}]});
    const invoice=await api(page,`invoices/${draft.id}/post`,'POST',{revision:draft.revision});
    await payment(page,b,100,'2026-09-02',{allocation_mode:'invoice',target_invoice_id:invoice.id,allow_advance:false});
    const returnDraft=await api(page,'supplier-returns','POST',{request_key:key('return'),invoice_id:invoice.id,business_date:'2026-09-03',reference:key('RETURN'),lines:[{invoice_line_id:invoice.lines[0].id,quantity:'.6'}]});
    await api(page,`supplier-returns/${returnDraft.id}/post`,'POST',{revision:returnDraft.revision});
    const bankRefund=await refund(page,b,'return_credit',invoice.id,20,'2026-09-04','bank_transfer');
    await page.evaluate(()=>refresh());
    await beginReverse(page,bankRefund);await fillReverse(page,'2026-09-07','تصحيح إشعار تحويل المورد');
    await clickResponse(page,A('confirm-refund-reversal'),`supplier-refunds/${bankRefund.id}/reverse`);
    await page.waitForSelector(A('print-refund-reversal'));
    const bankSaved=await api(page,`supplier-refunds/${bankRefund.id}`);
    assert.equal(bankSaved.reversal.journal.lines.find(l=>l.account_id===b.bank).credit_cents,2000);
    const oldStatement=await api(page,`suppliers/${b.supplier.id}/statement?as_of=2026-09-06`);
    const newStatement=await api(page,`suppliers/${b.supplier.id}/statement?as_of=2026-09-07`);
    assert.equal(oldStatement.totals.unapplied_return_credit_cents,4000);
    assert.equal(newStatement.totals.unapplied_return_credit_cents,6000);
    assert.equal((await api(page,'supplier-reconciliation?as_of=2026-09-07')).matched,true);
    await page.setViewportSize({width:392,height:844});
    assert.ok(await page.locator(A('print-refund-reversal')).isVisible());
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Dialog fits mobile width');
    await printDocument(page,A('print-refund-reversal'),[bankSaved.reversal.code,bankRefund.code,'تصحيح إشعار تحويل المورد',money(2000)]);
    assert.deepEqual(errors,[],'No JavaScript errors');
    console.log('PASS refund reversal UI: cash/advance, bank/return-credit, dated balances, reconciled journal, dirty guard, drill, saved print and mobile');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1});
