"""Supplier disbursements. Mutations participate in the caller's transaction.

Money is integer EGP piastres; a cheque is a liability until separately cleared.
Drafts do not affect the ledger, supplier balances, or invoice allocations.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import accounting
import accounting_periods
import payables


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _text(data, key, maximum=200, required=False):
    value = data.get(key, '')
    if not isinstance(value, str):
        raise ValueError('صيغة غير صحيحة للحقل: ' + key)
    value = value.strip()
    if len(value) > maximum or (required and not value):
        raise ValueError('راجع الحقل المطلوب: ' + key)
    return value


def _id(value):
    if type(value) is not int or value < 1:
        raise ValueError('اختيار غير صحيح')
    return value


def _date(value, label='التاريخ'):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError(label + ' غير صحيح') from None
    return value


def _cents(value):
    if isinstance(value, bool):
        raise ValueError('مبلغ السداد غير صحيح')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number > Decimal('100000000000'):
            raise ValueError()
        if number != number.quantize(Decimal('.01')):
            raise ValueError()
        return int(number * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('المبلغ موجب وبحد أقصى منزلتين عشريتين') from None


def _amount(data):
    if 'amount' in data:
        return _cents(data['amount'])
    value = data.get('amount_cents')
    if type(value) is not int or not 0 < value <= 10**13:
        raise ValueError('مبلغ السداد غير صحيح')
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _fingerprint(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _audit(db, payment_id, action, details):
    accounting._audit(db, 'supplier_payment', payment_id, action, details)


def _next_code(db):
    db.execute("INSERT INTO sequences(kind,value) VALUES('supplier_payment',1) ON CONFLICT(kind) DO UPDATE SET value=value+1")
    value = db.execute("SELECT value FROM sequences WHERE kind='supplier_payment'").fetchone()[0]
    return f'SPY-{value:06d}'


def initialize(db):
    for statement in [
        '''CREATE TABLE IF NOT EXISTS supplier_payments(
            id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, request_key TEXT NOT NULL UNIQUE,
            request_fingerprint TEXT NOT NULL, supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
            payment_date TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
            currency TEXT NOT NULL DEFAULT 'EGP' CHECK(currency='EGP'),
            method TEXT NOT NULL CHECK(method IN('cash','cheque','bank_transfer','bank_deposit','instapay','bank_card')),
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN('draft','posted','cancelled')),
            details TEXT NOT NULL, funding_account_id INTEGER NOT NULL REFERENCES accounts(id),
            allocation_mode TEXT NOT NULL CHECK(allocation_mode IN('manual','fifo','invoice')),
            target_invoice_id INTEGER REFERENCES invoices(id), requested_allocations TEXT NOT NULL DEFAULT '[]',
            allow_advance INTEGER NOT NULL DEFAULT 0 CHECK(allow_advance IN(0,1)), notes TEXT NOT NULL DEFAULT '',
            advance_cents INTEGER NOT NULL DEFAULT 0 CHECK(advance_cents>=0),
            journal_entry_id INTEGER REFERENCES journal_entries(id),
            cheque_bank_key TEXT, cheque_number_key TEXT,
            cheque_status TEXT CHECK(cheque_status IN('issued','cleared')),
            clearance_date TEXT, clearance_reference TEXT, clearance_bank_account_id INTEGER REFERENCES accounts(id),
            clearance_journal_entry_id INTEGER REFERENCES journal_entries(id),
            revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS active_supplier_cheque ON supplier_payments(cheque_bank_key,cheque_number_key)
            WHERE method='cheque' AND status!='cancelled' ''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS active_supplier_cheque_source ON supplier_payments(funding_account_id,cheque_number_key)
            WHERE method='cheque' AND status!='cancelled' ''',
        '''CREATE TABLE IF NOT EXISTS payment_allocations(
            id INTEGER PRIMARY KEY, payment_id INTEGER NOT NULL REFERENCES supplier_payments(id),
            invoice_id INTEGER NOT NULL REFERENCES invoices(id), amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
            UNIQUE(payment_id,invoice_id))''',
        'CREATE INDEX IF NOT EXISTS payment_allocations_invoice ON payment_allocations(invoice_id)',
        'CREATE INDEX IF NOT EXISTS supplier_payments_supplier_date ON supplier_payments(supplier_id,payment_date)',
    ]:
        db.execute(statement)
    protected = ('supplier_id', 'payment_date', 'amount_cents', 'currency', 'method', 'status', 'details',
                 'funding_account_id', 'allocation_mode', 'target_invoice_id', 'requested_allocations',
                 'allow_advance', 'notes', 'advance_cents', 'journal_entry_id', 'code', 'request_key',
                 'request_fingerprint', 'cheque_bank_key', 'cheque_number_key', 'created_at')
    financial_change = ' OR '.join(f'NEW.{column} IS NOT OLD.{column}' for column in protected)
    db.execute(f'''CREATE TRIGGER IF NOT EXISTS payment_immutable_posted_update BEFORE UPDATE ON supplier_payments
        WHEN OLD.status='posted' AND ({financial_change}) BEGIN SELECT RAISE(ABORT,'posted supplier payment is immutable'); END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS payment_immutable_posted_delete BEFORE DELETE ON supplier_payments
        WHEN OLD.status='posted' BEGIN SELECT RAISE(ABORT,'posted supplier payment is immutable'); END''')
    cleared_fields = ('cheque_status', 'clearance_date', 'clearance_reference', 'clearance_bank_account_id', 'clearance_journal_entry_id')
    cleared_change = ' OR '.join(f'NEW.{column} IS NOT OLD.{column}' for column in cleared_fields)
    db.execute(f'''CREATE TRIGGER IF NOT EXISTS payment_immutable_cleared_update BEFORE UPDATE ON supplier_payments
        WHEN OLD.cheque_status='cleared' AND ({cleared_change}) BEGIN SELECT RAISE(ABORT,'cleared cheque is immutable'); END''')
    for operation, alias in [('INSERT', 'NEW'), ('UPDATE', 'OLD'), ('DELETE', 'OLD')]:
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS payment_immutable_allocations_{operation.lower()}
            BEFORE {operation} ON payment_allocations WHEN EXISTS(SELECT 1 FROM supplier_payments WHERE id={alias}.payment_id AND status='posted')
            BEGIN SELECT RAISE(ABORT,'posted payment allocations are immutable'); END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS payment_immutable_allocations_new_parent_update
        BEFORE UPDATE ON payment_allocations WHEN EXISTS(SELECT 1 FROM supplier_payments WHERE id=NEW.payment_id AND status='posted')
        BEGIN SELECT RAISE(ABORT,'posted payment allocations are immutable'); END''')
    # The original chart has no issued-cheque liability. Add an explicit proposed
    # mapping; operators must review it just like the original chart mappings.
    parent = db.execute("SELECT id FROM accounts WHERE code='217000' AND posting=0").fetchone()
    if parent and not db.execute("SELECT 1 FROM accounts WHERE code='217004'").fetchone():
        db.execute('''INSERT INTO accounts(code,name,parent_id,posting,normal_side,statement,active,system,notes,role,created_at,updated_at)
            VALUES('217004','شيكات صادرة للموردين تحت التحصيل',?,1,'credit','balance_sheet',1,1,?, 'issued_supplier_cheques',?,?)''',
                   (parent['id'], 'يثبت إصدار الشيك هنا؛ يخفض البنك فقط عند إثبات التحصيل.', _now(), _now()))
    account = db.execute("SELECT id FROM accounts WHERE code='217004'").fetchone()
    if account:
        db.execute('''INSERT OR IGNORE INTO account_mappings(module,role,label,account_id,allowed_classes,status,expected_side,updated_at)
            VALUES('الموردون','issued_cheques','شيكات صادرة للموردين تحت التحصيل',?,'["2"]','proposed','credit',?)''',
                   (account['id'], _now()))


def payment_methods():
    def field(name, label, kind='text', required=True, options=None):
        value = {'name': name, 'label': label, 'type': kind, 'required': required}
        if options is not None:
            value['options'] = options
        return value
    return [
        {'id': 'cash', 'label': 'نقدي', 'voucher_title': 'إذن صرف نقدية وإقرار استلام المورد', 'funding_prefix': '111',
         'fields': [field('received_by', 'اسم مستلم النقدية'), field('receipt_reference', 'رقم إيصال المورد', required=False)]},
        {'id': 'cheque', 'label': 'شيك', 'voucher_title': 'إذن إصدار وتسليم شيك للمورد', 'funding_prefix': '112',
         'fields': [field('bank_name', 'البنك المسحوب عليه'), field('cheque_number', 'رقم الشيك'),
                    field('cheque_book', 'دفتر الشيكات'), field('beneficiary', 'المستفيد'),
                    field('cheque_date', 'تاريخ إصدار الشيك', 'date'), field('due_date', 'تاريخ الاستحقاق', 'date')]},
        {'id': 'bank_transfer', 'label': 'تحويل بنكي', 'voucher_title': 'إذن تحويل بنكي للمورد', 'funding_prefix': '112',
         'fields': [field('bank_name', 'بنك المستفيد'), field('beneficiary', 'اسم المستفيد'),
                    field('beneficiary_account', 'حساب المستفيد أو IBAN'), field('transaction_reference', 'مرجع التحويل'),
                    field('value_date', 'تاريخ القيمة', 'date')]},
        {'id': 'bank_deposit', 'label': 'إيداع بنكي', 'voucher_title': 'إذن إيداع بحساب المورد', 'funding_prefixes': ['111', '112'],
         'fields': [field('bank_name', 'بنك المستفيد'), field('beneficiary_account', 'حساب المستفيد'),
                    field('deposit_reference', 'رقم قسيمة الإيداع'), field('value_date', 'تاريخ الإيداع', 'date')]},
        {'id': 'instapay', 'label': 'إنستا باي', 'voucher_title': 'إذن سداد للمورد عبر إنستا باي', 'funding_prefix': '112',
         'fields': [field('recipient', 'حساب أو هاتف المستفيد'), field('transaction_reference', 'مرجع العملية'),
                    field('value_date', 'تاريخ العملية', 'date')]},
        {'id': 'bank_card', 'label': 'بطاقة بنكية', 'voucher_title': 'إذن سداد للمورد ببطاقة بنكية', 'funding_prefix': '112',
         'fields': [field('card_type', 'نوع البطاقة', 'select', options=['Visa', 'MasterCard', 'Meeza', 'other']),
                    field('last_four', 'آخر أربعة أرقام فقط'), field('transaction_reference', 'مرجع العملية'),
                    field('value_date', 'تاريخ العملية', 'date')]},
    ]


def _funding(db, account_id, method):
    account = db.execute('SELECT * FROM accounts WHERE id=?', (_id(account_id),)).fetchone()
    prefixes = ('111',) if method == 'cash' else ('111', '112') if method == 'bank_deposit' else ('112',)
    if not account or not account['active'] or not account['posting'] or account['normal_side'] != 'debit' or not account['code'].startswith(prefixes):
        if method == 'cash':
            raise ValueError('اختر حساب خزينة نقدية نشط 111')
        if method == 'bank_deposit':
            raise ValueError('اختر حساب خزينة 111 أو بنك 112 نشطًا كمصدر الإيداع')
        raise ValueError('اختر حساب بنك نشط 112؛ حسابات تحصيل البطاقات ليست مصدر سداد')
    return dict(account)


def _details(data, method):
    if not isinstance(data, dict):
        raise ValueError('تفاصيل طريقة السداد مطلوبة')
    fields = next(item['fields'] for item in payment_methods() if item['id'] == method)
    allowed = {field['name'] for field in fields}
    if set(data) - allowed:
        raise ValueError('حقول غير معتمدة لطريقة السداد؛ لا تحفظ رقم البطاقة الكامل أو رمز الأمان')
    result = {}
    for field in fields:
        value = _text(data, field['name'], 200, field['required'])
        if field['type'] == 'date':
            _date(value, field['label'])
        if 'options' in field and value not in field['options']:
            raise ValueError('نوع البطاقة غير صحيح')
        result[field['name']] = value
    if method == 'bank_card' and not re.fullmatch(r'[0-9]{4}', result['last_four']):
        raise ValueError('اكتب آخر أربعة أرقام فقط من البطاقة')
    if method == 'cheque' and result['due_date'] < result['cheque_date']:
        raise ValueError('استحقاق الشيك لا يسبق تاريخ إصداره')
    return result


def _payload(db, data):
    if not isinstance(data, dict):
        raise ValueError('بيانات السداد غير صحيحة')
    forbidden = {'pan', 'cvv', 'cvc', 'card_number', 'full_card_number', 'security_code', 'pin'}
    if forbidden.intersection(key.lower() for key in data):
        raise ValueError('لا تحفظ رقم البطاقة الكامل أو رمز الأمان')
    supplier_id = _id(data.get('supplier_id'))
    if not db.execute('SELECT id FROM suppliers WHERE id=?', (supplier_id,)).fetchone():
        raise ValueError('المورد غير موجود')
    if data.get('currency', 'EGP') != 'EGP' or accounting.get_settings(db)['base_currency'] != 'EGP':
        raise ValueError('سداد الموردين في هذه المرحلة بالجنيه المصري EGP فقط')
    method = data.get('method')
    if method not in {item['id'] for item in payment_methods()}:
        raise ValueError('طريقة السداد غير صحيحة')
    funding = _funding(db, data.get('funding_account_id'), method)
    mode = data.get('allocation_mode', 'manual')
    if mode not in ('manual', 'fifo', 'invoice'):
        raise ValueError('اختر تخصيصًا يدويًا أو أقدم استحقاق أو فاتورة معينة')
    target = _id(data.get('target_invoice_id')) if mode == 'invoice' else None
    requested = data.get('allocations', [])
    if not isinstance(requested, list) or len(requested) > 500:
        raise ValueError('توزيع السداد غير صحيح')
    allocations, seen = [], set()
    for line in requested:
        if not isinstance(line, dict):
            raise ValueError('بند تخصيص غير صحيح')
        invoice_id = _id(line.get('invoice_id'))
        if invoice_id in seen:
            raise ValueError('الفاتورة مكررة في التوزيع')
        seen.add(invoice_id)
        allocations.append({'invoice_id': invoice_id, 'amount_cents': _amount(line)})
    if mode != 'manual' and allocations:
        raise ValueError('التوزيع اليدوي متاح عند اختيار التخصيص اليدوي فقط')
    if type(data.get('allow_advance', False)) is not bool:
        raise ValueError('اختيار الدفعة المقدمة غير صحيح')
    return {'supplier_id': supplier_id, 'payment_date': _date(data.get('payment_date'), 'تاريخ السداد'),
            'amount_cents': _amount(data), 'currency': 'EGP', 'method': method,
            'funding_account_id': funding['id'], 'allocation_mode': mode, 'target_invoice_id': target,
            'requested_allocations': allocations, 'details': _details(data.get('details', {}), method),
            'allow_advance': data.get('allow_advance', False), 'notes': _text(data, 'notes', 2000)}


def account_balance(db, account_id, as_of=None):
    _id(account_id)
    if as_of:
        _date(as_of)
    return db.execute('''SELECT COALESCE(SUM(l.debit_cents-l.credit_cents),0)
        FROM journal_lines l JOIN journal_entries j ON j.id=l.journal_id
        WHERE l.account_id=? AND j.sealed=1 AND (? IS NULL OR j.entry_date<=?)''',
                      (account_id, as_of, as_of)).fetchone()[0]


def funding_balances(db, as_of=None):
    return [{**dict(row), 'balance_cents': account_balance(db, row['id'], as_of)}
            for row in db.execute("SELECT id,code,name FROM accounts WHERE active=1 AND posting=1 AND normal_side='debit' AND (code LIKE '111%' OR code LIKE '112%') ORDER BY code")]


def _preview(db, value):
    supplier_id, payment_date, amount = value['supplier_id'], value['payment_date'], value['amount_cents']
    import supplier_settlements
    # Capacity must remain valid at every future accounting boundary. An
    # intermediate payment followed by its later reversal still used capacity
    # during that interval; checking only the start and final balance misses it.
    dates = supplier_settlements.capacity_dates(db, payment_date)
    snapshots = [(day, payables.open_invoices(db, supplier_id, day), payables.supplier_balances(db, supplier_id, day)) for day in dates]
    historical = next(rows for day, rows, _ in snapshots if day == payment_date)
    balances = next(balance for day, _, balance in snapshots if day == payment_date)
    by_date = [{row.get('invoice_id', row.get('id')): row for row in rows} for _, rows, _ in snapshots]
    eligible = {}
    for row in historical:
        invoice_id = row.get('invoice_id', row.get('id'))
        available = min(snapshot.get(invoice_id, {}).get('outstanding_cents', 0) for snapshot in by_date)
        eligible[invoice_id] = {**row, 'invoice_id': invoice_id, 'outstanding_cents': available}
    net = min(balance['net_payable_cents'] for _, _, balance in snapshots)
    ceiling = max(net, 0)
    allocations = []
    if value['allocation_mode'] == 'manual':
        for line in value['requested_allocations']:
            row = eligible.get(line['invoice_id'])
            if not row:
                raise ValueError('الفاتورة ليست معتمدة لهذا المورد في تاريخ السداد')
            if line['amount_cents'] > row['outstanding_cents']:
                raise ValueError('المخصص يتجاوز المتبقي على الفاتورة ' + row['code'])
            allocations.append({'invoice_id': row['invoice_id'], 'code': row['code'],
                                'outstanding_cents': row['outstanding_cents'], 'amount_cents': line['amount_cents']})
    elif value['allocation_mode'] == 'invoice':
        row = eligible.get(value['target_invoice_id'])
        if not row:
            raise ValueError('الفاتورة ليست معتمدة لهذا المورد في تاريخ السداد')
        allocated = min(amount, row['outstanding_cents'], ceiling)
        if allocated:
            allocations.append({'invoice_id': row['invoice_id'], 'code': row['code'],
                                'outstanding_cents': row['outstanding_cents'], 'amount_cents': allocated})
    else:
        remaining = min(amount, ceiling)
        for row in sorted(eligible.values(), key=lambda item: (item.get('due_date') or item.get('business_date', ''), item['invoice_id'])):
            allocated = min(remaining, row['outstanding_cents'])
            if allocated:
                allocations.append({'invoice_id': row['invoice_id'], 'code': row['code'],
                                    'outstanding_cents': row['outstanding_cents'], 'amount_cents': allocated})
                remaining -= allocated
    allocated = sum(line['amount_cents'] for line in allocations)
    if allocated > amount:
        raise ValueError('مجموع التخصيص أكبر من مبلغ السداد')
    if allocated > ceiling:
        raise ValueError('التوزيع يتجاوز صافي المستحق للمورد؛ راجع أرصدة المرتجعات والدفعات المقدمة أولًا')
    advance = amount - allocated
    before = account_balance(db, value['funding_account_id'], payment_date)
    after = before if value['method'] == 'cheque' else before - amount
    warnings = []
    if value['method'] != 'cheque' and after < 0:
        warnings.append('الرصيد الدفتري لمصدر السداد غير كافٍ؛ راجع الأرصدة الافتتاحية والحركات قبل الاعتماد.')
    if balances['unapplied_return_credit_cents']:
        warnings.append('للمورد رصيد دائن من مرتجعات؛ لم يوزع تلقائيًا على فواتير أخرى.')
    if balances['advance_cents']:
        warnings.append('توجد دفعات مقدمة سابقة للمورد لم تطبق على فواتير بعد.')
    if advance and not value['allow_advance']:
        warnings.append('يتبقى مبلغ غير موزع؛ وزعه على الفواتير أو اختر تسجيله دفعة مقدمة صراحةً.')
    return {'currency': 'EGP', 'allocations': allocations, 'allocated_cents': allocated, 'advance_cents': advance,
            'net_payable_cents': balances['net_payable_cents'], 'outstanding_cents': balances['outstanding_cents'],
            'payable_balance_cents': balances.get('payable_balance_cents', balances['net_payable_cents']),
            'available_payable_cents': ceiling,
            'unapplied_return_credit_cents': balances['unapplied_return_credit_cents'],
            'existing_advance_cents': balances['advance_cents'], 'funding_balance_before_cents': before,
            'funding_balance_after_cents': after, 'warnings': warnings,
            'can_approve_allocation': not advance or value['allow_advance'],
            'open_invoices': list(eligible.values())}


def allocation_preview(db, data):
    return _preview(db, _payload(db, data))


def outstanding_for_supplier(db, supplier_id, as_of=None):
    return {'invoices': payables.open_invoices(db, _id(supplier_id), as_of),
            **payables.supplier_balances(db, supplier_id, as_of)}


def _cheque_unique(db, value, exclude_id=0):
    if value['method'] != 'cheque':
        return None, None
    bank = value['details']['bank_name'].casefold()
    number = value['details']['cheque_number'].casefold()
    if db.execute("SELECT id FROM supplier_payments WHERE method='cheque' AND status!='cancelled' AND (cheque_bank_key=? OR funding_account_id=?) AND cheque_number_key=? AND id!=?", (bank, value['funding_account_id'], number, exclude_id)).fetchone():
        raise ValueError('رقم الشيك مسجل بالفعل على نفس البنك')
    return bank, number


def save_payment(db, data):
    key = _text(data, 'request_key', 100, True)
    value = _payload(db, data)
    digest = _fingerprint(value)
    old = db.execute('SELECT id,request_fingerprint FROM supplier_payments WHERE request_key=?', (key,)).fetchone()
    if old:
        if old['request_fingerprint'] != digest:
            raise accounting.ConflictError('مفتاح الطلب مستخدم لسداد مختلف؛ أعد فتح السداد المحفوظ')
        return get_payment(db, old['id'])
    accounting_periods.assert_open(db, value['payment_date'])
    _preview(db, value)
    bank, number = _cheque_unique(db, value)
    stamp = _now()
    cursor = db.execute('''INSERT INTO supplier_payments(code,request_key,request_fingerprint,supplier_id,payment_date,amount_cents,
        method,details,funding_account_id,allocation_mode,target_invoice_id,requested_allocations,allow_advance,notes,
        cheque_bank_key,cheque_number_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (_next_code(db), key, digest, value['supplier_id'], value['payment_date'], value['amount_cents'], value['method'],
         _json(value['details']), value['funding_account_id'], value['allocation_mode'], value['target_invoice_id'],
         _json(value['requested_allocations']), int(value['allow_advance']), value['notes'], bank, number, stamp, stamp))
    _audit(db, cursor.lastrowid, 'create_draft', {'amount_cents': value['amount_cents'], 'method': value['method']})
    return get_payment(db, cursor.lastrowid)


def _payment(db, payment_id):
    row = db.execute('SELECT * FROM supplier_payments WHERE id=?', (_id(payment_id),)).fetchone()
    if not row:
        raise ValueError('سند السداد غير موجود')
    return dict(row)


def update_payment(db, payment_id, data):
    old = _payment(db, payment_id)
    accounting._revision(data, old)
    if old['status'] != 'draft':
        raise ValueError('السداد المعتمد أو الملغى لا يعدل')
    if data.get('action') == 'cancel':
        accounting_periods.assert_open(db, old['payment_date'])
        db.execute("UPDATE supplier_payments SET status='cancelled',revision=revision+1,updated_at=? WHERE id=?", (_now(), payment_id))
        _audit(db, payment_id, 'cancel_draft', {})
        return get_payment(db, payment_id)
    value = _payload(db, data)
    accounting_periods.assert_document_dates(db, value['payment_date'], old['payment_date'])
    _preview(db, value)
    bank, number = _cheque_unique(db, value, payment_id)
    db.execute('''UPDATE supplier_payments SET supplier_id=?,payment_date=?,amount_cents=?,method=?,details=?,funding_account_id=?,
        allocation_mode=?,target_invoice_id=?,requested_allocations=?,allow_advance=?,notes=?,cheque_bank_key=?,cheque_number_key=?,
        revision=revision+1,updated_at=? WHERE id=?''',
        (value['supplier_id'], value['payment_date'], value['amount_cents'], value['method'], _json(value['details']), value['funding_account_id'],
         value['allocation_mode'], value['target_invoice_id'], _json(value['requested_allocations']), int(value['allow_advance']),
         value['notes'], bank, number, _now(), payment_id))
    _audit(db, payment_id, 'update_draft', {'amount_cents': value['amount_cents']})
    return get_payment(db, payment_id)


def _saved_payload(row):
    return {**row, 'details': json.loads(row['details']), 'requested_allocations': json.loads(row['requested_allocations']),
            'allow_advance': bool(row['allow_advance'])}


def approve_payment(db, payment_id, data):
    row = _payment(db, payment_id)
    if row['status'] == 'posted':
        return get_payment(db, payment_id)
    accounting._revision(data, row)
    if row['status'] != 'draft':
        raise ValueError('السداد الملغى لا يعتمد')
    accounting_periods.assert_open(db, row['payment_date'])
    saved = _saved_payload(row)
    if accounting.get_settings(db)['base_currency'] != 'EGP':
        raise ValueError('راجع العملة الأساسية؛ سند السداد محفوظ بالجنيه المصري')
    # Revalidate account/details and live balances: a draft is not a reservation.
    _funding(db, row['funding_account_id'], row['method'])
    _details(saved['details'], row['method'])
    _cheque_unique(db, saved, payment_id)
    preview = _preview(db, saved)
    if not preview['can_approve_allocation']:
        raise ValueError('وزع كامل السداد أو اختر تسجيل المتبقي دفعة مقدمة قبل الاعتماد')
    payables.mapping_account(db, 'الموردون', 'payable_control')
    lines = []
    by_account = {}
    for allocation in preview['allocations']:
        posting = db.execute('SELECT payable_account_id FROM invoice_postings WHERE invoice_id=?', (allocation['invoice_id'],)).fetchone()
        if not posting:
            raise ValueError('الفاتورة غير معتمدة')
        account_id = posting['payable_account_id']
        by_account[account_id] = by_account.get(account_id, 0) + allocation['amount_cents']
    for account_id, amount in by_account.items():
        lines.append({'account_id': account_id, 'debit_cents': amount, 'credit_cents': 0, 'supplier_id': row['supplier_id']})
    if preview['advance_cents']:
        account = payables.mapping_account(db, 'الموردون', 'supplier_advances')
        lines.append({'account_id': account['id'], 'debit_cents': preview['advance_cents'], 'credit_cents': 0, 'supplier_id': row['supplier_id']})
    funding_id = row['funding_account_id']
    if row['method'] == 'cheque':
        funding_id = payables.mapping_account(db, 'الموردون', 'issued_cheques')['id']
        payable_ids = {item['payable_account_id'] for item in db.execute('SELECT DISTINCT payable_account_id FROM invoice_postings')}
        payable_ids.update(item['account_id'] for item in db.execute("SELECT account_id FROM account_mappings WHERE module='الموردون' AND role IN('payable_control','capital_payable_control')"))
        if funding_id in payable_ids:
            raise ValueError('حساب الشيكات الصادرة يجب أن يكون التزامًا مستقلًا عن حساب الموردين الرقابي')
    lines.append({'account_id': funding_id, 'debit_cents': 0, 'credit_cents': row['amount_cents'], 'supplier_id': row['supplier_id']})
    journal_id = payables.create_journal(db, 'supplier_payment', payment_id, row['payment_date'], lines)
    for allocation in preview['allocations']:
        db.execute('INSERT INTO payment_allocations(payment_id,invoice_id,amount_cents) VALUES(?,?,?)',
                   (payment_id, allocation['invoice_id'], allocation['amount_cents']))
    db.execute("UPDATE supplier_payments SET status='posted',advance_cents=?,journal_entry_id=?,cheque_status=?,revision=revision+1,updated_at=? WHERE id=?",
               (preview['advance_cents'], journal_id, 'issued' if row['method'] == 'cheque' else None, _now(), payment_id))
    _audit(db, payment_id, 'post', {'journal_entry_id': journal_id, 'allocated_cents': preview['allocated_cents'],
                                 'advance_cents': preview['advance_cents'], 'warnings': preview['warnings']})
    return get_payment(db, payment_id)


def clear_cheque(db, payment_id, data):
    row = _payment(db, payment_id)
    if row['method'] != 'cheque' or row['status'] != 'posted':
        raise ValueError('التحصيل متاح للشيك المعتمد فقط')
    clearance_date = _date(data.get('clearance_date'), 'تاريخ تحصيل الشيك')
    if payables._has_table(db, 'supplier_cheque_events'):
        import supplier_adjustments
        supplier_adjustments.assert_cheque_can_clear(db, payment_id, clearance_date)
        events = supplier_adjustments.cheque_events(db, payment_id, '9999-12-31')
        if events and (type(data.get('event_revision')) is not int or data['event_revision'] != len(events)):
            raise accounting.ConflictError('حالة الشيك تغيرت؛ أعد فتح المستند قبل إثبات الصرف')
    reference = _text(data, 'reference', 200, True)
    bank = _funding(db, data.get('bank_account_id'), 'bank_transfer')
    if bank['id'] != row['funding_account_id']:
        raise ValueError('حساب تحصيل الشيك يجب أن يطابق البنك المسحوب عليه في سند الإصدار')
    if row['cheque_status'] == 'cleared':
        if (clearance_date, reference, bank['id']) != (row['clearance_date'], row['clearance_reference'], row['clearance_bank_account_id']):
            raise accounting.ConflictError('الشيك محصل بالفعل ببيانات أخرى')
        return get_payment(db, payment_id)
    accounting._revision(data, row)
    accounting_periods.assert_open(db, clearance_date)
    if clearance_date < max(row['payment_date'], json.loads(row['details'])['cheque_date']):
        raise ValueError('تحصيل الشيك لا يسبق تاريخ إصداره أو سنده')
    # Debit the actual liability credited on issue, even if mappings later change.
    liability = db.execute('''SELECT l.account_id FROM journal_lines l JOIN accounts a ON a.id=l.account_id
        WHERE l.journal_id=? AND l.credit_cents>0 AND a.code LIKE '2%' ''', (row['journal_entry_id'],)).fetchone()
    if not liability:
        raise ValueError('قيد إصدار الشيك لا يحتوي حساب التزام صحيح')
    amount = row['amount_cents']
    journal_id = payables.create_journal(db, 'supplier_cheque_clearance', payment_id, clearance_date,
        [{'account_id': liability['account_id'], 'debit_cents': amount, 'credit_cents': 0, 'supplier_id': row['supplier_id']},
         {'account_id': bank['id'], 'debit_cents': 0, 'credit_cents': amount, 'supplier_id': row['supplier_id']}])
    db.execute("UPDATE supplier_payments SET cheque_status='cleared',clearance_date=?,clearance_reference=?,clearance_bank_account_id=?,clearance_journal_entry_id=?,revision=revision+1,updated_at=? WHERE id=?",
               (clearance_date, reference, bank['id'], journal_id, _now(), payment_id))
    _audit(db, payment_id, 'clear_cheque', {'clearance_journal_entry_id': journal_id, 'clearance_date': clearance_date})
    return get_payment(db, payment_id)


def _journal(db, journal_id):
    if not journal_id:
        return None
    row = dict(db.execute('SELECT * FROM journal_entries WHERE id=?', (journal_id,)).fetchone())
    row['lines'] = [dict(line) for line in db.execute('''SELECT l.*,a.code account_code,a.name account_name
        FROM journal_lines l JOIN accounts a ON a.id=l.account_id WHERE l.journal_id=? ORDER BY l.id''', (journal_id,))]
    return row


def get_payment(db, payment_id):
    result = _saved_payload(_payment(db, payment_id))
    supplier = db.execute('SELECT code,name FROM suppliers WHERE id=?', (result['supplier_id'],)).fetchone()
    result.update(supplier_code=supplier['code'], supplier_name=supplier['name'])
    account = db.execute('SELECT code,name FROM accounts WHERE id=?', (result['funding_account_id'],)).fetchone()
    result.update(funding_account_code=account['code'], funding_account_name=account['name'])
    result['allocations'] = [dict(row) for row in db.execute('''SELECT p.invoice_id,i.code,i.reference,i.business_date,p.amount_cents
        FROM payment_allocations p JOIN invoices i ON i.id=p.invoice_id WHERE p.payment_id=? ORDER BY p.id''', (payment_id,))]
    result['allocated_cents'] = sum(row['amount_cents'] for row in result['allocations'])
    result['journal'] = _journal(db, result['journal_entry_id'])
    result['clearance_journal'] = _journal(db, result['clearance_journal_entry_id'])
    result['voucher_title'] = next(item['voucher_title'] for item in payment_methods() if item['id'] == result['method'])
    result.pop('request_fingerprint', None)
    result.pop('cheque_bank_key', None)
    result.pop('cheque_number_key', None)
    if payables._has_table(db, 'supplier_payment_reversals'):
        import supplier_adjustments
        result = supplier_adjustments.payment_overlay(db, result, '9999-12-31')
    return result


def list_payments(db, supplier_id=None):
    if supplier_id is not None:
        _id(supplier_id)
    return [get_payment(db, row['id']) for row in db.execute('''SELECT id FROM supplier_payments
        WHERE (? IS NULL OR supplier_id=?) ORDER BY payment_date DESC,id DESC LIMIT 500''', (supplier_id, supplier_id))]
