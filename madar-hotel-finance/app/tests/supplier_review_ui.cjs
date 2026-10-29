/** Read-only supplier review acceptance. Run against a fresh disposable MADAR_DB only. */
'use strict';
const assert=require('node:assert/strict');
const path=require('node:path');
const {chromium}=require('playwright');
const base=process.env.AM_TEST_URL||'http://127.0.0.1:8136';
const W='#supplier-review-window';
const A=name=>`${W} [data-review-action="${name}"]`;
const F=name=>`${W} [data-review-field="${name}"]`;
const T=name=>`${W} [data-review-tab="${name}"]`;
const stamp=Date.now().toString(36);let serial=0;
const key=label=>`REVIEW-${stamp}-${label}-${++serial}`;
async function api(page,route,method='GET',data,expected=200){
  const result=await page.evaluate(async({route,method,data})=>{
    const boot=await(await fetch('/api/bootstrap')).json();
    const response=await fetch('/api/'+route,{method,headers:{'Content-Type':'application/json','X-Madar-CSRF':boot.csrf},body:data===undefined?undefined:JSON.stringify(data)});
    return{status:response.status,body:await response.json()};
  },{route,method,data});
  assert.equal(result.status,expected,`${method} ${route}: ${JSON.stringify(result.body)}`);return result.body;
}
async function fixture(page,boot,chart,label){
  const supplier=await api(page,'suppliers','POST',{name:`مورد مراجعة ${label} ${stamp}`,terms_days:0});
  const expense=chart.accounts.find(a=>a.active&&a.posting&&a.normal_side==='debit'&&a.code.startsWith('6'));
  const product=await api(page,'products','POST',{supplier_id:supplier.id,department_id:boot.departments[0].id,name:'خدمة '+label,unit:'خدمة',price:'100',expense_account_id:expense.id});
  return{supplier,product,department:boot.departments[0].id,cash:chart.accounts.find(a=>a.code==='111002').id,bank:chart.accounts.find(a=>a.code==='112001').id};
}
async function invoice(page,f,amount,due,date='2026-01-02',post=true){
  const draft=await api(page,'invoices','POST',{request_key:key('invoice'),supplier_id:f.supplier.id,department_id:f.department,reference:key('INV'),business_date:date,due_date:due,kind:'service',tax_rate:'0',lines:[{product_id:f.product.id,quantity:'1',price:String(amount)}]});
  return post?api(page,`invoices/${draft.id}/post`,'POST',{revision:draft.revision}):draft;
}
async function payment(page,f,amount,date,changes={},post=true){
  const draft=await api(page,'supplier-payments','POST',{request_key:key('payment'),supplier_id:f.supplier.id,payment_date:date,amount:String(amount),method:'cash',funding_account_id:f.cash,allocation_mode:'manual',allocations:[],allow_advance:true,details:{received_by:'مندوب الاختبار'},...changes});
  return post?api(page,`supplier-payments/${draft.id}/post`,'POST',{revision:draft.revision}):draft;
}
async function open(page,asOf='2026-01-15'){
  await page.evaluate(date=>SupplierReview.open(date),asOf);
  await page.waitForSelector(`${W}[open]`);await page.waitForSelector(`${A('print')}:not([disabled])`);
}
async function close(page){await page.locator(A('close')).first().click();await page.waitForFunction(()=>!document.querySelector('#supplier-review-window').open);}
async function row(page,kind,id){return page.locator(`${W} [data-review-row][data-review-kind="${kind}"][data-review-id="${id}"]`);}
async function drillDraft(page,kind,record,target){
  await open(page);await page.locator(T('drafts_current')).click();
  const selected=await row(page,kind,record.id);assert.equal(await selected.count(),1,`Current ${kind} draft is present`);
  await selected.locator('[data-review-action="open-row"]').click();
  await page.waitForSelector(`${target}[open]`);
  assert.equal(await page.locator(W).evaluate(e=>e.open),false,'Review closes before opening a current document');
  await page.waitForFunction(({target,code})=>document.querySelector(target).textContent.includes(code),{target,code:record.code});
  await page.keyboard.press('Escape');await page.waitForFunction(selector=>!document.querySelector(selector).open,target);
}
async function printed(page,words){
  const before=await page.evaluate(()=>window.__reviewPrints);await page.locator(A('print')).click();
  await page.waitForFunction(n=>window.__reviewPrints>n,before);
  const text=await page.locator('#print-document').textContent();
  for(const word of words)assert.ok(text.includes(word),'Print includes '+word);
  assert.match(text,/توقيع|راجعه|اعتمده/);
  await page.emulateMedia({media:'print'});assert.equal(await page.locator('dialog:visible').count(),0);assert.ok(await page.locator('#print-document').isVisible());await page.emulateMedia({media:'screen'});
}
async function drillReport(page,action,target,word){
  await open(page);await page.locator(A(action)).click();await page.waitForSelector(`${target}[open]`);
  await page.waitForFunction(({target,word})=>document.querySelector(target).textContent.includes(word),{target,word});
  assert.equal(await page.locator(W).evaluate(e=>e.open),false,'Report opens after closing review');
  await page.keyboard.press('Escape');await page.waitForFunction(selector=>!document.querySelector(selector).open,target);
}
(async()=>{
  const browser=await chromium.launch({executablePath:process.env.AM_CHROMIUM||'/usr/bin/chromium',args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:1080}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(()=>{window.__reviewPrints=0;window.print=()=>window.__reviewPrints++;});
  try{
    await page.goto(base);await page.waitForSelector('#loading[hidden]',{state:'attached'});
    const boot=await api(page,'bootstrap'),chart=await api(page,'accounting');
    for(const mapping of chart.mappings.filter(m=>['payable_control','supplier_advances','issued_cheques','withholding_payable'].includes(m.role)))
      await api(page,`account-mappings/${mapping.id}`,'PUT',{revision:mapping.revision,status:'confirmed',account_id:mapping.account_id});
    const f=await fixture(page,boot,chart,'مستحقات'),adv=await fixture(page,boot,chart,'مقدم'),credit=await fixture(page,boot,chart,'مرتجع');
    const first=await invoice(page,f,1000,'2026-01-10'),today=await invoice(page,f,200,'2026-01-15'),soon=await invoice(page,f,300,'2026-01-20');
    const code=await api(page,'withholding-codes','POST',{name:'TEST REVIEW WHT '+stamp,rate:'3',active:true,confirmed:true});
    const cheque=await payment(page,f,100,'2026-01-05',{method:'cheque',funding_account_id:f.bank,allocation_mode:'invoice',target_invoice_id:first.id,allow_advance:false,withholding_code_id:code.id,details:{bank_name:'بنك اختبار',cheque_number:key('CHQ'),cheque_book:'دفتر اختبار',beneficiary:f.supplier.name,cheque_date:'2026-01-05',due_date:'2026-01-12'}});
    await api(page,`supplier-payments/${cheque.id}/clear`,'POST',{revision:cheque.revision,clearance_date:'2026-01-25',bank_account_id:f.bank,reference:key('CLEAR')});
    const advance=await payment(page,adv,50,'2026-01-03'),target=await invoice(page,adv,100,'2026-01-30');
    const creditInvoice=await invoice(page,credit,100,'2026-01-10');
    await payment(page,credit,100,'2026-01-04',{allocation_mode:'invoice',target_invoice_id:creditInvoice.id,allow_advance:false});
    const rd=await api(page,'supplier-returns','POST',{request_key:key('return'),invoice_id:creditInvoice.id,business_date:'2026-01-06',reference:key('RET'),lines:[{invoice_line_id:creditInvoice.lines[0].id,quantity:'0.2'}]});
    await api(page,`supplier-returns/${rd.id}/post`,'POST',{revision:rd.revision});
    const drafts={invoice:await invoice(page,f,80,'2026-02-01','2026-02-01',false)};
    drafts.return=await api(page,'supplier-returns','POST',{request_key:key('return-draft'),invoice_id:first.id,business_date:'2026-02-01',reference:key('RET-DRAFT'),lines:[{invoice_line_id:first.lines[0].id,quantity:'0.01'}]});
    drafts.payment=await payment(page,f,10,'2026-02-01',{allocation_mode:'invoice',target_invoice_id:first.id,allow_advance:false},false);
    drafts.settlement=await api(page,'supplier-settlements','POST',{request_key:key('settlement-draft'),supplier_id:adv.supplier.id,source_type:'advance',source_id:advance.id,business_date:'2026-02-01',reference:key('SET-DRAFT'),allocations:[{invoice_id:target.id,amount:'10'}]});
    drafts.refund=await api(page,'supplier-refunds','POST',{request_key:key('refund-draft'),supplier_id:adv.supplier.id,source_type:'advance',source_id:advance.id,business_date:'2026-02-01',reference:key('REFUND-DRAFT'),reason:'استرداد تجريبي للمراجعة',amount:'10',method:'cash',funding_account_id:adv.cash,details:{received_by:'أمين الخزينة'}});
    drafts.remittance=await api(page,'withholding-remittances','POST',{request_key:key('remittance-draft'),business_date:'2026-02-01',period_from:'2026-01-01',period_to:'2026-01-05',authority_name:'جهة اختبار فقط',reference:key('REMIT-DRAFT'),method:'cash',funding_account_id:f.cash,details:{received_by:'مندوب الجهة'},allocations:[{payment_id:cheque.id,amount:'1'}]});
    const unmapped=await api(page,'products','POST',{supplier_id:f.supplier.id,department_id:f.department,name:'خدمة بلا حساب للمراجعة '+stamp,unit:'خدمة',price:'1'});
    await page.evaluate(()=>refresh());

    const beforeAudit=await api(page,'audit');
    const report=await api(page,'supplier-review?as_of=2026-01-15&horizon_days=7');
    assert.equal(report.position.open_invoice_cents,150000);assert.equal(report.position.overdue_cents,90000);assert.equal(report.position.due_today_cents,20000);assert.equal(report.position.due_soon_cents,30000);
    assert.equal(report.position.advance_cents,5000);assert.equal(report.position.return_credit_cents,2000);assert.equal(report.position.net_payable_cents,143000);
    assert.equal(report.position.open_cheque_net_cents,9700);assert.equal(report.position.open_cheque_count,1);
    assert.equal(report.queues.cheques[0].id,cheque.id);assert.equal(report.queues.cheques[0].gross_cents,10000);
    assert.ok(report.checks.suppliers.matched&&report.checks.withholding.matched&&report.checks.trial_balance.balanced);
    for(const[kind,draft]of Object.entries(drafts)){const found=report.queues.drafts_current.find(r=>r.kind===kind&&r.id===draft.id);assert.ok(found,kind+' current draft included');assert.equal(found.future_dated,true);}
    assert.equal(report.counts.drafts,6);assert.ok(report.queues.setup_current.some(r=>r.entity_id===unmapped.id&&r.action.type==='product'));
    assert.equal((await api(page,'supplier-review?as_of=2026-01-30')).position.open_cheque_count,0,'Later clearance disappears only on its effective date');
    await api(page,'supplier-review?as_of=2026-02-30','GET',undefined,400);await api(page,'supplier-review?horizon_days=91','GET',undefined,400);
    assert.deepEqual(await api(page,'audit'),beforeAudit,'Review reads do not write accounting or audit records');

    await page.locator('[data-page="supplier"]:visible').first().click();
    await page.locator('#supplier [data-supplier-review]').click();await page.waitForSelector(`${W}[open]`);await page.waitForSelector(`${A('print')}:not([disabled])`);await close(page);
    await open(page);assert.equal(await page.locator('select').count(),0);assert.equal(await page.locator('aside:visible').count(),0);
    await page.locator(T('overdue')).click();assert.equal(await(await row(page,'invoice',first.id)).count(),1);
    await page.locator(F('search')).fill('NO-MATCH-'+stamp);assert.equal(await page.locator(`${W} [data-review-row]`).count(),0,'Search filters displayed rows');
    await page.locator(F('search')).fill(first.code);assert.equal(await page.locator(`${W} [data-review-row]`).count(),1);
    await printed(page,['نيو بانوراما','2026-01-15',first.code]);
    await page.locator(F('as_of')).fill('2026-01-16');assert.ok(await page.locator(A('print')).isDisabled(),'Pending date cannot print an old report');
    await page.locator(T('cheques')).click();await page.locator(F('search')).fill('');assert.ok(await page.locator(A('print')).isDisabled(),'Tabs/search do not clear stale date guard');
    await page.locator(A('reload')).click();await page.waitForSelector(`${A('print')}:not([disabled])`);
    await page.locator(T('cheques')).click();const chequeRow=await row(page,'payment',cheque.id);assert.equal(await chequeRow.count(),1);
    await chequeRow.locator('[data-review-action="open-row"]').click();await page.waitForSelector('#supplier-ap-window[open]');
    assert.equal(await page.locator(W).evaluate(e=>e.open),false);assert.match(await page.locator('#supplier-ap-window').textContent(),/مصروف|صُرف|صرف الشيك/,'Historical queue opens the current cleared document');await page.keyboard.press('Escape');
    for(const[kind,draft]of Object.entries(drafts))await drillDraft(page,kind,draft,kind==='invoice'?'#entry-window':['payment','return'].includes(kind)?'#supplier-ap-window':kind==='remittance'?'#withholding-remittance-window':'#supplier-settlement-window');
    await drillReport(page,'reconciliation','#supplier-settlement-window','مطابقة الموردين');
    await drillReport(page,'withholding-balances','#withholding-remittance-window','موقف توريد الخصم');
    await drillReport(page,'trial-balance','#ledger-reports-window','ميزان المراجعة');
    await drillReport(page,'aging','#supplier-ap-window','أعمار');
    await open(page);await page.locator(T('negative_funding')).click();
    await page.locator(`${A('open-row')}`).first().click();await page.waitForSelector('#ledger-reports-window[open]');
    await page.waitForSelector('#ledger-reports-window [data-ledger-date="to"]');
    assert.equal(await page.locator('#ledger-reports-window [data-ledger-date="to"]').inputValue(),'2026-01-15','Account drill preserves the financial report date');
    assert.equal(await page.locator(W).evaluate(e=>e.open),false);await page.keyboard.press('Escape');

    // A damaged source must be explained without supplying an unsafe settlement action.
    // Override only the read response; the disposable accounting records stay intact.
    const diagnostic='تعذر تحديد مصدر رصيد الاختبار؛ يلزم مراجعة المستند الأصلي';
    await page.route('**/api/supplier-review?**',async route=>{
      const response=await route.fetch(),body=await response.json();
      body.credits_complete=false;body.queues.credits=[];body.checks.suppliers.matched=false;
      body.queues.financial_issues.push({kind:'supplier',message:diagnostic,action:{type:'reconciliation'}});
      await route.fulfill({response,json:body});
    });
    await open(page);await page.locator(T('credits')).click();
    assert.match(await page.locator('#supplier-review-body').textContent(),/تفاصيل بعض الأرصدة غير مكتملة/);
    assert.match(await page.locator('#supplier-review-table').textContent(),/تعذر عرض بعض مصادر الأرصدة/);
    assert.equal(await page.locator(`${W} [data-review-row]`).count(),0);
    assert.equal(await page.locator(A('open-row')).count(),0,'Unidentified credits have no settlement action');
    await printed(page,['تفاصيل بعض الأرصدة غير مكتملة','تعذر عرض بعض مصادر الأرصدة',diagnostic]);
    await close(page);await page.unroute('**/api/supplier-review?**');
    assert.equal((await api(page,'supplier-review?as_of=2026-01-15')).credits_complete,true,'A presentation override leaves the stored financial sources intact');
    await open(page);await page.locator(T('drafts_current')).click();await printed(page,['مسودات','2026-02-01',drafts.invoice.code]);
    await page.locator(W).screenshot({path:path.join(__dirname,'supplier-review.png')});
    await page.setViewportSize({width:392,height:850});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.equal(await page.locator(W).evaluate(e=>e.scrollWidth>e.clientWidth),false,'Review fits the viewport');
    await page.keyboard.press('Escape');assert.equal(await page.locator(W).evaluate(e=>e.open),false);
    // A completed read after Escape must not restore an obsolete modal.
    let delayed=false;await page.route('**/api/supplier-review?**',async route=>{delayed=true;await new Promise(resolve=>setTimeout(resolve,300));await route.continue();});
    await page.evaluate(()=>{SupplierReview.open('2026-01-15');});await page.waitForFunction(()=>document.querySelector('#supplier-review-window').open);
    await page.keyboard.press('Escape');await page.waitForTimeout(500);assert.ok(delayed);assert.equal(await page.locator(W).evaluate(e=>e.open),false,'Late snapshot does not reopen a closed review');await page.unroute('**/api/supplier-review?**');
    assert.deepEqual(await api(page,'audit'),beforeAudit,'Review UI navigation and printing remain read only');assert.deepEqual(errors,[]);
    console.log('PASS supplier review UI: historical NET cheques, overdue/today/soon, separate credits, six current future drafts, setup issues, read-only audit, incomplete-credit warning/print/action guard, filters/print guard, saved-document drills, Escape/late response and mobile.');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
