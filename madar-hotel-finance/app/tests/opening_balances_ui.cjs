/** Initial actual balances through settings. Run only against a fresh disposable MADAR_DB. */
'use strict';
const assert=require('node:assert/strict');
const path=require('node:path');
const {chromium}=require('playwright');
const base=process.env.AM_TEST_URL||'http://127.0.0.1:8143';
const W='#opening-window',L='#opening-line-window';
const A=name=>`${W} [data-opening-action="${name}"]`;
const F=name=>`${W} [data-opening-field="${name}"]`;
const LA=name=>`${L} [data-opening-action="${name}"]`;
const LF=name=>`${L} [data-opening-line-field="${name}"]`;
const stamp=Date.now().toString(36);let sequence=0;
const key=name=>`OPEN-UI-${stamp}-${name}-${++sequence}`;
const day='2026-01-01',asOf='2026-01-31';
const money=cents=>new Intl.NumberFormat('ar-EG',{minimumFractionDigits:2,maximumFractionDigits:2}).format(cents/100);

async function response(page,route,method='GET',data){
  return page.evaluate(async({route,method,data})=>{
    const boot=await(await fetch('/api/bootstrap')).json();
    const result=await fetch('/api/'+route,{method,headers:{'Content-Type':'application/json','X-Madar-CSRF':boot.csrf},body:data===undefined?undefined:JSON.stringify(data)});
    return{status:result.status,body:await result.json()};
  },{route,method,data});
}
async function api(page,route,method='GET',data,expected=200){
  const result=await response(page,route,method,data);
  assert.equal(result.status,expected,`${method} ${route}: ${JSON.stringify(result.body)}`);return result.body;
}
async function reject(page,route,method,data,pattern){
  const result=await response(page,route,method,data);
  assert.ok([400,409].includes(result.status),`${method} ${route} must reject: ${JSON.stringify(result)}`);
  if(pattern)assert.match(result.body.error||JSON.stringify(result.body),pattern);return result.body;
}
async function clickResponse(page,selector,route,method='POST'){
  const waiting=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/'+route&&r.request().method()===method);
  await page.locator(selector).last().click();const result=await waiting,body=await result.json();
  assert.equal(result.status(),200,`${method} ${route}: ${JSON.stringify(body)}`);return body;
}
async function pick(page,name,id){
  await page.locator(`${LA('pick')}[data-opening-pick="${name}"]`).click();
  await page.locator(`#picker-window [data-pick="${id}"]`).click();
}
async function addLine(page,line){
  await page.locator(A('line-new')).click();await page.waitForSelector(`${L}[open]`);
  await page.locator(`${L} [data-opening-kind="${line.kind}"]`).click();
  if(line.kind==='general'){
    await pick(page,'account_id',line.account_id);
    await page.locator(`${L} [data-opening-side="${line.side}"]`).click();
  }
  for(const name of ['supplier_id','custodian_id','department_id'])if(line[name])await pick(page,name,line[name]);
  for(const name of ['amount','reference','source_date','due_date','remaining_net_amount'])
    if(line[name]!==undefined)await page.locator(LF(name)).fill(String(line[name]));
  if(line.extra){
    await pick(page,'extra.bank_account_id',line.extra.bank_account_id);
    for(const name of ['beneficiary','cheque_number','bank_name'])await page.locator(LF('extra.'+name)).fill(line.extra[name]);
  }
  await page.locator(LA('line-save')).click();await page.waitForFunction(()=>!document.querySelector('#opening-line-window').open);
}
async function close(page){
  await page.locator(A('close')).first().click();await page.waitForFunction(()=>!document.querySelector('#opening-window').open);
}
async function printed(page,required,selector=A('print')){
  const before=await page.evaluate(()=>window.__openingPrints);await page.locator(selector).click();
  await page.waitForFunction(n=>window.__openingPrints>n,before);
  const content=await page.locator('#print-document').textContent();
  for(const word of required)assert.ok(content.includes(word),'Opening print contains '+word);
  assert.match(content,/توقيع|راجعه|اعتمده|المراجع/);
  await page.emulateMedia({media:'print'});assert.equal(await page.locator('dialog:visible').count(),0);assert.ok(await page.locator('#print-document').isVisible());await page.emulateMedia({media:'screen'});
  return content;
}
async function ledger(page,account,from=day,to=asOf){return api(page,`account-ledger?account_id=${account}&from_date=${from}&to_date=${to}`);}
async function snapshot(page,cash,bank){return{
  trial:await api(page,`trial-balance?from_date=${day}&to_date=${asOf}&include_zero=1`),
  cash:await api(page,`cash-book?account_id=${cash}&from_date=${day}&to_date=${asOf}`),
  bank:await api(page,`bank-book?account_id=${bank}&from_date=${day}&to_date=${asOf}`),
  reconciliation:await api(page,'supplier-reconciliation?as_of='+asOf),
  custody:await api(page,'custody-balances?as_of='+asOf),
  withholding:await api(page,'withholding-balances?as_of='+asOf)
};}
async function postDocument(page,route,data){
  const draft=await api(page,route,'POST',{request_key:key(route),...data});
  return api(page,`${route}/${draft.id}/post`,'POST',{revision:draft.revision});
}
function sourceId(line){return line.document_id??line.metadata?.document_id??line.document?.id;}

(async()=>{
  const browser=await chromium.launch({executablePath:process.env.AM_CHROMIUM||'/usr/bin/chromium',args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:1080}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));await page.addInitScript(()=>{window.__openingPrints=0;window.print=()=>window.__openingPrints++;});
  try{
    await page.goto(base);await page.waitForSelector('#loading[hidden]',{state:'attached'});
    const boot=await api(page,'bootstrap'),chart=await api(page,'accounting');
    const cash=chart.accounts.find(a=>a.code==='111002'),bank=chart.accounts.find(a=>a.code==='112001');
    const openingConfig=await api(page,'opening-config');
    const general=openingConfig.general_accounts;
    const equity=general.find(a=>a.code==='300001')||general.find(a=>a.code.startsWith('3')&&a.normal_side==='credit');
    assert.ok(cash&&bank&&equity,'The real chart provides cash, bank and equity accounts');
    assert.equal((await api(page,'opening-balances')).length,0,'Fresh database has no invented opening batches');
    assert.equal(openingConfig.posted_batch,null);
    const empty=await snapshot(page,cash.id,bank.id);assert.equal(empty.cash.closing_balance_cents,0);assert.equal(empty.bank.closing_balance_cents,0);
    const supplier=await api(page,'suppliers','POST',{name:'مورد افتتاحي موثق '+stamp,terms_days:0,tax_number:'OPEN-TAX-'+stamp});
    const custodian=await api(page,'custodians','POST',{name:'صاحب عهدة افتتاحية '+stamp,phone:'TEST-OPEN'});
    const before=await snapshot(page,cash.id,bank.id);
    const reference=key('BASELINE');

    // Enter from settings; a deliberately incomplete draft is reviewable and nonposting.
    await page.locator('#modules [data-page="accounting"]').click();
    await page.locator('#accounting [data-opening-balances]').click();await page.waitForSelector(A('new'));
    assert.equal(await page.locator('select').count(),0);assert.equal(await page.locator('aside:visible').count(),0);
    await page.locator(A('new')).click();await page.waitForSelector(F('business_date'));
    await page.locator(F('business_date')).fill(day);await page.locator(F('reference')).fill(reference);
    await page.locator(F('notes')).fill('ميزان فعلي تجريبي في قاعدة معزولة؛ أرصدة متبقية دون إعادة شراء أو صرف.');
    await addLine(page,{kind:'general',account_id:cash.id,side:'debit',amount:'1000',reference:'OPEN-CASH'});
    const unbalanced=await clickResponse(page,A('preview'),'opening-preview');
    assert.equal(unbalanced.totals.debit_cents,100000);assert.equal(unbalanced.totals.credit_cents,0);assert.equal(unbalanced.can_approve,false);
    const saved=await clickResponse(page,A('save'),'opening-balances');assert.equal(saved.status,'draft');
    assert.deepEqual(await snapshot(page,cash.id,bank.id),before,'Saving an unbalanced opening draft changes no ledgers or detailed balances');
    await reject(page,`opening-balances/${saved.id}/post`,'POST',{revision:saved.revision});

    // A dirty line and the batch remain under the operator's control.
    await page.locator(A('line-new')).click();await page.waitForSelector(`${L}[open]`);await page.locator(LF('reference')).fill(key('DISCARD-LINE'));
    page.once('dialog',dialog=>dialog.dismiss());await page.keyboard.press('Escape');assert.ok(await page.locator(L).evaluate(e=>e.open));
    page.once('dialog',dialog=>dialog.accept());await page.keyboard.press('Escape');assert.equal(await page.locator(L).evaluate(e=>e.open),false);
    await page.locator(F('notes')).fill('بيان افتتاحي معدل يحتفظ به المستخدم حتى الحفظ');
    page.once('dialog',dialog=>dialog.dismiss());await page.keyboard.press('Escape');assert.ok(await page.locator(W).evaluate(e=>e.open));

    // The operator explicitly confirms the technical clearing account in its own UI.
    const mapping=openingConfig.clearing_mapping;
    assert.equal(mapping.status,'proposed');
    page.once('dialog',dialog=>dialog.accept());
    await clickResponse(page,`${A('confirm-mapping')}[data-id="${mapping.id}"]`,`account-mappings/${mapping.id}`,'PUT');
    for(const mapping of chart.mappings.filter(m=>['payable_control','supplier_advances','operating_custodies','withholding_payable','issued_cheques'].includes(m.role)))
      await api(page,`account-mappings/${mapping.id}`,'PUT',{revision:mapping.revision,status:'confirmed',account_id:mapping.account_id});
    // Reopening refreshes all controlled-account mappings before selecting their source kinds.
    await clickResponse(page,A('save'),`opening-balances/${saved.id}`,'PUT');await close(page);
    await page.evaluate(id=>OpeningBalances.openBatch(id),saved.id);await page.waitForSelector(A('line-new'));
    const lines=[
      {kind:'general',account_id:bank.id,side:'debit',amount:'5000',reference:'OPEN-BANK'},
      {kind:'supplier_invoice',supplier_id:supplier.id,amount:'1500',reference:'OLD-INVOICE',source_date:'2025-12-01',due_date:'2026-01-15',remaining_net_amount:'1500'},
      {kind:'supplier_credit',supplier_id:supplier.id,amount:'100',reference:'OLD-CREDIT',source_date:'2025-12-02'},
      {kind:'supplier_advance',supplier_id:supplier.id,amount:'200',reference:'OLD-ADVANCE',source_date:'2025-12-03'},
      {kind:'custody',custodian_id:custodian.id,amount:'300',reference:'OLD-CUSTODY',source_date:'2025-12-04'},
      {kind:'withholding',supplier_id:supplier.id,amount:'50',reference:'OLD-WITHHOLDING',source_date:'2025-12-05'},
      {kind:'issued_cheque',supplier_id:supplier.id,amount:'200',reference:'OLD-CHEQUE',source_date:'2025-12-06',due_date:'2026-01-20',extra:{bank_account_id:bank.id,beneficiary:supplier.name,cheque_number:'OPEN-CHQ-'+stamp,bank_name:'بنك مصر'}},
      {kind:'general',account_id:equity.id,side:'credit',amount:'4850',reference:'OPEN-EQUITY'}
    ];
    for(const line of lines)await addLine(page,line);
    const preview=await clickResponse(page,A('preview'),'opening-preview');
    assert.equal(preview.totals.debit_cents,660000);assert.equal(preview.totals.credit_cents,660000);assert.equal(preview.totals.difference_cents,0);assert.equal(preview.can_approve,true,JSON.stringify(preview));
    assert.equal(preview.lines.length,9);assert.equal(await page.locator(`${W} [data-opening-row]`).count(),9);
    const balanced=await clickResponse(page,A('save'),`opening-balances/${saved.id}`,'PUT');
    assert.equal((await snapshot(page,cash.id,bank.id)).cash.closing_balance_cents,0,'Even a balanced saved draft has no financial effect');
    page.once('dialog',dialog=>dialog.accept());
    const posted=await clickResponse(page,A('post'),`opening-balances/${saved.id}/post`);
    assert.equal(posted.status,'posted');assert.equal(posted.lines.length,9);assert.ok(posted.posted_at);
    await page.waitForFunction(()=>!document.querySelector('#opening-window [data-opening-action="post"]'));
    assert.equal(await page.locator(A('post')).count(),0);assert.equal(await page.locator(A('line-new')).count(),0,'Posted source details cannot be edited');
    for(const line of posted.lines){
      assert.ok(line.journal_id,'Every opening line has its own linked journal');
      const journal=await api(page,'journals/'+line.journal_id);
      assert.equal(journal.source_type,'opening_balance');assert.equal(journal.debit_cents,journal.credit_cents);
      assert.equal(journal.debit_cents,line.amount_cents);
    }
    const position=await snapshot(page,cash.id,bank.id);
    assert.ok(position.trial.balanced);assert.equal(position.cash.closing_balance_cents,100000);assert.equal(position.bank.closing_balance_cents,500000);
    assert.equal((await ledger(page,preview.clearing_account_id)).closing_balance_cents,0,'All opening line journals net the technical clearing account to zero');
    assert.ok(position.reconciliation.matched,JSON.stringify(position.reconciliation));assert.ok(position.custody.matched);assert.ok(position.withholding.matched);
    assert.equal(position.custody.totals.remaining_cents,30000);
    const statement=(await api(page,`suppliers/${supplier.id}/statement?as_of=${asOf}`)).totals;
    assert.equal(statement.payable_balance_cents,140000);assert.equal(statement.advance_cents,20000);assert.equal(statement.net_payable_cents,120000);
    const aging=await api(page,'supplier-aging?as_of='+asOf),aged=aging.suppliers.find(s=>s.supplier_id===supplier.id);
    assert.equal(aged.net_payable_cents,120000);assert.equal(aged.current_cents,0,'Original due dates drive overdue opening invoices');
    const invoiceLine=posted.lines.find(l=>l.kind==='supplier_invoice'),creditLine=posted.lines.find(l=>l.kind==='supplier_credit'),advanceLine=posted.lines.find(l=>l.kind==='supplier_advance');
    const custodyLine=posted.lines.find(l=>l.kind==='custody'),whLine=posted.lines.find(l=>l.kind==='withholding');
    const invoiceId=sourceId(invoiceLine),creditId=sourceId(creditLine),advanceId=sourceId(advanceLine),custodyId=sourceId(custodyLine),whId=sourceId(whLine);
    for(const id of [invoiceId,creditId,advanceId,custodyId,whId])assert.ok(id,'Controlled opening lines materialize detailed sources');
    const openingInvoice=await api(page,'invoices/'+invoiceId);assert.ok(openingInvoice.is_opening);assert.equal(openingInvoice.lines.length,0);
    assert.equal((await api(page,'supplier-payments')).length,0,'Opening advances and cheques do not invent payment history');
    assert.equal((await api(page,'treasury-documents')).length,0,'Opening cash, advances, custodies and WH do not create fake cash vouchers');
    assert.equal((await api(page,'bootstrap')).products.length,0,'Opening supplier debt does not invent purchases or product lines');
    const frozenPrint=await printed(page,[posted.code,reference,'نيو بانوراما','OLD-INVOICE','OLD-CUSTODY','OLD-WITHHOLDING',money(660000)]);
    await page.locator(`${W} .ob-scroll`).evaluate(element=>{element.scrollTop=0;});
    await page.locator(W).screenshot({path:path.join(__dirname,'opening-balances.png')});
    const audit=await api(page,'audit');assert.deepEqual(await api(page,`opening-balances/${posted.id}`),posted);
    await printed(page,[posted.code,reference]);assert.deepEqual(await api(page,'audit'),audit,'Reading and printing opening evidence do not append financial events');
    await reject(page,`opening-balances/${posted.id}`,'PUT',{...balanced,revision:posted.revision,notes:'محاولة تعديل الأصل'});
    const replay=await api(page,`opening-balances/${posted.id}/post`,'POST',{revision:balanced.revision});assert.equal(replay.id,posted.id);
    assert.deepEqual(await snapshot(page,cash.id,bank.id),position,'Double-click/retry cannot duplicate opening journals or detailed sources');
    await close(page);

    await page.evaluate(id=>Treasury.openCustody(id),custodyId);
    await page.waitForSelector('#treasury-window [data-treasury-action="print"]');
    const custodyPrint=await printed(page,['عهدة افتتاحية تحت التسوية',custodian.name,'OLD-CUSTODY',money(30000)],'#treasury-window [data-treasury-action="print"]');
    assert.ok(!custodyPrint.includes('إقرار استلام:'),'The opening custody evidence cannot acknowledge a new cash payment');
    await page.locator('#treasury-window [data-treasury-action="close"]').first().click();
    await page.waitForFunction(()=>!document.querySelector('#treasury-window').open);

    // The old sources can take real partial applications after cutover.
    await postDocument(page,'supplier-payments',{supplier_id:supplier.id,payment_date:'2026-01-02',amount:'100',method:'cash',funding_account_id:cash.id,allocation_mode:'invoice',target_invoice_id:invoiceId,details:{received_by:'مندوب المورد الافتتاحي'}});
    await postDocument(page,'supplier-settlements',{supplier_id:supplier.id,business_date:'2026-01-03',reference:key('CREDIT-APPLY'),source_type:'return_credit',source_id:creditId,allocations:[{invoice_id:invoiceId,amount:'100'}]});
    await postDocument(page,'supplier-settlements',{supplier_id:supplier.id,business_date:'2026-01-04',reference:key('ADVANCE-APPLY'),source_type:'advance',source_origin:'opening',source_id:advanceId,allocations:[{invoice_id:invoiceId,amount:'100'}]});
    const expense=chart.accounts.find(a=>a.active&&a.posting&&a.code.startsWith('6')&&a.normal_side==='debit');
    await postDocument(page,'custody-documents',{kind:'settlement',source_issue_id:custodyId,business_date:'2026-01-05',reference:key('CUSTODY-SETTLE'),lines:[{expense_account_id:expense.id,department_id:boot.departments[0].id,reference:key('RECEIPT'),amount:'50'}]});
    await postDocument(page,'custody-documents',{kind:'return',source_issue_id:custodyId,cash_account_id:cash.id,business_date:'2026-01-06',reference:key('CUSTODY-RETURN'),amount:'100'});
    await page.evaluate(()=>WithholdingRemittances.newDocument({business_date:'2026-01-07',period_from:'2025-12-01',period_to:'2025-12-31'}));
    const RW='#withholding-remittance-window',RA=name=>`${RW} [data-remittance-action="${name}"]`,RF=name=>`${RW} [data-remittance-field="${name}"]`;
    await page.waitForSelector(RF('authority_name'));await page.locator(RF('authority_name')).fill('جهة الضريبة في اختبار الافتتاح');await page.locator(RF('reference')).fill(key('WH-REMIT'));
    await page.locator(`${RA('funding')}[data-account-id="${cash.id}"]`).click();await page.locator(RF('details.received_by')).fill('مندوب جهة الضريبة');
    const whKey='opening:'+whId;
    await page.locator(`${RA('toggle-source')}[data-payment-id="${whKey}"]`).click();await page.locator(`${RW} [data-remittance-allocation="${whKey}"]`).fill('20');
    const remitPreview=await clickResponse(page,RA('preview'),'withholding-remittance-preview');assert.equal(remitPreview.amount_cents,2000);
    const remitDraft=await clickResponse(page,RA('save'),'withholding-remittances');
    const remit=await clickResponse(page,RA('post'),`withholding-remittances/${remitDraft.id}/post`);assert.equal(remit.amount_cents,2000);
    await printed(page,[remit.code,'رصيد خصم افتتاحي',money(2000)],RA('print-document'));
    await page.locator(RA('close')).first().click();await page.waitForFunction(()=>!document.querySelector('#withholding-remittance-window').open);
    const after=await snapshot(page,cash.id,bank.id);assert.equal(after.cash.closing_balance_cents,98000);assert.equal(after.bank.closing_balance_cents,500000);
    assert.ok(after.trial.balanced&&after.reconciliation.matched&&after.custody.matched&&after.withholding.matched);
    assert.equal(after.custody.totals.remaining_cents,15000);
    const wh=after.withholding.sources.find(s=>s.source_type==='opening_withholding'&&s.source_id===whId);assert.equal(wh.remaining_cents,3000);
    const open=(await api(page,`suppliers/${supplier.id}/open-invoices?as_of=${asOf}`)).find(i=>i.id===invoiceId||i.invoice_id===invoiceId);
    assert.equal(open.outstanding_cents,120000);
    const laterStatement=(await api(page,`suppliers/${supplier.id}/statement?as_of=${asOf}`)).totals;
    assert.equal(laterStatement.advance_cents,10000);assert.equal(laterStatement.net_payable_cents,110000);
    assert.equal((await ledger(page,cash.id,'2026-01-02')).opening_balance_cents,100000,'A later period begins from the approved opening balance');
    assert.equal((await ledger(page,bank.id,'2026-01-02')).opening_balance_cents,500000);
    await reject(page,'treasury-documents','POST',{request_key:key('BACKDATE'),kind:'receipt',business_date:'2025-12-31',reference:key('BEFORE-BASELINE'),amount:'1',cash_account_id:cash.id,counter_account_id:equity.id,party_name:'اختبار رفض تاريخ أقدم'});
    await reject(page,'opening-balances','POST',{request_key:key('SECOND-BASELINE'),business_date:day,reference:key('SECOND'),lines:[{kind:'general',account_id:cash.id,side:'debit',amount:'1'},{kind:'general',account_id:equity.id,side:'credit',amount:'1'}]});
    assert.equal((await api(page,'opening-balances')).length,1,'A posted initial baseline prevents a second initial batch');

    // Later operations never rewrite the approved batch or its printed amounts.
    await page.evaluate(id=>OpeningBalances.openBatch(id),posted.id);await page.waitForSelector(A('print'));
    assert.equal(await printed(page,[posted.code,reference,money(660000)]),frozenPrint);
    await page.setViewportSize({width:392,height:850});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.equal(await page.locator(W).evaluate(e=>e.scrollWidth>e.clientWidth),false,'The posted opening batch fits a phone width');
    assert.equal(await page.locator('select').count(),0);assert.equal(await page.locator('aside:visible').count(),0);await close(page);
    await page.evaluate(()=>OpeningBalances.open());await page.waitForSelector(A('open'));
    assert.equal(await page.locator(W).evaluate(e=>e.scrollWidth>e.clientWidth),false,'The batch list fits a phone width');await close(page);
    let delayed=false;
    await page.route('**/api/opening-config',async route=>{delayed=true;await new Promise(resolve=>setTimeout(resolve,250));await route.continue();});
    await page.evaluate(()=>{OpeningBalances.open();});await page.waitForSelector(`${W}[open]`);await page.keyboard.press('Escape');
    await page.waitForTimeout(500);assert.ok(delayed);assert.equal(await page.locator(W).evaluate(e=>e.open),false,'A late settings read cannot reopen a closed opening window');
    await page.unroute('**/api/opening-config');
    assert.deepEqual(errors,[],errors.join('\n'));
    console.log('PASS opening balances UI: settings entry, unbalanced nonposting draft, explicit mapping, nine balanced source kinds, atomic journals and zero clearing, reconciled cash/bank/AP/aging/custody/WH, partial source usage, frozen print, baseline guards, immutable replay, dirty forms and mobile.');
  }catch(error){
    await page.screenshot({path:path.join(__dirname,'opening-balances-failure.png'),fullPage:true}).catch(()=>{});
    console.error('Opening UI context:',await page.locator('dialog[open]').allTextContents().catch(()=>[]));throw error;
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
