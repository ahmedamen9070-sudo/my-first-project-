"""Recorded remittances of saved supplier withholding, without external actions.

The caller owns the transaction. Drafts reserve nothing; approved documents and
their allocations are immutable. Corrections append exact opposite journals.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

import accounting
import accounting_periods
import payables

MAX_CENTS = 10**13
HEADER_FIELDS = ('business_date', 'period_from', 'period_to', 'authority_name',
                 'reference', 'method', 'funding_account_id', 'notes', 'amount_cents')
SNAPSHOT_FIELDS = ('payment_id', 'account_id', 'supplier_id', 'amount_cents',
                   'payment_code', 'supplier_name', 'supplier_code', 'supplier_tax_number',
                   'withholding_code', 'withholding_name', 'rate_basis')
OPENING_SNAPSHOT_FIELDS = ('opening_source_id', 'account_id', 'supplier_id', 'amount_cents',
                           'payment_code', 'supplier_name', 'supplier_code', 'supplier_tax_number',
                           'source_date', 'reference')


def _has(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _source_key(row):
    return ('opening', row.get('source_id', row.get('opening_source_id'))) if row.get('source_type') == 'opening_withholding' else ('payment', row.get('payment_id'))


def _allocation_sort(row):
    kind, ident = _source_key(row)
    return (0 if kind == 'payment' else 1, ident)


def initialize(db):
    for sql in (
        '''CREATE TABLE IF NOT EXISTS withholding_remittances(
            id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,request_key TEXT NOT NULL UNIQUE,
            request_fingerprint TEXT NOT NULL,business_date TEXT NOT NULL,period_from TEXT NOT NULL,
            period_to TEXT NOT NULL,authority_name TEXT NOT NULL,reference TEXT NOT NULL,
            method TEXT NOT NULL CHECK(method IN('cash','bank_transfer')),
            funding_account_id INTEGER NOT NULL REFERENCES accounts(id),details TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN('draft','posted','cancelled')),
            revision INTEGER NOT NULL DEFAULT 1,journal_id INTEGER UNIQUE REFERENCES journal_entries(id),
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS withholding_remittance_allocations(
            remittance_id INTEGER NOT NULL REFERENCES withholding_remittances(id),
            payment_id INTEGER NOT NULL REFERENCES payment_withholding(payment_id),
            account_id INTEGER NOT NULL REFERENCES accounts(id),supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
            amount_cents INTEGER NOT NULL CHECK(amount_cents>0),payment_code TEXT NOT NULL,
            supplier_name TEXT NOT NULL,supplier_code TEXT NOT NULL,supplier_tax_number TEXT NOT NULL DEFAULT '',
            withholding_code TEXT NOT NULL,withholding_name TEXT NOT NULL,rate_basis INTEGER NOT NULL,
            PRIMARY KEY(remittance_id,payment_id))''',
        '''CREATE TABLE IF NOT EXISTS withholding_remittance_reversals(
            id INTEGER PRIMARY KEY,remittance_id INTEGER NOT NULL UNIQUE REFERENCES withholding_remittances(id),
            business_date TEXT NOT NULL,reference TEXT NOT NULL,reason TEXT NOT NULL,
            request_key TEXT NOT NULL UNIQUE,request_fingerprint TEXT NOT NULL,
            journal_id INTEGER NOT NULL UNIQUE REFERENCES journal_entries(id),created_at TEXT NOT NULL)''',
        'DROP INDEX IF EXISTS withholding_remittance_receipt',
        '''CREATE INDEX IF NOT EXISTS withholding_remittance_receipt_lookup
            ON withholding_remittances(authority_name,reference)''',
        'CREATE INDEX IF NOT EXISTS withholding_remittance_source ON withholding_remittance_allocations(payment_id,remittance_id)',
        'CREATE INDEX IF NOT EXISTS withholding_remittance_date ON withholding_remittances(status,business_date)',
    ):
        db.execute(sql)
    for table, condition in (
        ('withholding_remittances', "OLD.status='posted'"),
        ('withholding_remittance_allocations', "EXISTS(SELECT 1 FROM withholding_remittances WHERE id=OLD.remittance_id AND status='posted')"),
        ('withholding_remittance_reversals', '1'),
    ):
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()}
                BEFORE {operation} ON {table} WHEN {condition}
                BEGIN SELECT RAISE(ABORT,'posted withholding remittance is immutable'); END''')
    for operation in ('INSERT', 'UPDATE'):
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_remittance_allocation_new_parent_{operation.lower()}
            BEFORE {operation} ON withholding_remittance_allocations
            WHEN EXISTS(SELECT 1 FROM withholding_remittances WHERE id=NEW.remittance_id AND status='posted')
            BEGIN SELECT RAISE(ABORT,'posted withholding remittance allocations are immutable'); END''')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _money(value):
    if isinstance(value, bool):
        raise ValueError('مبلغ التوريد غير صحيح')
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 < amount <= Decimal('100000000000') or amount != amount.quantize(Decimal('.01')):
            raise ValueError()
        return int(amount * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('مبلغ التوريد موجب وبحد أقصى منزلتين عشريتين') from None


def _amount(data):
    if 'amount' in data:
        amount = _money(data['amount'])
        if 'amount_cents' in data and payables._money(data['amount_cents'], False) != amount:
            raise ValueError('مبالغ التوريد المدخلة غير متطابقة')
        return amount
    return payables._money(data.get('amount_cents'), False)


def _details(method, incoming):
    if not isinstance(incoming, dict):
        raise ValueError('تفاصيل طريقة التوريد مطلوبة')
    allowed = {'received_by', 'receipt_reference'} if method == 'cash' else {'bank_name', 'transaction_reference', 'value_date'}
    if set(incoming) - allowed:
        raise ValueError('تفاصيل لا تخص طريقة التوريد المختارة')
    result = {key: payables._text(incoming, key, 200, key != 'receipt_reference') for key in sorted(allowed)}
    if method == 'bank_transfer':
        result['value_date'] = payables._date(result['value_date'], 'تاريخ القيمة البنكي')
    return result


def _payload(data):
    if not isinstance(data, dict):
        raise ValueError('بيانات توريد الخصم غير صحيحة')
    method = data.get('method')
    if method not in ('cash', 'bank_transfer'):
        raise ValueError('توريد الخصم نقدي أو بتحويل بنكي')
    business_date = payables._date(data.get('business_date'), 'تاريخ التوريد')
    period_from = payables._date(data.get('period_from'), 'بداية فترة الخصم')
    period_to = payables._date(data.get('period_to'), 'نهاية فترة الخصم')
    if period_from > period_to or period_to > business_date:
        raise ValueError('فترة الخصم مرتبة ولا تنتهي بعد تاريخ التوريد')
    incoming = data.get('allocations')
    if not isinstance(incoming, list) or not 1 <= len(incoming) <= 500:
        raise ValueError('حدد سندات الخصم ومبالغ التوريد')
    allocations, seen = [], set()
    for item in incoming:
        if not isinstance(item, dict):
            raise ValueError('بند توزيع التوريد غير صحيح')
        if item.get('source_type') == 'opening_withholding':
            source_id = payables._id(item.get('source_id', item.get('opening_source_id')))
            if item.get('payment_id') is not None:
                raise ValueError('رصيد الخصم الافتتاحي مستقل عن سندات السداد')
            allocation = {'source_type': 'opening_withholding', 'source_id': source_id, 'amount_cents': _amount(item)}
        else:
            if item.get('source_type') not in (None, 'payment_withholding'):
                raise ValueError('نوع مصدر الخصم غير صحيح')
            allocation = {'payment_id': payables._id(item.get('payment_id')), 'amount_cents': _amount(item)}
        key = _source_key(allocation)
        if key in seen:
            raise ValueError('سند الخصم مكرر في توزيع التوريد')
        seen.add(key)
        allocations.append(allocation)
    amount = payables._money(sum(row['amount_cents'] for row in allocations), False)
    if ('amount' in data or 'amount_cents' in data) and _amount(data) != amount:
        raise ValueError('إجمالي التوريد يجب أن يساوي مجموع توزيعه')
    return {'business_date': business_date, 'period_from': period_from, 'period_to': period_to,
            'authority_name': payables._text(data, 'authority_name', 200, True),
            'reference': payables._text(data, 'reference', 100, True), 'method': method,
            'funding_account_id': payables._id(data.get('funding_account_id')),
            'details': _details(method, data.get('details', {})), 'notes': payables._text(data, 'notes', 2000),
            'amount_cents': amount, 'allocations': sorted(allocations, key=_allocation_sort)}


def _funding(db, value):
    account = payables._posting_account(db, value['funding_account_id'])
    prefix = '111' if value['method'] == 'cash' else '112'
    if account['normal_side'] != 'debit' or not account['code'].startswith(prefix):
        raise ValueError('اختر خزينة نقدية للتوريد النقدي أو حساب بنك للتحويل')
    return account


def _preview(db, value, exclude_remittance_id=None):
    import withholding_balances as balances
    import supplier_payments
    funding = _funding(db, value)
    report = balances.balance_report(db, value['business_date'], value['period_from'], value['period_to'])
    sources = {_source_key(row): row for row in report['sources']}
    allocations, by_account = [], {}
    for item in value['allocations']:
        source_key = _source_key(item)
        source = sources.get(source_key)
        if source is None:
            raise ValueError('سند الخصم ليس مرحلًا ضمن الفترة وتاريخ التوريد المحددين')
        opening = source_key[0] == 'opening'
        available = max((balances.opening_capacity if opening else balances.capacity)(db, source_key[1], value['business_date'], exclude_remittance_id), 0)
        if item['amount_cents'] > available:
            raise ValueError('التوريد يتجاوز الخصم المتاح للسند ' + source.get('payment_code', source.get('code', '')))
        # Snapshot the original source identity and account, never today's code
        # rate or mapping. These rows later identify the remittance journal.
        original = db.execute('SELECT * FROM opening_withholding_sources WHERE id=?' if opening else 'SELECT * FROM payment_withholding WHERE payment_id=?', (source_key[1],)).fetchone()
        if original is None or original['amount_cents' if opening else 'withholding_cents'] <= 0:
            raise ValueError('سند السداد لا يحتوي خصمًا مرحلًا موجبًا')
        original = dict(original)
        account = payables._posting_account(db, original['account_id'])
        if account['normal_side'] != 'credit' or not account['code'].startswith('2') or account['id'] == funding['id']:
            raise ValueError('حساب الخصم الأصلي ليس التزامًا مستقلًا صالحًا للتوريد')
        line = {'payment_id': None if opening else item['payment_id'], 'account_id': account['id'],
                'supplier_id': original['supplier_id'], 'amount_cents': item['amount_cents'],
                'payment_code': source.get('payment_code', source.get('code', '')),
                'supplier_name': original['supplier_name'], 'supplier_code': original['supplier_code'],
                'supplier_tax_number': original['supplier_tax_number'], 'withholding_code': 'OPENING' if opening else original['code'],
                'withholding_name': 'رصيد خصم افتتاحي' if opening else original['name'], 'rate_basis': None if opening else original['rate_basis'],
                'account_code': account['code'], 'account_name': account['name'], 'available_cents': available}
        if opening:
            line.update(source_type='opening_withholding', source_id=source_key[1], opening_source_id=source_key[1],
                        source_date=original['source_date'], payment_date=original['source_date'], reference=original['reference'], rate=None)
        allocations.append(line)
        by_account[account['id']] = by_account.get(account['id'], 0) + item['amount_cents']
    account_caps = {}
    for account_id, amount in by_account.items():
        cap = max(balances.account_capacity(db, account_id, value['business_date'], exclude_remittance_id), 0)
        account_caps[account_id] = cap
        if amount > cap:
            raise ValueError('إجمالي التوريد يتجاوز صافي المستحق على حساب الخصم بعد خصم الزيادات السابقة')
    for row in allocations:
        row['account_available_cents'] = account_caps[row['account_id']]
    before = supplier_payments.account_balance(db, funding['id'], value['business_date'])
    after = before - value['amount_cents']
    warnings = []
    if after < 0:
        warnings.append('الرصيد الدفتري لمصدر التوريد غير كافٍ؛ راجع الأرصدة الافتتاحية والحركات.')
    if not report['matched']:
        warnings.append('حسابات الخصم غير مطابقة للقيود؛ راجع الفروق قبل اعتماد التوريد.')
    return {'business_date': value['business_date'], 'period_from': value['period_from'], 'period_to': value['period_to'],
            'amount_cents': value['amount_cents'], 'currency': accounting.get_settings(db)['base_currency'],
            'allocations': allocations, 'account_capacities': [{'account_id': key, 'available_cents': amount} for key, amount in sorted(account_caps.items())],
            'funding_account_id': funding['id'], 'funding_account_code': funding['code'], 'funding_account_name': funding['name'],
            'funding_balance_before_cents': before, 'funding_balance_after_cents': after,
            'matched': bool(report['matched']), 'can_approve': bool(report['matched']),
            'exceptions': report.get('exceptions', []), 'accounts': report.get('accounts', []), 'warnings': warnings}


def preview(db, data):
    return _preview(db, _payload(data))


def _receipt_unique(db, value, exclude=0):
    # The caller serializes writes. A correction can reuse the actual receipt
    # reference after its previous recording was reversed, without editing it.
    if db.execute("""SELECT 1 FROM withholding_remittances r
        WHERE r.authority_name=? AND r.reference=? AND r.status<>'cancelled' AND r.id<>?
        AND NOT EXISTS(SELECT 1 FROM withholding_remittance_reversals rr
            WHERE rr.remittance_id=r.id AND rr.business_date<=?)""",
        (value['authority_name'], value['reference'], exclude, value['business_date'])).fetchone():
        raise ValueError('مرجع توريد الجهة مستخدم في مستند آخر؛ راجع المستند المحفوظ')


def save(db, data, remittance_id=None):
    if not isinstance(data, dict):
        raise ValueError('بيانات توريد الخصم غير صحيحة')
    remittance_id = remittance_id if remittance_id is not None else data.get('id')
    old = payables._require(db, 'withholding_remittances', remittance_id) if remittance_id is not None else None
    if old:
        payables._revision(data, old)
        if old['status'] != 'draft':
            raise ValueError('التوريد المعتمد أو الملغى لا يعدل')
        if data.get('action') == 'cancel':
            accounting_periods.assert_open(db, old['business_date'])
            db.execute("UPDATE withholding_remittances SET status='cancelled',revision=revision+1,updated_at=? WHERE id=?", (payables._now(), old['id']))
            payables._audit(db, 'withholding_remittance', old['id'], 'cancel_draft', {})
            return detail(db, old['id'])
    value = _payload(data)
    digest = _digest(value)
    key = old['request_key'] if old else payables._text(data, 'request_key', 100, True)
    if old is None:
        prior = db.execute('SELECT id,request_fingerprint FROM withholding_remittances WHERE request_key=?', (key,)).fetchone()
        if prior:
            if prior['request_fingerprint'] != digest:
                raise accounting.ConflictError('مفتاح الطلب مستخدم لتوريد ببيانات أخرى')
            return detail(db, prior['id'])
    accounting_periods.assert_document_dates(db, value['business_date'], old['business_date'] if old else None)
    plan = _preview(db, value, old['id'] if old else None)
    _receipt_unique(db, value, old['id'] if old else 0)
    stamp = payables._now()
    values = tuple(value[field] for field in HEADER_FIELDS)
    if old:
        remittance_id = old['id']
        db.execute(f"UPDATE withholding_remittances SET request_fingerprint=?,{','.join(field+'=?' for field in HEADER_FIELDS)},details=?,revision=revision+1,updated_at=? WHERE id=?", (digest, *values, _json(value['details']), stamp, remittance_id))
        db.execute('DELETE FROM withholding_remittance_allocations WHERE remittance_id=?', (remittance_id,))
        if _has(db, 'opening_remittance_allocations'):
            db.execute('DELETE FROM opening_remittance_allocations WHERE remittance_id=?', (remittance_id,))
    else:
        code = payables._code(db, 'withholding_remittance', 'WTR')
        remittance_id = db.execute(f"INSERT INTO withholding_remittances(code,request_key,request_fingerprint,{','.join(HEADER_FIELDS)},details,created_at,updated_at) VALUES(?,?,?,{','.join('?' for _ in HEADER_FIELDS)},?,?,?)", (code, key, digest, *values, _json(value['details']), stamp, stamp)).lastrowid
    for row in plan['allocations']:
        opening = row.get('source_type') == 'opening_withholding'
        table = 'opening_remittance_allocations' if opening else 'withholding_remittance_allocations'
        fields = OPENING_SNAPSHOT_FIELDS if opening else SNAPSHOT_FIELDS
        db.execute(f"INSERT INTO {table}(remittance_id,{','.join(fields)}) VALUES(?,{','.join('?' for _ in fields)})", (remittance_id, *(row[field] for field in fields)))
    payables._audit(db, 'withholding_remittance', remittance_id, 'update_draft' if old else 'create_draft', {'amount_cents': value['amount_cents'], 'matched': plan['matched']})
    return detail(db, remittance_id)


def _stored_value(db, old):
    allocations = [dict(row) for row in db.execute('SELECT payment_id,amount_cents FROM withholding_remittance_allocations WHERE remittance_id=? ORDER BY payment_id', (old['id'],))]
    if _has(db, 'opening_remittance_allocations'):
        allocations += [{'source_type': 'opening_withholding', 'source_id': row['opening_source_id'], 'amount_cents': row['amount_cents']}
                        for row in db.execute('SELECT opening_source_id,amount_cents FROM opening_remittance_allocations WHERE remittance_id=? ORDER BY opening_source_id', (old['id'],))]
    return {field: old[field] for field in HEADER_FIELDS} | {'details': json.loads(old['details']),
        'allocations': allocations}


def _snapshots(db, remittance_id):
    rows = [dict(row) for row in db.execute(f"SELECT {','.join(SNAPSHOT_FIELDS)} FROM withholding_remittance_allocations WHERE remittance_id=? ORDER BY payment_id", (remittance_id,))]
    if _has(db, 'opening_remittance_allocations'):
        rows += [{**dict(row), 'source_type': 'opening_withholding'} for row in db.execute(f"SELECT {','.join(OPENING_SNAPSHOT_FIELDS)} FROM opening_remittance_allocations WHERE remittance_id=? ORDER BY opening_source_id", (remittance_id,))]
    return rows


def approve(db, remittance_id, data):
    old = payables._require(db, 'withholding_remittances', remittance_id)
    if old['status'] == 'posted':
        return detail(db, old['id'])
    payables._revision(data, old)
    if old['status'] != 'draft':
        raise ValueError('التوريد الملغى لا يعتمد')
    accounting_periods.assert_open(db, old['business_date'])
    value = _stored_value(db, old)
    if accounting.get_settings(db)['base_currency'] != 'EGP':
        raise ValueError('التوريد محفوظ بالجنيه المصري؛ راجع العملة الأساسية')
    plan = _preview(db, value, old['id'])
    if not plan['can_approve']:
        raise ValueError('لا يعتمد التوريد قبل مطابقة حسابات الخصم مع القيود')
    import withholding_balances as balances
    for boundary in balances.capacity_dates(db, old['business_date']):
        if not balances.balance_report(db, boundary)['matched']:
            raise ValueError('توجد فروق لاحقة في حسابات الخصم؛ راجع القيود قبل اعتماد توريد بتاريخ أقدم')
    stored = _snapshots(db, old['id'])
    current = [{**{field: row[field] for field in (OPENING_SNAPSHOT_FIELDS if row.get('source_type') == 'opening_withholding' else SNAPSHOT_FIELDS)},
                **({'source_type': 'opening_withholding'} if row.get('source_type') == 'opening_withholding' else {})} for row in plan['allocations']]
    if stored != current:
        raise accounting.ConflictError('تغيرت نسخة مصادر التوريد؛ راجع المستند وأعد حفظه')
    _receipt_unique(db, value, old['id'])
    lines = [{'account_id': row['account_id'], 'debit_cents': row['amount_cents'],
              'supplier_id': row['supplier_id'], 'source_line_id': row.get('payment_id', row.get('opening_source_id'))} for row in stored]
    lines.append({'account_id': old['funding_account_id'], 'credit_cents': old['amount_cents']})
    journal_id = payables.create_journal(db, 'withholding_remittance', old['id'], old['business_date'], lines)
    db.execute("UPDATE withholding_remittances SET status='posted',journal_id=?,revision=revision+1,updated_at=? WHERE id=?", (journal_id, payables._now(), old['id']))
    payables._audit(db, 'withholding_remittance', old['id'], 'post', {'journal_id': journal_id, 'amount_cents': old['amount_cents'], 'warnings': plan['warnings']})
    return detail(db, old['id'])


def reverse(db, remittance_id, data):
    if not isinstance(data, dict):
        raise ValueError('بيانات عكس التوريد غير صحيحة')
    old = payables._require(db, 'withholding_remittances', remittance_id)
    value = {'remittance_id': old['id'], 'business_date': payables._date(data.get('business_date'), 'تاريخ عكس التوريد'),
             'reference': payables._text(data, 'reference', 100, True), 'reason': payables._text(data, 'reason', 2000, True)}
    key = payables._text(data, 'request_key', 100, True)
    event_revision = data.get('event_revision', data.get('adjustment_revision', 0))
    if type(data.get('revision')) is not int or type(event_revision) is not int:
        raise accounting.ConflictError('أعد فتح مستند التوريد قبل العكس')
    if 'event_revision' in data and 'adjustment_revision' in data and (type(data['adjustment_revision']) is not int or data['adjustment_revision'] != event_revision):
        raise accounting.ConflictError('حالة عكس التوريد تغيرت؛ أعد فتح المستند')
    prior = db.execute('SELECT * FROM withholding_remittance_reversals WHERE remittance_id=? OR request_key=?', (old['id'], key)).fetchone()
    if prior:
        if prior['remittance_id'] != old['id'] or prior['request_key'] != key or prior['request_fingerprint'] != _digest(value):
            raise accounting.ConflictError('التوريد معكوس بالفعل أو مفتاح الطلب مستخدم لإجراء آخر')
        return detail(db, old['id'])
    if old['status'] != 'posted':
        raise ValueError('عكس التوريد متاح للمستند المعتمد فقط')
    payables._revision(data, old)
    if event_revision != 0:
        raise accounting.ConflictError('حالة عكس التوريد تغيرت؛ أعد فتح المستند')
    accounting_periods.assert_open(db, value['business_date'])
    if value['business_date'] < old['business_date']:
        raise ValueError('تاريخ عكس التوريد لا يسبق المستند الأصلي')
    original = payables.journal_detail(db, old['journal_id'])
    if original['source_type'] != 'withholding_remittance' or original['source_id'] != old['id']:
        raise ValueError('قيد التوريد الأصلي لا يطابق المستند')
    reversal_id = db.execute('SELECT COALESCE(MAX(id),0)+1 FROM withholding_remittance_reversals').fetchone()[0]
    lines = [{'account_id': row['account_id'], 'debit_cents': row['credit_cents'], 'credit_cents': row['debit_cents'],
              'supplier_id': row['supplier_id'], 'department_id': row['department_id'], 'source_line_id': row['source_line_id']} for row in original['lines']]
    journal_id = payables.create_journal(db, 'withholding_remittance_reversal', reversal_id, value['business_date'], lines)
    db.execute('''INSERT INTO withholding_remittance_reversals(id,remittance_id,business_date,reference,reason,request_key,request_fingerprint,journal_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', (reversal_id, old['id'], value['business_date'], value['reference'], value['reason'], key, _digest(value), journal_id, payables._now()))
    payables._audit(db, 'withholding_remittance_reversal', reversal_id, 'reverse', {'remittance_id': old['id'], 'journal_id': journal_id, 'reason': value['reason']})
    return detail(db, old['id'])


def detail(db, remittance_id, as_of=None):
    result = payables._require(db, 'withholding_remittances', remittance_id)
    result['details'] = json.loads(result['details'])
    result.pop('request_fingerprint', None)
    account = payables._require(db, 'accounts', result['funding_account_id'])
    result.update(funding_account_code=account['code'], funding_account_name=account['name'], currency=accounting.get_settings(db)['base_currency'])
    result['allocations'] = [dict(row) for row in db.execute('''SELECT r.*,a.code account_code,a.name account_name,p.payment_date
        FROM withholding_remittance_allocations r JOIN accounts a ON a.id=r.account_id
        JOIN supplier_payments p ON p.id=r.payment_id WHERE remittance_id=? ORDER BY r.payment_id''', (result['id'],))]
    for line in result['allocations']:
        line['rate'] = format(Decimal(line['rate_basis']) / 100, '.2f')
    if _has(db, 'opening_remittance_allocations'):
        for row in db.execute('''SELECT r.*,a.code account_code,a.name account_name
            FROM opening_remittance_allocations r JOIN accounts a ON a.id=r.account_id
            WHERE remittance_id=? ORDER BY r.opening_source_id''', (result['id'],)):
            line = dict(row)
            line.update(source_type='opening_withholding', source_id=line['opening_source_id'], payment_id=None,
                        payment_date=line['source_date'], withholding_code='OPENING',
                        withholding_name='رصيد خصم افتتاحي', rate_basis=None, rate=None)
            result['allocations'].append(line)
    result['journal'] = payables.journal_detail(db, result['journal_id']) if result['journal_id'] else None
    as_of = payables._as_of(as_of if as_of is not None else '9999-12-31')
    reversed_row = db.execute('SELECT * FROM withholding_remittance_reversals WHERE remittance_id=? AND business_date<=?', (result['id'], as_of)).fetchone()
    result['reversal'] = dict(reversed_row) if reversed_row else None
    if result['reversal']:
        result['reversal'].pop('request_fingerprint', None)
        result['reversal'].update(code=f"WTRR-{reversed_row['id']:06d}", journal=payables.journal_detail(db, reversed_row['journal_id']))
    result['effective_status'] = 'reversed' if reversed_row else result['status']
    result['adjustment_revision'] = 1 if reversed_row else 0
    result['voucher_title'] = 'إذن توريد خصم ضريبي نقدي' if result['method'] == 'cash' else 'إشعار توريد خصم ضريبي بنكي'
    return result


def list_remittances(db, as_of=None):
    if as_of is None:
        rows = db.execute('SELECT id FROM withholding_remittances ORDER BY business_date DESC,id DESC LIMIT 500').fetchall()
    else:
        as_of = payables._as_of(as_of)
        rows = db.execute('SELECT id FROM withholding_remittances WHERE business_date<=? ORDER BY business_date DESC,id DESC LIMIT 500', (as_of,)).fetchall()
    return [detail(db, row['id'], as_of) for row in rows]
