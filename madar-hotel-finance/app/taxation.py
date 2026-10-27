"""Explicit input-VAT configuration and immutable supplier-document tax snapshots.

Rates and registration are supplied by the operator. Prices are exclusive of tax.
The caller owns the transaction; this module never commits or creates fake rates.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import accounting

KINDS = {'vat', 'zero_rated', 'exempt', 'out_of_scope'}
STATUSES = {'unconfigured', 'registered', 'not_registered'}
MAX_CENTS = 10**13


def _has(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _id(value):
    if type(value) is not int or value < 1:
        raise ValueError('اختيار ضريبي غير صحيح')
    return value


def _text(data, key, maximum=200, required=False):
    value = data.get(key, '')
    if not isinstance(value, str):
        raise ValueError('صيغة حقل غير صحيحة: ' + key)
    value = value.strip()
    if (required and not value) or len(value) > maximum:
        raise ValueError('راجع الحقل: ' + key)
    return value


def _basis(value, label):
    if isinstance(value, bool):
        raise ValueError(label + ' غير صحيحة')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 <= number <= 100 or number != number.quantize(Decimal('.01')):
            raise ValueError()
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(label + ' من صفر إلى ١٠٠ وبحد أقصى منزلتين عشريتين') from None
    return int(number * 100)


def _percent(basis):
    return format(Decimal(basis) / 100, '.2f')


def _round(value):
    return int(Decimal(value).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _posted(db, invoice_id):
    return _has(db, 'invoice_postings') and db.execute('SELECT 1 FROM invoice_postings WHERE invoice_id=?', (invoice_id,)).fetchone() is not None


def initialize(db):
    for sql in [
        """CREATE TABLE IF NOT EXISTS tax_settings(id INTEGER PRIMARY KEY CHECK(id=1),registration_status TEXT NOT NULL DEFAULT 'unconfigured' CHECK(registration_status IN('unconfigured','registered','not_registered')),tax_number TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',revision INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS tax_codes(id INTEGER PRIMARY KEY,code TEXT UNIQUE NOT NULL,name TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN('vat','zero_rated','exempt','out_of_scope')),rate_basis INTEGER NOT NULL CHECK(rate_basis BETWEEN 0 AND 10000),recoverable_basis INTEGER NOT NULL CHECK(recoverable_basis BETWEEN 0 AND 10000),active INTEGER NOT NULL DEFAULT 1 CHECK(active IN(0,1)),confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN(0,1)),notes TEXT NOT NULL DEFAULT '',revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,CHECK((kind='vat' AND rate_basis>0) OR (kind<>'vat' AND rate_basis=0 AND recoverable_basis=0)))""",
        """CREATE TABLE IF NOT EXISTS invoice_tax_settings(invoice_id INTEGER PRIMARY KEY REFERENCES invoices(id),tax_code_id INTEGER NOT NULL REFERENCES tax_codes(id),code_revision INTEGER NOT NULL,settings_revision INTEGER NOT NULL,tax_code_name TEXT NOT NULL,tax_code TEXT NOT NULL,kind TEXT NOT NULL,rate_basis INTEGER NOT NULL,recoverable_basis INTEGER NOT NULL,recoverable_account_id INTEGER REFERENCES accounts(id),registration_status TEXT NOT NULL DEFAULT 'unconfigured',tax_number TEXT NOT NULL DEFAULT '',price_basis TEXT NOT NULL DEFAULT 'exclusive' CHECK(price_basis='exclusive'))""",
        """CREATE TABLE IF NOT EXISTS invoice_tax_lines(invoice_line_id INTEGER PRIMARY KEY REFERENCES invoice_lines(id) ON DELETE CASCADE,invoice_id INTEGER NOT NULL REFERENCES invoices(id),net_cents INTEGER NOT NULL CHECK(net_cents>=0),tax_cents INTEGER NOT NULL CHECK(tax_cents>=0),recoverable_cents INTEGER NOT NULL CHECK(recoverable_cents>=0),nonrecoverable_cents INTEGER NOT NULL CHECK(nonrecoverable_cents>=0),CHECK(tax_cents=recoverable_cents+nonrecoverable_cents))""",
        """CREATE TABLE IF NOT EXISTS supplier_return_tax_lines(return_line_id INTEGER PRIMARY KEY REFERENCES supplier_return_lines(id) ON DELETE CASCADE,return_id INTEGER NOT NULL REFERENCES supplier_returns(id),invoice_line_id INTEGER NOT NULL REFERENCES invoice_lines(id),net_cents INTEGER NOT NULL CHECK(net_cents>=0),tax_cents INTEGER NOT NULL CHECK(tax_cents>=0),recoverable_cents INTEGER NOT NULL CHECK(recoverable_cents>=0),nonrecoverable_cents INTEGER NOT NULL CHECK(nonrecoverable_cents>=0),CHECK(tax_cents=recoverable_cents+nonrecoverable_cents))""",
        'CREATE INDEX IF NOT EXISTS tax_invoice_lines_invoice ON invoice_tax_lines(invoice_id)',
        'CREATE INDEX IF NOT EXISTS tax_return_lines_invoice_line ON supplier_return_tax_lines(invoice_line_id,return_id)',
    ]:
        db.execute(sql)
    columns = {row['name'] for row in db.execute('PRAGMA table_info(invoice_tax_settings)')}
    for column, ddl in [('registration_status', "TEXT NOT NULL DEFAULT 'unconfigured'"), ('tax_number', "TEXT NOT NULL DEFAULT ''")]:
        if column not in columns:
            db.execute(f'ALTER TABLE invoice_tax_settings ADD COLUMN {column} {ddl}')
    db.execute("INSERT OR IGNORE INTO tax_settings(id,updated_at) VALUES(1,?)", (accounting._now(),))
    for table, condition in [
        ('invoice_tax_settings', 'EXISTS(SELECT 1 FROM invoice_postings WHERE invoice_id=OLD.invoice_id)'),
        ('invoice_tax_lines', 'EXISTS(SELECT 1 FROM invoice_postings WHERE invoice_id=OLD.invoice_id)'),
        ('supplier_return_tax_lines', "EXISTS(SELECT 1 FROM supplier_returns WHERE id=OLD.return_id AND status='posted')"),
    ]:
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT,'posted tax snapshot is immutable'); END")
        inserted = condition.replace('OLD.', 'NEW.')
        db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_insert BEFORE INSERT ON {table} WHEN {inserted} BEGIN SELECT RAISE(ABORT,'posted tax snapshot is immutable'); END")
        db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_new_parent_update BEFORE UPDATE ON {table} WHEN {inserted} BEGIN SELECT RAISE(ABORT,'posted tax snapshot is immutable'); END")


def settings(db):
    if not _has(db, 'tax_settings'):
        return {'id': 1, 'registration_status': 'unconfigured', 'tax_number': '', 'notes': '', 'revision': 0, 'updated_at': None}
    return dict(db.execute('SELECT * FROM tax_settings WHERE id=1').fetchone())


def _code_result(row):
    result = dict(row)
    result.update(active=bool(result['active']), confirmed=bool(result['confirmed']), rate=_percent(result['rate_basis']), recoverable_percent=_percent(result['recoverable_basis']))
    return result


def snapshot(db):
    return {'settings': settings(db), 'codes': [_code_result(row) for row in db.execute('SELECT * FROM tax_codes ORDER BY id')] if _has(db, 'tax_codes') else [], 'price_basis': 'exclusive'}


def update_settings(db, data):
    current = settings(db)
    accounting._revision(data, current)
    if set(data) - {'revision', 'registration_status', 'tax_number', 'notes'}:
        raise ValueError('حقول إعداد ضريبي غير معروفة')
    status = data.get('registration_status', current['registration_status'])
    if status not in STATUSES:
        raise ValueError('حدد موقف التسجيل الضريبي')
    number = _text(data, 'tax_number', 80) if 'tax_number' in data else current['tax_number']
    notes = _text(data, 'notes', 2000) if 'notes' in data else current['notes']
    if status == 'registered' and not number:
        raise ValueError('أدخل رقم التسجيل الضريبي قبل اعتماد موقف مسجل')
    db.execute('UPDATE tax_settings SET registration_status=?,tax_number=?,notes=?,revision=revision+1,updated_at=? WHERE id=1', (status, number, notes, accounting._now()))
    accounting._audit(db, 'tax_settings', 1, 'update_tax_settings', {'registration_status': status, 'tax_number': number})
    return settings(db)


def _code_payload(data, current=None):
    allowed = {'name', 'kind', 'rate', 'recoverable_percent', 'active', 'confirmed', 'notes', 'revision'}
    if set(data) - allowed:
        raise ValueError('حقول كود ضريبي غير معروفة')
    current = current or {}
    name = _text(data, 'name', 160, True) if 'name' in data or not current else current['name']
    kind = data.get('kind', current.get('kind'))
    if kind not in KINDS:
        raise ValueError('حدد نوع المعاملة الضريبية')
    if kind == 'vat' and not current and ('rate' not in data or 'recoverable_percent' not in data):
        raise ValueError('أدخل نسبة الضريبة ونسبة القابل للخصم صراحةً')
    rate = _basis(data.get('rate', _percent(current.get('rate_basis', 0))), 'نسبة الضريبة')
    recoverable = _basis(data.get('recoverable_percent', _percent(current.get('recoverable_basis', 0))), 'نسبة القابل للخصم')
    if kind == 'vat' and not rate:
        raise ValueError('للضريبة الصفرية اختر نوع نسبة صفرية')
    if kind != 'vat' and (rate or recoverable):
        raise ValueError('المعفى وخارج النطاق والنسبة الصفرية لا تحمل نسبة ضريبة أو خصم')
    active = data.get('active', bool(current.get('active', True)))
    confirmed = data.get('confirmed', bool(current.get('confirmed', False)))
    if type(active) is not bool or type(confirmed) is not bool:
        raise ValueError('حالة الكود الضريبي غير صحيحة')
    notes = _text(data, 'notes', 2000) if 'notes' in data else current.get('notes', '')
    return name, kind, rate, recoverable, int(active), int(confirmed), notes


def create_code(db, data):
    values = _code_payload(data)
    if db.execute('SELECT 1 FROM tax_codes WHERE name=?', (values[0],)).fetchone():
        raise ValueError('يوجد كود ضريبي بنفس الاسم')
    db.execute("INSERT INTO sequences(kind,value) VALUES('tax_code',1) ON CONFLICT(kind) DO UPDATE SET value=value+1")
    sequence = db.execute("SELECT value FROM sequences WHERE kind='tax_code'").fetchone()[0]
    code = f'TAX-{sequence:06d}'
    stamp = accounting._now()
    cursor = db.execute('INSERT INTO tax_codes(code,name,kind,rate_basis,recoverable_basis,active,confirmed,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)', (code, *values, stamp, stamp))
    accounting._audit(db, 'tax_code', cursor.lastrowid, 'create_tax_code', {'code': code, 'kind': values[1], 'rate_basis': values[2], 'recoverable_basis': values[3]})
    return _code_result(db.execute('SELECT * FROM tax_codes WHERE id=?', (cursor.lastrowid,)).fetchone())


def update_code(db, code_id, data):
    current = db.execute('SELECT * FROM tax_codes WHERE id=?', (_id(code_id),)).fetchone()
    if current is None:
        raise ValueError('الكود الضريبي غير موجود')
    current = dict(current)
    accounting._revision(data, current)
    values = _code_payload(data, current)
    if db.execute('SELECT 1 FROM tax_codes WHERE name=? AND id<>?', (values[0], code_id)).fetchone():
        raise ValueError('يوجد كود ضريبي بنفس الاسم')
    db.execute('UPDATE tax_codes SET name=?,kind=?,rate_basis=?,recoverable_basis=?,active=?,confirmed=?,notes=?,revision=revision+1,updated_at=? WHERE id=?', (*values, accounting._now(), code_id))
    accounting._audit(db, 'tax_code', code_id, 'update_tax_code', {'previous_rate_basis': current['rate_basis'], 'rate_basis': values[2], 'previous_recoverable_basis': current['recoverable_basis'], 'recoverable_basis': values[3], 'active': bool(values[4]), 'confirmed': bool(values[5])})
    return _code_result(db.execute('SELECT * FROM tax_codes WHERE id=?', (code_id,)).fetchone())


def _vat_account(db, confirmed=False):
    row = db.execute("SELECT m.*,a.active,a.posting,a.normal_side,a.code FROM account_mappings m JOIN accounts a ON a.id=m.account_id WHERE m.module='الضرائب' AND m.role='recoverable_input_vat'").fetchone()
    valid = row is not None and row['active'] and row['posting'] and row['normal_side'] == 'debit' and row['code'].startswith('1')
    if not valid or (confirmed and row['status'] != 'confirmed'):
        if confirmed:
            raise ValueError('راجع وأكد ربط حساب ضريبة المدخلات القابلة للخصم قبل الترحيل')
        return None
    return row['account_id']


def _selected_code(db, data):
    selected = data.get('tax_code_id')
    if selected in (None, ''):
        if _basis(data.get('tax_rate', '0'), 'نسبة الضريبة'):
            raise ValueError('اختر كودًا ضريبيًا مضبوطًا؛ لا تستخدم نسبة حرة غير معتمدة')
        return None, None
    code = db.execute('SELECT * FROM tax_codes WHERE id=?', (_id(selected),)).fetchone() if _has(db, 'tax_codes') else None
    if code is None or not code['active'] or not code['confirmed']:
        raise ValueError('اختر كودًا ضريبيًا نشطًا تمت مراجعته وتأكيده')
    status = settings(db)
    if status['registration_status'] == 'unconfigured':
        raise ValueError('حدد موقف التسجيل الضريبي أولًا من إعدادات الضرائب')
    if code['recoverable_basis'] and status['registration_status'] != 'registered':
        raise ValueError('الضريبة القابلة للخصم تتطلب تأكيد موقف مسجل ورقم التسجيل؛ لغير المسجل تحمل الضريبة على المصروف')
    if 'tax_rate' in data and _basis(data['tax_rate'], 'نسبة الضريبة') not in (0, code['rate_basis']):
        raise ValueError('نسبة الضريبة تؤخذ من الكود المختار ولا تعدل على الفاتورة')
    return dict(code), status


def prepare_invoice(db, data, values, lines):
    code, status = _selected_code(db, data)
    adjusted = list(values)
    subtotal = sum(line[-1] for line in lines)
    if code is None:
        adjusted[5:] = [subtotal, 0, 0, subtotal]
        return tuple(adjusted), None
    details = []
    for line in lines:
        net = line[-1]
        tax = _round(Decimal(net) * code['rate_basis'] / 10000)
        recoverable = _round(Decimal(tax) * code['recoverable_basis'] / 10000)
        details.append({'product_id': line[0], 'net_cents': net, 'tax_cents': tax, 'recoverable_cents': recoverable, 'nonrecoverable_cents': tax - recoverable})
    tax = sum(line['tax_cents'] for line in details)
    if subtotal + tax > MAX_CENTS:
        raise ValueError('إجمالي الفاتورة بعد الضريبة يتجاوز النطاق المسموح')
    adjusted[5:] = [subtotal, code['rate_basis'], tax, subtotal + tax]
    plan = {'tax_code_id': code['id'], 'code_revision': code['revision'], 'settings_revision': status['revision'],
            'tax_code_name': code['name'], 'tax_code': code['code'], 'kind': code['kind'], 'rate_basis': code['rate_basis'],
            'recoverable_basis': code['recoverable_basis'], 'registration_status': status['registration_status'], 'tax_number': status['tax_number'], 'recoverable_account_id': _vat_account(db) if any(line['recoverable_cents'] for line in details) else None,
            'lines': details}
    return tuple(adjusted), plan


def save_invoice_tax(db, invoice_id, plan):
    if _posted(db, invoice_id):
        raise ValueError('ضريبة الفاتورة المعتمدة محفوظة ولا تعدل')
    if not _has(db, 'invoice_tax_settings'):
        if plan is not None:
            raise ValueError('إعداد الضرائب لم يهيأ بعد')
        return
    db.execute('DELETE FROM invoice_tax_lines WHERE invoice_id=?', (invoice_id,))
    db.execute('DELETE FROM invoice_tax_settings WHERE invoice_id=?', (invoice_id,))
    if plan is None:
        return
    fields = ('tax_code_id', 'code_revision', 'settings_revision', 'tax_code_name', 'tax_code', 'kind', 'rate_basis', 'recoverable_basis', 'recoverable_account_id', 'registration_status', 'tax_number')
    db.execute(f"INSERT INTO invoice_tax_settings(invoice_id,{','.join(fields)}) VALUES(?,{','.join('?' for _ in fields)})", (invoice_id, *(plan[field] for field in fields)))
    lines = db.execute('SELECT id,product_id,total_cents FROM invoice_lines WHERE invoice_id=? ORDER BY id', (invoice_id,)).fetchall()
    if len(lines) != len(plan['lines']):
        raise ValueError('عدد بنود الضريبة لا يطابق بنود الفاتورة')
    for actual, tax in zip(lines, plan['lines']):
        if actual['product_id'] != tax['product_id'] or actual['total_cents'] != tax['net_cents']:
            raise ValueError('بنود الضريبة تختلف عن الفاتورة المحفوظة')
        db.execute('INSERT INTO invoice_tax_lines(invoice_line_id,invoice_id,net_cents,tax_cents,recoverable_cents,nonrecoverable_cents) VALUES(?,?,?,?,?,?)', (actual['id'], invoice_id, tax['net_cents'], tax['tax_cents'], tax['recoverable_cents'], tax['nonrecoverable_cents']))


def _invoice_snapshot(db, invoice_id):
    if not _has(db, 'invoice_tax_settings'):
        return None
    row = db.execute('SELECT * FROM invoice_tax_settings WHERE invoice_id=?', (invoice_id,)).fetchone()
    return dict(row) if row else None


def validate_invoice_for_posting(db, invoice_id):
    snap = _invoice_snapshot(db, invoice_id)
    invoice = db.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
    if invoice is None:
        raise ValueError('الفاتورة غير موجودة')
    if snap is None:
        if invoice['tax_cents'] or invoice['tax_basis']:
            raise ValueError('الفاتورة بلا نسخة إعدادات للمعالجة الضريبية المعتمدة؛ أعد مراجعتها وحفظها')
        return None
    current = db.execute('SELECT * FROM tax_codes WHERE id=?', (snap['tax_code_id'],)).fetchone()
    status = settings(db)
    if not current or not current['active'] or not current['confirmed'] or current['revision'] != snap['code_revision'] or status['revision'] != snap['settings_revision']:
        raise accounting.ConflictError('تغير إعداد الضريبة بعد حفظ المسودة؛ راجع الفاتورة وأعد حفظها قبل الاعتماد')
    if current['recoverable_basis'] and status['registration_status'] != 'registered':
        raise ValueError('راجع موقف التسجيل قبل خصم ضريبة المدخلات')
    rows = db.execute('SELECT t.*,il.total_cents actual_net FROM invoice_tax_lines t JOIN invoice_lines il ON il.id=t.invoice_line_id WHERE t.invoice_id=?', (invoice_id,)).fetchall()
    count = db.execute('SELECT COUNT(*) FROM invoice_lines WHERE invoice_id=?', (invoice_id,)).fetchone()[0]
    if len(rows) != count:
        raise ValueError('نسخة ضريبة البنود غير مكتملة؛ أعد حفظ المسودة')
    for line in rows:
        expected_tax = _round(Decimal(line['actual_net']) * snap['rate_basis'] / 10000)
        expected_recoverable = _round(Decimal(expected_tax) * snap['recoverable_basis'] / 10000)
        if (line['net_cents'], line['tax_cents'], line['recoverable_cents'], line['nonrecoverable_cents']) != (line['actual_net'], expected_tax, expected_recoverable, expected_tax - expected_recoverable):
            raise ValueError('حساب ضريبة البند لا يطابق النسخة المحفوظة')
    tax = sum(row['tax_cents'] for row in rows)
    if invoice['subtotal_cents'] != sum(row['net_cents'] for row in rows) or invoice['tax_cents'] != tax or invoice['tax_basis'] != snap['rate_basis'] or invoice['total_cents'] != invoice['subtotal_cents'] + tax:
        raise ValueError('إجماليات الضريبة لا تطابق الفاتورة المحفوظة')
    if sum(row['recoverable_cents'] for row in rows):
        account = _vat_account(db, confirmed=True)
        if account != snap['recoverable_account_id']:
            raise accounting.ConflictError('تغير حساب ضريبة المدخلات بعد حفظ المسودة؛ أعد مراجعتها وحفظها')
    return snap


def invoice_line_debits(db, invoice_id, invoice_line_id, expense_account_id, supplier_id, department_id):
    line = db.execute('SELECT * FROM invoice_lines WHERE id=? AND invoice_id=?', (invoice_line_id, invoice_id)).fetchone()
    if line is None:
        raise ValueError('بند الفاتورة غير موجود')
    tax = db.execute('SELECT * FROM invoice_tax_lines WHERE invoice_line_id=?', (invoice_line_id,)).fetchone() if _has(db, 'invoice_tax_lines') else None
    expense = line['total_cents'] + (tax['nonrecoverable_cents'] if tax else 0)
    common = {'supplier_id': supplier_id, 'department_id': department_id, 'source_line_id': invoice_line_id}
    lines = [{'account_id': expense_account_id, 'debit_cents': expense, **common}] if expense else []
    if tax and tax['recoverable_cents']:
        snap = _invoice_snapshot(db, invoice_id)
        lines.append({'account_id': snap['recoverable_account_id'], 'debit_cents': tax['recoverable_cents'], **common})
    return lines


def enrich_invoice(db, result):
    result = dict(result)
    snap = _invoice_snapshot(db, result['id'])
    result.update(tax_code_id=snap['tax_code_id'] if snap else None, tax_code_name=snap['tax_code_name'] if snap else None,
                  tax_kind=snap['kind'] if snap else None, tax_code_revision=snap['code_revision'] if snap else None, tax_settings_revision=snap['settings_revision'] if snap else None, tax_rate=_percent(snap['rate_basis']) if snap else '0.00',
                  tax_recoverable_percent=_percent(snap['recoverable_basis']) if snap else '0.00', tax_price_basis='exclusive',
                  tax_registration_status=snap['registration_status'] if snap else None, tax_registration_number=snap['tax_number'] if snap else None)
    recoverable = nonrecoverable = 0
    for line in result.get('lines', []):
        row = db.execute('SELECT * FROM invoice_tax_lines WHERE invoice_line_id=?', (line['id'],)).fetchone() if _has(db, 'invoice_tax_lines') else None
        line.update(net_cents=line['total_cents'], tax_cents=row['tax_cents'] if row else 0,
                    recoverable_cents=row['recoverable_cents'] if row else 0, nonrecoverable_cents=row['nonrecoverable_cents'] if row else 0)
        line['gross_cents'] = line['net_cents'] + line['tax_cents']
        recoverable += line['recoverable_cents']; nonrecoverable += line['nonrecoverable_cents']
    result.update(tax_recoverable_cents=recoverable, tax_nonrecoverable_cents=nonrecoverable)
    return result


def return_line_amounts(db, invoice_line_id, quantity):
    if not _has(db, 'invoice_tax_lines'):
        return None
    original = db.execute('SELECT * FROM invoice_tax_lines WHERE invoice_line_id=?', (_id(invoice_line_id),)).fetchone()
    if original is None:
        return None
    line = db.execute('SELECT * FROM invoice_lines WHERE id=?', (invoice_line_id,)).fetchone()
    if line is None:
        raise ValueError('بند الفاتورة غير موجود')
    try:
        qty = Decimal(str(quantity)); original_qty = Decimal(line['quantity'])
    except (InvalidOperation, ValueError):
        raise ValueError('كمية المرتجع غير صحيحة') from None
    if not qty.is_finite() or qty <= 0 or qty != qty.quantize(Decimal('.001')):
        raise ValueError('كمية المرتجع موجبة وبحد أقصى ثلاث منازل')
    posted = db.execute("SELECT l.* FROM supplier_return_lines l JOIN supplier_returns r ON r.id=l.return_id WHERE l.invoice_line_id=? AND r.status='posted' ORDER BY r.business_date,r.id,l.id", (invoice_line_id,)).fetchall()
    used = dict.fromkeys(('net_cents', 'tax_cents', 'recoverable_cents', 'nonrecoverable_cents'), 0)
    used_qty = Decimal(0)
    for prior in posted:
        tax = db.execute('SELECT * FROM supplier_return_tax_lines WHERE return_line_id=?', (prior['id'],)).fetchone()
        if tax is None:
            raise ValueError('بيانات ضريبة مرتجع معتمد سابقة غير مكتملة')
        used_qty += Decimal(prior['quantity'])
        for key in used: used[key] += tax[key]
    cumulative_qty = used_qty + qty
    if cumulative_qty > original_qty:
        raise ValueError('كمية المرتجع تتجاوز الكمية المتبقية من الفاتورة')
    if cumulative_qty == original_qty:
        result = {key: original[key] - used[key] for key in used}
    else:
        net_target = min(_round(cumulative_qty * line['price_cents']), original['net_cents'])
        tax_target = min(_round(Decimal(original['tax_cents']) * cumulative_qty / original_qty), original['tax_cents'])
        net = max(net_target - used['net_cents'], 0)
        tax = max(tax_target - used['tax_cents'], 0)
        desired_recoverable = _round(Decimal(original['recoverable_cents']) * cumulative_qty / original_qty) - used['recoverable_cents']
        remaining_recoverable = original['recoverable_cents'] - used['recoverable_cents']
        remaining_nonrecoverable = original['nonrecoverable_cents'] - used['nonrecoverable_cents']
        lower, upper = max(0, tax - remaining_nonrecoverable), min(tax, remaining_recoverable)
        recovered = max(lower, min(desired_recoverable, upper))
        result = {'net_cents': net, 'tax_cents': tax, 'recoverable_cents': recovered, 'nonrecoverable_cents': tax - recovered}
    if any(value < 0 for value in result.values()) or result['tax_cents'] != result['recoverable_cents'] + result['nonrecoverable_cents']:
        raise ValueError('ضريبة المرتجع تتجاوز المتاح من الفاتورة الأصلية')
    snap = _invoice_snapshot(db, line['invoice_id'])
    return {**result, 'gross_cents': result['net_cents'] + result['tax_cents'], 'recoverable_account_id': snap['recoverable_account_id']}


def save_return_tax(db, return_id):
    if not _has(db, 'supplier_return_tax_lines'):
        return
    record = db.execute('SELECT status FROM supplier_returns WHERE id=?', (_id(return_id),)).fetchone()
    if record is None or record['status'] != 'draft':
        raise ValueError('نسخة ضريبة المرتجع المعتمد لا تعدل')
    db.execute('DELETE FROM supplier_return_tax_lines WHERE return_id=?', (return_id,))
    for line in db.execute('SELECT * FROM supplier_return_lines WHERE return_id=? ORDER BY id', (return_id,)).fetchall():
        amounts = return_line_amounts(db, line['invoice_line_id'], line['quantity'])
        if amounts is None:
            continue
        if amounts['gross_cents'] != line['total_cents']:
            raise ValueError('قيمة المرتجع لا تطابق صافي البند وضريبته الأصلية')
        db.execute('INSERT INTO supplier_return_tax_lines(return_line_id,return_id,invoice_line_id,net_cents,tax_cents,recoverable_cents,nonrecoverable_cents) VALUES(?,?,?,?,?,?,?)', (line['id'], return_id, line['invoice_line_id'], amounts['net_cents'], amounts['tax_cents'], amounts['recoverable_cents'], amounts['nonrecoverable_cents']))


def return_line_credits(db, return_id, invoice_line_id, expense_account_id, supplier_id, department_id):
    line = db.execute('SELECT * FROM supplier_return_lines WHERE return_id=? AND invoice_line_id=?', (return_id, invoice_line_id)).fetchone()
    if line is None:
        raise ValueError('بند المرتجع غير موجود')
    tax = db.execute('SELECT * FROM supplier_return_tax_lines WHERE return_line_id=?', (line['id'],)).fetchone() if _has(db, 'supplier_return_tax_lines') else None
    expense = tax['net_cents'] + tax['nonrecoverable_cents'] if tax else line['total_cents']
    common = {'supplier_id': supplier_id, 'department_id': department_id, 'source_line_id': invoice_line_id}
    lines = [{'account_id': expense_account_id, 'credit_cents': expense, **common}] if expense else []
    if tax and tax['recoverable_cents']:
        original = db.execute('SELECT invoice_id FROM invoice_lines WHERE id=?', (invoice_line_id,)).fetchone()
        snap = _invoice_snapshot(db, original['invoice_id'])
        lines.append({'account_id': snap['recoverable_account_id'], 'credit_cents': tax['recoverable_cents'], **common})
    return lines


def enrich_return(db, result):
    result = dict(result)
    totals = dict.fromkeys(('net_cents', 'tax_cents', 'recoverable_cents', 'nonrecoverable_cents', 'gross_cents'), 0)
    for line in result.get('lines', []):
        tax = db.execute('SELECT * FROM supplier_return_tax_lines WHERE return_line_id=?', (line['id'],)).fetchone() if _has(db, 'supplier_return_tax_lines') else None
        parts = {'net_cents': tax['net_cents'] if tax else line['total_cents'], 'tax_cents': tax['tax_cents'] if tax else 0,
                 'recoverable_cents': tax['recoverable_cents'] if tax else 0, 'nonrecoverable_cents': tax['nonrecoverable_cents'] if tax else 0, 'gross_cents': line['total_cents']}
        line.update(parts)
        for key in totals: totals[key] += parts[key]
    result.update(tax_cents=totals['tax_cents'], subtotal_cents=totals['net_cents'], tax_recoverable_cents=totals['recoverable_cents'], tax_nonrecoverable_cents=totals['nonrecoverable_cents'])
    return result


def _date(value, label):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value: raise ValueError()
    except (TypeError, ValueError):
        raise ValueError(label + ' غير صحيح') from None
    return value


def report(db, from_date=None, to_date=None):
    today = date.today().isoformat()
    start = _date(from_date or today[:4] + '-01-01', 'بداية التقرير')
    end = _date(to_date or today, 'نهاية التقرير')
    if end < start:
        raise ValueError('نهاية التقرير لا تسبق بدايته')
    rows = []
    for invoice in db.execute('''SELECT i.*,s.name supplier_name FROM invoices i JOIN suppliers s ON s.id=i.supplier_id JOIN invoice_postings p ON p.invoice_id=i.id WHERE i.business_date BETWEEN ? AND ? ORDER BY i.business_date,i.id''', (start, end)):
        invoice = dict(invoice)
        snap = _invoice_snapshot(db, invoice['id'])
        sums = db.execute('SELECT COALESCE(SUM(recoverable_cents),0),COALESCE(SUM(nonrecoverable_cents),0) FROM invoice_tax_lines WHERE invoice_id=?', (invoice['id'],)).fetchone() if _has(db, 'invoice_tax_lines') else (0, 0)
        rows.append({'source_type': 'supplier_invoice', 'source_id': invoice['id'], 'code': invoice['code'], 'date': invoice['business_date'], 'supplier_id': invoice['supplier_id'], 'supplier_name': invoice['supplier_name'], 'tax_code_id': snap['tax_code_id'] if snap else None, 'tax_code_name': snap['tax_code_name'] if snap else 'دون كود ضريبي', 'tax_kind': snap['kind'] if snap else None, 'net_cents': invoice['subtotal_cents'], 'tax_cents': invoice['tax_cents'], 'recoverable_cents': sums[0], 'nonrecoverable_cents': sums[1], 'gross_cents': invoice['total_cents']})
    for returned in db.execute("SELECT r.*,s.name supplier_name FROM supplier_returns r JOIN suppliers s ON s.id=r.supplier_id WHERE r.status='posted' AND r.business_date BETWEEN ? AND ? ORDER BY r.business_date,r.id", (start, end)):
        returned = dict(returned)
        snap = _invoice_snapshot(db, returned['invoice_id'])
        sums = db.execute('SELECT COALESCE(SUM(net_cents),0),COALESCE(SUM(tax_cents),0),COALESCE(SUM(recoverable_cents),0),COALESCE(SUM(nonrecoverable_cents),0) FROM supplier_return_tax_lines WHERE return_id=?', (returned['id'],)).fetchone() if _has(db, 'supplier_return_tax_lines') else (0, 0, 0, 0)
        net = sums[0] if snap else returned['total_cents']
        rows.append({'source_type': 'supplier_return', 'source_id': returned['id'], 'code': returned['code'], 'date': returned['business_date'], 'supplier_id': returned['supplier_id'], 'supplier_name': returned['supplier_name'], 'tax_code_id': snap['tax_code_id'] if snap else None, 'tax_code_name': snap['tax_code_name'] if snap else 'دون كود ضريبي', 'tax_kind': snap['kind'] if snap else None, 'net_cents': -net, 'tax_cents': -sums[1], 'recoverable_cents': -sums[2], 'nonrecoverable_cents': -sums[3], 'gross_cents': -returned['total_cents']})
    rows.sort(key=lambda row: (row['date'], row['source_type'], row['source_id']))
    keys = ('net_cents', 'tax_cents', 'recoverable_cents', 'nonrecoverable_cents', 'gross_cents')
    return {'from_date': start, 'to_date': end, 'currency': accounting.get_settings(db)['base_currency'], 'rows': rows,
            'totals': {key: sum(row[key] for row in rows) for key in keys}, 'basis': 'document_date', 'includes_returns': True, 'price_basis': 'exclusive'}


def preview(db, data):
    """Read-only exact-cent preview; never accepts an arbitrary posted tax rate."""
    incoming = data.get('lines')
    if not isinstance(incoming, list) or not 1 <= len(incoming) <= 100:
        raise ValueError('أضف من بند إلى مائة بند للمعاينة')
    lines = []
    for index, row in enumerate(incoming):
        if not isinstance(row, dict) or isinstance(row.get('quantity'), bool) or isinstance(row.get('price'), bool):
            raise ValueError('بند معاينة غير صحيح')
        try:
            qty, price = Decimal(str(row.get('quantity'))), Decimal(str(row.get('price')))
            if not qty.is_finite() or not price.is_finite() or not 0 < qty <= 1000000 or not 0 <= price <= 100000000:
                raise ValueError()
            if qty != qty.quantize(Decimal('.001')) or price != price.quantize(Decimal('.01')):
                raise ValueError()
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError('راجع الكمية حتى ثلاث منازل والسعر حتى منزلتين') from None
        product_id = row.get('product_id', index + 1)
        _id(product_id)
        total = _round(qty * price * 100)
        lines.append((product_id, '', '', str(qty), int(price * 100), total))
    subtotal = sum(line[-1] for line in lines)
    if subtotal > MAX_CENTS:
        raise ValueError('إجمالي المعاينة يتجاوز النطاق المسموح')
    adjusted, plan = prepare_invoice(db, data, (0, '', '', 0, '', subtotal, 0, 0, subtotal), lines)
    details = plan['lines'] if plan else [{'product_id': line[0], 'net_cents': line[-1], 'tax_cents': 0, 'recoverable_cents': 0, 'nonrecoverable_cents': 0} for line in lines]
    return {'subtotal_cents': adjusted[5], 'tax_cents': adjusted[7], 'total_cents': adjusted[8],
            'tax_recoverable_cents': sum(line['recoverable_cents'] for line in details),
            'tax_nonrecoverable_cents': sum(line['nonrecoverable_cents'] for line in details),
            'tax_code_id': plan['tax_code_id'] if plan else None, 'tax_rate': _percent(adjusted[6]),
            'price_basis': 'exclusive', 'lines': [{**line, 'gross_cents': line['net_cents'] + line['tax_cents']} for line in details]}
