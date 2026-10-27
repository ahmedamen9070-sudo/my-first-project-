'use strict';
// Supplier master choices stay close to the record; codes and posted prices are automatic.
function dueFromTerms(ds, days) {
  if (days === null || days === undefined || !ds) return '';
  const d = new Date(ds + 'T12:00:00Z');
  d.setUTCDate(d.getUTCDate() + Number(days));
  return d.toISOString().slice(0, 10);
}
function categoryChoices(selected, onChange) {
  const wrapper = document.createElement('div');
  wrapper.className = 'full master-options';
  wrapper.innerHTML = '<span class="master-label">أنواع التوريد • يمكن اختيار أكثر من نوع</span><div class="choices">' + (state.categories || []).map(c => `<button type="button" class="choice ${selected.includes(c.id) ? 'selected' : ''}" aria-pressed="${selected.includes(c.id)}" data-category="${c.id}">${esc(c.name)}</button>`).join('') + '</div>';
  wrapper.onclick = e => {
    const b = e.target.closest('[data-category]'); if (!b) return;
    const id = +b.dataset.category, index = selected.indexOf(id);
    if (index < 0) selected.push(id); else selected.splice(index, 1);
    b.classList.toggle('selected', index < 0); b.setAttribute('aria-pressed', String(index < 0));
    onChange?.(selected);
  };
  return wrapper;
}
function supplierMaster(id = null) {
  const s = state.suppliers.find(s => s.id === id) || {};
  const categories = [...(s.category_ids || [])];
  form(id ? 'مورد • تعديل البيانات والتوريد' : 'مورد جديد • الكود يتولد تلقائيًا', [
    {name:'name',label:'اسم المورد',required:true,value:s.name},
    {name:'phone',label:'الهاتف',max:40,value:s.phone},
    {name:'tax_number',label:'رقم التسجيل الضريبي إن وجد',max:60,value:s.tax_number},
    {name:'terms_days',label:'مدة الأجل بالأيام • اتركها فارغة إن لم تتحدد',type:'number',value:s.terms_days??''},
    {name:'notes',label:'ملاحظات',max:2000,value:s.notes},
  ], data => api('suppliers'+(id?'/'+id:''), id?'PUT':'POST', {...data,terms_days:data.terms_days===''?null:Number(data.terms_days),category_ids:categories}), id?()=>supplierFile(id):null);
  const terms = $('#record-fields [name=terms_days]'); terms.step = '1'; terms.max = '3650';
  $('#record-fields').append(categoryChoices(categories));
}
function productMaster(supplierId = null, productId = null) {
  const restoreFile=Boolean(supplierId||productId);
  if (!state.suppliers.length) return toast('أضف موردًا أولًا.');
  const old = state.products.find(p => p.id === productId);
  if (old) supplierId = old.supplier_id;
  const chooseDepartment = supplier => picker('القسم الافتراضي للبند', state.departments.map(d=>({id:d.id,label:d.name,detail:d.cost_code})), department => edit(supplier, department));
  function edit(supplier, department) {
    let categoryId=old?.category_id||null, accountId=old?.expense_account_id||null;
    form('بند مصروف أو خدمة • '+supplier.label, [
      {name:'name',label:'اسم البند أو الخدمة',required:true,value:old?.name},
      {name:'unit',label:'الوحدة (مثال: شهر، زيارة، قطعة)',required:true,max:30,value:old?.unit},
      {name:'price',label:old?.last_purchase_date?'آخر سعر شراء مرحّل • يتحدث من اعتماد الفواتير':'السعر الأولي بالعملة الأساسية دون ضريبة',type:'number',value:String((old?.price_cents||0)/100),required:true},
    ], data => api('products'+(old?'/'+old.id:''),old?'PUT':'POST',{...data,supplier_id:supplier.id,department_id:department.id,category_id:categoryId,expense_account_id:accountId}), restoreFile?()=>supplierFile(supplier.id):null);
    if (old?.last_purchase_date) $('#record-fields [name=price]').readOnly=true;
    const extra=document.createElement('div'); extra.className='full master-options';
    const supplierRecord=state.suppliers.find(s=>s.id===supplier.id);
    const categories=(state.categories||[]).filter(c=>(supplierRecord.category_ids||[]).includes(c.id));
    extra.innerHTML='<span class="master-label">نوع التوريد لهذا البند</span><div class="choices master-product-categories">'+categories.map(c=>`<button type="button" class="choice ${c.id===categoryId?'selected':''}" data-item-category="${c.id}">${esc(c.name)}</button>`).join('')+'</div>'+(categories.length?'':'<p class="master-hint">أضف أنواع التوريد من بيانات المورد قبل تحديد نوع البند.</p>')+'<span class="master-label">حساب تكلفة المصروف أو الخدمة • مطلوب قبل ترحيل الفاتورة</span><button type="button" class="choice master-account">'+(accountId?'الحساب مرتبط • تغيير الحساب':'اختيار حساب من الشجرة')+'</button><p class="master-hint">القسم الافتراضي: '+esc(department.label)+' • '+esc(department.detail||'')+'<br>العملة EGP. اعتماد الفاتورة يثبت سعر الشراء المرجعي؛ المسودة والمرتجع لا يغيرانه.</p>';
    extra.onclick=async e=>{
      const category=e.target.closest('[data-item-category]');
      if(category){categoryId=+category.dataset.itemCategory;extra.querySelectorAll('[data-item-category]').forEach(b=>b.classList.toggle('selected',+b.dataset.itemCategory===categoryId))}
      const b=e.target.closest('.master-account'); if(!b)return;
      try {const chart=await api('accounting');picker('حساب تكلفة البند',chart.accounts.filter(a=>a.active&&a.posting&&a.normal_side==='debit'&&['5','6','8'].includes(a.code[0])).map(a=>({id:a.id,label:a.name,detail:a.code})),a=>{accountId=a.id;b.textContent=a.detail+' • '+a.label})}catch(err){toast(err.message)}
    };
    $('#record-fields').append(extra);
  }
  if(supplierId){const s=state.suppliers.find(s=>s.id===supplierId);if(!s)return toast('المورد غير موجود');chooseDepartment({id:s.id,label:s.name})}
  else picker('اختر المورد لتعريف بند له',state.suppliers.map(s=>({id:s.id,label:s.name,detail:s.code})),chooseDepartment);
}
function priceDifference(line) {
  const p=state.products.find(p=>p.id===line.product_id);
  const baseline=Object.prototype.hasOwnProperty.call(line,'previous_price_cents')?line.previous_price_cents:p?.last_purchase_price_cents;
  if(baseline===null||baseline===undefined)return '<small class="price-comparison neutral">أول شراء • لا يوجد سعر مرحّل سابق</small>';
  const delta=Math.round(Number(line.price)*100)-baseline;
  return `<small class="price-comparison ${delta>0?'higher':delta<0?'lower':'neutral'}">آخر شراء ${money(baseline)} • ${delta>0?'أعلى بمقدار '+money(delta):delta<0?'أقل بمقدار '+money(-delta):'نفس السعر'}</small>`;
}
$('#post-invoice').onclick=async()=>{
  if(busy)return;if(dirty)return toast('احفظ التعديلات أولًا، ثم اعتمد النسخة المحفوظة.');
  busy=true;$('#invoice-error').textContent='';renderEntry();
  try{const r=await api('invoices/'+draft.id+'/post','POST',{revision:draft.revision});draft={...r,lines:r.lines.map(l=>({...l,price:l.price_cents/100}))};dirty=false;await refresh();renderEntry();toast('تم اعتماد الفاتورة وترحيل قيدها المحاسبي')}catch(e){$('#invoice-error').textContent=e.message}finally{busy=false;renderEntry()}
};
$('#return-invoice').onclick=()=>SupplierAP.returns(draft.id);
$('#pay-invoice').onclick=()=>SupplierAP.payment(draft.supplier_id,draft.id);
$('#invoice-journal').onclick=async()=>{
  try{const journal=await api('journals/'+draft.journal_id);const lines=journal.lines||[];show('القيد المحاسبي • '+(journal.code||''),`<p>تاريخ القيد: ${esc(journal.entry_date)} • ${esc(draft.code)}</p><div class="table-scroll"><table><tr><th>الحساب</th><th>مدين</th><th>دائن</th></tr>${lines.map(l=>`<tr><td>${esc(l.account_code||'')} • ${esc(l.account_name||l.account_id)}</td><td>${money(l.debit_cents)}</td><td>${money(l.credit_cents)}</td></tr>`).join('')}</table></div>`)}catch(e){toast(e.message)}
};
