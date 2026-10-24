#!/usr/bin/env python3
"""A&M Accounting Hotel for Audit &Control development application: single operator, isolated draft purchasing workflow."""
import argparse, json, os, sqlite3, secrets, mimetypes, shutil, tempfile
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
    if decimal(data.get('tax_rate','0'),'100')!=0: raise ValueError('الضرائب لم تضبط بعد؛ احفظ الفاتورة دون ضريبة مؤقتًا')
    supplier=require(db,'suppliers',data.get('supplier_id'))
    dep=require(db,'departments',data.get('department_id'))
    ref=txt(data,'reference',100,True)
    ds=txt(data,'business_date',10,True)
    try: parsed=date.fromisoformat(ds)
    except ValueError: raise ValueError('تاريخ التشغيل غير صحيح')
    if parsed.isoformat()!=ds: raise ValueError('صيغة التاريخ غير صحيحة')
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
    return (supplier['id'],ref,ds,dep['id'],txt(data,'notes',2000),sub,int(tax*100),tax_cents,sub+tax_cents),lines

def invoice(db,pk):
    r=db.execute('''SELECT i.*,s.name supplier_name,s.code supplier_code,d.name department_name,d.cost_code FROM invoices i JOIN suppliers s ON s.id=i.supplier_id JOIN departments d ON d.id=i.department_id WHERE i.id=?''',(pk,)).fetchone()
    if not r: raise ValueError('الفاتورة غير موجودة')
    r=dict(r);r['lines']=[dict(x) for x in db.execute('SELECT product_id,name,unit,quantity,price_cents,total_cents FROM invoice_lines WHERE invoice_id=? ORDER BY id',(pk,))]
    return r

class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args): pass
    def send(self,status,data,ctype='application/json; charset=utf-8'):
        raw=json.dumps(data,ensure_ascii=False).encode() if 'json' in ctype else data
        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','same-origin');self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        path=urlparse(self.path).path
        try:
            if path.startswith('/api/'):
                with connect() as db:
                    if path=='/api/bootstrap':
                        return self.send(200,{'csrf':TOKEN,'departments':[dict(r) for r in db.execute('SELECT * FROM departments ORDER BY id')],'suppliers':[dict(r) for r in db.execute('SELECT * FROM suppliers ORDER BY id DESC')],'products':[dict(r) for r in db.execute('SELECT * FROM products ORDER BY id DESC')],'mode':'development','currency':'غير محددة','currency_configured':False})
                    if path=='/api/dashboard':
                        r=dict(db.execute("SELECT COUNT(*) count,COALESCE(SUM(total_cents),0) total_cents FROM invoices WHERE status='draft'").fetchone())
                        r['suppliers']=db.execute('SELECT COUNT(*) FROM suppliers').fetchone()[0];r['products']=db.execute('SELECT COUNT(*) FROM products').fetchone()[0]
                        r['recent']=[dict(x) for x in db.execute('SELECT i.id,i.code,i.reference,i.total_cents,i.status,s.name supplier_name FROM invoices i JOIN suppliers s ON s.id=i.supplier_id ORDER BY i.updated_at DESC,i.id DESC LIMIT 8')]
                        return self.send(200,r)
                    if path=='/api/invoices':
                        args=parse_qs(urlparse(self.path).query);q=args.get('q',[''])[0][:100];supplier=args.get('supplier_id',[''])[0]
                        rows=db.execute('''SELECT i.*,s.name supplier_name,d.name department_name,d.cost_code FROM invoices i JOIN suppliers s ON s.id=i.supplier_id JOIN departments d ON d.id=i.department_id WHERE (i.code LIKE ? OR i.reference LIKE ? OR s.name LIKE ?) AND (?='' OR i.supplier_id=?) ORDER BY i.id DESC LIMIT 500''',('%'+q+'%',)*3+(supplier,supplier))
                        return self.send(200,[dict(r) for r in rows])
                    if path.startswith('/api/invoices/'):
                        return self.send(200,invoice(db,int(path.rsplit('/',1)[1])))
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
            path=urlparse(self.path).path
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if path=='/api/suppliers' and method=='POST':
                    name=txt(data,'name',150,True)
                    if db.execute('SELECT 1 FROM suppliers WHERE name=?',(name,)).fetchone(): raise ValueError('المورد مسجل بنفس الاسم')
                    c=code(db,'supplier','SUP');cur=db.execute('INSERT INTO suppliers(code,name,phone,tax_number,notes,created_at) VALUES(?,?,?,?,?,?)',(c,name,txt(data,'phone',40),txt(data,'tax_number',60),txt(data,'notes',2000),now()));pk=cur.lastrowid
                    audit(db,'supplier',pk,'create',{'code':c});result=dict(require(db,'suppliers',pk))
                elif path=='/api/products' and method=='POST':
                    sup=require(db,'suppliers',data.get('supplier_id'));dep=require(db,'departments',data.get('department_id'))
                    c=code(db,'product','ITM');cur=db.execute('INSERT INTO products(code,supplier_id,name,unit,price_cents,department_id) VALUES(?,?,?,?,?,?)',(c,sup['id'],txt(data,'name',150,True),txt(data,'unit',30,True),cents(data.get('price')),dep['id']));pk=cur.lastrowid
                    audit(db,'product',pk,'create',{'code':c});result=dict(require(db,'products',pk))
                elif path=='/api/departments' and method=='POST':
                    c=code(db,'department','DEP-N');cost=code(db,'cost','CC-N');cur=db.execute('INSERT INTO departments(code,name,cost_code) VALUES(?,?,?)',(c,txt(data,'name',100,True),cost));pk=cur.lastrowid
                    audit(db,'department',pk,'create',{'code':c,'cost_code':cost});result=dict(require(db,'departments',pk))
                elif path=='/api/invoices' and method=='POST':
                    key=txt(data,'request_key',100,True)
                    old=db.execute('SELECT id FROM invoices WHERE request_key=?',(key,)).fetchone()
                    if old: return self.send(200,invoice(db,old['id']))
                    values,lines=invoice_payload(db,data);stamp=now();c=code(db,'invoice','PIN')
                    cur=db.execute("INSERT INTO invoices(code,request_key,supplier_id,reference,business_date,department_id,notes,subtotal_cents,tax_basis,tax_cents,total_cents,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)",(c,key,*values,stamp,stamp));pk=cur.lastrowid
                    db.executemany('INSERT INTO invoice_lines(invoice_id,product_id,name,unit,quantity,price_cents,total_cents) VALUES(?,?,?,?,?,?,?)',[(pk,*l) for l in lines]);db.execute('UPDATE invoices SET kind=? WHERE id=?',(data.get('kind','expense'),pk));audit(db,'invoice',pk,'create_draft',{'code':c,'total_cents':values[-1]});result=invoice(db,pk)
                elif path.startswith('/api/invoices/') and method=='PUT':
                    pk=int(path.rsplit('/',1)[1]);old=require(db,'invoices',pk)
                    if old['status']!='draft': raise ValueError('الفاتورة الملغاة لا تعدل')
                    if data.get('revision')!=old['revision']: return self.send(409,{'error':'الفاتورة تغيرت من نافذة أخرى. أعد فتحها قبل التعديل.'})
                    if data.get('action')=='cancel':
                        db.execute("UPDATE invoices SET status='cancelled',revision=revision+1,updated_at=? WHERE id=?",(now(),pk));audit(db,'invoice',pk,'cancel_draft',{'code':old['code']})
                    else:
                        values,lines=invoice_payload(db,data)
                        db.execute('UPDATE invoices SET supplier_id=?,reference=?,business_date=?,department_id=?,notes=?,subtotal_cents=?,tax_basis=?,tax_cents=?,total_cents=?,revision=revision+1,updated_at=? WHERE id=?',(*values,now(),pk))
                        db.execute('DELETE FROM invoice_lines WHERE invoice_id=?',(pk,));db.executemany('INSERT INTO invoice_lines(invoice_id,product_id,name,unit,quantity,price_cents,total_cents) VALUES(?,?,?,?,?,?,?)',[(pk,*l) for l in lines]);db.execute('UPDATE invoices SET kind=? WHERE id=?',(data.get('kind','expense'),pk));audit(db,'invoice',pk,'update_draft',{'previous_total_cents':old['total_cents'],'total_cents':values[-1]})
                    result=invoice(db,pk)
                elif path=='/api/backups' and method=='POST':
                    backup_dir=DB.parent/'backups';backup_dir.mkdir(exist_ok=True);dest=backup_dir/(datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-')+secrets.token_hex(3)+'.sqlite')
                    with sqlite3.connect(dest) as target,connect() as source:
                        source.backup(target)
                        if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('فشل التحقق من النسخة الاحتياطية')
                    audit(db,'backup',0,'create',{'filename':dest.name});result={'filename':dest.name,'verified':True}
                else: return self.send(404,{'error':'المسار غير موجود'})
            return self.send(200,result)
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
