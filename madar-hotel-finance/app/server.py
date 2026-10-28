#!/usr/bin/env python3
"""A&M Accounting Hotel for Audit &Control development application: single operator, isolated draft purchasing workflow."""
import argparse, json, os, sqlite3, secrets, mimetypes, shutil, tempfile
import accounting, payables, supplier_payments, supplier_settlements, supplier_adjustments, taxation, accounting_periods, ledger_reports
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get('MADAR_DB', ROOT/'data'/'madar.sqlite'))
STATIC = ROOT/'static'
TOKEN = secrets.token_urlsafe(32)

def connect():
    db=sqlite3.connect(DB, timeout=20)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db

def init():
    DB.parent.mkdir(parents=True,exist_ok=True)
    with connect() as db:
        db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS sequences(kind TEXT PRIMARY KEY, value INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS suppliers(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '', tax_number TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS departments(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, cost_code TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, supplier_id INTEGER NOT NULL REFERENCES suppliers(id), name TEXT NOT NULL, unit TEXT NOT NULL, price_cents INTEGER NOT NULL CHECK(price_cents>=0), department_id INTEGER NOT NULL REFERENCES departments(id), UNIQUE(supplier_id,name));
        CREATE TABLE IF NOT EXISTS invoices(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, request_key TEXT UNIQUE NOT NULL, supplier_id INTEGER NOT NULL REFERENCES suppliers(id), reference TEXT NOT NULL, business_date TEXT NOT NULL, department_id INTEGER NOT NULL REFERENCES departments(id), notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL CHECK(status IN ('draft','cancelled')), subtotal_cents INTEGER NOT NULL, tax_basis INTEGER NOT NULL, tax_cents INTEGER NOT NULL, total_cents INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS active_supplier_reference ON invoices(supplier_id,reference) WHERE status='draft';
        CREATE TABLE IF NOT EXISTS invoice_lines(id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE, product_id INTEGER NOT NULL REFERENCES products(id), name TEXT NOT NULL, unit TEXT NOT NULL, quantity TEXT NOT NULL, price_cents INTEGER NOT NULL, total_cents INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, entity TEXT NOT NULL, entity_id INTEGER NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL);
        ''')
        columns={r['name'] for r in db.execute('PRAGMA table_info(invoices)')}
        if 'kind' not in columns: db.execute("ALTER TABLE invoices ADD COLUMN kind TEXT NOT NULL DEFAULT 'expense'")
        for row in [('DEP-001','المطبخ الرئيسي','CC-KIT'),('DEP-002','المطعم','CC-REST'),('DEP-003','خدمة الغرف','CC-ROOM')]:
            db.execute('INSERT OR IGNORE INTO departments(code,name,cost_code) VALUES(?,?,?)',row)
        accounting.initialize(db)
        payables.initialize(db)
        supplier_payments.initialize(db)
        supplier_settlements.initialize(db)
        supplier_adjustments.initialize(db)
        taxation.initialize(db)
        accounting_periods.initialize(db)

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def code(db,kind,prefix):
    db.execute('INSERT INTO sequences(kind,value) VALUES(?,1) ON CONFLICT(kind) DO UPDATE SET value=value+1',(kind,))
    return f'{prefix}-{db.execute("SELECT value FROM sequences WHERE kind=?",(kind,)).fetchone()[0]:06d}'
def audit(db,entity,ident,action,details):
    db.execute('INSERT INTO audit(entity,entity_id,action,details,created_at) VALUES(?,?,?,?,?)',(entity,ident,action,json.dumps(details,ensure_ascii=False),now()))
def txt(data,key,maxlen=200,required=False):
    value=data.get(key,'')
    if not isinstance(value,str): raise ValueError('صيغة حقل غير صحيحة: '+key)
    value=value.strip()
    if (required and not value) or len(value)>maxlen: raise ValueError('راجع الحقل: '+key)
    return value

def ident(value):
    if isinstance(value,bool) or not isinstance(value,int) or value<1: raise ValueError('اختيار غير صحيح')
    return value

def decimal(value,maxval='100000000'):
    if isinstance(value,bool): raise ValueError('قيمة رقمية غير صحيحة')
    try: x=Decimal(str(value))
    except (InvalidOperation,ValueError): raise ValueError('قيمة رقمية غير صحيحة')
    if not x.is_finite() or x<0 or x>Decimal(maxval): raise ValueError('القيمة خارج النطاق المسموح')
    return x

def cents(value):
    x=decimal(value)
    if x!=x.quantize(Decimal('.01')): raise ValueError('السعر بحد أقصى منزلتين عشريتين')
    return int(x*100)

def require(db,table,pk):
    r=db.execute(f'SELECT * FROM {table} WHERE id=?',(ident(pk),)).fetchone()
    if not r: raise ValueError('العنصر المطلوب غير موجود')
    return r

def invoice_payload(db,data):
    if data.get('kind','expense') not in ('expense','service'): raise ValueError('نوع العملية غير صحيح')
    supplier=require(db,'suppliers',data.get('supplier_id'))
    dep=require(db,'departments',data.get('department_id'))
    ref=txt(data,'reference',100,True)
    ds=txt(data,'business_date',10,True)
    try: parsed=date.fromisoformat(ds)
    except ValueError: raise ValueError('تاريخ التشغيل غير صحيح')
    if parsed.isoformat()!=ds: raise ValueError('صيغة التاريخ غير صحيحة')
    accounting_periods.assert_open(db,ds)
    tax=decimal(data.get('tax_rate','0'),'100')
    if tax!=tax.quantize(Decimal('.01')): raise ValueError('نسبة الضريبة بمنزلتين كحد أقصى')
    rows=data.get('lines')
    if not isinstance(rows,list) or not 1<=len(rows)<=100: raise ValueError('أضف من صنف إلى ١٠٠ صنف')
    lines=[];seen=set()
    for line in rows:
        if not isinstance(line,dict): raise ValueError('بند غير صحيح')
        product=require(db,'products',line.get('product_id'))
        if product['supplier_id']!=supplier['id']: raise ValueError('الصنف غير مرتبط بالمورد المختار')
        if product['id'] in seen: raise ValueError('الصنف مكرر؛ عدّل كمية البند الموجود')
        seen.add(product['id'])
        qty=decimal(line.get('quantity'),'1000000')
        if qty<=0 or qty!=qty.quantize(Decimal('.001')): raise ValueError('الكمية موجبة وبحد أقصى ٣ منازل عشرية')
        price=cents(line.get('price'))
        total=int((qty*price).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
        lines.append((product['id'],product['name'],product['unit'],str(qty),price,total))
    sub=sum(l[-1] for l in lines)
    if sub>10**13: raise ValueError('إجمالي الفاتورة يتجاوز النطاق المسموح')
    tax_cents=int((Decimal(sub)*tax/100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    values=(supplier['id'],ref,ds,dep['id'],txt(data,'notes',2000),sub,int(tax*100),tax_cents,sub+tax_cents)
    values,plan=taxation.prepare_invoice(db,data,values,lines)
    return values,lines,plan

def invoice(db,pk):
    r=db.execute('''SELECT i.*,s.name supplier_name,s.code supplier_code,d.name department_name,d.cost_code FROM invoices i JOIN suppliers s ON s.id=i.supplier_id JOIN departments d ON d.id=i.department_id WHERE i.id=?''',(pk,)).fetchone()
    if not r: raise ValueError('الفاتورة غير موجودة')
    return payables.enrich_invoice(db,dict(r))

def invoice_retry(db,pk,data):
    """Compare a replay without re-reading mutable product defaults or names."""
    old=invoice(db,pk)
    kind=data.get('kind','expense')
    if kind not in ('expense','service'): raise ValueError('نوع العملية غير صحيح')
    supplier_id=ident(data.get('supplier_id'));department_id=ident(data.get('department_id'))
    reference=txt(data,'reference',100,True);business_date=txt(data,'business_date',10,True)
    try: parsed=date.fromisoformat(business_date)
    except ValueError: raise ValueError('تاريخ التشغيل غير صحيح')
    if parsed.isoformat()!=business_date: raise ValueError('صيغة التاريخ غير صحيحة')
    tax=decimal(data.get('tax_rate','0'),'100')
    if tax!=tax.quantize(Decimal('.01')): raise ValueError('نسبة الضريبة بمنزلتين كحد أقصى')
    incoming_code=data.get('tax_code_id')
    if incoming_code is not None: ident(incoming_code)
    if incoming_code!=old.get('tax_code_id'):
        raise accounting.ConflictError('مفتاح الحفظ مستخدم لمعالجة ضريبية أخرى')
    if incoming_code is None and tax:
        raise ValueError('اختر كود ضريبة مضبوطًا؛ النسب اليدوية غير متاحة')
    if incoming_code is not None and tax not in (0,Decimal(old['tax_basis'])/100):
        raise accounting.ConflictError('نسبة ضريبة إعادة الطلب تختلف عن النسخة المحفوظة')
    rows=data.get('lines')
    if not isinstance(rows,list) or not 1<=len(rows)<=100: raise ValueError('أضف من صنف إلى ١٠٠ صنف')
    normalized=[];seen=set();subtotal=0;tax_cents=0
    for line in rows:
        if not isinstance(line,dict): raise ValueError('بند غير صحيح')
        product_id=ident(line.get('product_id'))
        if product_id in seen: raise ValueError('الصنف مكرر؛ عدّل كمية البند الموجود')
        seen.add(product_id)
        quantity=decimal(line.get('quantity'),'1000000')
        if quantity<=0 or quantity!=quantity.quantize(Decimal('.001')): raise ValueError('الكمية موجبة وبحد أقصى ٣ منازل عشرية')
        price=cents(line.get('price'))
        net=int((quantity*price).quantize(Decimal('1'),rounding=ROUND_HALF_UP));subtotal+=net
        tax_cents+=int((Decimal(net)*old['tax_basis']/10000).quantize(Decimal('1'),rounding=ROUND_HALF_UP)) if incoming_code else 0
        normalized.append((product_id,quantity,price))
    if subtotal>10**13: raise ValueError('إجمالي الفاتورة يتجاوز النطاق المسموح')
    tax_basis=old['tax_basis'] if incoming_code else 0
    # The original save already derived an omitted due date from supplier terms.
    # Preserve that snapshot when terms are edited before a network retry.
    due=data.get('due_date',old['due_date'])
    if due=='': due=None
    if due is not None:
        if not isinstance(due,str): raise ValueError('تاريخ الاستحقاق غير صحيح')
        try: parsed_due=date.fromisoformat(due)
        except ValueError: raise ValueError('تاريخ الاستحقاق غير صحيح')
        if parsed_due.isoformat()!=due: raise ValueError('صيغة تاريخ الاستحقاق غير صحيحة')
    headers=(supplier_id,reference,business_date,department_id,txt(data,'notes',2000),subtotal,tax_basis,tax_cents,subtotal+tax_cents,kind,due)
    saved=tuple(old[key] for key in ('supplier_id','reference','business_date','department_id','notes','subtotal_cents','tax_basis','tax_cents','total_cents','kind','due_date'))
    old_lines=sorted((line['product_id'],Decimal(line['quantity']),line['price_cents']) for line in old['lines'])
    if headers!=saved or sorted(normalized)!=old_lines:
        raise accounting.ConflictError('مفتاح الحفظ مستخدم لفاتورة ببيانات أخرى؛ افتح المستند المحفوظ أو ابدأ مسودة جديدة')
    return old

def update_supplier(db,pk,data):
    old=require(db,'suppliers',pk)
    values={key:txt(data,key,limit,required) if key in data else old[key] for key,limit,required in [('name',150,True),('phone',40,False),('tax_number',60,False),('notes',2000,False)]}
    if db.execute('SELECT 1 FROM suppliers WHERE name=? AND id<>?',(values['name'],pk)).fetchone(): raise ValueError('المورد مسجل بنفس الاسم')
    db.execute('UPDATE suppliers SET name=?,phone=?,tax_number=?,notes=? WHERE id=?',(*values.values(),pk))
    result=payables.configure_supplier(db,pk,data)
    audit(db,'supplier',pk,'update',{'code':old['code']})
    return result

def update_product(db,pk,data):
    old=require(db,'products',pk)
    supplier_id=data.get('supplier_id',old['supplier_id']);require(db,'suppliers',supplier_id)
    if supplier_id!=old['supplier_id'] and db.execute('SELECT 1 FROM invoice_lines WHERE product_id=?',(pk,)).fetchone(): raise ValueError('لا يمكن نقل بند استخدم في فاتورة إلى مورد آخر')
    department_id=data.get('department_id',old['department_id']);require(db,'departments',department_id)
    name=txt(data,'name',150,True) if 'name' in data else old['name']
    unit=txt(data,'unit',30,True) if 'unit' in data else old['unit']
    price=cents(data['price']) if 'price' in data else old['price_cents']
    enriched=payables.enrich_product(db,dict(old))
    last_price=enriched.get('last_purchase_price_cents')
    if last_price is not None:
        if 'price' in data and price!=last_price: raise ValueError('آخر سعر شراء معتمد يتغير باعتماد فاتورة جديدة فقط')
        price=old['price_cents']
    db.execute('UPDATE products SET supplier_id=?,name=?,unit=?,price_cents=?,department_id=? WHERE id=?',(supplier_id,name,unit,price,department_id,pk))
    result=payables.configure_product(db,pk,data)
    audit(db,'product',pk,'update',{'code':old['code']})
    return result

class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args): pass
    def send(self,status,data,ctype='application/json; charset=utf-8',download_name=None):
        raw=json.dumps(data,ensure_ascii=False).encode() if 'json' in ctype else data
        self.send_response(status);
        if download_name:self.send_header('Content-Disposition','attachment; filename="'+download_name+'"')
        self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','same-origin');self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        parsed=urlparse(self.path);path=parsed.path;args=parse_qs(parsed.query);parts=path.strip('/').split('/')
        try:
            if path.startswith('/api/'):
                with connect() as db:
                    if path=='/api/accounting':return self.send(200,accounting.snapshot(db))
                    if path=='/api/trial-balance':
                        include_zero=args.get('include_zero',['0'])[0]
                        if include_zero not in ('0','1'):raise ValueError('اختيار عرض الحسابات الصفرية غير صحيح')
                        db.execute('BEGIN')
                        return self.send(200,ledger_reports.trial_balance(db,args.get('from_date',[None])[0],args.get('to_date',[None])[0],include_zero=='1'))
                    if path=='/api/account-ledger':
                        supplier=args.get('supplier_id',[''])[0]
                        db.execute('BEGIN')
                        return self.send(200,ledger_reports.account_ledger(db,ident(int(args.get('account_id',[''])[0])),args.get('from_date',[None])[0],args.get('to_date',[None])[0],ident(int(supplier)) if supplier else None))
                    if path=='/api/tax-config':return self.send(200,taxation.snapshot(db))
                    if path=='/api/tax-report':
                        end=args.get('to_date',args.get('as_of',[None]))[0]
                        start=args.get('from_date',[end[:4]+'-01-01' if end else None])[0]
                        return self.send(200,taxation.report(db,start,end))
                    if path=='/api/accounting-periods':return self.send(200,accounting_periods.snapshot(db,int(args['year'][0]) if args.get('year') else None))
                    if path=='/api/period-preview':return self.send(200,accounting_periods.preview(db,args.get('month',[''])[0]))
                    if path=='/api/accounts-export':return self.send(200,accounting.export_csv(db).encode('utf-8'),'text/csv; charset=utf-8','AM-chart-of-accounts.csv')
                    if path=='/api/bootstrap':
                        settings=accounting.get_settings(db)
                        masters=payables.master_data(db);tax_config=taxation.snapshot(db);masters.update(tax_codes=tax_config['codes'],tax_settings=tax_config['settings'])
                        return self.send(200,{**masters,'csrf':TOKEN,'departments':[dict(r) for r in db.execute('SELECT * FROM departments ORDER BY id')],'suppliers':[payables.enrich_supplier(db,dict(r)) for r in db.execute('SELECT * FROM suppliers ORDER BY id DESC')],'products':[payables.enrich_product(db,dict(r)) for r in db.execute('SELECT * FROM products ORDER BY id DESC')],'payment_methods':supplier_payments.payment_methods(),'mode':'development','currency':settings['base_currency'],'currency_configured':bool(settings['base_currency']),'accounting_settings':settings})
                    if path=='/api/dashboard':
                        r=dict(db.execute("SELECT COUNT(*) count,COALESCE(SUM(total_cents),0) total_cents FROM invoices i WHERE status='draft' AND NOT EXISTS(SELECT 1 FROM invoice_postings p WHERE p.invoice_id=i.id)").fetchone())
                        r['suppliers']=db.execute('SELECT COUNT(*) FROM suppliers').fetchone()[0];r['products']=db.execute('SELECT COUNT(*) FROM products').fetchone()[0]
                        r['recent']=[payables.enrich_invoice(db,dict(x)) for x in db.execute('SELECT i.*,s.name supplier_name FROM invoices i JOIN suppliers s ON s.id=i.supplier_id ORDER BY i.updated_at DESC,i.id DESC LIMIT 8')]
                        r['ledger_summary']=payables.ledger_summary(db);r.update(r['ledger_summary'])
                        return self.send(200,r)
                    if path=='/api/invoices':
                        q=args.get('q',[''])[0][:100];supplier=args.get('supplier_id',[''])[0]
                        rows=db.execute('''SELECT i.*,s.name supplier_name,d.name department_name,d.cost_code FROM invoices i JOIN suppliers s ON s.id=i.supplier_id JOIN departments d ON d.id=i.department_id WHERE (i.code LIKE ? OR i.reference LIKE ? OR s.name LIKE ?) AND (?='' OR i.supplier_id=?) ORDER BY i.id DESC LIMIT 500''',('%'+q+'%',)*3+(supplier,supplier))
                        return self.send(200,[payables.enrich_invoice(db,dict(r)) for r in rows])
                    if len(parts)==3 and parts[:2]==['api','invoices']:
                        return self.send(200,invoice(db,int(parts[2])))
                    if path=='/api/supplier-payments':
                        supplier=args.get('supplier_id',[''])[0]
                        return self.send(200,supplier_payments.list_payments(db,ident(int(supplier)) if supplier else None))
                    if len(parts)==3 and parts[:2]==['api','supplier-payments']:
                        return self.send(200,supplier_payments.get_payment(db,ident(int(parts[2]))))
                    if path=='/api/supplier-credit-sources':
                        return self.send(200,supplier_settlements.source_balances(db,ident(int(args.get('supplier_id',[''])[0])),args.get('as_of',[None])[0]))
                    if path=='/api/supplier-settlements':
                        supplier=args.get('supplier_id',[''])[0]
                        return self.send(200,supplier_settlements.list_settlements(db,ident(int(supplier)) if supplier else None))
                    if len(parts)==3 and parts[:2]==['api','supplier-settlements']:
                        return self.send(200,supplier_settlements.detail(db,ident(int(parts[2]))))
                    if path=='/api/supplier-refunds':
                        supplier=args.get('supplier_id',[''])[0]
                        return self.send(200,supplier_adjustments.list_refunds(db,ident(int(supplier)) if supplier else None))
                    if len(parts)==3 and parts[:2]==['api','supplier-refunds']:
                        return self.send(200,supplier_adjustments.get_refund(db,ident(int(parts[2])),args.get('as_of',[None])[0]))
                    if len(parts)==4 and parts[:2]==['api','supplier-payments'] and parts[3] in ('cheque-events','events'):
                        return self.send(200,[{key:value for key,value in row.items() if key!='request_fingerprint'} for row in supplier_adjustments.cheque_events(db,ident(int(parts[2])),args.get('as_of',[None])[0])])
                    if path=='/api/supplier-reconciliation':
                        import supplier_reconciliation
                        return self.send(200,supplier_reconciliation.report(db,args.get('as_of',[None])[0]))
                    if path=='/api/supplier-returns':
                        supplier=args.get('supplier_id',[''])[0]
                        return self.send(200,payables.list_returns(db,ident(int(supplier)) if supplier else None))
                    if len(parts)==3 and parts[:2]==['api','supplier-returns']:
                        return self.send(200,payables.return_detail(db,ident(int(parts[2]))))
                    if path=='/api/supplier-aging':
                        return self.send(200,payables.aging(db,args.get('as_of',[None])[0]))
                    if len(parts)==4 and parts[:2]==['api','suppliers'] and parts[3]=='statement':
                        return self.send(200,payables.statement(db,ident(int(parts[2])),args.get('as_of',[None])[0]))
                    if len(parts)==4 and parts[:2]==['api','suppliers'] and parts[3]=='open-invoices':
                        return self.send(200,payables.open_invoices(db,ident(int(parts[2])),args.get('as_of',[None])[0]))
                    if len(parts)==3 and parts[:2]==['api','journals']:
                        return self.send(200,payables.journal_detail(db,ident(int(parts[2]))))
                    if path=='/api/audit':
                        return self.send(200,[dict(r) for r in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 100')])
                return self.send(404,{'error':'المسار غير موجود'})
            f=(STATIC/('index.html' if path=='/' else path.lstrip('/'))).resolve()
            if not f.is_relative_to(STATIC.resolve()) or not f.is_file(): return self.send(404,{'error':'الملف غير موجود'})
            mime=mimetypes.guess_type(f.name)[0] or 'application/octet-stream'
            self.send(200,f.read_bytes(),mime+('; charset=utf-8' if mime.startswith('text/') or f.suffix=='.js' else ''))
        except (ValueError,sqlite3.Error) as exc:
            self.send(400,{'error':str(exc) if isinstance(exc,ValueError) else 'تعذر قراءة البيانات'})
    def do_POST(self): self.mutate('POST')
    def do_PUT(self): self.mutate('PUT')
    def mutate(self,method):
        if self.headers.get('X-Madar-CSRF')!=TOKEN: return self.send(403,{'error':'أعد تحميل الصفحة لتجديد جلسة العمل'})
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=250000: raise ValueError('حجم الطلب غير صالح')
            data=json.loads(self.rfile.read(size))
            if not isinstance(data,dict): raise ValueError('صيغة الطلب غير صالحة')
            path=urlparse(self.path).path;parts=path.strip('/').split('/')
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if path=='/api/tax-settings' and method=='PUT':
                    result=taxation.update_settings(db,data)
                elif path=='/api/tax-codes' and method=='POST':
                    result=taxation.create_code(db,data)
                elif len(parts)==3 and parts[:2]==['api','tax-codes'] and method=='PUT':
                    result=taxation.update_code(db,ident(int(parts[2])),data)
                elif path=='/api/tax-preview' and method=='POST':
                    result=taxation.preview(db,data)
                elif len(parts)==4 and parts[:2]==['api','accounting-periods'] and parts[3] in ('close','reopen') and method=='POST':
                    result=(accounting_periods.close if parts[3]=='close' else accounting_periods.reopen)(db,parts[2],data)
                elif path=='/api/accounts' and method=='POST':
                    result=accounting.create_account(db,data)
                elif path.startswith('/api/accounts/') and method=='PUT':
                    result=accounting.update_account(db,int(path.rsplit('/',1)[1]),data)
                elif path.startswith('/api/account-mappings/') and method=='PUT':
                    result=accounting.update_mapping(db,int(path.rsplit('/',1)[1]),data)
                elif path=='/api/accounting-settings' and method=='PUT':
                    result=accounting.update_settings(db,data)
                elif path=='/api/accounting/export' and method=='POST':
                    folder=DB.parent/'exports';folder.mkdir(exist_ok=True)
                    filename=datetime.now(timezone.utc).strftime('AM-accounts-%Y%m%d-%H%M%S-')+secrets.token_hex(3)+'.csv'
                    target=folder/filename
                    target.write_text(accounting.export_csv(db),encoding='utf-8')
                    audit(db,'account_export',0,'export_csv',{'filename':filename})
                    result={'filename':filename,'folder':'data/exports','path':str(target.resolve())}
                elif path=='/api/suppliers' and method=='POST':
                    name=txt(data,'name',150,True)
                    if db.execute('SELECT 1 FROM suppliers WHERE name=?',(name,)).fetchone(): raise ValueError('المورد مسجل بنفس الاسم')
                    c=code(db,'supplier','SUP');cur=db.execute('INSERT INTO suppliers(code,name,phone,tax_number,notes,created_at) VALUES(?,?,?,?,?,?)',(c,name,txt(data,'phone',40),txt(data,'tax_number',60),txt(data,'notes',2000),now()));pk=cur.lastrowid
                    audit(db,'supplier',pk,'create',{'code':c});result=payables.configure_supplier(db,pk,data)
                elif len(parts)==3 and parts[:2]==['api','suppliers'] and method=='PUT':
                    result=update_supplier(db,ident(int(parts[2])),data)
                elif path=='/api/products' and method=='POST':
                    sup=require(db,'suppliers',data.get('supplier_id'));dep=require(db,'departments',data.get('department_id'))
                    c=code(db,'product','ITM');cur=db.execute('INSERT INTO products(code,supplier_id,name,unit,price_cents,department_id) VALUES(?,?,?,?,?,?)',(c,sup['id'],txt(data,'name',150,True),txt(data,'unit',30,True),cents(data.get('price')),dep['id']));pk=cur.lastrowid
                    audit(db,'product',pk,'create',{'code':c});result=payables.configure_product(db,pk,data)
                elif len(parts)==3 and parts[:2]==['api','products'] and method=='PUT':
                    result=update_product(db,ident(int(parts[2])),data)
                elif path=='/api/departments' and method=='POST':
                    c=code(db,'department','DEP-N');cost=code(db,'cost','CC-N');cur=db.execute('INSERT INTO departments(code,name,cost_code) VALUES(?,?,?)',(c,txt(data,'name',100,True),cost));pk=cur.lastrowid
                    audit(db,'department',pk,'create',{'code':c,'cost_code':cost});result=dict(require(db,'departments',pk))
                elif path=='/api/invoices' and method=='POST':
                    key=txt(data,'request_key',100,True)
                    old=db.execute('SELECT id FROM invoices WHERE request_key=?',(key,)).fetchone()
                    if old: return self.send(200,invoice_retry(db,old['id'],data))
                    values,lines,tax_plan=invoice_payload(db,data);stamp=now();c=code(db,'invoice','PIN')
                    cur=db.execute("INSERT INTO invoices(code,request_key,supplier_id,reference,business_date,department_id,notes,subtotal_cents,tax_basis,tax_cents,total_cents,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)",(c,key,*values,stamp,stamp));pk=cur.lastrowid
                    db.executemany('INSERT INTO invoice_lines(invoice_id,product_id,name,unit,quantity,price_cents,total_cents) VALUES(?,?,?,?,?,?,?)',[(pk,*l) for l in lines]);db.execute('UPDATE invoices SET kind=? WHERE id=?',(data.get('kind','expense'),pk));taxation.save_invoice_tax(db,pk,tax_plan);payables.set_invoice_terms(db,pk,data);audit(db,'invoice',pk,'create_draft',{'code':c,'total_cents':values[-1]});result=invoice(db,pk)
                elif len(parts)==4 and parts[:2]==['api','invoices'] and parts[3]=='post' and method=='POST':
                    result=payables.post_invoice(db,ident(int(parts[2])),data)
                elif len(parts)==3 and parts[:2]==['api','invoices'] and method=='PUT':
                    pk=ident(int(parts[2]));old=invoice(db,pk)
                    if old['status']!='draft': raise ValueError('الفاتورة المعتمدة أو الملغاة لا تعدل؛ استخدم مستند المرتجع عند الحاجة')
                    accounting_periods.assert_open(db,old['business_date'])
                    if isinstance(data.get('revision'),bool) or not isinstance(data.get('revision'),int) or data.get('revision')!=old['revision']: raise accounting.ConflictError('الفاتورة تغيرت من نافذة أخرى. أعد فتحها قبل التعديل.')
                    if data.get('action')=='cancel':
                        db.execute("UPDATE invoices SET status='cancelled',revision=revision+1,updated_at=? WHERE id=?",(now(),pk));audit(db,'invoice',pk,'cancel_draft',{'code':old['code']})
                    else:
                        values,lines,tax_plan=invoice_payload(db,data)
                        db.execute('UPDATE invoices SET supplier_id=?,reference=?,business_date=?,department_id=?,notes=?,subtotal_cents=?,tax_basis=?,tax_cents=?,total_cents=?,revision=revision+1,updated_at=? WHERE id=?',(*values,now(),pk))
                        db.execute('DELETE FROM invoice_lines WHERE invoice_id=?',(pk,));db.executemany('INSERT INTO invoice_lines(invoice_id,product_id,name,unit,quantity,price_cents,total_cents) VALUES(?,?,?,?,?,?,?)',[(pk,*l) for l in lines]);db.execute('UPDATE invoices SET kind=? WHERE id=?',(data.get('kind','expense'),pk));taxation.save_invoice_tax(db,pk,tax_plan);payables.set_invoice_terms(db,pk,data);audit(db,'invoice',pk,'update_draft',{'previous_total_cents':old['total_cents'],'total_cents':values[-1]})
                    result=invoice(db,pk)
                elif path in ('/api/supplier-payment-preview','/api/payment-preview') and method=='POST':
                    result=supplier_payments.allocation_preview(db,data)
                elif path=='/api/supplier-payments' and method=='POST':
                    result=supplier_payments.save_payment(db,data)
                elif len(parts)==3 and parts[:2]==['api','supplier-payments'] and method=='PUT':
                    result=supplier_payments.update_payment(db,ident(int(parts[2])),data)
                elif len(parts)==4 and parts[:2]==['api','supplier-payments'] and parts[3]=='post' and method=='POST':
                    result=supplier_payments.approve_payment(db,ident(int(parts[2])),data)
                elif len(parts)==4 and parts[:2]==['api','supplier-payments'] and parts[3]=='clear' and method=='POST':
                    result=supplier_payments.clear_cheque(db,ident(int(parts[2])),data)
                elif path in ('/api/settlement-preview','/api/supplier-settlement-preview') and method=='POST':
                    result=supplier_settlements.preview(db,data)
                elif path=='/api/supplier-settlements' and method=='POST':
                    result=supplier_settlements.save(db,data)
                elif len(parts)==3 and parts[:2]==['api','supplier-settlements'] and method=='PUT':
                    result=supplier_settlements.save(db,data,ident(int(parts[2])))
                elif len(parts)==4 and parts[:2]==['api','supplier-settlements'] and parts[3]=='post' and method=='POST':
                    result=supplier_settlements.approve(db,ident(int(parts[2])),data)
                elif len(parts)==4 and parts[:2]==['api','supplier-settlements'] and parts[3]=='reverse' and method=='POST':
                    result=supplier_settlements.reverse(db,ident(int(parts[2])),data)
                elif path in ('/api/refund-preview','/api/supplier-refund-preview') and method=='POST':
                    result=supplier_adjustments.refund_preview(db,data)
                elif path=='/api/supplier-refunds' and method=='POST':
                    result=supplier_adjustments.save_refund(db,data)
                elif len(parts)==3 and parts[:2]==['api','supplier-refunds'] and method=='PUT':
                    result=supplier_adjustments.save_refund(db,data,ident(int(parts[2])))
                elif len(parts)==4 and parts[:2]==['api','supplier-refunds'] and parts[3]=='post' and method=='POST':
                    result=supplier_adjustments.approve_refund(db,ident(int(parts[2])),data)
                elif len(parts)==4 and parts[:2]==['api','supplier-refunds'] and parts[3]=='reverse' and method=='POST':
                    result=supplier_adjustments.reverse_refund(db,ident(int(parts[2])),data)
                elif len(parts)==4 and parts[:2]==['api','supplier-payments'] and parts[3]=='reverse' and method=='POST':
                    pk=ident(int(parts[2]));supplier_adjustments.reverse_payment(db,pk,data);result=supplier_payments.get_payment(db,pk)
                elif len(parts)==4 and parts[:2]==['api','supplier-payments'] and parts[3]=='cheque-event' and method=='POST':
                    pk=ident(int(parts[2]));supplier_adjustments.cheque_event(db,pk,data);result=supplier_payments.get_payment(db,pk)
                elif path in ('/api/supplier-return-preview','/api/return-preview') and method=='POST':
                    payload={**data,'reference':data.get('reference') or 'معاينة مرتجع'}
                    original,values,return_lines=payables._return_payload(db,payload)
                    preview_lines=[]
                    for line_id,quantity,gross in return_lines:
                        line=dict(require(db,'invoice_lines',line_id))
                        amounts=taxation.return_line_amounts(db,line_id,quantity)
                        if amounts is None: amounts={'net_cents':gross,'tax_cents':0,'recoverable_cents':0,'nonrecoverable_cents':0,'gross_cents':gross}
                        preview_lines.append({'invoice_line_id':line_id,'name':line['name'],'unit':line['unit'],'quantity':quantity,'price_cents':line['price_cents'],'total_cents':gross,**amounts})
                    result={'invoice_id':original['id'],'business_date':values[2],'total_cents':values[-1],'gross_cents':values[-1],'net_cents':sum(x['net_cents'] for x in preview_lines),'subtotal_cents':sum(x['net_cents'] for x in preview_lines),'tax_cents':sum(x['tax_cents'] for x in preview_lines),'lines':preview_lines}
                elif path=='/api/supplier-returns' and method=='POST':
                    if 'id' in data: raise ValueError('استخدم تعديل مستند المرتجع المحفوظ')
                    result=payables.save_return(db,data)
                elif len(parts)==3 and parts[:2]==['api','supplier-returns'] and method=='PUT':
                    result=payables.save_return(db,{**data,'id':ident(int(parts[2]))})
                elif len(parts)==4 and parts[:2]==['api','supplier-returns'] and parts[3]=='post' and method=='POST':
                    result=payables.approve_return(db,ident(int(parts[2])),data)
                elif path=='/api/backups' and method=='POST':
                    backup_dir=DB.parent/'backups';backup_dir.mkdir(exist_ok=True);dest=backup_dir/(datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-')+secrets.token_hex(3)+'.sqlite')
                    with sqlite3.connect(dest) as target,connect() as source:
                        source.backup(target)
                        if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('فشل التحقق من النسخة الاحتياطية')
                    audit(db,'backup',0,'create',{'filename':dest.name});result={'filename':dest.name,'verified':True}
                else: return self.send(404,{'error':'المسار غير موجود'})
            return self.send(200,result)
        except accounting.ConflictError as exc:
            return self.send(409,{'error':str(exc)})
        except sqlite3.IntegrityError:
            return self.send(409,{'error':'بيانات مكررة: راجع اسم الصنف أو رقم فاتورة المورد'})
        except (ValueError,TypeError,KeyError,InvalidOperation,json.JSONDecodeError) as exc:
            return self.send(400,{'error':str(exc) if isinstance(exc,ValueError) else 'راجع البيانات المطلوبة'})
        except sqlite3.Error:
            return self.send(500,{'error':'تعذر حفظ البيانات؛ لم تكتمل العملية'})

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8090);parser.add_argument('--host',default='127.0.0.1');args=parser.parse_args();init()
    print(f'A&M development server on {args.host}:{args.port}; isolated database {DB}',flush=True)
    ThreadingHTTPServer((args.host,args.port),Handler).serve_forever()
