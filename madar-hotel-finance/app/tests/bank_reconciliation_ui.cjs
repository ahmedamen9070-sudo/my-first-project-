/** External bank statements and reviewed matching, on a disposable local database. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const net=require('node:net');
const os=require('node:os');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
const W='#bank-reconciliation-window';
const A=name=>`${W} [data-reconciliation-action="${name}"]`;
const F=name=>`${W} [data-reconciliation-field="${name}"]`;
const L=(name,index)=>`${W} [data-reconciliation-line-field="${name}"][data-index="${index}"]`;
const stamp=Date.now().toString(36);let serial=0;
const key=name=>`BANK-REC-UI-${stamp}-${name}-${++serial}`;
const money=cents=>new Intl.NumberFormat('ar-EG',{minimumFractionDigits:2,maximumFractionDigits:2}).format(cents/100);
const plan=doc=>doc.snapshot||doc.preview||doc;
const signed=row=>row.amount_cents??row.signed_cents??(Number(row.debit_cents||0)-Number(row.credit_cents||0));

async function request(page,route,method='GET',data){
  return page.evaluate(async({route,method,data})=>{
    const boot=await(await fetch('/api/bootstrap')).json();
    const result=await fetch('/api/'+route,{method,headers:{'Content-Type':'application/json','X-Madar-CSRF':boot.csrf},body:data===undefined?undefined:JSON.stringify(data)});
    return{status:result.status,body:await result.json()};
  },{route,method,data});
}
async function api(page,route,method='GET',data){
  const r=await request(page,route,method,data);
  assert.equal(r.status,200,`${method} ${route}: ${JSON.stringify(r.body)}`);return r.body;
}
async function rejects(page,route,method,data){
  const r=await request(page,route,method,data);
  assert.ok(r.status>=400&&r.status<500,`${method} ${route} must reject: ${JSON.stringify(r)}`);return r;
}
async function clicked(page,selector,route,method='POST',expected=200){
  const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/'+route&&r.request().method()===method);
  await page.locator(selector).click();const result=await pending,body=await result.json();
  assert.equal(result.status(),expected,`${method} ${route}: ${JSON.stringify(body)}`);return body;
}
async function choose(page,selector,id){
  await page.locator(selector).click();await page.locator(`#picker-window [data-pick="${id}"]`).click();
}
async function post(page,route,data){
  const draft=await api(page,route,'POST',{request_key:key(route),...data});
  return api(page,`${route}/${draft.id}/post`,'POST',{revision:draft.revision});
}
async function close(page){await page.locator(A('close')).first().click();await page.waitForFunction(()=>!document.querySelector('#bank-reconciliation-window').open);}
async function printed(page,words){
  const before=await page.evaluate(()=>window.__reconciliationPrints);
  await page.locator(A('print')).click();await page.waitForFunction(n=>window.__reconciliationPrints>n,before);
  const content=await page.locator('#print-document').textContent();
  for(const word of words)assert.ok(content.includes(word),'Printed certificate includes '+word);
  assert.match(content,/راجعه|اعتمده|توقيع/);
  await page.emulateMedia({media:'print'});assert.equal(await page.locator('dialog:visible').count(),0);
  assert.ok(await page.locator('#print-document').isVisible());await page.emulateMedia({media:'screen'});return content;
}
async function bankRows(page,account){return(await api(page,`bank-book?account_id=${account}&from_date=2026-01-01&to_date=2026-12-31`)).rows;}
async function lineOf(page,account,document){const row=(await bankRows(page,account)).find(row=>row.journal_id===document.journal_id);assert.ok(row,'Book includes source journal');return row.line_id||row.id;}
async function match(page,statementIds,bookIds){
  for(const id of statementIds)await page.locator(`${A('toggle-statement')}[data-id="${id}"]`).click();
  for(const id of bookIds)await page.locator(`${A('toggle-book')}[data-id="${id}"]`).click();
  await page.locator(A('match-selected')).click();
}
async function freePort(){const listener=net.createServer();await new Promise(resolve=>listener.listen(0,'127.0.0.1',resolve));const port=listener.address().port;await new Promise(resolve=>listener.close(resolve));return port;}

(async()=>{
  const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'am-bank-reconciliation-ui-'));
  const port=await freePort(),base=`http://127.0.0.1:${port}`;
  const server=spawn('python3',[path.join(__dirname,'..','server.py'),'--port',String(port)],{env:{...process.env,MADAR_DB:path.join(temporary,'isolated.sqlite')},stdio:['ignore','ignore','pipe']});
  let serverErrors='',browser,page;server.stderr.on('data',chunk=>{serverErrors=(serverErrors+chunk).slice(-16000);});
  try{
    let ready=false;for(let attempt=0;attempt<120;attempt++){if(server.exitCode!==null)throw Error('Isolated server failed: '+serverErrors);try{ready=(await fetch(base+'/api/bootstrap')).ok;}catch{}if(ready)break;await new Promise(resolve=>setTimeout(resolve,50));}assert.ok(ready,'Fresh isolated server starts');
    browser=await chromium.launch({executablePath:process.env.AM_CHROMIUM||'/usr/bin/chromium',args:['--no-sandbox']});
    page=await browser.newPage({viewport:{width:1440,height:1080}});const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.addInitScript(()=>{window.__reconciliationPrints=0;window.print=()=>window.__reconciliationPrints++;});
    await page.goto(base);await page.waitForSelector('#loading[hidden]',{state:'attached'});
    const boot=await api(page,'bootstrap'),chart=await api(page,'accounting');
    for(const mapping of chart.mappings)if(mapping.status!=='confirmed')await api(page,`account-mappings/${mapping.id}`,'PUT',{revision:mapping.revision,status:'confirmed'});
    const config=await api(page,'bank-config'),bank=config.bank_accounts.find(a=>a.code==='112001'),other=config.bank_accounts.find(a=>a.code==='112003');
    const equity=config.counter_accounts_by_kind.receipt.find(a=>a.code.startsWith('3')),expense=config.counter_accounts_by_kind.payment.find(a=>a.code.startsWith('6')),department=boot.departments[0].id;
    const supplier=await api(page,'suppliers','POST',{name:'مورد تسوية البنك '+stamp,tax_number:'REC-TAX-'+stamp});
    const initial=await post(page,'opening-balances',{business_date:'2026-01-01',reference:key('OPEN'),lines:[
      {kind:'general',account_id:bank.id,side:'debit',amount:'1000'},
      {kind:'general',account_id:other.id,side:'debit',amount:'500'},
      {kind:'supplier_invoice',supplier_id:supplier.id,reference:'REC-OLD-INVOICE',source_date:'2025-12-01',due_date:'2025-12-31',amount:'100',remaining_net_amount:'100'},
      {kind:'issued_cheque',amount:'100',reference:'REC-OLD-CHEQUE',source_date:'2025-12-01',due_date:'2026-01-20',extra:{bank_account_id:bank.id,bank_name:'بنك اختبار',beneficiary:'مستفيد شيك قائم',cheque_number:'REC-OLD-'+stamp}},
      {kind:'general',account_id:equity.id,side:'credit',amount:'1300'}]});
    assert.equal(initial.status,'posted');
    const move=async(kind,amount,date,account=bank.id)=>post(page,'bank-documents',{kind,business_date:date,value_date:date,reference:key('DOC'),bank_reference:key('BANK'),amount,account_id:account,counter_account_id:kind==='receipt'?equity.id:expense.id,party_name:'طرف موثق للتجربة',department_id:department});
    const deposit1=await move('receipt','300','2026-01-02'),deposit2=await move('receipt','200','2026-01-03'),withdrawal=await move('payment','100','2026-01-04'),transit=await move('receipt','50','2026-01-20');
    const ids={deposit1:await lineOf(page,bank.id,deposit1),deposit2:await lineOf(page,bank.id,deposit2),withdrawal:await lineOf(page,bank.id,withdrawal),transit:await lineOf(page,bank.id,transit)};

    // Use the actual entry control. The new screen must not create any journal.
    const beforeRead=await api(page,'audit');
    await page.evaluate(()=>page('accounting'));
    await page.locator('[data-bank-reconciliation]').first().click();await page.waitForSelector(A('new'));
    assert.equal(await page.locator('select').count(),0);await close(page);assert.deepEqual(await api(page,'audit'),beforeRead);
    await page.evaluate(id=>BankReconciliation.newDocument({account_id:id}),bank.id);await page.waitForSelector(F('reference'));
    await page.locator(F('from_date')).fill('2026-01-01');await page.locator(F('to_date')).fill('2026-01-31');
    const reference=key('STATEMENT-JAN');await page.locator(F('reference')).fill(reference);
    await page.locator(F('opening_balance')).fill('1000');await page.locator(F('closing_balance')).fill('1390');
    await page.locator(F('prepared_by')).fill('المحاسب القائم بالمطابقة');await page.locator(F('reviewed_by')).fill('المراجع المالي');

    // Header mapping uses column zero and preserves leading-zero bank references.
    await page.locator(A('load-csv')).click();
    await page.locator('#reconciliation-csv-file').setInputFiles({name:'كشف-بنك.csv',mimeType:'text/csv',buffer:Buffer.from('Date;Reference;Description;Deposit;Withdrawal;Balance\n2026-01-04;000A;إيداع مجمع;500.00;;1500.00\n2026-01-05;000B;سحب;;100.00;1400.00\n','utf8')});
    await page.waitForSelector(A('parse-csv'));
    await clicked(page,A('inspect-csv'),'bank-statement-inspect');
    for(const [column,index]of Object.entries({date:0,reference:1,description:2,deposit:3,withdrawal:4,balance:5}))await choose(page,`${A('pick-csv-column')}[data-column="${column}"]`,index);
    const imported=await clicked(page,A('parse-csv'),'bank-statement-import');
    assert.equal(imported.count,2);assert.equal(imported.lines[0].reference,'000A');assert.equal(imported.lines[0].amount_cents,50000);assert.equal(imported.lines[1].amount_cents,-10000);
    await page.locator(A('add-line')).click();
    await page.locator(L('date',2)).fill('2026-01-06');await page.locator(L('reference',2)).fill('000C');await page.locator(L('description',2)).fill('عمولة ظهرت بكشف البنك فقط');
    await page.locator(`${A('line-direction')}[data-direction="withdrawal"][data-index="2"]`).click();await page.locator(L('amount',2)).fill('10');
    await page.locator(F('closing_balance')).fill('1390');
    let preview=await clicked(page,A('preview'),'bank-reconciliation-preview');
    assert.equal(preview.totals.statement_closing_cents,139000);assert.equal(preview.totals.book_opening_cents,100000);assert.equal(preview.totals.book_closing_cents,145000);
    assert.equal(preview.pending_cheques.length,1,'An issued cheque remains information, never a second bank deduction');
    assert.ok(!preview.can_confirm);assert.equal(preview.unmatched_statement_lines.length,3);
    await match(page,[1],[ids.deposit1,ids.deposit2]);await match(page,[2],[ids.withdrawal]);
    let draft=await clicked(page,A('save'),'bank-reconciliations');assert.equal(draft.status,'draft');assert.equal(draft.matches.length,2);
    assert.ok(!draft.preview.can_confirm,'The bank-only fee must be recorded before certification');await close(page);

    const fee=await move('fee','10','2026-01-06');ids.fee=await lineOf(page,bank.id,fee);
    await page.evaluate(id=>BankReconciliation.openDocument(id),draft.id);await page.waitForSelector(A('preview'));
    await clicked(page,A('preview'),'bank-reconciliation-preview');await match(page,[3],[ids.fee]);
    draft=await clicked(page,A('save'),`bank-reconciliations/${draft.id}`,'PUT');assert.ok(draft.preview.can_confirm,draft.preview.blockers?.map(b=>b.message).join('; '));

    // A zero-net financial change is still new evidence that needs review.
    const zeroIn=await move('receipt','7','2026-01-08'),zeroOut=await move('payment','7','2026-01-09');
    ids.zeroIn=await lineOf(page,bank.id,zeroIn);ids.zeroOut=await lineOf(page,bank.id,zeroOut);
    await clicked(page,A('confirm'),`bank-reconciliations/${draft.id}/confirm`,'POST',409);
    assert.ok((await page.locator('#bank-reconciliation-error').textContent()).trim(),'Stale evidence gives a visible review message');
    await clicked(page,A('preview'),'bank-reconciliation-preview');draft=await clicked(page,A('save'),`bank-reconciliations/${draft.id}`,'PUT');
    const financialBefore=(await bankRows(page,bank.id)).map(row=>[row.line_id,row.debit_cents,row.credit_cents]);
    const first=await clicked(page,A('confirm'),`bank-reconciliations/${draft.id}/confirm`);
    assert.equal(first.status,'confirmed');assert.equal(plan(first).totals.unmatched_book_cents,5000);assert.equal(plan(first).unmatched_book_lines.length,3,'Net-zero pending lines remain individually visible');
    assert.equal(plan(first).totals.book_closing_cents,144000);assert.equal(plan(first).totals.adjusted_statement_cents,144000);assert.equal(plan(first).totals.difference_cents,0);
    assert.deepEqual((await bankRows(page,bank.id)).map(row=>[row.line_id,row.debit_cents,row.credit_cents]),financialBefore,'Certification is nonposting');
    const originalPrint=await printed(page,[reference,'نيو بانوراما',money(139000),money(144000),'المراجع المالي']);
    await page.locator(`${W} .br-kpis`).evaluate(element=>element.closest('.br-section').scrollIntoView({block:'start'}));
    await page.locator(W).screenshot({path:path.join(__dirname,'bank-reconciliation.png')});
    const audit=await api(page,'audit');assert.deepEqual(await api(page,`bank-reconciliations/${first.id}`),first);await printed(page,[reference]);assert.deepEqual(await api(page,'audit'),audit);
    const replay=await api(page,`bank-reconciliations/${first.id}/confirm`,'POST',{revision:draft.revision});assert.equal(replay.id,first.id);
    await rejects(page,`bank-reconciliations/${first.id}`,'PUT',{revision:first.revision,reference:'CHANGE-FROZEN'});await close(page);

    // The central journal guard covers backdated bank movements, not another bank.
    const lockedPayload={request_key:key('LOCKED'),kind:'receipt',business_date:'2026-01-10',value_date:'2026-01-10',reference:key('LOCKED-REF'),bank_reference:key('LOCKED-BANK'),amount:'1',account_id:bank.id,counter_account_id:equity.id,party_name:'اختبار قفل حساب البنك'};
    const lockedDraft=await api(page,'bank-documents','POST',lockedPayload);
    await rejects(page,`bank-documents/${lockedDraft.id}/post`,'POST',{revision:lockedDraft.revision});
    await api(page,`bank-documents/${lockedDraft.id}`,'PUT',{revision:lockedDraft.revision,action:'cancel'});
    const otherReceipt=await move('receipt','25','2026-01-10',other.id);assert.equal(otherReceipt.status,'posted');
    assert.equal((await api(page,'bank-documents','POST',{request_key:deposit1.request_key,...Object.fromEntries(['kind','business_date','value_date','reference','bank_reference','currency','account_id','counter_account_id','destination_account_id','party_name','department_id','notes'].map(k=>[k,deposit1[k]])),amount:'300'})).id,deposit1.id,'An existing exact request replay still works behind the reconciliation lock');

    // A later statement carries the exact three outstanding book identities.
    await page.evaluate(id=>BankReconciliation.newDocument({account_id:id}),bank.id);await page.waitForSelector(F('reference'));
    await page.locator(F('from_date')).fill('2026-02-01');await page.locator(F('to_date')).fill('2026-02-28');await page.locator(F('reference')).fill(key('FEB'));
    await page.locator(F('opening_balance')).fill('1390');await page.locator(F('closing_balance')).fill('1440');await page.locator(F('prepared_by')).fill('المحاسب');await page.locator(F('reviewed_by')).fill('المراجع');
    for(const [index,entry]of [{date:'2026-02-02',amount:'50',direction:'deposit'},{date:'2026-02-03',amount:'7',direction:'deposit'},{date:'2026-02-04',amount:'7',direction:'withdrawal'}].entries()){
      await page.locator(A('add-line')).click();await page.locator(L('date',index)).fill(entry.date);await page.locator(L('reference',index)).fill('FEB-'+index);await page.locator(L('description',index)).fill('تصفية حركة دفترية قائمة');
      if(entry.direction==='withdrawal')await page.locator(`${A('line-direction')}[data-direction="withdrawal"][data-index="${index}"]`).click();await page.locator(L('amount',index)).fill(entry.amount);
    }
    preview=await clicked(page,A('preview'),'bank-reconciliation-preview');
    assert.deepEqual(preview.book_lines.map(row=>row.line_id||row.id).sort((a,b)=>a-b),[ids.transit,ids.zeroIn,ids.zeroOut].sort((a,b)=>a-b));
    await match(page,[1],[ids.transit]);await match(page,[2],[ids.zeroIn]);await match(page,[3],[ids.zeroOut]);
    const secondDraft=await clicked(page,A('save'),'bank-reconciliations');const second=await clicked(page,A('confirm'),`bank-reconciliations/${secondDraft.id}/confirm`);
    assert.equal(plan(second).unmatched_book_lines.length,0);assert.equal(plan(second).totals.difference_cents,0);await close(page);
    const voidPayload=doc=>({request_key:key('VOID'),revision:doc.revision,event_revision:doc.adjustment_revision||0,business_date:'2026-03-02',reference:key('VOID-REF'),reason:'تصحيح محضر مراجعة مستقل في اختبار معزول'});
    await rejects(page,`bank-reconciliations/${first.id}/void`,'POST',voidPayload(first));
    const voidedSecond=await api(page,`bank-reconciliations/${second.id}/void`,'POST',voidPayload(second));assert.ok(voidedSecond.void);
    const voidedFirst=await api(page,`bank-reconciliations/${first.id}/void`,'POST',voidPayload(first));assert.ok(voidedFirst.void);
    assert.equal(plan(voidedFirst).totals.book_closing_cents,144000,'Void leaves original evidence frozen');

    // A source bank payment is NET after withholding and is never copied by matching.
    const openingInvoice=initial.lines.find(row=>row.kind==='supplier_invoice').document_id;
    const withholding=await api(page,'withholding-codes','POST',{name:'خصم اختبار البنك '+stamp,rate:'3',active:true,confirmed:true});
    const paid=await post(page,'supplier-payments',{supplier_id:supplier.id,payment_date:'2026-01-12',amount:'100',method:'bank_transfer',funding_account_id:other.id,allocation_mode:'invoice',target_invoice_id:openingInvoice,withholding_code_id:withholding.id,details:{bank_name:'بنك اختبار',beneficiary:supplier.name,beneficiary_account:'TEST-ACCOUNT',transaction_reference:key('AP-BANK'),value_date:'2026-01-12'}});
    assert.equal(paid.net_cash_cents,9700);
    const otherPreview=await api(page,'bank-reconciliation-preview','POST',{account_id:other.id,from_date:'2026-01-01',to_date:'2026-01-31',reference:key('OTHER'),opening_balance:'500',closing_balance:'428',prepared_by:'محاسب',reviewed_by:'مراجع',lines:[{date:'2026-01-10',reference:'OTHER-25',description:'متحصل',amount:'25'},{date:'2026-01-12',reference:'OTHER-97',description:'سداد مورد بالصافي',amount:'-97'}],matches:[],opening_outstanding_line_ids:[]});
    const apLines=otherPreview.book_lines.filter(row=>row.source_type==='supplier_payment'&&row.source_id===paid.id);assert.equal(apLines.length,1);assert.equal(signed(apLines[0]),-9700);

    // The global monthly lock still applies after a reconciliation is voided.
    await api(page,'accounting-periods/2026-01/close','POST',{request_key:key('CLOSE-MONTH'),revision:1,reference:key('MONTH'),reason:'مراجعة الحركات في قاعدة اختبار معزولة'});
    const oldBankData={...lockedPayload,request_key:key('MONTH-LOCK'),reference:key('MONTH-LOCK-REF')};await rejects(page,'bank-documents','POST',oldBankData);
    await page.evaluate(id=>BankReconciliation.openDocument(id),first.id);await page.waitForSelector(A('print'));const afterVoidPrint=await printed(page,[reference]);assert.ok(afterVoidPrint.includes(reference));assert.ok(originalPrint.includes(money(144000)));await close(page);

    await page.evaluate(id=>BankReconciliation.newDocument({account_id:id}),bank.id);await page.waitForSelector(F('reference'));await page.locator(F('reference')).fill('تعديل لم يحفظ');
    page.once('dialog',dialog=>dialog.dismiss());await page.keyboard.press('Escape');assert.ok(await page.locator(W).evaluate(element=>element.open));
    page.once('dialog',dialog=>dialog.accept());await page.keyboard.press('Escape');assert.equal(await page.locator(W).evaluate(element=>element.open),false);
    await page.route('**/api/bank-reconciliation-config',async route=>{await new Promise(resolve=>setTimeout(resolve,250));await route.continue();});
    await page.evaluate(()=>{BankReconciliation.open();});await page.waitForSelector(`${W}[open]`);await page.locator(A('close')).first().click();await page.waitForTimeout(400);assert.equal(await page.locator(W).evaluate(element=>element.open),false);await page.unroute('**/api/bank-reconciliation-config');
    await page.evaluate(()=>BankReconciliation.open());await page.waitForSelector(A('new'));
    for(const width of [1024,768,390]){await page.setViewportSize({width,height:900});assert.ok((await page.locator(W).boundingBox()).width<=width+1);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);}
    await close(page);assert.deepEqual(errors,[]);assert.ok((await api(page,'trial-balance?from_date=2026-01-01&to_date=2026-12-31')).balanced);
    console.log('PASS bank reconciliation UI: mapped CSV and manual rows, grouped matches, missing fee resolution, net-zero stale evidence, pending carry, immutable nonposting certificates/print, NET AP source, account/month locks, latest void, dirty/late response/mobile.');
  }catch(error){if(page)try{await page.screenshot({path:path.join(__dirname,'bank-reconciliation-failure.png'),fullPage:true});}catch{}throw error;}
  finally{if(browser)await browser.close();server.kill('SIGTERM');if(server.exitCode===null)await new Promise(resolve=>server.once('exit',resolve));fs.rmSync(temporary,{recursive:true,force:true});}
})().catch(error=>{console.error(error);process.exitCode=1;});
