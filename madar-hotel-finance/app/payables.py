"""Supplier subledger, immutable approvals, returns and dated payable reports.

The caller owns its transaction. Drafts never create accounting movements. Money is
integer cents and return quantities always refer to the original invoice line.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import accounting
import taxation
import accounting_periods

MAX_CENTS = 10**13
CATEGORIES = ('أغذية', 'مشروبات', 'مجمدات', 'منظفات', 'مستلزمات فندقية', 'خدمات', 'أخرى')
BUCKETS = ('current_cents', 'days_1_30_cents', 'days_31_60_cents', 'days_61_90_cents', 'days_over_90_cents')


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _id(value):
    if type(value) is not int or value < 1:
        raise ValueError('معرّف غير صحيح')
    return value


def _require(db, table, pk):
    row = db.execute(f'SELECT * FROM {table} WHERE id=?', (_id(pk),)).fetchone()
    if row is None:
        raise ValueError('العنصر المطلوب غير موجود')
    return dict(row)


def _text(data, key, maximum=200, required=False):
    value = data.get(key, '')
    if not isinstance(value, str):
        raise ValueError('صيغة غير صحيحة للحقل: ' + key)
    value = value.strip()
    if (required and not value) or len(value) > maximum:
        raise ValueError('راجع الحقل: ' + key)
    return value


def _date(value, label='التاريخ'):
    if not isinstance(value, str):
        raise ValueError(label + ' غير صحيح')
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(label + ' غير صحيح') from None
    if parsed.isoformat() != value:
        raise ValueError('صيغة ' + label + ' غير صحيحة')
    return value


def _as_of(value):
    return _date(value if value else date.today().isoformat(), 'تاريخ التقرير')


def _quantity(value):
    if isinstance(value, bool):
        raise ValueError('الكمية غير صحيحة')
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise ValueError('الكمية غير صحيحة') from None
    if not number.is_finite() or not 0 < number <= 1000000 or number != number.quantize(Decimal('.001')):
        raise ValueError('الكمية موجبة وبحد أقصى ٣ منازل عشرية')
    return number


def _quantity_text(value):
    return format(value.normalize(), 'f') if value else '0'


def _money(value, allow_zero=True):
    if type(value) is not int or value < (0 if allow_zero else 1) or value > MAX_CENTS:
        raise ValueError('المبلغ خارج النطاق المسموح')
    return value


def _revision(data, old):
    if type(data.get('revision')) is not int or data['revision'] != old['revision']:
        raise accounting.ConflictError('تغيرت البيانات من نافذة أخرى. أعد فتحها قبل الحفظ.')


def _code(db, kind, prefix):
    db.execute('INSERT INTO sequences(kind,value) VALUES(?,1) ON CONFLICT(kind) DO UPDATE SET value=value+1', (kind,))
    return f'{prefix}-{db.execute("SELECT value FROM sequences WHERE kind=?", (kind,)).fetchone()[0]:06d}'


def _audit(db, entity, ident, action, details):
    if _has_table(db, 'audit'):
        db.execute('INSERT INTO audit(entity,entity_id,action,details,created_at) VALUES(?,?,?,?,?)',
                   (entity, ident, action, json.dumps(details, ensure_ascii=False), _now()))


def _has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def initialize(db):
    """Add accounting tables without changing the legacy invoice status constraint."""
    statements = [
        'CREATE TABLE IF NOT EXISTS sequences(kind TEXT PRIMARY KEY,value INTEGER NOT NULL)',
        'CREATE TABLE IF NOT EXISTS supply_categories(id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE)',
        '''CREATE TABLE IF NOT EXISTS supplier_categories(supplier_id INTEGER NOT NULL REFERENCES suppliers(id),category_id INTEGER NOT NULL REFERENCES supply_categories(id),PRIMARY KEY(supplier_id,category_id))''',
        '''CREATE TABLE IF NOT EXISTS journal_entries(id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,source_type TEXT NOT NULL,source_id INTEGER NOT NULL,entry_date TEXT NOT NULL,created_at TEXT NOT NULL,sealed INTEGER NOT NULL DEFAULT 0 CHECK(sealed IN(0,1)),request_fingerprint TEXT NOT NULL,UNIQUE(source_type,source_id))''',
        '''CREATE TABLE IF NOT EXISTS journal_lines(id INTEGER PRIMARY KEY,journal_id INTEGER NOT NULL REFERENCES journal_entries(id),account_id INTEGER NOT NULL REFERENCES accounts(id),debit_cents INTEGER NOT NULL DEFAULT 0 CHECK(debit_cents>=0),credit_cents INTEGER NOT NULL DEFAULT 0 CHECK(credit_cents>=0),supplier_id INTEGER REFERENCES suppliers(id),department_id INTEGER REFERENCES departments(id),source_line_id INTEGER,CHECK((debit_cents>0 AND credit_cents=0) OR (credit_cents>0 AND debit_cents=0)))''',
        'CREATE INDEX IF NOT EXISTS journal_lines_supplier ON journal_lines(supplier_id,journal_id)',
        '''CREATE TABLE IF NOT EXISTS invoice_terms(invoice_id INTEGER PRIMARY KEY REFERENCES invoices(id),due_date TEXT)''',
        '''CREATE TABLE IF NOT EXISTS invoice_postings(invoice_id INTEGER PRIMARY KEY REFERENCES invoices(id),journal_id INTEGER NOT NULL UNIQUE REFERENCES journal_entries(id),payable_account_id INTEGER NOT NULL REFERENCES accounts(id),posted_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS invoice_posting_lines(invoice_line_id INTEGER PRIMARY KEY REFERENCES invoice_lines(id),invoice_id INTEGER NOT NULL REFERENCES invoices(id),product_id INTEGER NOT NULL REFERENCES products(id),expense_account_id INTEGER NOT NULL REFERENCES accounts(id),department_id INTEGER NOT NULL REFERENCES departments(id),previous_price_cents INTEGER)''',
        '''CREATE TABLE IF NOT EXISTS supplier_returns(id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,request_key TEXT UNIQUE,invoice_id INTEGER NOT NULL REFERENCES invoices(id),supplier_id INTEGER NOT NULL REFERENCES suppliers(id),business_date TEXT NOT NULL,reference TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',status TEXT NOT NULL CHECK(status IN('draft','posted','cancelled')),total_cents INTEGER NOT NULL CHECK(total_cents>=0),revision INTEGER NOT NULL DEFAULT 1,journal_id INTEGER UNIQUE REFERENCES journal_entries(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS active_return_reference ON supplier_returns(supplier_id,reference) WHERE status<>'cancelled' ''',
        '''CREATE TABLE IF NOT EXISTS supplier_return_lines(id INTEGER PRIMARY KEY,return_id INTEGER NOT NULL REFERENCES supplier_returns(id) ON DELETE CASCADE,invoice_line_id INTEGER NOT NULL REFERENCES invoice_lines(id),quantity TEXT NOT NULL,total_cents INTEGER NOT NULL CHECK(total_cents>=0),UNIQUE(return_id,invoice_line_id))''',
        'CREATE INDEX IF NOT EXISTS supplier_returns_invoice ON supplier_returns(invoice_id,status,business_date)',
    ]
    for statement in statements:
        db.execute(statement)
    for name in CATEGORIES:
        db.execute('INSERT OR IGNORE INTO supply_categories(name) VALUES(?)', (name,))
    for table, column, ddl in [
        ('suppliers', 'terms_days', 'INTEGER CHECK(terms_days BETWEEN 0 AND 3650)'),
        ('products', 'category_id', 'INTEGER REFERENCES supply_categories(id)'),
        ('products', 'expense_account_id', 'INTEGER REFERENCES accounts(id)'),
    ]:
        columns = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
        if column not in columns:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}')
    # Protect posted financial snapshots even from an old application path.
    for table, when in [
        ('invoices', 'EXISTS(SELECT 1 FROM invoice_postings WHERE invoice_id=OLD.id)'),
        ('invoice_lines', 'EXISTS(SELECT 1 FROM invoice_postings WHERE invoice_id=OLD.invoice_id)'),
        ('invoice_terms', 'EXISTS(SELECT 1 FROM invoice_postings WHERE invoice_id=OLD.invoice_id)'),
        ('invoice_postings', '1'), ('invoice_posting_lines', '1'),
        ('supplier_returns', "OLD.status='posted'"),
        ('supplier_return_lines', "EXISTS(SELECT 1 FROM supplier_returns WHERE id=OLD.return_id AND status='posted')"),
        ('journal_entries', 'OLD.sealed=1'),
        ('journal_lines', 'EXISTS(SELECT 1 FROM journal_entries WHERE id=OLD.journal_id AND sealed=1)'),
    ]:
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS ap_immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} WHEN {when} BEGIN SELECT RAISE(ABORT,'posted accounting document is immutable'); END''')
    for table, when in [
        ('invoice_lines', 'EXISTS(SELECT 1 FROM invoice_postings WHERE invoice_id=NEW.invoice_id)'),
        ('invoice_terms', 'EXISTS(SELECT 1 FROM invoice_postings WHERE invoice_id=NEW.invoice_id)'),
        ('supplier_return_lines', "EXISTS(SELECT 1 FROM supplier_returns WHERE id=NEW.return_id AND status='posted')"),
        ('journal_lines', 'EXISTS(SELECT 1 FROM journal_entries WHERE id=NEW.journal_id AND sealed=1)'),
    ]:
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS ap_immutable_{table}_insert BEFORE INSERT ON {table} WHEN {when} BEGIN SELECT RAISE(ABORT,'posted accounting document is immutable'); END''')


def _posting_account(db, account_id, expense=False):
    row = _require(db, 'accounts', account_id)
    if not row['active'] or not row['posting']:
        raise ValueError('الحساب يجب أن يكون حساب حركة نشطًا')
    if expense and (row['code'][0] not in ('5', '6', '8') or row['normal_side'] != 'debit'):
        raise ValueError('اربط الصنف بحساب تكلفة أو مصروف مدين من المجموعات ٥ أو ٦ أو ٨')
    return row


def mapping_account(db, module, role, require_confirmed=True):
    mapping = db.execute('SELECT * FROM account_mappings WHERE module=? AND role=?', (module, role)).fetchone()
    if mapping is None or (require_confirmed and mapping['status'] != 'confirmed'):
        raise ValueError('يجب تأكيد ربط الحساب أولًا: ' + role)
    account = _posting_account(db, mapping['account_id'])
    if account['code'][0] not in json.loads(mapping['allowed_classes']) or account['normal_side'] != mapping['expected_side']:
        raise ValueError('ربط الحساب غير متوافق مع طبيعة الحركة')
    return account


def create_journal(db, source_type, source_id, entry_date, lines):
    """Create one sealed, balanced journal per source; identical retries are safe."""
    source_type = _text({'type': source_type}, 'type', 80, True)
    _id(source_id)
    entry_date = _date(entry_date, 'تاريخ القيد')
    if not isinstance(lines, list) or not 2 <= len(lines) <= 1000:
        raise ValueError('القيد يحتاج طرفين على الأقل')
    prepared = []
    for line in lines:
        if not isinstance(line, dict):
            raise ValueError('طرف القيد غير صحيح')
        account = _posting_account(db, line.get('account_id'))
        debit = _money(line.get('debit_cents', 0))
        credit = _money(line.get('credit_cents', 0))
        if (debit > 0) == (credit > 0):
            raise ValueError('طرف القيد يجب أن يكون مدينًا أو دائنًا فقط')
        supplier = line.get('supplier_id')
        department = line.get('department_id')
        source_line = line.get('source_line_id')
        if supplier is not None:
            _require(db, 'suppliers', supplier)
        if department is not None:
            _require(db, 'departments', department)
        if source_line is not None:
            _id(source_line)
        prepared.append((account['id'], debit, credit, supplier, department, source_line))
    debit_sum = sum(line[1] for line in prepared)
    if debit_sum != sum(line[2] for line in prepared) or not 0 < debit_sum <= MAX_CENTS:
        raise ValueError('القيد غير متوازن أو يتجاوز النطاق المسموح')
    canonical = json.dumps([entry_date, sorted(prepared, key=lambda row: tuple(-1 if x is None else x for x in row))], separators=(',', ':'))
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    old = db.execute('SELECT * FROM journal_entries WHERE source_type=? AND source_id=?', (source_type, source_id)).fetchone()
    if old:
        if old['request_fingerprint'] != fingerprint or not old['sealed']:
            raise accounting.ConflictError('مصدر القيد مرحل بالفعل ببيانات مختلفة')
        return old['id']
    accounting_periods.assert_open(db,entry_date)
    code = _code(db, 'journal', 'JE')
    journal_id = db.execute('INSERT INTO journal_entries(code,source_type,source_id,entry_date,created_at,request_fingerprint) VALUES(?,?,?,?,?,?)',
                            (code, source_type, source_id, entry_date, _now(), fingerprint)).lastrowid
    db.executemany('INSERT INTO journal_lines(journal_id,account_id,debit_cents,credit_cents,supplier_id,department_id,source_line_id) VALUES(?,?,?,?,?,?,?)', [(journal_id, *line) for line in prepared])
    db.execute('UPDATE journal_entries SET sealed=1 WHERE id=?', (journal_id,))
    return journal_id


def enrich_supplier(db, row):
    result = dict(row)
    result['category_ids'] = [r[0] for r in db.execute('SELECT category_id FROM supplier_categories WHERE supplier_id=? ORDER BY category_id', (result['id'],))]
    result['categories'] = [dict(r) for r in db.execute('SELECT c.* FROM supply_categories c JOIN supplier_categories sc ON sc.category_id=c.id WHERE sc.supplier_id=? ORDER BY c.id', (result['id'],))]
    return result


def _last_purchase(db, product_id, before=None):
    values = [product_id]
    clause = ''
    if before is not None:
        clause = ' AND (i.business_date<? OR (i.business_date=? AND i.id<?))'
        values.extend((before['business_date'], before['business_date'], before['id']))
    return db.execute('''SELECT i.id invoice_id,i.code invoice_code,i.business_date,il.price_cents FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id JOIN invoice_postings p ON p.invoice_id=i.id WHERE il.product_id=? AND i.status<>'cancelled' ''' + clause + ' ORDER BY i.business_date DESC,i.id DESC,il.id DESC LIMIT 1', values).fetchone()


def enrich_product(db, row):
    result = dict(row)
    latest = _last_purchase(db, result['id'])
    result.update(last_purchase_price_cents=latest['price_cents'] if latest else None,
                  last_purchase_date=latest['business_date'] if latest else None,
                  last_purchase_invoice_id=latest['invoice_id'] if latest else None,
                  last_purchase_invoice_code=latest['invoice_code'] if latest else None,
                  effective_price_cents=latest['price_cents'] if latest else result['price_cents'])
    return result


def master_data(db):
    return {'categories': [dict(r) for r in db.execute('SELECT * FROM supply_categories ORDER BY id')],
            'payment_terms': {'unit': 'days', 'maximum': 3650, 'default': None}, 'price_basis': 'last_posted_purchase'}


def configure_supplier(db, supplier_id, data):
    supplier = _require(db, 'suppliers', supplier_id)
    days = data.get('terms_days', supplier.get('terms_days'))
    if days is not None and (type(days) is not int or not 0 <= days <= 3650):
        raise ValueError('أجل السداد من صفر إلى ٣٦٥٠ يومًا أو يترك دون تحديد')
    category_ids = data.get('category_ids')
    if category_ids is not None:
        if not isinstance(category_ids, list) or len(category_ids) > 100:
            raise ValueError('أنواع التوريد غير صحيحة')
        category_ids = [_id(value) for value in category_ids]
        if len(set(category_ids)) != len(category_ids):
            raise ValueError('نوع التوريد مكرر')
        for category_id in category_ids:
            _require(db, 'supply_categories', category_id)
        used = {r[0] for r in db.execute('SELECT DISTINCT category_id FROM products WHERE supplier_id=? AND category_id IS NOT NULL', (supplier_id,))}
        if used - set(category_ids):
            raise ValueError('لا يمكن إزالة نوع توريد مرتبط بأصناف المورد')
    db.execute('UPDATE suppliers SET terms_days=? WHERE id=?', (days, supplier_id))
    if category_ids is not None:
        db.execute('DELETE FROM supplier_categories WHERE supplier_id=?', (supplier_id,))
        db.executemany('INSERT INTO supplier_categories(supplier_id,category_id) VALUES(?,?)', [(supplier_id, pk) for pk in category_ids])
    return enrich_supplier(db, _require(db, 'suppliers', supplier_id))


def configure_product(db, product_id, data):
    product = _require(db, 'products', product_id)
    category = data.get('category_id', product.get('category_id'))
    expense = data.get('expense_account_id', product.get('expense_account_id'))
    if category is not None:
        _require(db, 'supply_categories', category)
        if not db.execute('SELECT 1 FROM supplier_categories WHERE supplier_id=? AND category_id=?', (product['supplier_id'], category)).fetchone():
            raise ValueError('نوع الصنف يجب أن يكون ضمن أنواع توريد المورد')
    if expense is not None:
        _posting_account(db, expense, expense=True)
    db.execute('UPDATE products SET category_id=?,expense_account_id=? WHERE id=?', (category, expense, product_id))
    return enrich_product(db, _require(db, 'products', product_id))


def assert_invoice_editable(db, invoice_id):
    invoice = _require(db, 'invoices', invoice_id)
    if db.execute('SELECT 1 FROM invoice_postings WHERE invoice_id=?', (invoice_id,)).fetchone():
        raise ValueError('الفاتورة المعتمدة لا تعدل أو تلغى؛ استخدم مرتجعًا أو قيد تصحيح')
    if invoice['status'] != 'draft':
        raise ValueError('الفاتورة الملغاة لا تعدل')
    return invoice


def set_invoice_terms(db, invoice_id, data):
    invoice = assert_invoice_editable(db, invoice_id)
    old = db.execute('SELECT due_date FROM invoice_terms WHERE invoice_id=?', (invoice_id,)).fetchone()
    due = data.get('due_date', old['due_date'] if old else None)
    if 'due_date' not in data and due is None:
        supplier = _require(db, 'suppliers', invoice['supplier_id'])
        if supplier['terms_days'] is not None:
            due = (date.fromisoformat(invoice['business_date']) + timedelta(days=supplier['terms_days'])).isoformat()
    if due == '':
        due = None
    if due is not None:
        _date(due, 'تاريخ الاستحقاق')
        if due < invoice['business_date']:
            raise ValueError('تاريخ الاستحقاق لا يسبق تاريخ الفاتورة')
    db.execute('INSERT INTO invoice_terms(invoice_id,due_date) VALUES(?,?) ON CONFLICT(invoice_id) DO UPDATE SET due_date=excluded.due_date', (invoice_id, due))
    return due


def _returned(db, line_id, as_of=None):
    query = '''SELECT l.quantity,l.total_cents FROM supplier_return_lines l JOIN supplier_returns r ON r.id=l.return_id WHERE l.invoice_line_id=? AND r.status='posted' '''
    values = [line_id]
    if as_of:
        query += ' AND r.business_date<=?'
        values.append(as_of)
    rows = db.execute(query, values).fetchall()
    return sum((Decimal(r['quantity']) for r in rows), Decimal(0)), sum(r['total_cents'] for r in rows)


def enrich_invoice(db, row):
    result = dict(row)
    invoice_id = result['id']
    posting = db.execute('SELECT * FROM invoice_postings WHERE invoice_id=?', (invoice_id,)).fetchone()
    terms = db.execute('SELECT due_date FROM invoice_terms WHERE invoice_id=?', (invoice_id,)).fetchone()
    result['status'] = 'posted' if posting else result['status']
    result['due_date'] = terms['due_date'] if terms else None
    result['journal_id'] = posting['journal_id'] if posting else None
    journal = db.execute('SELECT code FROM journal_entries WHERE id=?', (posting['journal_id'],)).fetchone() if posting else None
    result['journal_code'] = journal['code'] if journal else None
    result['posted_at'] = posting['posted_at'] if posting else None
    result['lines'] = []
    for line in db.execute('''SELECT il.*,p.expense_account_id current_expense_account_id FROM invoice_lines il JOIN products p ON p.id=il.product_id WHERE il.invoice_id=? ORDER BY il.id''', (invoice_id,)):
        line = dict(line)
        snapshot = db.execute('SELECT * FROM invoice_posting_lines WHERE invoice_line_id=?', (line['id'],)).fetchone()
        previous = snapshot['previous_price_cents'] if snapshot else None
        if not posting:
            last = _last_purchase(db, line['product_id'], before=result)
            previous = last['price_cents'] if last else None
        difference = line['price_cents'] - previous if previous is not None else None
        line['expense_account_id'] = snapshot['expense_account_id'] if snapshot else line['current_expense_account_id']
        line['previous_price_cents'] = previous
        line['price_difference_cents'] = difference
        line['price_comparison_note'] = ('أول شراء معتمد للصنف' if difference is None else 'السعر دون تغيير' if difference == 0 else ('زيادة السعر بمقدار ' if difference > 0 else 'انخفاض السعر بمقدار ') + f'{abs(difference)/100:.2f}')
        returned_qty, returned_cents = _returned(db, line['id'])
        line['returned_quantity'] = _quantity_text(returned_qty)
        line['returnable_quantity'] = _quantity_text(Decimal(line['quantity']) - returned_qty)
        line['returned_cents'] = returned_cents
        result['lines'].append(line)
    return taxation.enrich_invoice(db,result)


def _invoice_detail(db, invoice_id):
    row = db.execute('''SELECT i.*,s.name supplier_name,s.code supplier_code,d.name department_name,d.cost_code FROM invoices i JOIN suppliers s ON s.id=i.supplier_id JOIN departments d ON d.id=i.department_id WHERE i.id=?''', (_id(invoice_id),)).fetchone()
    if row is None:
        raise ValueError('الفاتورة غير موجودة')
    return enrich_invoice(db, row)


def post_invoice(db, invoice_id, data):
    invoice = _require(db, 'invoices', invoice_id)
    if db.execute('SELECT 1 FROM invoice_postings WHERE invoice_id=?', (invoice_id,)).fetchone():
        return _invoice_detail(db, invoice_id)
    assert_invoice_editable(db, invoice_id)
    _revision(data, invoice)
    accounting_periods.assert_open(db,invoice['business_date'])
    taxation.validate_invoice_for_posting(db,invoice_id)
    due = set_invoice_terms(db, invoice_id, data)
    if due is None:
        raise ValueError('حدد تاريخ الاستحقاق قبل اعتماد الفاتورة')
    _date(invoice['business_date'], 'تاريخ الفاتورة')
    amount = _money(invoice['total_cents'], allow_zero=False)
    payable = mapping_account(db, 'الموردون', 'payable_control')
    lines = list(db.execute('SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY id', (invoice_id,)))
    if not lines:
        raise ValueError('الفاتورة دون أصناف')
    journal_lines = []
    snapshots = []
    for line in lines:
        product = _require(db, 'products', line['product_id'])
        if product['supplier_id'] != invoice['supplier_id']:
            raise ValueError('الصنف غير مرتبط بالمورد')
        if product.get('expense_account_id') is None:
            raise ValueError('حدد حساب تكلفة أو مصروف للصنف: ' + line['name'])
        account = _posting_account(db, product['expense_account_id'], expense=True)
        qty = _quantity(line['quantity'])
        price = _money(line['price_cents'])
        expected = int((qty * price).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        if expected != line['total_cents']:
            raise ValueError('إجمالي بند الفاتورة غير متوافق مع الكمية والسعر')
        last = _last_purchase(db, product['id'], before=invoice)
        previous = last['price_cents'] if last else None
        snapshots.append((line['id'], invoice_id, product['id'], account['id'], invoice['department_id'], previous))
        journal_lines.extend(taxation.invoice_line_debits(db,invoice_id,line['id'],account['id'],invoice['supplier_id'],invoice['department_id']))
    if sum(line['total_cents'] for line in lines) != invoice['subtotal_cents'] or invoice['subtotal_cents'] + invoice['tax_cents'] != amount:
        raise ValueError('إجمالي الفاتورة لا يساوي صافي البنود والضريبة')
    journal_lines.append({'account_id': payable['id'], 'credit_cents': amount, 'supplier_id': invoice['supplier_id'], 'department_id': invoice['department_id']})
    journal_id = create_journal(db, 'supplier_invoice', invoice_id, invoice['business_date'], journal_lines)
    # Revision changes before insertion because the posting snapshot locks this row.
    db.execute('UPDATE invoices SET revision=revision+1,updated_at=? WHERE id=?', (_now(), invoice_id))
    db.execute('INSERT INTO invoice_postings(invoice_id,journal_id,payable_account_id,posted_at) VALUES(?,?,?,?)', (invoice_id, journal_id, payable['id'], _now()))
    db.executemany('INSERT INTO invoice_posting_lines(invoice_line_id,invoice_id,product_id,expense_account_id,department_id,previous_price_cents) VALUES(?,?,?,?,?,?)', snapshots)
    for product_id in {line['product_id'] for line in lines}:
        last = _last_purchase(db, product_id)
        db.execute('UPDATE products SET price_cents=? WHERE id=?', (last['price_cents'], product_id))
    _audit(db, 'invoice', invoice_id, 'post_invoice', {'journal_id': journal_id, 'total_cents': amount, 'due_date': due})
    return _invoice_detail(db, invoice_id)


def _return_payload(db, data, old=None):
    invoice_id = _id(data.get('invoice_id', old['invoice_id'] if old else None))
    invoice = _require(db, 'invoices', invoice_id)
    posting = db.execute('SELECT * FROM invoice_postings WHERE invoice_id=?', (invoice_id,)).fetchone()
    if not posting or invoice['status'] == 'cancelled':
        raise ValueError('المرتجع يرتبط بفاتورة معتمدة فقط')
    business_date = _date(data.get('business_date', old['business_date'] if old else ''), 'تاريخ المرتجع')
    if business_date < invoice['business_date']:
        raise ValueError('تاريخ المرتجع لا يسبق تاريخ الفاتورة')
    reference = _text(data, 'reference', 100, True)
    notes = _text(data, 'notes', 2000)
    incoming = data.get('lines')
    if not isinstance(incoming, list) or not 1 <= len(incoming) <= 100:
        raise ValueError('اختر بنود المرتجع')
    lines = []
    seen = set()
    for selected in incoming:
        if not isinstance(selected, dict):
            raise ValueError('بند المرتجع غير صحيح')
        line_id = _id(selected.get('invoice_line_id'))
        if line_id in seen:
            raise ValueError('بند المرتجع مكرر')
        seen.add(line_id)
        original = _require(db, 'invoice_lines', line_id)
        if original['invoice_id'] != invoice_id:
            raise ValueError('بند المرتجع لا يتبع الفاتورة المختارة')
        qty = _quantity(selected.get('quantity'))
        returned_qty, returned_cents = _returned(db, line_id)
        remaining_qty = Decimal(original['quantity']) - returned_qty
        if qty > remaining_qty:
            raise ValueError('كمية المرتجع تتجاوز الكمية المتاحة من الفاتورة')
        tax_amounts=taxation.return_line_amounts(db,line_id,qty)
        if tax_amounts is not None:
            total=tax_amounts['gross_cents']
        else:
            # Cumulative quantity rounding prevents repeated fractional cents.
            cumulative = int(((returned_qty + qty) * original['price_cents']).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            total = original['total_cents'] - returned_cents if qty == remaining_qty else cumulative - returned_cents
            if total < 0 or total + returned_cents > original['total_cents']:
                raise ValueError('قيمة المرتجع تتجاوز قيمة البند الأصلي')
        lines.append((line_id, _quantity_text(qty), total))
    total = sum(line[2] for line in lines)
    _money(total)
    return invoice, (invoice_id, invoice['supplier_id'], business_date, reference, notes, total), lines


def save_return(db, data):
    old = None
    return_id = data.get('id')
    if return_id is not None:
        old = _require(db, 'supplier_returns', return_id)
        if old['status'] != 'draft':
            raise ValueError('المرتجع المعتمد أو الملغى لا يعدل')
        _revision(data, old)
        accounting_periods.assert_open(db,old['business_date'])
        if data.get('action') == 'cancel':
            db.execute("UPDATE supplier_returns SET status='cancelled',revision=revision+1,updated_at=? WHERE id=?", (_now(), return_id))
            _audit(db, 'supplier_return', return_id, 'cancel_draft', {})
            return return_detail(db, return_id)
    else:
        key = _text(data, 'request_key', 100) or None
        if key:
            existing = db.execute('SELECT id FROM supplier_returns WHERE request_key=?', (key,)).fetchone()
            if existing:
                return return_detail(db, existing['id'])
    _, values, lines = _return_payload(db, data, old)
    accounting_periods.assert_open(db,values[2])
    if old:
        db.execute('UPDATE supplier_returns SET invoice_id=?,supplier_id=?,business_date=?,reference=?,notes=?,total_cents=?,revision=revision+1,updated_at=? WHERE id=?', (*values, _now(), return_id))
        db.execute('DELETE FROM supplier_return_lines WHERE return_id=?', (return_id,))
    else:
        code = _code(db, 'supplier_return', 'SRT')
        return_id = db.execute("INSERT INTO supplier_returns(code,request_key,invoice_id,supplier_id,business_date,reference,notes,total_cents,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'draft',?,?)", (code, key, *values, _now(), _now())).lastrowid
    db.executemany('INSERT INTO supplier_return_lines(return_id,invoice_line_id,quantity,total_cents) VALUES(?,?,?,?)', [(return_id, *line) for line in lines])
    taxation.save_return_tax(db,return_id)
    _audit(db, 'supplier_return', return_id, 'update_draft' if old else 'create_draft', {'total_cents': values[-1]})
    return return_detail(db, return_id)


def approve_return(db, return_id, data):
    old = _require(db, 'supplier_returns', return_id)
    if old['status'] == 'posted':
        return return_detail(db, return_id)
    if old['status'] != 'draft':
        raise ValueError('المرتجع الملغى لا يعتمد')
    _revision(data, old)
    accounting_periods.assert_open(db,old['business_date'])
    payload = dict(old)
    payload['lines'] = [dict(row) for row in db.execute('SELECT invoice_line_id,quantity FROM supplier_return_lines WHERE return_id=? ORDER BY id', (return_id,))]
    invoice, values, lines = _return_payload(db, payload, old)
    # Rebuild the draft tax split against quantities actually approved by now.
    for line_id,quantity,cents in lines:
        db.execute('UPDATE supplier_return_lines SET total_cents=? WHERE return_id=? AND invoice_line_id=?',(cents,return_id,line_id))
    taxation.save_return_tax(db,return_id)
    posting = db.execute('SELECT * FROM invoice_postings WHERE invoice_id=?', (invoice['id'],)).fetchone()
    payable = _posting_account(db, posting['payable_account_id'])
    journal_lines = [{'account_id': payable['id'], 'debit_cents': values[-1], 'supplier_id': old['supplier_id'], 'department_id': invoice['department_id']}] if values[-1] else []
    for line_id, quantity, cents in lines:
        snapshot = db.execute('SELECT * FROM invoice_posting_lines WHERE invoice_line_id=?', (line_id,)).fetchone()
        if snapshot is None:
            raise ValueError('الربط المحاسبي لبند الفاتورة غير موجود')
        journal_lines.extend(taxation.return_line_credits(db,return_id,line_id,snapshot['expense_account_id'],old['supplier_id'],snapshot['department_id']))
    journal_id = create_journal(db, 'supplier_return', return_id, old['business_date'], journal_lines) if values[-1] else None
    # Recalculate draft rounding against the actual approved returns, not other drafts.
    for line_id, quantity, cents in lines:
        db.execute('UPDATE supplier_return_lines SET total_cents=? WHERE return_id=? AND invoice_line_id=?', (cents, return_id, line_id))
    db.execute("UPDATE supplier_returns SET status='posted',total_cents=?,journal_id=?,revision=revision+1,updated_at=? WHERE id=?", (values[-1], journal_id, _now(), return_id))
    _audit(db, 'supplier_return', return_id, 'post_return', {'journal_id': journal_id, 'total_cents': values[-1]})
    return return_detail(db, return_id)


def post_return(db, data):
    """Convenience for trusted integrations; normal UI saves and approves separately."""
    draft = save_return(db, data)
    return approve_return(db, draft['id'], {'revision': draft['revision']})


def return_detail(db, return_id):
    result = _require(db, 'supplier_returns', return_id)
    invoice = _require(db, 'invoices', result['invoice_id'])
    supplier = _require(db, 'suppliers', result['supplier_id'])
    result.update(invoice_code=invoice['code'], supplier_name=supplier['name'], supplier_code=supplier['code'])
    result['lines'] = [dict(row) for row in db.execute('''SELECT rl.*,il.product_id,il.name,il.unit,il.price_cents FROM supplier_return_lines rl JOIN invoice_lines il ON il.id=rl.invoice_line_id WHERE rl.return_id=? ORDER BY rl.id''', (return_id,))]
    return taxation.enrich_return(db,result)


def list_returns(db, supplier_id=None):
    query = 'SELECT id FROM supplier_returns'
    values = []
    if supplier_id is not None:
        _require(db, 'suppliers', supplier_id)
        query += ' WHERE supplier_id=?'
        values.append(supplier_id)
    return [return_detail(db, row['id']) for row in db.execute(query + ' ORDER BY id DESC LIMIT 500', values).fetchall()]


def _payment_effective_filter(db, alias, as_of):
    if not _has_table(db, 'supplier_payment_reversals'):
        return '', []
    return f" AND NOT EXISTS(SELECT 1 FROM supplier_payment_reversals pr WHERE pr.payment_id={alias}.id AND pr.business_date<=?)", [as_of]


def _settlement_effects(db, supplier_id, as_of):
    targets, credit_sources, advances = {}, {}, 0
    if not (_has_table(db, 'supplier_settlements') and _has_table(db, 'settlement_allocations')):
        return targets, credit_sources, advances
    clause, values = '', [supplier_id, as_of]
    if _has_table(db, 'supplier_settlement_reversals'):
        clause = ' AND NOT EXISTS(SELECT 1 FROM supplier_settlement_reversals sr WHERE sr.settlement_id=s.id AND sr.business_date<=?)'
        values.append(as_of)
    rows = db.execute("SELECT s.id,s.source_type,s.source_id,s.amount_cents FROM supplier_settlements s WHERE s.supplier_id=? AND s.status='posted' AND s.business_date<=?" + clause, values).fetchall()
    for row in rows:
        if row['source_type'] == 'advance':
            advances += row['amount_cents']
        else:
            credit_sources[row['source_id']] = credit_sources.get(row['source_id'], 0) + row['amount_cents']
        for allocation in db.execute('SELECT invoice_id,amount_cents FROM settlement_allocations WHERE settlement_id=?', (row['id'],)):
            targets[allocation['invoice_id']] = targets.get(allocation['invoice_id'], 0) + allocation['amount_cents']
    return targets, credit_sources, advances


def _refund_effects(db, supplier_id, as_of):
    credit_sources, advances = {}, 0
    if _has_table(db, 'supplier_refunds'):
        for row in db.execute("SELECT source_type,source_id,amount_cents FROM supplier_refunds WHERE supplier_id=? AND status='posted' AND business_date<=?", (supplier_id, as_of)):
            if row['source_type'] == 'advance':
                advances += row['amount_cents']
            else:
                credit_sources[row['source_id']] = credit_sources.get(row['source_id'], 0) + row['amount_cents']
    return credit_sources, advances


def _invoice_payments(db, invoice_id, as_of):
    if not _has_table(db, 'supplier_payments') or not _has_table(db, 'payment_allocations'):
        return 0
    clause, values = _payment_effective_filter(db, 'p', as_of)
    return db.execute("SELECT COALESCE(SUM(a.amount_cents),0) FROM payment_allocations a JOIN supplier_payments p ON p.id=a.payment_id WHERE a.invoice_id=? AND p.status='posted' AND p.payment_date<=?" + clause, (invoice_id, as_of, *values)).fetchone()[0]


def open_invoices(db, supplier_id, as_of=None):
    """Historical invoice balances include dated settlements, refunds and reversals."""
    _require(db, 'suppliers', supplier_id)
    as_of = _as_of(as_of)
    targets, credit_used, _ = _settlement_effects(db, supplier_id, as_of)
    credit_refunded, _ = _refund_effects(db, supplier_id, as_of)
    rows = db.execute("SELECT i.*,t.due_date FROM invoices i JOIN invoice_postings p ON p.invoice_id=i.id JOIN invoice_terms t ON t.invoice_id=i.id WHERE i.supplier_id=? AND i.status<>'cancelled' AND i.business_date<=? ORDER BY t.due_date,i.business_date,i.id", (supplier_id, as_of)).fetchall()
    result = []
    for row in rows:
        row = dict(row)
        invoice_id = row['id']
        returned = db.execute("SELECT COALESCE(SUM(total_cents),0) FROM supplier_returns WHERE invoice_id=? AND status='posted' AND business_date<=?", (invoice_id, as_of)).fetchone()[0]
        paid = _invoice_payments(db, invoice_id, as_of)
        settled = targets.get(invoice_id, 0)
        used, refunded = credit_used.get(invoice_id, 0), credit_refunded.get(invoice_id, 0)
        raw = row['total_cents'] - returned - paid - settled + used + refunded
        row.update(invoice_id=invoice_id, status='posted', returned_cents=returned, paid_cents=paid,
                   settled_cents=settled, credit_used_cents=used, credit_refunded_cents=refunded,
                   raw_outstanding_cents=raw, outstanding_cents=max(raw, 0), return_credit_cents=max(-raw, 0),
                   days_overdue=max((date.fromisoformat(as_of) - date.fromisoformat(row['due_date'])).days, 0))
        result.append(row)
    return result


def invoice_outstanding(db, invoice_id, as_of=None):
    invoice = _require(db, 'invoices', invoice_id)
    return next((row['outstanding_cents'] for row in open_invoices(db, invoice['supplier_id'], as_of) if row['id'] == invoice_id), 0)


def _advance_balance(db, supplier_id, as_of):
    if not _has_table(db, 'supplier_payments') or not _has_table(db, 'payment_allocations'):
        return 0
    clause, values = _payment_effective_filter(db, 'p', as_of)
    original = db.execute("SELECT COALESCE(SUM(p.amount_cents-(SELECT COALESCE(SUM(pa.amount_cents),0) FROM payment_allocations pa WHERE pa.payment_id=p.id)),0) FROM supplier_payments p WHERE p.supplier_id=? AND p.status='posted' AND p.payment_date<=?" + clause, (supplier_id, as_of, *values)).fetchone()[0]
    _, _, settled = _settlement_effects(db, supplier_id, as_of)
    _, refunded = _refund_effects(db, supplier_id, as_of)
    return original - settled - refunded


def supplier_balances(db, supplier_id, as_of=None):
    as_of = _as_of(as_of)
    rows = open_invoices(db, supplier_id, as_of)
    payable = sum(row['raw_outstanding_cents'] for row in rows)
    advance = _advance_balance(db, supplier_id, as_of)
    return {'payable_balance_cents': payable, 'net_payable_cents': payable - advance,
            'outstanding_cents': sum(row['outstanding_cents'] for row in rows),
            'unapplied_return_credit_cents': sum(row['return_credit_cents'] for row in rows),
            'advance_cents': advance}


def aging(db, as_of=None):
    as_of = _as_of(as_of)
    suppliers = []
    totals = dict.fromkeys((*BUCKETS, 'total_cents', 'credit_cents', 'payable_balance_cents', 'net_payable_cents', 'advance_cents'), 0)
    for supplier in db.execute('SELECT * FROM suppliers ORDER BY name,id').fetchall():
        invoices = open_invoices(db, supplier['id'], as_of)
        row = dict.fromkeys(totals, 0)
        row.update(supplier_id=supplier['id'], code=supplier['code'], name=supplier['name'], invoices=invoices)
        for invoice in invoices:
            days = invoice['days_overdue']
            bucket = BUCKETS[0 if days == 0 else 1 if days <= 30 else 2 if days <= 60 else 3 if days <= 90 else 4]
            invoice['aging_bucket'] = bucket
            row[bucket] += invoice['outstanding_cents']
            row['credit_cents'] += invoice['return_credit_cents']
        row['total_cents'] = sum(row[key] for key in BUCKETS)
        row['payable_balance_cents'] = row['total_cents'] - row['credit_cents']
        row['advance_cents'] = _advance_balance(db, supplier['id'], as_of)
        row['net_payable_cents'] = row['payable_balance_cents'] - row['advance_cents']
        if invoices or row['advance_cents']:
            suppliers.append(row)
            for key in totals:
                totals[key] += row[key]
    return {'as_of': as_of, 'suppliers': suppliers, 'totals': totals, 'currency': accounting.get_settings(db)['base_currency'], 'basis': 'due_date', 'credits_are_separate': True}


def _payable_account_ids(db):
    ids = {row[0] for row in db.execute('SELECT DISTINCT payable_account_id FROM invoice_postings')}
    ids.update(row[0] for row in db.execute("SELECT account_id FROM account_mappings WHERE module='الموردون' AND role IN('payable_control','capital_payable_control')"))
    return sorted(ids)


def _advance_account_ids(db):
    ids = {row[0] for row in db.execute("SELECT account_id FROM account_mappings WHERE module='الموردون' AND role='supplier_advances'")}
    payable_ids = set(_payable_account_ids(db))
    if _has_table(db, 'supplier_payments'):
        ids.update(row[0] for row in db.execute("SELECT DISTINCT l.account_id FROM journal_lines l JOIN journal_entries j ON j.id=l.journal_id WHERE j.source_type='supplier_payment' AND l.debit_cents>0") if row[0] not in payable_ids)
    if _has_table(db, 'supplier_settlements'):
        ids.update(row[0] for row in db.execute("SELECT DISTINCT source_account_id FROM supplier_settlements WHERE source_type='advance' AND status='posted'"))
    return sorted(ids)


def _statement_document(db, row):
    lookup = {'supplier_invoice': ('invoices', 'id'), 'supplier_return': ('supplier_returns', 'id'),
              'supplier_payment': ('supplier_payments', 'id'), 'supplier_cheque_clearance': ('supplier_payments', 'id'),
              'supplier_settlement': ('supplier_settlements', 'id'), 'supplier_refund': ('supplier_refunds', 'id')}
    source_type = row['type']
    if source_type in lookup:
        table, key = lookup[source_type]
        if _has_table(db, table):
            doc = db.execute(f'SELECT code FROM {table} WHERE {key}=?', (row['document_id'],)).fetchone()
            if doc:
                row['code'] = doc['code']
    elif source_type == 'supplier_settlement_reversal' and _has_table(db, 'supplier_settlements'):
        doc = db.execute('SELECT code FROM supplier_settlements WHERE id=?', (row['document_id'],)).fetchone()
        if doc:
            row['code'] = doc['code'] + ' / عكس'
    elif source_type in ('supplier_payment_reversal', 'supplier_cheque_bounce') and _has_table(db, 'supplier_payment_reversals'):
        doc = db.execute('SELECT p.code FROM supplier_payments p JOIN supplier_payment_reversals r ON r.payment_id=p.id WHERE r.id=?', (row['document_id'],)).fetchone()
        if doc:
            row['code'] = doc['code'] + ' / عكس'
    return row


def statement(db, supplier_id, as_of=None):
    supplier = enrich_supplier(db, _require(db, 'suppliers', supplier_id))
    as_of = _as_of(as_of)
    account_ids = _payable_account_ids(db)
    advance_ids = _advance_account_ids(db)
    placeholders = ','.join('?' for _ in account_ids) or 'NULL'
    advance_placeholders = ','.join('?' for _ in advance_ids) or 'NULL'
    raw_rows = db.execute(f"""SELECT j.id journal_id,j.code journal_code,j.source_type,j.source_id,j.entry_date date,
        SUM(CASE WHEN l.account_id IN({placeholders}) THEN l.debit_cents ELSE 0 END) debit_cents,
        SUM(CASE WHEN l.account_id IN({placeholders}) THEN l.credit_cents ELSE 0 END) credit_cents,
        SUM(CASE WHEN l.account_id IN({advance_placeholders}) THEN l.debit_cents-l.credit_cents ELSE 0 END) advance_cents
        FROM journal_entries j JOIN journal_lines l ON l.journal_id=j.id
        WHERE j.sealed=1 AND l.supplier_id=? AND j.entry_date<=? GROUP BY j.id ORDER BY j.entry_date,j.id""", (*account_ids, *account_ids, *advance_ids, supplier_id, as_of)).fetchall()
    balance = 0
    rows = []
    labels = {'supplier_invoice': 'فاتورة مورد', 'supplier_return': 'مرتجع مورد', 'supplier_payment': 'سداد مورد',
              'supplier_cheque_clearance': 'صرف شيك مورد', 'supplier_settlement': 'تسوية رصيد مورد',
              'supplier_settlement_reversal': 'عكس تسوية مورد', 'supplier_refund': 'استرداد من مورد',
              'supplier_payment_reversal': 'عكس سداد مورد', 'supplier_cheque_bounce': 'إلغاء صرف شيك مرتد'}
    for raw in raw_rows:
        row = dict(raw)
        row.update(type=row['source_type'], document_id=row['source_id'], code=row['journal_code'], description=labels.get(row['source_type'], row['source_type']))
        _statement_document(db, row)
        balance += row['credit_cents'] - row['debit_cents']
        row['balance_cents'] = balance
        rows.append(row)
    balances = supplier_balances(db, supplier_id, as_of)
    return {'supplier': supplier, 'as_of': as_of, 'rows': rows,
            'totals': {'debit_cents': sum(row['debit_cents'] for row in rows), 'credit_cents': sum(row['credit_cents'] for row in rows), 'balance_cents': balance, **balances},
            'currency': accounting.get_settings(db)['base_currency']}


def journal_detail(db, journal_id):
    """Read-only journal drilldown with the historical accounts and dimensions."""
    result = _require(db, 'journal_entries', journal_id)
    if not result['sealed']:
        raise ValueError('القيد لم يكتمل بعد')
    result['lines'] = [dict(row) for row in db.execute('''SELECT l.*,a.code account_code,a.name account_name,s.name supplier_name,d.name department_name,d.cost_code
        FROM journal_lines l JOIN accounts a ON a.id=l.account_id
        LEFT JOIN suppliers s ON s.id=l.supplier_id LEFT JOIN departments d ON d.id=l.department_id
        WHERE l.journal_id=? ORDER BY l.id''', (journal_id,))]
    result['debit_cents'] = sum(row['debit_cents'] for row in result['lines'])
    result['credit_cents'] = sum(row['credit_cents'] for row in result['lines'])
    result.pop('request_fingerprint', None)
    return result


def ledger_summary(db, as_of=None):
    as_of = _as_of(as_of)
    report = aging(db, as_of)
    gross, effective = 0, 0
    if _has_table(db, 'supplier_payments'):
        gross = db.execute("SELECT COALESCE(SUM(amount_cents),0) FROM supplier_payments WHERE status='posted' AND payment_date<=?", (as_of,)).fetchone()[0]
        clause, values = _payment_effective_filter(db, 'p', as_of)
        effective = db.execute("SELECT COALESCE(SUM(p.amount_cents),0) FROM supplier_payments p WHERE p.status='posted' AND p.payment_date<=?" + clause, (as_of, *values)).fetchone()[0]
    refunds = db.execute("SELECT COALESCE(SUM(amount_cents),0) FROM supplier_refunds WHERE status='posted' AND business_date<=?", (as_of,)).fetchone()[0] if _has_table(db, 'supplier_refunds') else 0
    return {'posted_invoices_cents': db.execute("SELECT COALESCE(SUM(i.total_cents),0) FROM invoices i JOIN invoice_postings p ON p.invoice_id=i.id WHERE i.status<>'cancelled' AND i.business_date<=?", (as_of,)).fetchone()[0],
            'returns_cents': db.execute("SELECT COALESCE(SUM(total_cents),0) FROM supplier_returns WHERE status='posted' AND business_date<=?", (as_of,)).fetchone()[0],
            'payments_cents': effective, 'gross_payments_cents': gross, 'reversed_payments_cents': gross-effective,
            'refunds_cents': refunds, 'net_payable_cents': report['totals']['net_payable_cents'], 'advance_cents': report['totals']['advance_cents'],
            'journal_count': db.execute('SELECT COUNT(*) FROM journal_entries WHERE sealed=1 AND entry_date<=?', (as_of,)).fetchone()[0]}
