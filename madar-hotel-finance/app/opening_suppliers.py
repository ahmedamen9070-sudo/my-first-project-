"""Supplier opening sources, without recording purchases or cash payments.

The opening batch supplies one balanced journal per line. The invoice header is
an allocation identity for an existing payable, not a new supplier purchase.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

import accounting
import payables

KINDS = ('supplier_invoice', 'supplier_credit', 'supplier_advance')


def _has(db, table):
    return payables._has_table(db, table)


def initialize(db):
    for sql in (
        '''CREATE TABLE IF NOT EXISTS opening_supplier_invoices(
            invoice_id INTEGER PRIMARY KEY REFERENCES invoices(id),
            opening_line_id INTEGER NOT NULL UNIQUE REFERENCES opening_lines(id),
            kind TEXT NOT NULL CHECK(kind IN('supplier_invoice','supplier_credit')),
            source_date TEXT NOT NULL,remaining_net_cents INTEGER CHECK(remaining_net_cents>=0))''',
        '''CREATE TABLE IF NOT EXISTS opening_supplier_advances(
            id INTEGER PRIMARY KEY,opening_line_id INTEGER NOT NULL UNIQUE REFERENCES opening_lines(id),
            code TEXT NOT NULL UNIQUE,supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
            account_id INTEGER NOT NULL REFERENCES accounts(id),business_date TEXT NOT NULL,
            source_date TEXT NOT NULL,reference TEXT NOT NULL,amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
            journal_id INTEGER NOT NULL UNIQUE REFERENCES journal_entries(id),created_at TEXT NOT NULL)''',
    ):
        db.execute(sql)
    for table in ('opening_supplier_invoices', 'opening_supplier_advances'):
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()}
                BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'supplier opening source is immutable'); END''')


def _net(data, amount):
    extra = data.get('extra') or {}
    value = data.get('remaining_net_cents', extra.get('remaining_net_cents'))
    incoming = data.get('remaining_net_amount', extra.get('remaining_net_amount'))
    if incoming not in (None, ''):
        try:
            number = Decimal(str(incoming))
            if isinstance(incoming, bool) or not number.is_finite() or number < 0 or number != number.quantize(Decimal('.01')):
                raise ValueError()
            parsed = int(number * 100)
        except (ValueError, TypeError, InvalidOperation):
            raise ValueError('صافي الرصيد الافتتاحي موجب أو صفر وبحد أقصى منزلتين') from None
        if value is not None and value != parsed:
            raise ValueError('صافي الرصيد الافتتاحي المدخل غير متطابق')
        value = parsed
    if value is not None:
        payables._money(value)
        if value > amount:
            raise ValueError('صافي الوعاء المتبقي لا يتجاوز إجمالي رصيد الفاتورة')
    return value


def prepare(db, line, business_date):
    value = dict(line)
    kind = value.get('kind')
    if kind not in KINDS:
        raise ValueError('نوع رصيد المورد الافتتاحي غير صحيح')
    supplier = payables._require(db, 'suppliers', value.get('supplier_id'))
    amount = payables._money(value.get('amount_cents'), allow_zero=False)
    account = payables.mapping_account(db, 'الموردون', 'supplier_advances' if kind == 'supplier_advance' else 'payable_control', require_confirmed=False)
    expected_side = 'debit' if kind in ('supplier_credit', 'supplier_advance') else 'credit'
    if value.get('account_id') not in (None, account['id']):
        raise ValueError('حساب افتتاح المورد يحدد من الربط المعتمد للموردين')
    if value.get('side') not in (None, expected_side):
        raise ValueError('جانب رصيد المورد لا يطابق نوع الافتتاح')
    reference = payables._text(value, 'reference', 100, True)
    if kind == 'supplier_invoice' and not value.get('source_date'):
        raise ValueError('أدخل تاريخ الفاتورة الأصلي لرصيد المورد الافتتاحي')
    source_date = payables._date(value.get('source_date') or business_date, 'تاريخ مستند المورد الأصلي')
    if source_date > business_date:
        raise ValueError('تاريخ مستند المورد الأصلي لا يلي تاريخ الافتتاح')
    if kind == 'supplier_invoice' and not value.get('due_date'):
        raise ValueError('أدخل تاريخ الاستحقاق الأصلي لفاتورة المورد الافتتاحية لحساب أعمار الديون')
    due = payables._date(value.get('due_date') or business_date, 'تاريخ استحقاق الرصيد')
    if kind == 'supplier_invoice' and due < source_date:
        raise ValueError('استحقاق الفاتورة لا يسبق تاريخ مستندها الأصلي')
    if kind in ('supplier_invoice', 'supplier_credit'):
        if db.execute("SELECT 1 FROM invoices WHERE supplier_id=? AND reference=? AND status<>'cancelled'", (supplier['id'], reference)).fetchone():
            raise ValueError('رقم مستند المورد مسجل بالفعل؛ لا تسجل رصيده مرتين')
    department_id = value.get('department_id')
    if department_id is not None:
        payables._require(db, 'departments', department_id)
    net = _net(value, amount) if kind == 'supplier_invoice' else None
    return {**value, 'kind': kind, 'account_id': account['id'], 'side': expected_side,
            'amount_cents': amount, 'supplier_id': supplier['id'], 'custodian_id': None,
            'reference': reference, 'source_date': source_date, 'due_date': due,
            'department_id': department_id, 'remaining_net_cents': net,
            'extra': {**(value.get('extra') or {}), 'remaining_net_cents': net}}


def post_line(db, line, journal_id):
    initialize(db)
    kind, line_id = line['kind'], line['id']
    if kind == 'supplier_advance':
        code = payables._code(db, 'opening_supplier_advance', 'OSA')
        ident = db.execute('''INSERT INTO opening_supplier_advances(opening_line_id,code,supplier_id,account_id,
            business_date,source_date,reference,amount_cents,journal_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (line_id, code, line['supplier_id'], line['account_id'], line['business_date'],
             line['source_date'], line['reference'], line['amount_cents'], journal_id, payables._now())).lastrowid
        return {'document_type': 'opening_supplier_advance', 'document_id': ident, 'advance_id': ident, 'code': code}
    if kind not in ('supplier_invoice', 'supplier_credit'):
        raise ValueError('نوع افتتاح المورد غير صحيح')
    # Legacy invoice allocations require a real invoice identity. No product or
    # expense line is created, and the opening journal contains no expense/VAT.
    department_id = line.get('department_id')
    if department_id is None:
        department = db.execute('SELECT id FROM departments ORDER BY id LIMIT 1').fetchone()
        if department is None:
            raise ValueError('أضف قسمًا لتخزين هوية مستند المورد الافتتاحي')
        department_id = department['id']
    signed = line['amount_cents'] if kind == 'supplier_invoice' else -line['amount_cents']
    code = payables._code(db, 'opening_supplier_invoice', 'OSI' if kind == 'supplier_invoice' else 'OSC')
    net = line.get('remaining_net_cents', (line.get('extra') or {}).get('remaining_net_cents'))
    stamp = payables._now()
    cursor = db.execute('''INSERT INTO invoices(code,request_key,supplier_id,reference,business_date,department_id,
        notes,status,subtotal_cents,tax_basis,tax_cents,total_cents,revision,created_at,updated_at,kind)
        VALUES(?,?,?,?,?,?,?,'draft',?,0,0,?,1,?,?,'service')''',
        (code, 'opening-supplier:' + str(line_id), line['supplier_id'], line['reference'], line['business_date'],
         department_id, 'رصيد افتتاحي لمستند سابق؛ لا يثبت شراءً أو مصروفًا جديدًا.', net or 0, signed, stamp, stamp))
    invoice_id = cursor.lastrowid
    db.execute('INSERT INTO invoice_terms(invoice_id,due_date) VALUES(?,?)', (invoice_id, line['due_date']))
    db.execute('INSERT INTO opening_supplier_invoices(invoice_id,opening_line_id,kind,source_date,remaining_net_cents) VALUES(?,?,?,?,?)',
               (invoice_id, line_id, kind, line['source_date'], net))
    db.execute('INSERT INTO invoice_postings(invoice_id,journal_id,payable_account_id,posted_at) VALUES(?,?,?,?)',
               (invoice_id, journal_id, line['account_id'], stamp))
    return {'document_type': 'opening_supplier_invoice', 'document_id': invoice_id, 'invoice_id': invoice_id, 'code': code}


def invoice_origin(db, invoice_id):
    if not _has(db, 'opening_supplier_invoices'):
        return None
    row = db.execute('SELECT * FROM opening_supplier_invoices WHERE invoice_id=?', (invoice_id,)).fetchone()
    return dict(row) if row else None


def origin(data):
    value = data.get('source_origin', 'operating')
    if value not in ('operating', 'opening'):
        raise ValueError('نوع مصدر رصيد المورد غير صحيح')
    if value == 'opening' and data.get('source_type') != 'advance':
        raise ValueError('رصيد الافتتاح المستقل متاح للدفعات المقدمة فقط')
    return value


def source_key(data):
    return data['source_type'], data['source_id'], data.get('source_origin', 'operating')


def origin_clause(db, table, alias, source_origin='operating'):
    columns = {row['name'] for row in db.execute('PRAGMA table_info(' + table + ')')}
    if 'source_origin' in columns:
        return f' AND {alias}.source_origin=?', [source_origin]
    return ('', []) if source_origin == 'operating' else (' AND 0', [])


def advance(db, source_id, supplier_id=None):
    if not _has(db, 'opening_supplier_advances'):
        raise ValueError('رصيد الدفعة المقدمة الافتتاحي غير موجود')
    row = payables._require(db, 'opening_supplier_advances', source_id)
    if supplier_id is not None and row['supplier_id'] != supplier_id:
        raise ValueError('الرصيد الافتتاحي لا يتبع المورد المختار')
    return row


def advance_sources(db, supplier_id, as_of):
    if not _has(db, 'opening_supplier_advances'):
        return []
    import supplier_settlements
    result = []
    for row in db.execute('SELECT * FROM opening_supplier_advances WHERE supplier_id=? AND business_date<=? ORDER BY business_date,id', (supplier_id, as_of)):
        row = dict(row)
        used = supplier_settlements.source_used(db, 'advance', row['id'], as_of, source_origin='opening')
        refunded = supplier_settlements._refund_used(db, 'advance', row['id'], as_of, source_origin='opening')
        result.append({'source_type': 'advance', 'source_origin': 'opening', 'source_id': row['id'],
            'supplier_id': supplier_id, 'code': row['code'], 'reference': row['reference'],
            'source_date': row['business_date'], 'business_date': row['business_date'],
            'original_source_date': row['source_date'], 'original_cents': row['amount_cents'],
            'settled_cents': used, 'refunded_cents': refunded,
            'available_cents': max(row['amount_cents'] - used - refunded, 0), 'account_id': row['account_id']})
    return result


def total_advances(db, supplier_id, as_of):
    if not _has(db, 'opening_supplier_advances'):
        return 0
    return db.execute('SELECT COALESCE(SUM(amount_cents),0) FROM opening_supplier_advances WHERE supplier_id=? AND business_date<=?', (supplier_id, as_of)).fetchone()[0]
