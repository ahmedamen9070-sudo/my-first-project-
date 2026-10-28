"""Operator-configured supplier withholding at payment, in exact EGP cents.

No rates, taxpayer obligations, tax remittances or electronic filings are inferred.
Payment amounts settle gross AP; the supplier receives gross less withholding.
The caller owns every transaction. Saved posted snapshots are immutable.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import accounting
import payables

BASIS = 'allocated_net_exclusive_vat'
MAX_CENTS = 10**13


def _has(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _id(value):
    if type(value) is not int or value <= 0:
        raise ValueError('اختيار كود الخصم غير صحيح')
    return value


def _cents(value, zero=True):
    if type(value) is not int or not (0 if zero else 1) <= value <= MAX_CENTS:
        raise ValueError('مبلغ الخصم أو الوعاء غير صحيح')
    return value


def _rate(value):
    if isinstance(value, bool):
        raise ValueError('نسبة الخصم غير صحيحة')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 < number <= 100 or number != number.quantize(Decimal('.01')):
            raise ValueError()
        return int(number * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('أدخل نسبة خصم أكبر من صفر وحتى ١٠٠ وبحد أقصى منزلتين عشريتين') from None


def _percent(rate):
    return format(Decimal(rate) / 100, '.2f')


def _rounded(numerator, denominator):
    return (numerator * 2 + denominator) // (denominator * 2)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def initialize(db):
    for sql in (
        '''CREATE TABLE IF NOT EXISTS withholding_codes(
            id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,name TEXT NOT NULL UNIQUE,
            rate_basis INTEGER NOT NULL CHECK(rate_basis>0 AND rate_basis<=10000),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN(0,1)),
            confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN(0,1)),notes TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS payment_withholding(
            payment_id INTEGER PRIMARY KEY REFERENCES supplier_payments(id),
            withholding_code_id INTEGER NOT NULL REFERENCES withholding_codes(id),
            code_revision INTEGER NOT NULL,code TEXT NOT NULL,name TEXT NOT NULL,
            rate_basis INTEGER NOT NULL CHECK(rate_basis>0 AND rate_basis<=10000),
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            base_cents INTEGER NOT NULL CHECK(base_cents>=0),withholding_cents INTEGER NOT NULL CHECK(withholding_cents>=0),
            net_cash_cents INTEGER NOT NULL CHECK(net_cash_cents>0),gross_cents INTEGER NOT NULL CHECK(gross_cents>0),
            supplier_id INTEGER NOT NULL REFERENCES suppliers(id),supplier_name TEXT NOT NULL,
            supplier_code TEXT NOT NULL,supplier_tax_number TEXT NOT NULL DEFAULT '',
            basis TEXT NOT NULL CHECK(basis='allocated_net_exclusive_vat'),prepared_fingerprint TEXT NOT NULL,
            CHECK(net_cash_cents+withholding_cents=gross_cents))''',
        '''CREATE TABLE IF NOT EXISTS payment_withholding_lines(
            payment_id INTEGER NOT NULL REFERENCES payment_withholding(payment_id) ON DELETE CASCADE,
            invoice_id INTEGER NOT NULL REFERENCES invoices(id),invoice_code TEXT NOT NULL,invoice_reference TEXT NOT NULL,
            invoice_net_cents INTEGER NOT NULL CHECK(invoice_net_cents>=0),
            invoice_gross_cents INTEGER NOT NULL CHECK(invoice_gross_cents>0),
            allocated_cents INTEGER NOT NULL CHECK(allocated_cents>0),base_cents INTEGER NOT NULL CHECK(base_cents>=0),
            withholding_cents INTEGER NOT NULL CHECK(withholding_cents>=0),PRIMARY KEY(payment_id,invoice_id),
            CHECK(base_cents<=invoice_net_cents))''',
        'CREATE INDEX IF NOT EXISTS payment_withholding_invoice ON payment_withholding_lines(invoice_id,payment_id)',
    ):
        db.execute(sql)
    for table in ('payment_withholding', 'payment_withholding_lines'):
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()}
                BEFORE {operation} ON {table}
                WHEN EXISTS(SELECT 1 FROM supplier_payments WHERE id=OLD.payment_id AND status='posted')
                BEGIN SELECT RAISE(ABORT,'posted withholding snapshot is immutable'); END''')
        for operation in ('INSERT', 'UPDATE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_new_parent_{operation.lower()}
                BEFORE {operation} ON {table}
                WHEN EXISTS(SELECT 1 FROM supplier_payments WHERE id=NEW.payment_id AND status='posted')
                BEGIN SELECT RAISE(ABORT,'posted withholding snapshot is immutable'); END''')


def _code(row):
    result = dict(row)
    result.update(rate=_percent(result['rate_basis']),active=bool(result['active']),confirmed=bool(result['confirmed']),basis=BASIS)
    return result


def snapshot(db):
    return {'codes': [_code(row) for row in db.execute('SELECT * FROM withholding_codes ORDER BY id')] if _has(db, 'withholding_codes') else [],
            'basis': BASIS, 'at_payment': True,
            'notes': ['الخصم على الجزء المسدد من صافي الفاتورة دون ضريبة القيمة المضافة.',
                      'كود واحد لكل سند؛ المقدمات والسداد المختلط مع المقدمات لا يخصم منها في هذه المرحلة.',
                      'النسب والحساب يراجعها المشغل؛ لا نسب أو التزامات ضريبية مفترضة.']}


def _payload(data, old=None):
    if not isinstance(data, dict) or set(data) - {'name','rate','active','confirmed','notes','revision'}:
        raise ValueError('حقول كود الخصم غير صحيحة')
    old = old or {}
    name = accounting._text(data,'name',160,True) if 'name' in data or not old else old['name']
    if not old and 'rate' not in data:
        raise ValueError('أدخل نسبة الخصم صراحةً')
    rate = _rate(data.get('rate', _percent(old.get('rate_basis',0))))
    active, confirmed = data.get('active',bool(old.get('active',1))), data.get('confirmed',bool(old.get('confirmed',0)))
    if type(active) is not bool or type(confirmed) is not bool:
        raise ValueError('حالة كود الخصم غير صحيحة')
    notes = accounting._text(data,'notes',2000) if 'notes' in data else old.get('notes','')
    return name,rate,int(active),int(confirmed),notes


def create_code(db,data):
    values = _payload(data)
    if db.execute('SELECT 1 FROM withholding_codes WHERE name=?',(values[0],)).fetchone():
        raise ValueError('يوجد كود خصم بنفس الاسم')
    db.execute("INSERT INTO sequences(kind,value) VALUES('withholding_code',1) ON CONFLICT(kind) DO UPDATE SET value=value+1")
    code = 'WHT-' + f"{db.execute('SELECT value FROM sequences WHERE kind=?',('withholding_code',)).fetchone()[0]:06d}"
    stamp = accounting._now()
    ident = db.execute('''INSERT INTO withholding_codes(code,name,rate_basis,active,confirmed,notes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?)''',(code,*values,stamp,stamp)).lastrowid
    accounting._audit(db,'withholding_code',ident,'create_withholding_code',{'code':code,'rate_basis':values[1]})
    return _code(db.execute('SELECT * FROM withholding_codes WHERE id=?',(ident,)).fetchone())


def update_code(db,code_id,data):
    row = db.execute('SELECT * FROM withholding_codes WHERE id=?',(_id(code_id),)).fetchone()
    if row is None:
        raise ValueError('كود الخصم غير موجود')
    old = dict(row)
    accounting._revision(data,old)
    values = _payload(data,old)
    if db.execute('SELECT 1 FROM withholding_codes WHERE name=? AND id<>?',(values[0],code_id)).fetchone():
        raise ValueError('يوجد كود خصم بنفس الاسم')
    db.execute('UPDATE withholding_codes SET name=?,rate_basis=?,active=?,confirmed=?,notes=?,revision=revision+1,updated_at=? WHERE id=?',(*values,accounting._now(),code_id))
    accounting._audit(db,'withholding_code',code_id,'update_withholding_code',{'rate_basis':values[1],'previous_rate_basis':old['rate_basis']})
    return _code(db.execute('SELECT * FROM withholding_codes WHERE id=?',(code_id,)).fetchone())


def _account(db,value):
    account = db.execute('''SELECT a.*,m.status mapping_status FROM account_mappings m JOIN accounts a ON a.id=m.account_id
        WHERE m.module='الضرائب' AND m.role='withholding_payable' ''').fetchone()
    if account is None or not account['active'] or not account['posting'] or account['normal_side']!='credit' or not account['code'].startswith('2') or account['mapping_status']!='confirmed':
        raise ValueError('راجع وأكد ربط حساب الخصم والتحصيل المستحق للغير قبل استخدام الخصم')
    forbidden = {row[0] for row in db.execute("SELECT account_id FROM account_mappings WHERE role IN('payable_control','capital_payable_control','supplier_advances','issued_cheques')")}
    if _has(db,'invoice_postings'):
        forbidden.update(row[0] for row in db.execute('SELECT DISTINCT payable_account_id FROM invoice_postings'))
    if _has(db,'supplier_payments') and _has(db,'journal_lines'):
        # Original cheque liability accounts remain distinct even after today's
        # mapping changes. The saved WHT credit is excluded from this lookup.
        forbidden.update(row[0] for row in db.execute('''SELECT DISTINCT l.account_id
            FROM supplier_payments p JOIN journal_lines l ON l.journal_id=p.journal_entry_id
            WHERE p.method='cheque' AND p.status='posted' AND l.credit_cents>0
            AND NOT EXISTS(SELECT 1 FROM payment_withholding w WHERE w.payment_id=p.id AND w.account_id=l.account_id)'''))
    if value.get('funding_account_id') is not None:
        forbidden.add(value['funding_account_id'])
    if account['id'] in forbidden:
        raise ValueError('حساب الخصم المستحق يجب أن يكون مستقلًا عن الموردين والمقدمات والشيكات ومصدر السداد')
    return dict(account)


def _used_base(db,invoice_id,as_of,exclude_payment_id):
    if not _has(db,'payment_withholding_lines'):
        return 0
    clause, values = payables._payment_effective_filter(db,'p',as_of)
    return db.execute('''SELECT COALESCE(SUM(w.base_cents),0) FROM payment_withholding_lines w
        JOIN supplier_payments p ON p.id=w.payment_id WHERE w.invoice_id=? AND p.status='posted'
        AND p.payment_date<=? AND p.id<>?''' + clause,(invoice_id,as_of,exclude_payment_id or 0,*values)).fetchone()[0]


def prepare(db,value,allocations,exclude_payment_id=None):
    """Compute a reviewed prospective snapshot without changing a draft or ledger."""
    amount = _cents(value.get('amount_cents'),False)
    selected = value.get('withholding_code_id')
    empty = {'withholding_code_id':None,'withholding_cents':0,'withholding_base_cents':0,'net_cash_cents':amount,'withholding':None}
    if selected is None or selected == '':
        return empty
    code = db.execute('SELECT * FROM withholding_codes WHERE id=?',(_id(selected),)).fetchone() if _has(db,'withholding_codes') else None
    if code is None or not code['active'] or not code['confirmed']:
        raise ValueError('اختر كود خصم نشطًا تمت مراجعته وتأكيده')
    supplier = db.execute('SELECT * FROM suppliers WHERE id=?',(_id(value.get('supplier_id')),)).fetchone()
    if supplier is None:
        raise ValueError('المورد غير موجود')
    payment_date = payables._date(value.get('payment_date'),'تاريخ السداد')
    if not isinstance(allocations,list) or not allocations:
        raise ValueError('الخصم متاح للسداد المخصص لفواتير فقط؛ سجل الدفعة المقدمة في سند مستقل')
    allocated = sum(_cents(line.get('amount_cents'),False) for line in allocations if isinstance(line,dict))
    if len([line for line in allocations if isinstance(line,dict)]) != len(allocations) or allocated != amount:
        raise ValueError('مع الخصم يجب تخصيص كامل إجمالي السداد للفواتير دون دفعة مقدمة')
    account = _account(db,value)
    import supplier_settlements
    dates = supplier_settlements.capacity_dates(db,payment_date)
    lines, seen = [],set()
    for allocation in allocations:
        invoice_id = _id(allocation.get('invoice_id'))
        if invoice_id in seen:
            raise ValueError('الفاتورة مكررة في وعاء الخصم')
        seen.add(invoice_id)
        invoice = db.execute('''SELECT i.* FROM invoices i JOIN invoice_postings ip ON ip.invoice_id=i.id
            WHERE i.id=? AND i.supplier_id=? AND i.business_date<=? AND i.status<>'cancelled' ''',(invoice_id,supplier['id'],payment_date)).fetchone()
        if invoice is None or invoice['total_cents']<=0:
            raise ValueError('وعاء الخصم يرتبط بفاتورة معتمدة موجبة للمورد في تاريخ السداد')
        net,gross = _cents(invoice['subtotal_cents']),_cents(invoice['total_cents'],False)
        if net>gross:
            raise ValueError('صافي الفاتورة يزيد عن إجماليها؛ راجع مستند المصدر')
        allocated_cents = allocation['amount_cents']
        if allocated_cents>gross:
            raise ValueError('المخصص يتجاوز إجمالي الفاتورة')
        remaining = min(net-_used_base(db,invoice_id,day,exclude_payment_id) for day in dates)
        if remaining<0:
            raise ValueError('وعاء الخصم السابق يتجاوز صافي الفاتورة؛ راجع المستندات المرتبطة')
        base = min(_rounded(allocated_cents*net,gross),remaining)
        withheld = _rounded(base*code['rate_basis'],10000)
        lines.append({'invoice_id':invoice_id,'invoice_code':invoice['code'],'invoice_reference':invoice['reference'],
                      'invoice_net_cents':net,'invoice_gross_cents':gross,'allocated_cents':allocated_cents,
                      'base_cents':base,'withholding_cents':withheld})
    lines.sort(key=lambda item:item['invoice_id'])
    total,base = sum(line['withholding_cents'] for line in lines),sum(line['base_cents'] for line in lines)
    if total>=amount:
        raise ValueError('صافي السداد للمورد بعد الخصم يجب أن يكون أكبر من صفر')
    plan = {'withholding_code_id':code['id'],'code_revision':code['revision'],'code':code['code'],'name':code['name'],
            'rate_basis':code['rate_basis'],'account_id':account['id'],'base_cents':base,'withholding_cents':total,
            'net_cash_cents':amount-total,'gross_cents':amount,'supplier_id':supplier['id'],'supplier_name':supplier['name'],
            'supplier_code':supplier['code'],'supplier_tax_number':supplier['tax_number'],'basis':BASIS,'lines':lines}
    plan['prepared_fingerprint'] = _digest(plan)
    return {'withholding_code_id':code['id'],'withholding_cents':total,'withholding_base_cents':base,
            'net_cash_cents':amount-total,'withholding':{**plan,'rate':_percent(code['rate_basis']),
                'account_code':account['code'],'account_name':account['name']}}


SNAP_FIELDS = ('withholding_code_id','code_revision','code','name','rate_basis','account_id','base_cents',
               'withholding_cents','net_cash_cents','gross_cents','supplier_id','supplier_name','supplier_code',
               'supplier_tax_number','basis','prepared_fingerprint')
LINE_FIELDS = ('invoice_id','invoice_code','invoice_reference','invoice_net_cents','invoice_gross_cents',
               'allocated_cents','base_cents','withholding_cents')


def save_snapshot(db,payment_id,prepared):
    payment = db.execute('SELECT status FROM supplier_payments WHERE id=?',(_id(payment_id),)).fetchone()
    if payment is None or payment['status']!='draft':
        raise ValueError('نسخة خصم السداد المعتمد أو الملغى لا تعدل')
    if not _has(db,'payment_withholding'):
        if prepared.get('withholding') is not None:
            raise ValueError('إعداد الخصم لم يهيأ بعد')
        return
    db.execute('DELETE FROM payment_withholding_lines WHERE payment_id=?',(payment_id,))
    db.execute('DELETE FROM payment_withholding WHERE payment_id=?',(payment_id,))
    plan = prepared.get('withholding')
    if plan is None:
        return
    db.execute(f"INSERT INTO payment_withholding(payment_id,{','.join(SNAP_FIELDS)}) VALUES(?,{','.join('?' for _ in SNAP_FIELDS)})",(payment_id,*(plan[field] for field in SNAP_FIELDS)))
    for line in plan['lines']:
        db.execute(f"INSERT INTO payment_withholding_lines(payment_id,{','.join(LINE_FIELDS)}) VALUES(?,{','.join('?' for _ in LINE_FIELDS)})",(payment_id,*(line[field] for field in LINE_FIELDS)))


def _stored(db,payment_id):
    if not _has(db,'payment_withholding'):
        return None
    row = db.execute('SELECT * FROM payment_withholding WHERE payment_id=?',(payment_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result['lines'] = [dict(line) for line in db.execute('SELECT * FROM payment_withholding_lines WHERE payment_id=? ORDER BY invoice_id',(payment_id,))]
    for line in result['lines']:
        line.pop('payment_id',None)
    return result


def validate_for_post(db,payment_id,prepared):
    stored,plan = _stored(db,payment_id),prepared.get('withholding')
    if stored is None and plan is None:
        return
    if stored is None or plan is None:
        raise accounting.ConflictError('تغير اختيار الخصم؛ أعد مراجعة السداد وحفظه قبل الاعتماد')
    if any(stored[field]!=plan[field] for field in SNAP_FIELDS) or stored['lines']!=plan['lines']:
        raise accounting.ConflictError('تغير كود الخصم أو الحساب أو الوعاء بعد حفظ المسودة؛ راجع السداد وأعد حفظه قبل الاعتماد')


def enrich_payment(db,result):
    result = dict(result)
    plan = _stored(db,result['id'])
    result.update(withholding_code_id=None,withholding_cents=0,withholding_base_cents=0,net_cash_cents=result['amount_cents'],withholding=None)
    if plan is None:
        return result
    account = db.execute('SELECT code,name FROM accounts WHERE id=?',(plan['account_id'],)).fetchone()
    plan.update(rate=_percent(plan['rate_basis']),account_code=account['code'] if account else '',account_name=account['name'] if account else '',
                certificate_code='WHT-'+result['code'])
    plan.pop('prepared_fingerprint',None)
    result.update(withholding_code_id=plan['withholding_code_id'],withholding_cents=plan['withholding_cents'],
                  withholding_base_cents=plan['base_cents'],net_cash_cents=plan['net_cash_cents'],withholding=plan)
    return result


def net_cash(db,payment_id,fallback):
    plan = _stored(db,payment_id)
    return plan['net_cash_cents'] if plan else fallback


def _range(from_date,to_date):
    if to_date is None:
        try:
            to_date = datetime.now(ZoneInfo('Africa/Cairo')).date().isoformat()
        except ZoneInfoNotFoundError:
            to_date = date.today().isoformat()
    to_date = payables._date(to_date,'نهاية التقرير')
    from_date = payables._date(from_date or to_date[:4]+'-01-01','بداية التقرير')
    if from_date>to_date:
        raise ValueError('بداية التقرير لا تتجاوز نهايته')
    return from_date,to_date


def report(db,from_date=None,to_date=None,supplier_id=None):
    from_date,to_date = _range(from_date,to_date)
    if supplier_id is not None:
        supplier_id = _id(supplier_id)
        if db.execute('SELECT 1 FROM suppliers WHERE id=?',(supplier_id,)).fetchone() is None:
            raise ValueError('المورد غير موجود')
    rows = []
    if _has(db,'payment_withholding'):
        for raw in db.execute('''SELECT w.*,p.code payment_code,p.payment_date,p.method,p.status,p.journal_entry_id FROM payment_withholding w
            JOIN supplier_payments p ON p.id=w.payment_id WHERE p.status='posted' AND (? IS NULL OR w.supplier_id=?)
            AND p.payment_date<=? ORDER BY p.payment_date,p.id''',(supplier_id,supplier_id,to_date)):
            original = dict(raw)
            base = {key:original[key] for key in ('payment_id','supplier_id','supplier_name','supplier_code','supplier_tax_number',
                      'withholding_code_id','name','rate_basis','account_id','basis')}
            base.update(tax_code=original['code'],rate=_percent(original['rate_basis']),payment_code=original['payment_code'],
                        certificate_code='WHT-'+original['payment_code'],method=original['method'])
            account = db.execute('SELECT code,name FROM accounts WHERE id=?',(original['account_id'],)).fetchone()
            base.update(account_code=account['code'] if account else '',account_name=account['name'] if account else '')
            if original['payment_date']>=from_date:
                rows.append({**base,'date':original['payment_date'],'business_date':original['payment_date'],
                             'type':'payment','source_type':'supplier_payment','source_id':original['payment_id'],'journal_id':original['journal_entry_id'],
                             'code':original['payment_code'],'original_code':original['payment_code'],
                             'base_cents':original['base_cents'],'withholding_cents':original['withholding_cents'],
                             'gross_cents':original['gross_cents'],'net_cash_cents':original['net_cash_cents']})
            if _has(db,'supplier_payment_reversals'):
                reversal = db.execute('SELECT * FROM supplier_payment_reversals WHERE payment_id=? AND business_date>=? AND business_date<=?',(original['payment_id'],from_date,to_date)).fetchone()
                if reversal:
                    rows.append({**base,'date':reversal['business_date'],'business_date':reversal['business_date'],
                                 'type':'reversal','source_type':'supplier_payment_reversal','source_id':reversal['id'],'journal_id':reversal['journal_id'],
                                 'code':f"SPR-{reversal['id']:06d}",'original_code':original['payment_code'],
                                 'reference':reversal['reference'],'reason':reversal['reason'],
                                 'base_cents':-original['base_cents'],'withholding_cents':-original['withholding_cents'],
                                 'gross_cents':-original['gross_cents'],'net_cash_cents':-original['net_cash_cents']})
    rows.sort(key=lambda item:(item['date'],item['payment_id'],item['type']=='reversal'))
    amount_fields = ('base_cents','withholding_cents','gross_cents','net_cash_cents')
    totals = {key:sum(row[key] for row in rows) for key in amount_fields}
    totals.update(payment_count=sum(row['type']=='payment' for row in rows),reversal_count=sum(row['type']=='reversal' for row in rows))
    return {'from_date':from_date,'to_date':to_date,'supplier_id':supplier_id,'currency':accounting.get_settings(db)['base_currency'],
            'basis':BASIS,'rows':rows,'totals':totals,
            'notes':['حركة الخصم من سندات السداد المعتمدة وعكسها بتاريخ كل مستند وبنسب وحسابات محفوظة.',
                     'إصدار الشيك يثبت الخصم عند السداد؛ صرفه أو ارتداده يعرض الأثر المحاسبي المرتبط.',
                     'لا يمثل التقرير توريد الضريبة أو إقرارًا مقدمًا للجهة المختصة.']}
