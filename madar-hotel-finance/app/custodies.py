"""Operating cash custodies: immutable documents, dated capacity and reconciliation.

The caller owns the transaction. No operation commits, creates opening balances,
settles supplier invoices, or calculates tax. Drafts do not reserve a custody.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import accounting
import accounting_periods
import payables
import supplier_payments

ALL_TIME = '9999-12-31'
KINDS = ('issue', 'settlement', 'return')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _fingerprint(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _as_of(value=None):
    if value is None:
        try:
            value = datetime.now(ZoneInfo('Africa/Cairo')).date().isoformat()
        except ZoneInfoNotFoundError:
            value = date.today().isoformat()
    return payables._date(value, 'تاريخ التقرير')


def _amount(value):
    if isinstance(value, bool):
        raise ValueError('المبلغ موجب وبحد أقصى منزلتين عشريتين')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 < number <= Decimal(payables.MAX_CENTS) / 100 or number != number.quantize(Decimal('.01')):
            raise ValueError()
        return int(number * 100)
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError('المبلغ موجب وبحد أقصى منزلتين عشريتين') from None


def initialize(db):
    for statement in (
        '''CREATE TABLE IF NOT EXISTS custodians(id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,phone TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1 CHECK(active IN(0,1)),
            revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS custody_documents(id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,
            request_key TEXT NOT NULL UNIQUE,request_fingerprint TEXT NOT NULL,prepared_fingerprint TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN('issue','settlement','return')),business_date TEXT NOT NULL,
            reference TEXT NOT NULL,custodian_id INTEGER NOT NULL REFERENCES custodians(id),
            source_issue_id INTEGER REFERENCES custody_documents(id),cash_account_id INTEGER REFERENCES accounts(id),
            custody_account_id INTEGER NOT NULL REFERENCES accounts(id),amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
            purpose TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',payload TEXT NOT NULL,
            custodian_name_snapshot TEXT NOT NULL DEFAULT '',custodian_code_snapshot TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN('draft','posted','cancelled')),
            revision INTEGER NOT NULL DEFAULT 1,journal_id INTEGER UNIQUE REFERENCES journal_entries(id),
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS custody_lines(id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES custody_documents(id),
            expense_account_id INTEGER NOT NULL REFERENCES accounts(id),
            department_id INTEGER NOT NULL REFERENCES departments(id),reference TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK(amount_cents>0))''',
        '''CREATE TABLE IF NOT EXISTS custody_reversals(id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL UNIQUE REFERENCES custody_documents(id),code TEXT NOT NULL UNIQUE,
            request_key TEXT NOT NULL UNIQUE,request_fingerprint TEXT NOT NULL,business_date TEXT NOT NULL,
            reference TEXT NOT NULL,reason TEXT NOT NULL,journal_id INTEGER NOT NULL UNIQUE REFERENCES journal_entries(id),
            created_at TEXT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS custody_source_date ON custody_documents(source_issue_id,business_date)',
        'CREATE INDEX IF NOT EXISTS custody_person_date ON custody_documents(custodian_id,business_date)',
    ):
        db.execute(statement)
    for table, condition in (
        ('custody_documents', "OLD.status='posted'"),
        ('custody_lines', "EXISTS(SELECT 1 FROM custody_documents WHERE id=OLD.document_id AND status='posted')"),
        ('custody_reversals', '1'),
    ):
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()}
                BEFORE {operation} ON {table} WHEN {condition}
                BEGIN SELECT RAISE(ABORT,'posted custody document is immutable'); END''')
    for operation in ('INSERT', 'UPDATE'):
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_custody_lines_parent_{operation.lower()}
            BEFORE {operation} ON custody_lines
            WHEN EXISTS(SELECT 1 FROM custody_documents WHERE id=NEW.document_id AND status='posted')
            BEGIN SELECT RAISE(ABORT,'posted custody lines are immutable'); END''')


def save_custodian(db, data, custodian_id=None):
    if not isinstance(data, dict):
        raise ValueError('بيانات صاحب العهدة غير صحيحة')
    old = payables._require(db, 'custodians', custodian_id) if custodian_id is not None else None
    if old:
        payables._revision(data, old)
    name = payables._text(data, 'name', 150, True)
    phone = payables._text(data, 'phone', 40)
    active = data.get('active', True)
    if type(active) is not bool:
        raise ValueError('حالة صاحب العهدة غير صحيحة')
    if db.execute('SELECT 1 FROM custodians WHERE name=? AND id<>?', (name, custodian_id or 0)).fetchone():
        raise ValueError('صاحب العهدة مسجل بنفس الاسم')
    stamp = payables._now()
    if old:
        db.execute('UPDATE custodians SET name=?,phone=?,active=?,revision=revision+1,updated_at=? WHERE id=?', (name, phone, int(active), stamp, custodian_id))
    else:
        code = payables._code(db, 'custodian', 'CUS')
        custodian_id = db.execute('INSERT INTO custodians(code,name,phone,active,created_at,updated_at) VALUES(?,?,?,?,?,?)', (code, name, phone, int(active), stamp, stamp)).lastrowid
    accounting._audit(db, 'custodian', custodian_id, 'update' if old else 'create', {'name': name})
    return payables._require(db, 'custodians', custodian_id)


def _cash_accounts(db):
    excluded = {r['account_id'] for r in db.execute("SELECT account_id FROM account_mappings WHERE module='الخزينة' AND role='cash_transfer_clearing'")}
    return [dict(r) for r in db.execute("SELECT * FROM accounts WHERE active=1 AND posting=1 AND normal_side='debit' AND code LIKE '111%' ORDER BY code") if r['code'] != '111005' and r['role'] != 'cash_in_transit' and r['id'] not in excluded]


def _blocked_accounts(db):
    """Independent subledgers must never share the custody control account."""
    import supplier_reconciliation
    payable, advances = supplier_reconciliation._account_ids(db)
    result = set(payable) | set(advances)
    result.update(r[0] for r in db.execute("SELECT account_id FROM account_mappings WHERE module='الضرائب' OR role='issued_cheques'"))
    for table, field in (('payment_withholding', 'account_id'), ('invoice_tax_settings', 'recoverable_account_id'),
                         ('withholding_remittance_allocations', 'account_id'), ('opening_withholding_sources', 'account_id'),
                         ('opening_remittance_allocations', 'account_id')):
        columns = {r['name'] for r in db.execute('PRAGMA table_info(' + table + ')')}
        if field in columns:
            result.update(r[0] for r in db.execute(f'SELECT DISTINCT {field} FROM {table}') if r[0] is not None)
    return result


def _custody_account(db, require_confirmed=True):
    account = payables.mapping_account(db, 'الخزينة', 'operating_custodies', require_confirmed=require_confirmed)
    if not account['code'].startswith('115') or account['normal_side'] != 'debit' or account['id'] in _blocked_accounts(db):
        raise ValueError('حساب العهد التشغيلية يجب أن يكون أصلًا مدينًا مستقلًا عن الموردين والضرائب')
    return account


def _cash(db, account_id):
    import treasury
    return treasury._cash_account(db, account_id)


def config(db):
    import treasury
    blocked = treasury.blocked_counter_account_ids(db)
    mapping = db.execute("SELECT m.*,a.code account_code,a.name account_name FROM account_mappings m JOIN accounts a ON a.id=m.account_id WHERE m.module='الخزينة' AND m.role='operating_custodies'").fetchone()
    return {'custodians': [dict(r) for r in db.execute('SELECT * FROM custodians ORDER BY name,id')],
            'cash_accounts': _cash_accounts(db),
            'expense_accounts': [dict(r) for r in db.execute("SELECT * FROM accounts WHERE active=1 AND posting=1 AND normal_side='debit' AND substr(code,1,1) IN('5','6','8') ORDER BY code") if r['id'] not in blocked],
            'departments': [dict(r) for r in db.execute('SELECT * FROM departments ORDER BY id')],
            'custody_mapping': dict(mapping) if mapping else None,
            'opening_status': accounting.opening_status(db),
            'notes': ['العهد التشغيلية مستقلة عن مقدمات الموردين وسلف الرواتب.', 'تسوية العهدة بمستندات المصروف المباشر فقط؛ فواتير الموردين المسجلة تستخدم دورة الموردين دون تكرار.']}


def _normalize(data):
    if not isinstance(data, dict) or data.get('kind') not in KINDS:
        raise ValueError('نوع مستند العهدة غير صحيح')
    if data.get('currency', 'EGP') != 'EGP':
        raise ValueError('مستندات العهد بالجنيه المصري EGP فقط')
    kind = data['kind']
    value = {'kind': kind, 'business_date': payables._date(data.get('business_date'), 'تاريخ العهدة'),
             'reference': payables._text(data, 'reference', 100, True),
             'custodian_id': payables._id(data.get('custodian_id')) if kind == 'issue' else None,
             'source_issue_id': payables._id(data.get('source_issue_id')) if kind != 'issue' else None,
             'cash_account_id': payables._id(data.get('cash_account_id')) if kind != 'settlement' else None,
             'amount_cents': _amount(data.get('amount')) if kind != 'settlement' else 0,
             'purpose': payables._text(data, 'purpose', 500, kind == 'issue'),
             'notes': payables._text(data, 'notes', 2000), 'lines': []}
    if kind == 'settlement':
        if not isinstance(data.get('lines'), list) or not 1 <= len(data['lines']) <= 100:
            raise ValueError('أضف مستندات مصروف العهدة من بند إلى ١٠٠ بند')
        seen = set()
        for line in data['lines']:
            if not isinstance(line, dict):
                raise ValueError('بند تسوية غير صحيح')
            item = {'expense_account_id': payables._id(line.get('expense_account_id')),
                    'department_id': payables._id(line.get('department_id')),
                    'reference': payables._text(line, 'reference', 100, True),
                    'amount_cents': _amount(line.get('amount'))}
            # A receipt may be split over accounts/departments, but exact duplicate
            # lines are usually double entry and are rejected explicitly.
            identity = tuple(item.values())
            if identity in seen:
                raise ValueError('بند مصروف مكرر في تسوية العهدة')
            seen.add(identity)
            value['lines'].append(item)
        value['amount_cents'] = sum(r['amount_cents'] for r in value['lines'])
        payables._money(value['amount_cents'], allow_zero=False)
    return value


def _effective(db, document, as_of):
    reversal = db.execute('SELECT * FROM custody_reversals WHERE document_id=? AND business_date<=?', (document['id'], as_of)).fetchone()
    return document['status'] == 'posted' and document['business_date'] <= as_of and reversal is None


def _remaining(db, issue_id, as_of):
    issue = payables._require(db, 'custody_documents', issue_id)
    if not _effective(db, issue, as_of):
        return 0
    used = sum(r['amount_cents'] for r in db.execute("SELECT * FROM custody_documents WHERE source_issue_id=? AND status='posted' AND business_date<=?", (issue_id, as_of)) if _effective(db, r, as_of))
    return issue['amount_cents'] - used


def capacity_dates(db, issue_id, business_date):
    days = {payables._date(business_date), ALL_TIME}
    for r in db.execute("SELECT business_date FROM custody_documents WHERE (id=? OR source_issue_id=?) AND status='posted' AND business_date>=?", (issue_id, issue_id, business_date)):
        days.add(r[0])
    for r in db.execute('''SELECT r.business_date FROM custody_reversals r JOIN custody_documents d ON d.id=r.document_id
            WHERE (d.id=? OR d.source_issue_id=?) AND r.business_date>=?''', (issue_id, issue_id, business_date)):
        days.add(r[0])
    return sorted(days)


def capacity(db, issue_id, business_date):
    return min(_remaining(db, issue_id, day) for day in capacity_dates(db, issue_id, business_date))


def _prepare(db, value):
    if accounting.get_settings(db)['base_currency'] != 'EGP':
        raise ValueError('دورة العهد الحالية تعمل بالجنيه المصري EGP')
    account = None
    source = None
    if value['kind'] == 'issue':
        account = _custody_account(db)
        custodian = payables._require(db, 'custodians', value['custodian_id'])
        if not custodian['active']:
            raise ValueError('صاحب العهدة موقوف؛ لا تصرف له عهدة جديدة')
        available = None
    else:
        source = payables._require(db, 'custody_documents', value['source_issue_id'])
        if source['kind'] != 'issue' or source['status'] != 'posted':
            raise ValueError('اختر عهدة مصروفة ومعتمدة')
        origin = _opening_origin(db, source['id'])
        if origin:
            import opening_controls
            if not opening_controls.source_matches(db, 'custody', {**source, **origin}):
                raise ValueError('مصدر العهدة الافتتاحية لا يطابق كشف الافتتاح المعتمد')
        if value['business_date'] < source['business_date']:
            raise ValueError('تاريخ التسوية أو الرد لا يسبق صرف العهدة')
        account = payables._posting_account(db, source['custody_account_id'])
        if account['normal_side'] != 'debit' or not account['code'].startswith('115'):
            raise ValueError('حساب أصل العهدة غير مناسب')
        if account['id'] in _blocked_accounts(db):
            raise ValueError('حساب أصل العهدة يتداخل مع حساب مراقبة آخر؛ راجع الربط')
        custodian = payables._require(db, 'custodians', source['custodian_id'])
        available = capacity(db, source['id'], value['business_date'])
        if value['amount_cents'] > available:
            raise ValueError('المبلغ يتجاوز الرصيد المتاح للعهدة في التاريخ المختار أو حركة لاحقة')
    cash = _cash(db, value['cash_account_id']) if value['cash_account_id'] else None
    amount = value['amount_cents']
    lines = []
    if value['kind'] == 'issue':
        lines = [{'account_id': account['id'], 'debit_cents': amount}, {'account_id': cash['id'], 'credit_cents': amount}]
    elif value['kind'] == 'return':
        lines = [{'account_id': cash['id'], 'debit_cents': amount}, {'account_id': account['id'], 'credit_cents': amount}]
    else:
        import treasury
        blocked_expenses = treasury.blocked_counter_account_ids(db)
        for line in value['lines']:
            expense = payables._posting_account(db, line['expense_account_id'], expense=True)
            payables._require(db, 'departments', line['department_id'])
            if expense['id'] in blocked_expenses or expense['id'] == account['id']:
                raise ValueError('حساب المصروف يتداخل مع حساب مراقبة مستقل')
            lines.append({'account_id': expense['id'], 'department_id': line['department_id'], 'debit_cents': line['amount_cents']})
        lines.append({'account_id': account['id'], 'credit_cents': amount})
    for line in lines:
        account_label = payables._require(db, 'accounts', line['account_id'])
        line.update(account_code=account_label['code'], account_name=account_label['name'])
        if line.get('department_id'):
            dep = payables._require(db, 'departments', line['department_id'])
            line.update(department_name=dep['name'], cost_code=dep['cost_code'])
    before = supplier_payments.account_balance(db, cash['id'], value['business_date']) if cash else None
    after = before + (amount if value['kind'] == 'return' else -amount) if cash else None
    warnings = [accounting.opening_status(db, value['business_date'])['message']]
    if cash and after < 0:
        warnings.append('الحركة تؤدي إلى رصيد دفتري سالب للخزينة؛ راجع التمويل الفعلي وأرصدة البداية.')
    return {**value, 'custodian_id': custodian['id'], 'custodian_name': custodian['name'],
            'custodian_code': custodian['code'], 'custody_account_id': account['id'],
            'custody_account_code': account['code'], 'custody_account_name': account['name'],
            'source_issue_code': source['code'] if source else None,
            'available_cents': available, 'remaining_cents': available - amount if available is not None else amount,
            'cash_balance_before_cents': before, 'cash_balance_after_cents': after,
            'warnings': warnings, 'journal_lines': lines, 'can_approve': True}


def _prepared_hash(plan):
    # Available amount and balance warnings are live facts, not financial routing.
    return _fingerprint({'custodian_id': plan['custodian_id'], 'custody_account_id': plan['custody_account_id'],
                         'journal_lines': [{k: line.get(k, 0 if k.endswith('_cents') else None)
                                            for k in ('account_id', 'department_id', 'debit_cents', 'credit_cents')}
                                           for line in plan['journal_lines']]})


def preview(db, data):
    return _prepare(db, _normalize(data))


def save(db, data, document_id=None):
    if not isinstance(data, dict):
        raise ValueError('بيانات مستند العهدة غير صحيحة')
    old = payables._require(db, 'custody_documents', document_id) if document_id is not None else None
    if old:
        if old['status'] == 'cancelled' and data.get('action') == 'cancel':
            return detail(db, document_id)
        if old['status'] != 'draft':
            raise ValueError('المستند المعتمد أو الملغى لا يعدل')
        payables._revision(data, old)
        accounting_periods.assert_document_dates(db, old['business_date'])
        if data.get('action') == 'cancel':
            db.execute("UPDATE custody_documents SET status='cancelled',revision=revision+1,updated_at=? WHERE id=?", (payables._now(), document_id))
            accounting._audit(db, 'custody_document', document_id, 'cancel_draft', {})
            return detail(db, document_id)
    value = _normalize(data)
    key = payables._text(data, 'request_key', 100, not bool(old)) or (old['request_key'] if old else '')
    fingerprint = _fingerprint(value)
    prior = db.execute('SELECT id,request_fingerprint FROM custody_documents WHERE request_key=?', (key,)).fetchone()
    if not old and prior:
        if prior['request_fingerprint'] != fingerprint:
            raise accounting.ConflictError('مفتاح الطلب مستخدم لمستند عهدة مختلف')
        return detail(db, prior['id'])
    if old and key != old['request_key']:
        raise accounting.ConflictError('لا يتغير مفتاح المستند المحفوظ')
    accounting_periods.assert_document_dates(db, value['business_date'], old['business_date'] if old else None)
    plan = _prepare(db, value)
    stamp = payables._now()
    fields = (fingerprint, _prepared_hash(plan), value['kind'], value['business_date'], value['reference'],
              plan['custodian_id'], value['source_issue_id'], value['cash_account_id'], plan['custody_account_id'],
              value['amount_cents'], value['purpose'], value['notes'], _json(value), stamp)
    if old:
        db.execute('''UPDATE custody_documents SET request_fingerprint=?,prepared_fingerprint=?,kind=?,business_date=?,reference=?,
            custodian_id=?,source_issue_id=?,cash_account_id=?,custody_account_id=?,amount_cents=?,purpose=?,notes=?,payload=?,
            updated_at=?,revision=revision+1 WHERE id=?''', (*fields, document_id))
        db.execute('DELETE FROM custody_lines WHERE document_id=?', (document_id,))
    else:
        code = payables._code(db, 'custody_document', 'CST')
        document_id = db.execute('''INSERT INTO custody_documents(request_key,code,request_fingerprint,prepared_fingerprint,kind,
            business_date,reference,custodian_id,source_issue_id,cash_account_id,custody_account_id,amount_cents,purpose,notes,payload,
            updated_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (key, code, *fields, stamp)).lastrowid
    db.executemany('INSERT INTO custody_lines(document_id,expense_account_id,department_id,reference,amount_cents) VALUES(?,?,?,?,?)',
                   [(document_id, line['expense_account_id'], line['department_id'], line['reference'], line['amount_cents']) for line in value['lines']])
    accounting._audit(db, 'custody_document', document_id, 'update_draft' if old else 'save_draft', {'kind': value['kind'], 'amount_cents': value['amount_cents']})
    return detail(db, document_id)


def approve(db, document_id, data):
    old = payables._require(db, 'custody_documents', document_id)
    if old['status'] == 'posted':
        return detail(db, document_id)
    if old['status'] != 'draft':
        raise ValueError('الاعتماد متاح للمسودة فقط')
    payables._revision(data, old)
    accounting_periods.assert_open(db, old['business_date'])
    plan = _prepare(db, json.loads(old['payload']))
    if _prepared_hash(plan) != old['prepared_fingerprint']:
        raise accounting.ConflictError('ربط حساب العهدة تغير؛ أعد حفظ المسودة قبل الاعتماد')
    journal_id = payables.create_journal(db, 'custody_' + old['kind'], document_id, old['business_date'], plan['journal_lines'])
    db.execute("UPDATE custody_documents SET journal_id=?,custodian_name_snapshot=?,custodian_code_snapshot=?,revision=revision+1,updated_at=?,status='posted' WHERE id=?", (journal_id, plan['custodian_name'], plan['custodian_code'], payables._now(), document_id))
    accounting._audit(db, 'custody_document', document_id, 'post', {'journal_id': journal_id, 'amount_cents': old['amount_cents']})
    return detail(db, document_id)


def reverse(db, document_id, data):
    old = payables._require(db, 'custody_documents', document_id)
    if _opening_origin(db, document_id):
        raise ValueError('العهدة الافتتاحية تحفظ ضمن قيد الافتتاح؛ تسويتها وردها من مستنداتها المستقلة')
    if not isinstance(data, dict):
        raise ValueError('بيانات عكس العهدة غير صحيحة')
    value = {'document_id': document_id, 'business_date': payables._date(data.get('business_date'), 'تاريخ العكس'),
             'reference': payables._text(data, 'reference', 100, True), 'reason': payables._text(data, 'reason', 2000, True)}
    key = payables._text(data, 'request_key', 100, True)
    fingerprint = _fingerprint(value)
    prior = db.execute('SELECT * FROM custody_reversals WHERE document_id=? OR request_key=?', (document_id, key)).fetchone()
    if prior:
        if prior['document_id'] != document_id or prior['request_key'] != key or prior['request_fingerprint'] != fingerprint:
            raise accounting.ConflictError('المستند معكوس بالفعل أو مفتاح التصحيح مستخدم لإجراء مختلف')
        return detail(db, document_id)
    if old['status'] != 'posted':
        raise ValueError('العكس متاح لمستند عهدة معتمد فقط')
    payables._revision(data, old)
    event = data.get('event_revision', data.get('adjustment_revision', 0))
    if type(event) is not int or event != 0:
        raise accounting.ConflictError('حالة العكس تغيرت؛ أعد فتح المستند')
    accounting_periods.assert_open(db, value['business_date'])
    if value['business_date'] < old['business_date']:
        raise ValueError('تاريخ العكس لا يسبق تاريخ المستند الأصلي')
    if old['kind'] == 'issue':
        for child in db.execute("SELECT * FROM custody_documents WHERE source_issue_id=? AND status='posted'", (document_id,)):
            reversal = db.execute('SELECT business_date FROM custody_reversals WHERE document_id=?', (child['id'],)).fetchone()
            if reversal is None:
                raise ValueError('اعكس تسويات وردود هذه العهدة أولًا قبل عكس صرفها')
            if reversal['business_date'] > value['business_date']:
                raise ValueError('تاريخ عكس صرف العهدة لا يسبق عكس المستندات التابعة')
    original = payables.journal_detail(db, old['journal_id'])
    if original['source_type'] != 'custody_' + old['kind'] or original['source_id'] != document_id:
        raise ValueError('القيد الأصلي لا يطابق مستند العهدة')
    reversal_id = db.execute('SELECT COALESCE(MAX(id),0)+1 FROM custody_reversals').fetchone()[0]
    lines = [{'account_id': row['account_id'], 'department_id': row['department_id'], 'supplier_id': row['supplier_id'],
              'debit_cents': row['credit_cents'], 'credit_cents': row['debit_cents']} for row in original['lines']]
    journal_id = payables.create_journal(db, 'custody_reversal', reversal_id, value['business_date'], lines)
    code = payables._code(db, 'custody_reversal', 'CSR')
    db.execute('''INSERT INTO custody_reversals(id,document_id,code,request_key,request_fingerprint,business_date,reference,reason,journal_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)''', (reversal_id, document_id, code, key, fingerprint, value['business_date'], value['reference'], value['reason'], journal_id, payables._now()))
    accounting._audit(db, 'custody_document', document_id, 'reverse', {'journal_id': journal_id, 'business_date': value['business_date'], 'reason': value['reason']})
    return detail(db, document_id)


def _opening_origin(db, document_id):
    if not payables._has_table(db, 'opening_custody_sources'):
        return None
    row = db.execute('SELECT * FROM opening_custody_sources WHERE document_id=?', (document_id,)).fetchone()
    return dict(row) if row else None


def detail(db, document_id, as_of=None):
    document = payables._require(db, 'custody_documents', document_id)
    origin = _opening_origin(db, document_id)
    document.update(is_opening=origin is not None, opening_line_id=origin['opening_line_id'] if origin else None,
                    opening_batch_id=origin['opening_batch_id'] if origin else None,
                    source_title='عهدة افتتاحية تحت التسوية' if origin else None)
    as_of = _as_of(as_of) if as_of is not None else ALL_TIME
    person = payables._require(db, 'custodians', document['custodian_id'])
    document['custodian_name'] = document['custodian_name_snapshot'] or person['name']
    document['custodian_code'] = document['custodian_code_snapshot'] or person['code']
    for field in ('cash_account', 'custody_account'):
        account = db.execute('SELECT code,name FROM accounts WHERE id=?', (document[field + '_id'],)).fetchone()
        document[field + '_code'] = account['code'] if account else ''
        document[field + '_name'] = account['name'] if account else ''
    source = db.execute('SELECT code FROM custody_documents WHERE id=?', (document['source_issue_id'],)).fetchone()
    document['source_issue_code'] = source['code'] if source else None
    document['lines'] = [dict(r) for r in db.execute('''SELECT l.*,a.code expense_account_code,a.name expense_account_name,d.name department_name,d.cost_code
        FROM custody_lines l JOIN accounts a ON a.id=l.expense_account_id JOIN departments d ON d.id=l.department_id
        WHERE l.document_id=? ORDER BY l.id''', (document_id,))]
    document['journal'] = payables.journal_detail(db, document['journal_id']) if document['journal_id'] else None
    reversal = db.execute('SELECT * FROM custody_reversals WHERE document_id=? AND business_date<=?', (document_id, as_of)).fetchone()
    document['reversal'] = dict(reversal) if reversal else None
    if reversal:
        document['reversal']['journal'] = payables.journal_detail(db, reversal['journal_id'])
        document['reversal'].pop('request_fingerprint', None)
    document['effective_status'] = 'reversed' if reversal else document['status']
    document['adjustment_revision'] = int(reversal is not None)
    for field in ('request_fingerprint', 'prepared_fingerprint', 'payload'):
        document.pop(field, None)
    return document


def list_documents(db, as_of=None):
    end = _as_of(as_of) if as_of is not None else ALL_TIME
    return [detail(db, row[0], end) for row in db.execute('SELECT id FROM custody_documents WHERE business_date<=? ORDER BY business_date DESC,id DESC', (end,))]


def _document_lines(db, document):
    amount, control, cash = document['amount_cents'], document['custody_account_id'], document['cash_account_id']
    if document['kind'] == 'issue':
        origin = _opening_origin(db, document['id'])
        return [(control, amount, 0, None, None), (origin['clearing_account_id'] if origin else cash, 0, amount, None, None)]
    if document['kind'] == 'return':
        return [(cash, amount, 0, None, None), (control, 0, amount, None, None)]
    return [(r['expense_account_id'], r['amount_cents'], 0, r['department_id'], None) for r in db.execute('SELECT * FROM custody_lines WHERE document_id=? ORDER BY id', (document['id'],))] + [(control, 0, amount, None, None)]


def balance_report(db, as_of=None):
    """Compare dated document positions with original sealed control-account lines."""
    as_of = _as_of(as_of)
    if not payables._has_table(db, 'custody_documents'):
        return {'as_of': as_of, 'currency': accounting.get_settings(db)['base_currency'], 'issues': [], 'custodians': [], 'accounts': [],
                'totals': dict.fromkeys(('issued_cents','settled_cents','returned_cents','remaining_cents','ledger_cents','difference_cents'),0),
                'matched': True, 'exceptions': [], 'opening_status': accounting.opening_status(db, as_of), 'counts': {'issues': 0,'open_issues': 0,'exceptions': 0}}
    people = {r['id']: dict(r) for r in db.execute('SELECT * FROM custodians')}
    accounts = {r['id']: dict(r) for r in db.execute('SELECT * FROM accounts')}
    all_documents = {r['id']: dict(r) for r in db.execute('SELECT * FROM custody_documents')}
    docs = {key: value for key, value in all_documents.items() if value['status'] == 'posted' and value['business_date'] <= as_of}
    reversals = {r['document_id']: dict(r) for r in db.execute('SELECT * FROM custody_reversals WHERE business_date<=?', (as_of,))}
    control_ids = {r['custody_account_id'] for r in all_documents.values()}
    control_ids.update(r[0] for r in db.execute("SELECT account_id FROM account_mappings WHERE module='الخزينة' AND role='operating_custodies'"))
    exceptions, expected, actual, by_journal, issue_rows = [], {}, {}, {}, []

    def issue(kind, message, **values):
        exceptions.append({'type': kind, 'message': message, **values})

    for document in docs.values():
        sign = 1 if document['kind'] == 'issue' else -1
        amount = sign * document['amount_cents']
        key = (document['custodian_id'], document['custody_account_id'])
        expected[key] = expected.get(key, 0) + amount
        if document['kind'] != 'issue':
            source = all_documents.get(document['source_issue_id'])
            if source is None or source['kind'] != 'issue' or source['status'] != 'posted' or source['custodian_id'] != document['custodian_id'] or source['custody_account_id'] != document['custody_account_id'] or source['business_date'] > document['business_date']:
                issue('invalid_custody_source', 'مستند التسوية أو الرد لا يطابق عهدة المصدر.', document_id=document['id'], custodian_id=document['custodian_id'])
        rows = _document_lines(db, document)
        origin = _opening_origin(db, document['id'])
        if origin:
            import opening_controls
            if not opening_controls.source_matches(db, 'custody', {**document, **origin}):
                issue('invalid_opening_custody_source', 'مصدر العهدة الافتتاحية لا يطابق كشف الافتتاح المعتمد.', document_id=document['id'], custodian_id=document['custodian_id'])
        by_journal[document['journal_id']] = (document, 'opening_balance' if origin else 'custody_' + document['kind'],
            origin['opening_line_id'] if origin else document['id'], document['business_date'], rows)
        reversal = reversals.get(document['id'])
        if reversal:
            expected[key] -= amount
            by_journal[reversal['journal_id']] = (document, 'custody_reversal', reversal['id'], reversal['business_date'], [(a,c,d,p,s) for a,d,c,p,s in rows])
    for journal_id, (document, source_type, source_id, ds, lines) in by_journal.items():
        journal = db.execute('SELECT * FROM journal_entries WHERE id=?', (journal_id,)).fetchone()
        if journal is None or not journal['sealed'] or journal['source_type'] != source_type or journal['source_id'] != source_id or journal['entry_date'] != ds:
            issue('custody_source_journal_mismatch', 'قيد العهدة غير موجود أو غير مكتمل أو لا يطابق المستند وتاريخه.', document_id=document['id'], journal_id=journal_id, custodian_id=document['custodian_id'])
            continue
        actual_lines = [(r['account_id'], r['debit_cents'], r['credit_cents'], r['department_id'], r['supplier_id']) for r in db.execute('SELECT * FROM journal_lines WHERE journal_id=?', (journal_id,))]
        sorter = lambda row: tuple(-1 if x is None else x for x in row)
        if sorted(lines, key=sorter) != sorted(actual_lines, key=sorter):
            issue('custody_journal_lines_mismatch', 'أطراف القيد أو أبعاده تختلف عن مستند العهدة.', document_id=document['id'], journal_id=journal_id, custodian_id=document['custodian_id'])
    if control_ids:
        marks = ','.join('?' for _ in control_ids)
        for row in db.execute(f'''SELECT l.*,j.source_type,j.source_id,j.entry_date,j.code journal_code FROM journal_lines l
                JOIN journal_entries j ON j.id=l.journal_id WHERE j.sealed=1 AND j.entry_date<=? AND l.account_id IN({marks}) ORDER BY j.id,l.id''', (as_of, *sorted(control_ids))):
            source = by_journal.get(row['journal_id'])
            if source is None:
                issue('untracked_custody_journal', 'قيد على حساب العهد لا يرتبط بمستند عهدة معتمد حتى تاريخ التقرير.', journal_id=row['journal_id'], journal_code=row['journal_code'], account_id=row['account_id'])
                key = (None, row['account_id'])
            else:
                key = (source[0]['custodian_id'], row['account_id'])
            actual[key] = actual.get(key, 0) + row['debit_cents'] - row['credit_cents']
    totals = dict.fromkeys(('issued_cents','settled_cents','returned_cents','remaining_cents','ledger_cents','difference_cents'), 0)
    for document in docs.values():
        factor = 0 if document['id'] in reversals else 1
        origin = _opening_origin(db, document['id'])
        field = 'opening_cents' if origin else {'issue':'issued_cents','settlement':'settled_cents','return':'returned_cents'}[document['kind']]
        totals[field] = totals.get(field, 0) + factor * document['amount_cents']
        if document['kind'] != 'issue':
            continue
        children = [d for d in docs.values() if d['source_issue_id'] == document['id'] and d['id'] not in reversals]
        settled = sum(d['amount_cents'] for d in children if d['kind'] == 'settlement')
        returned = sum(d['amount_cents'] for d in children if d['kind'] == 'return')
        remaining = factor * document['amount_cents'] - settled - returned
        person = people.get(document['custodian_id'], {})
        account = accounts.get(document['custody_account_id'], {})
        if remaining < 0:
            issue('overused_custody', 'تسويات وردود العهدة تتجاوز الصرف المتاح.', document_id=document['id'], custodian_id=document['custodian_id'])
        issue_rows.append({'id': document['id'], 'code': document['code'], 'business_date': document['business_date'],
                           'custodian_id': document['custodian_id'], 'custodian_name': document['custodian_name_snapshot'] or person.get('name',''),
                           'custodian_code': document['custodian_code_snapshot'] or person.get('code',''),
                           'custody_account_id': document['custody_account_id'], 'custody_account_code': account.get('code',''),
                           'custody_account_name': account.get('name',''), 'purpose': document['purpose'],
                           'issued_cents': factor * document['amount_cents'], 'settled_cents': settled, 'returned_cents': returned,
                           'remaining_cents': remaining, 'available_cents': max(remaining,0),
                           'effective_status': 'reversed' if not factor else 'posted', 'is_opening': origin is not None,
                           'source_title': 'عهدة افتتاحية تحت التسوية' if origin else 'عهدة مصروفة'})
    person_rows, account_rows = [], []
    for custodian_id in sorted(set(people) | {key[0] for key in actual if key[0] is not None}):
        book = sum(value for key,value in expected.items() if key[0] == custodian_id)
        ledger = sum(value for key,value in actual.items() if key[0] == custodian_id)
        person = people.get(custodian_id, {})
        person_rows.append({'custodian_id': custodian_id, 'code': person.get('code',''), 'name': person.get('name',''),
                            'remaining_cents': book, 'ledger_cents': ledger, 'difference_cents': ledger-book,
                            'matched': book == ledger and not any(e.get('custodian_id') == custodian_id for e in exceptions)})
    for account_id in sorted(control_ids):
        book = sum(value for key,value in expected.items() if key[1] == account_id)
        ledger = sum(value for key,value in actual.items() if key[1] == account_id)
        account = accounts.get(account_id, {})
        account_rows.append({'account_id': account_id, 'code': account.get('code',''), 'name': account.get('name',''),
                             'remaining_cents': book, 'ledger_cents': ledger, 'difference_cents': ledger-book,
                             'matched': book == ledger and not any(e.get('account_id') == account_id or e.get('journal_id') in {d['journal_id'] for d in docs.values() if d['custody_account_id'] == account_id} for e in exceptions)})
    for key in set(expected) | set(actual):
        difference = actual.get(key,0) - expected.get(key,0)
        if difference:
            issue('custody_balance_difference', 'رصيد العهدة حسب الشخص والحساب لا يطابق القيود.', custodian_id=key[0], account_id=key[1], difference_cents=difference)
    totals['remaining_cents'] = sum(expected.values())
    totals['ledger_cents'] = sum(actual.values())
    totals['difference_cents'] = totals['ledger_cents'] - totals['remaining_cents']
    return {'as_of': as_of, 'currency': accounting.get_settings(db)['base_currency'], 'opening_status': accounting.opening_status(db, as_of), 'issues': sorted(issue_rows,key=lambda r:(r['business_date'],r['id'])),
            'custodians': person_rows, 'accounts': account_rows, 'totals': totals,
            'matched': not exceptions and totals['difference_cents'] == 0 and all(r['matched'] for r in person_rows + account_rows),
            'exceptions': exceptions, 'counts': {'issues':len(issue_rows),'open_issues':sum(r['remaining_cents'] > 0 for r in issue_rows),'exceptions':len(exceptions)}}
