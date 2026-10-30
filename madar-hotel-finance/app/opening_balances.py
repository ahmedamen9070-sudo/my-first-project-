"""Reviewed initial balances and their immutable, balanced opening journals.

The caller owns its transaction. An initial batch must explicitly balance; this
module never invents capital, a payment, or an operating purchase. Each source
line has a journal against a temporary equity account, whose batch balance is 0.
"""
from __future__ import annotations

import hashlib
import importlib
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import accounting
import accounting_periods
import payables

KINDS = {
    'general': 'رصيد حساب أستاذ', 'supplier_invoice': 'فاتورة مورد سابقة',
    'supplier_credit': 'رصيد دائن لصالح المنشأة لدى المورد',
    'supplier_advance': 'دفعة مقدمة سابقة للمورد', 'custody': 'عهدة سابقة تحت التسوية',
    'withholding': 'خصم ضريبي سابق مستحق التوريد', 'issued_cheque': 'شيك سابق لم يصرف',
}
ROLES = {
    'supplier_invoice': ('الموردون', 'payable_control'),
    'supplier_credit': ('الموردون', 'payable_control'),
    'supplier_advance': ('الموردون', 'supplier_advances'),
    'custody': ('الخزينة', 'operating_custodies'),
    'withholding': ('الضرائب', 'withholding_payable'),
    'issued_cheque': ('الموردون', 'issued_cheques'),
}
CLEARING = ('الإعدادات', 'opening_clearing')
SIDES = {'supplier_invoice': 'credit', 'supplier_credit': 'debit',
         'supplier_advance': 'debit', 'custody': 'debit', 'withholding': 'credit',
         'issued_cheque': 'credit'}


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError('صيغة بيانات الرصيد الافتتاحي غير صحيحة') from None


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def initialize(db):
    """Add configuration and empty source tables, preserving operator edits."""
    for sql in (
        '''CREATE TABLE IF NOT EXISTS opening_batches(
            id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,request_key TEXT NOT NULL UNIQUE,
            request_fingerprint TEXT NOT NULL,prepared_fingerprint TEXT NOT NULL,
            business_date TEXT NOT NULL,reference TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN('draft','posted','cancelled')),
            revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
            debit_cents INTEGER NOT NULL CHECK(debit_cents>=0),credit_cents INTEGER NOT NULL CHECK(credit_cents>=0),
            clearing_account_id INTEGER NOT NULL REFERENCES accounts(id),
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL,posted_at TEXT)''',
        '''CREATE TABLE IF NOT EXISTS opening_lines(
            id INTEGER PRIMARY KEY,batch_id INTEGER NOT NULL REFERENCES opening_batches(id),
            line_no INTEGER NOT NULL,business_date TEXT NOT NULL,kind TEXT NOT NULL,
            account_id INTEGER NOT NULL REFERENCES accounts(id),side TEXT NOT NULL CHECK(side IN('debit','credit')),
            amount_cents INTEGER NOT NULL CHECK(amount_cents>0),supplier_id INTEGER REFERENCES suppliers(id),
            custodian_id INTEGER,reference TEXT NOT NULL,due_date TEXT,source_date TEXT,
            department_id INTEGER REFERENCES departments(id),extra_json TEXT NOT NULL DEFAULT '{}',
            prepared_json TEXT NOT NULL,journal_id INTEGER UNIQUE REFERENCES journal_entries(id),
            document_type TEXT,document_id INTEGER,UNIQUE(batch_id,line_no))''',
        'CREATE INDEX IF NOT EXISTS opening_lines_batch ON opening_lines(batch_id,line_no)',
        "CREATE UNIQUE INDEX IF NOT EXISTS one_posted_initial_opening ON opening_batches(status) WHERE status='posted'",
        "CREATE UNIQUE INDEX IF NOT EXISTS one_draft_initial_opening ON opening_batches(status) WHERE status='draft'",
    ):
        db.execute(sql)
    for operation in ('UPDATE', 'DELETE'):
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_opening_batch_{operation.lower()}
            BEFORE {operation} ON opening_batches WHEN OLD.status='posted'
            BEGIN SELECT RAISE(ABORT,'posted opening batch is immutable'); END''')
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_opening_line_{operation.lower()}
            BEFORE {operation} ON opening_lines
            WHEN EXISTS(SELECT 1 FROM opening_batches WHERE id=OLD.batch_id AND status='posted')
            BEGIN SELECT RAISE(ABORT,'posted opening line is immutable'); END''')
    for operation in ('INSERT', 'UPDATE'):
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_opening_line_parent_{operation.lower()}
            BEFORE {operation} ON opening_lines
            WHEN EXISTS(SELECT 1 FROM opening_batches WHERE id=NEW.batch_id AND status='posted')
            BEGIN SELECT RAISE(ABORT,'posted opening line is immutable'); END''')
    account = db.execute("SELECT * FROM accounts WHERE role='opening_balance_clearing' ORDER BY id LIMIT 1").fetchone()
    stamp = payables._now()
    if account is None:
        parent = db.execute("SELECT * FROM accounts WHERE code='300000'").fetchone()
        if parent is None or parent['posting'] or parent['statement'] != 'balance_sheet':
            raise ValueError('تجميع حقوق الملكية مطلوب لإعداد حساب الافتتاح الوسيط')
        code = '300007' if not db.execute("SELECT 1 FROM accounts WHERE code='300007'").fetchone() else accounting._auto_code(db, parent, True)
        ident = db.execute('''INSERT INTO accounts(code,name,parent_id,posting,normal_side,statement,
            active,system,notes,role,created_at,updated_at) VALUES(?,?,?,1,'credit','balance_sheet',1,1,?,?,?,?)''',
            (code, 'حساب وسيط قيد الافتتاح', parent['id'],
             'حساب مؤقت؛ يجب أن يصبح رصيده صفرًا بعد اعتماد دفعة افتتاح متوازنة.',
             'opening_balance_clearing', stamp, stamp)).lastrowid
        account = db.execute('SELECT * FROM accounts WHERE id=?', (ident,)).fetchone()
    db.execute('''INSERT OR IGNORE INTO account_mappings(module,role,label,account_id,allowed_classes,
        status,expected_side,updated_at) VALUES(?,?,?,?,?,'proposed','credit',?)''',
        (*CLEARING, 'الحساب الوسيط لقيد الافتتاح', account['id'], '["3"]', stamp))
    for name in ('opening_suppliers', 'opening_controls', 'banks'):
        module = importlib.import_module(name)
        if name != 'banks' or hasattr(module, 'initialize_opening'):
            getattr(module, 'initialize_opening' if name == 'banks' else 'initialize')(db)


def _adapter(kind):
    if kind.startswith('supplier_'):
        module = importlib.import_module('opening_suppliers')
        return module.prepare, module.post_line
    if kind in ('custody', 'withholding'):
        module = importlib.import_module('opening_controls')
        return module.prepare, module.post_line
    if kind == 'issued_cheque':
        module = importlib.import_module('banks')
        return module.prepare_opening_cheque, module.post_opening_cheque
    return None, None


def _mapping(db, module, role):
    row = db.execute('''SELECT m.*,a.code account_code,a.name account_name
        FROM account_mappings m JOIN accounts a ON a.id=m.account_id WHERE m.module=? AND m.role=?''',
        (module, role)).fetchone()
    return dict(row) if row else None


def _earliest(db):
    return db.execute("SELECT MIN(entry_date) FROM journal_entries WHERE sealed=1 AND source_type<>'opening_balance'").fetchone()[0]


def _posted(db):
    row = db.execute("SELECT id,code,business_date,reference,status,revision,posted_at FROM opening_batches WHERE status='posted'").fetchone()
    return dict(row) if row else None


def protected_account_ids(db):
    """Current and historical implemented controls cannot bypass their detail."""
    import supplier_reconciliation
    payable, advances = supplier_reconciliation._account_ids(db)
    result = payable | advances
    result.update(row[0] for row in db.execute('''SELECT id FROM accounts WHERE role IN
        ('opening_balance_clearing','operating_custodies','withholding_payable','cheques_issued',
         'supplier_control','capital_supplier_control','supplier_advances')
        OR code='217004' '''))
    result.update(row[0] for row in db.execute('''SELECT account_id FROM account_mappings WHERE
        (module='الموردون' AND role IN('payable_control','capital_payable_control','supplier_advances','issued_cheques'))
        OR (module='الخزينة' AND role='operating_custodies')
        OR (module='الضرائب' AND role='withholding_payable') OR (module=? AND role=?)''', CLEARING))
    for table, column in (('custody_documents', 'custody_account_id'), ('payment_withholding', 'account_id'),
                          ('withholding_remittance_allocations', 'account_id'),
                          ('opening_withholding_sources', 'account_id'),
                          ('opening_bank_cheques', 'liability_account_id')):
        if payables._has_table(db, table):
            result.update(row[0] for row in db.execute(f'SELECT DISTINCT {column} FROM {table}') if row[0] is not None)
    result.update(row[0] for row in db.execute("SELECT DISTINCT account_id FROM opening_lines WHERE kind<>'general'"))
    import banks
    if hasattr(banks, 'issued_cheque_account_ids'):
        result.update(banks.issued_cheque_account_ids(db))
    return result


def config(db):
    accounts = [dict(row) for row in db.execute('SELECT * FROM accounts WHERE active=1 AND posting=1 ORDER BY code')]
    blocked = protected_account_ids(db)
    mappings = [dict(row) for row in db.execute('''SELECT m.*,a.code account_code,a.name account_name
        FROM account_mappings m JOIN accounts a ON a.id=m.account_id ORDER BY m.module,m.id''')]
    kinds = {}
    for kind, pair in ROLES.items():
        mapping = _mapping(db, *pair)
        kinds[kind] = next((row for row in accounts if mapping and row['id'] == mapping['account_id']), None)
    bank_accounts = []
    for account in accounts:
        if account['code'].startswith('112') and account['code'] != '112002' and account.get('role') != 'foreign_bank' and account['normal_side'] == 'debit':
            master = db.execute('SELECT * FROM bank_accounts WHERE chart_account_id=?', (account['id'],)).fetchone() if payables._has_table(db, 'bank_accounts') else None
            bank_accounts.append({**account, **({key: master[key] for key in ('bank_name', 'branch', 'account_number', 'iban')} if master else {})})
    posted = _posted(db)
    return {'accounts': accounts, 'general_accounts': [row for row in accounts if row['id'] not in blocked],
            'mappings': mappings, 'clearing_mapping': _mapping(db, *CLEARING), 'kind_accounts': kinds,
            'suppliers': [dict(row) for row in db.execute('SELECT * FROM suppliers ORDER BY code,id')],
            'custodians': [dict(row) for row in db.execute('SELECT * FROM custodians ORDER BY code,id')] if payables._has_table(db, 'custodians') else [],
            'departments': [dict(row) for row in db.execute('SELECT * FROM departments ORDER BY code,id')],
            'bank_accounts': bank_accounts, 'kinds': [{'id': key, 'label': label} for key, label in KINDS.items()],
            'posted_batch': posted, 'earliest_financial_date': _earliest(db),
            'baseline_date': posted['business_date'] if posted else _earliest(db), 'currency': 'EGP',
            'notes': ['أدخل الأرصدة الفعلية وتفاصيلها، ثم راجع تساوي المدين والدائن قبل الاعتماد.',
                      'لا يفترض النظام رأس مال أو مبلغًا لتغطية فرق الافتتاح؛ الحساب الوسيط يجب أن يساوي صفرًا.',
                      'الأرصدة التفصيلية للموردين والعهد والخصم والشيكات تدخل من أنواعها المخصصة.',
                      'اعتماد الافتتاح نهائي لمرة واحدة؛ الوحدات التي لم تنفذ تفاصيلها بعد تقبل رصيد أستاذ فقط.']}


def _amount(line, amount_key='amount', cents_key='amount_cents', allow_zero=False):
    cents = line.get(cents_key)
    value = line.get(amount_key)
    if value not in (None, ''):
        try:
            number = Decimal(str(value))
            if isinstance(value, (bool, float)) or not number.is_finite() or number != number.quantize(Decimal('.01')):
                raise ValueError()
            parsed = int(number * 100)
        except (ValueError, TypeError, InvalidOperation, OverflowError):
            raise ValueError('أدخل المبلغ بدقة منزلتين عشريتين دون تقريب') from None
        if cents is not None and (type(cents) is not int or cents != parsed):
            raise ValueError('المبلغ وقيمته بالقروش غير متطابقين')
        cents = parsed
    return payables._money(cents, allow_zero)


def _normalize_line(db, raw, number, business_date, reference, blocked):
    if not isinstance(raw, dict) or not isinstance(raw.get('kind', 'general'), str) or raw.get('kind', 'general') not in KINDS:
        raise ValueError('نوع بند الافتتاح غير صحيح')
    kind = raw.get('kind', 'general')
    extra = raw.get('extra', {})
    if not isinstance(extra, dict):
        raise ValueError('تفاصيل بند الافتتاح غير صحيحة')
    _json(extra)
    line = {'kind': kind, 'amount_cents': _amount(raw), 'side': raw.get('side', SIDES.get(kind)),
            'extra': dict(extra), 'business_date': business_date}
    for key in ('account_id', 'supplier_id', 'custodian_id', 'department_id'):
        value = raw.get(key)
        line[key] = payables._id(value) if value is not None else None
        if value is not None and value > payables.MAX_CENTS:
            raise ValueError('معرّف بند الافتتاح خارج النطاق المسموح')
    for key in ('source_date', 'due_date'):
        value = raw.get(key)
        line[key] = payables._date(value, key) if value not in (None, '') else None
    line['reference'] = payables._text(raw, 'reference', 100) or (f'{reference[:94]}-{number}' if kind == 'general' else '')
    if raw.get('remaining_net_amount') not in (None, '') or raw.get('remaining_net_cents') is not None:
        line['remaining_net_cents'] = _amount(raw, 'remaining_net_amount', 'remaining_net_cents', True)
        line['extra']['remaining_net_cents'] = line['remaining_net_cents']
    if kind == 'general':
        account = payables._posting_account(db, line['account_id'])
        if account['id'] in blocked:
            raise ValueError('حساب مراقبة تفصيلي؛ استخدم نوع المورد أو العهدة أو الخصم أو الشيك المناسب')
        if line['side'] not in ('debit', 'credit'):
            raise ValueError('حدد الجانب المدين أو الدائن لبند الافتتاح')
        if line['supplier_id'] is not None or line['custodian_id'] is not None:
            raise ValueError('أرصدة الموردين والعهد تدخل من أنواعها التفصيلية')
        line['source_date'] = line['source_date'] or business_date
    else:
        if kind == 'custody' and line['supplier_id'] is not None:
            raise ValueError('العهدة الافتتاحية لا تتبع موردًا')
        if kind != 'custody' and line['custodian_id'] is not None:
            raise ValueError('صاحب العهدة متاح لنوع العهدة فقط')
        prepare, _ = _adapter(kind)
        line = prepare(db, line, business_date)
        account = payables._posting_account(db, line.get('account_id'))
        if line.get('side') != SIDES[kind] or line.get('amount_cents') != _amount(raw):
            raise ValueError('خطة الرصيد التفصيلي غير متوافقة مع المبلغ والجانب')
    if line.get('source_date') and line['source_date'] > business_date:
        raise ValueError('تاريخ المصدر لا يلي تاريخ الافتتاح')
    if line.get('department_id') is not None:
        payables._require(db, 'departments', line['department_id'])
    line.update(account_id=account['id'], account_code=account['code'], account_name=account['name'], line_no=number)
    if line.get('supplier_id') is not None:
        supplier = payables._require(db, 'suppliers', line['supplier_id'])
        line.update(supplier_name=supplier['name'], supplier_code=supplier['code'])
    _json(line)
    return line


def _prepare(db, data, exclude_batch_id=None):
    if not isinstance(data, dict):
        raise ValueError('بيانات دفعة الافتتاح غير صحيحة')
    business_date = payables._date(data.get('business_date'), 'تاريخ الافتتاح')
    reference = payables._text(data, 'reference', 100, True)
    notes = payables._text(data, 'notes', 2000)
    if data.get('currency', 'EGP') != 'EGP' or accounting.get_settings(db)['base_currency'] != 'EGP':
        raise ValueError('الأرصدة الافتتاحية الحالية بالجنيه المصري فقط')
    raw_lines = data.get('lines')
    if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= 1000:
        raise ValueError('دفعة الافتتاح تحتاج من بند واحد إلى ألف بند')
    clearing = payables.mapping_account(db, *CLEARING, require_confirmed=False)
    blocked = protected_account_ids(db)
    lines = [_normalize_line(db, row, index, business_date, reference, blocked) for index, row in enumerate(raw_lines, 1)]
    invoice_refs = [(row['supplier_id'], row['reference']) for row in lines if row['kind'] in ('supplier_invoice', 'supplier_credit')]
    if len(invoice_refs) != len(set(invoice_refs)):
        raise ValueError('رقم مستند المورد مكرر داخل دفعة الافتتاح')
    cheques = []
    for line in lines:
        if line['kind'] != 'issued_cheque':
            continue
        extra = line['extra']
        identity = (extra['bank_account_id'], extra['bank_name'].casefold(), extra['cheque_number'].casefold())
        if any(identity[2] == old[2] and (identity[0] == old[0] or identity[1] == old[1]) for old in cheques):
            raise ValueError('رقم الشيك مكرر على البنك نفسه داخل دفعة الافتتاح')
        cheques.append(identity)
    debit = sum(row['amount_cents'] for row in lines if row['side'] == 'debit')
    credit = sum(row['amount_cents'] for row in lines if row['side'] == 'credit')
    if max(debit, credit) > payables.MAX_CENTS:
        raise ValueError('إجمالي دفعة الافتتاح يتجاوز النطاق المسموح')
    blockers = []
    if debit != credit or debit == 0:
        blockers.append({'code': 'unbalanced', 'message': 'أكمل الأرصدة حتى يتساوى المدين والدائن دون فرق.'})
    for pair in {CLEARING} | {ROLES[row['kind']] for row in lines if row['kind'] != 'general'}:
        mapping = _mapping(db, *pair)
        if mapping is None or mapping['status'] != 'confirmed':
            blockers.append({'code': 'unconfirmed_mapping', 'message': 'راجع وأكد ربط الحساب: ' + (mapping['label'] if mapping else pair[1])})
    posted = _posted(db)
    if posted:
        blockers.append({'code': 'already_posted', 'message': 'تم اعتماد دفعة الافتتاح بالفعل؛ لا تسجل الأرصدة مرتين.'})
    earliest = _earliest(db)
    if earliest and business_date > earliest:
        blockers.append({'code': 'baseline_after_activity', 'message': 'تاريخ الافتتاح يجب ألا يلي أقدم قيد مالي مرحل: ' + earliest})
    if business_date > '0001-01-01':
        previous_day = (date.fromisoformat(business_date) - timedelta(days=1)).isoformat()
        pending, _ = accounting_periods._pending(db, '0001-01-01', previous_day)
        pending = [row for row in pending if not (row['type'] == 'opening_balance' and row['id'] == exclude_batch_id)]
        if pending:
            blockers.append({'code': 'drafts_before_baseline', 'message':
                             'توجد مسودات قبل تاريخ الافتتاح. ألغها أو عدل تاريخها أولًا، أو اختر تاريخ افتتاح أقدم.',
                             'count': len(pending), 'documents': pending})
    try:
        accounting_periods.assert_open(db, business_date)
    except ValueError as error:
        blockers.append({'code': 'closed_period', 'message': str(error)})
    result = {'business_date': business_date, 'reference': reference, 'notes': notes, 'lines': lines,
              'totals': {'debit_cents': debit, 'credit_cents': credit, 'difference_cents': debit - credit, 'line_count': len(lines)},
              'clearing_account_id': clearing['id'], 'clearing_account_code': clearing['code'],
              'clearing_account_name': clearing['name'], 'clearing_balance_cents': credit - debit,
              'can_approve': not blockers, 'blockers': blockers, 'warnings': []}
    return result


def preview(db, data):
    batch_id = data.get('batch_id') if isinstance(data, dict) else None
    if batch_id is not None:
        payables._id(batch_id)
    return _prepare(db, data, batch_id)


def _prepared_hash(plan):
    return _hash({key: plan[key] for key in ('business_date', 'reference', 'notes', 'lines', 'clearing_account_id')})


def _request_hash(data):
    return _hash({key: value for key, value in data.items() if key not in ('revision', 'action')})


def _insert_lines(db, batch_id, plan):
    for line in plan['lines']:
        db.execute('''INSERT INTO opening_lines(batch_id,line_no,business_date,kind,account_id,side,amount_cents,
            supplier_id,custodian_id,reference,due_date,source_date,department_id,extra_json,prepared_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (batch_id, line['line_no'], plan['business_date'], line['kind'], line['account_id'], line['side'],
             line['amount_cents'], line.get('supplier_id'), line.get('custodian_id'), line['reference'],
             line.get('due_date'), line.get('source_date'), line.get('department_id'), _json(line.get('extra', {})), _json(line)))


def save(db, data, batch_id=None):
    if not isinstance(data, dict):
        raise ValueError('بيانات دفعة الافتتاح غير صحيحة')
    old = payables._require(db, 'opening_batches', batch_id) if batch_id is not None else None
    if old:
        if old['status'] != 'draft':
            if old['status'] == 'cancelled' and data.get('action') == 'cancel':
                return detail(db, old['id'])
            raise ValueError('دفعة الافتتاح المعتمدة أو الملغاة لا تعدل')
        payables._revision(data, old)
        if data.get('action') == 'cancel':
            accounting_periods.assert_open(db, old['business_date'])
            db.execute("UPDATE opening_batches SET status='cancelled',revision=revision+1,updated_at=? WHERE id=?", (payables._now(), old['id']))
            payables._audit(db, 'opening_batch', old['id'], 'cancel', {})
            return detail(db, old['id'])
    request_key = payables._text(data, 'request_key', 160, True) if old is None else old['request_key']
    if old and data.get('request_key', request_key) != request_key:
        raise ValueError('مفتاح دفعة الافتتاح لا يتغير')
    fingerprint = _request_hash({**data, 'request_key': request_key})
    if old is None:
        prior = db.execute('SELECT * FROM opening_batches WHERE request_key=?', (request_key,)).fetchone()
        if prior:
            if prior['request_fingerprint'] != fingerprint:
                raise accounting.ConflictError('مفتاح الطلب مستخدم لدفعة افتتاح ببيانات مختلفة')
            return detail(db, prior['id'])
    if _posted(db):
        raise ValueError('تم اعتماد الأرصدة الافتتاحية بالفعل؛ لا تنشئ أو تعدل دفعة افتتاح أخرى')
    active = db.execute("SELECT id FROM opening_batches WHERE status='draft' ORDER BY id LIMIT 1").fetchone()
    if active and (old is None or active['id'] != old['id']):
        raise accounting.ConflictError('توجد مسودة افتتاح بالفعل؛ افتحها لاستكمالها أو ألغها أولًا')
    plan = _prepare(db, data, old['id'] if old else None)
    accounting_periods.assert_document_dates(db, plan['business_date'], old['business_date'] if old else None)
    stamp, totals = payables._now(), plan['totals']
    if old:
        batch_id = old['id']
        db.execute('''UPDATE opening_batches SET request_fingerprint=?,prepared_fingerprint=?,business_date=?,
            reference=?,notes=?,debit_cents=?,credit_cents=?,clearing_account_id=?,revision=revision+1,updated_at=? WHERE id=?''',
            (fingerprint, _prepared_hash(plan), plan['business_date'], plan['reference'], plan['notes'],
             totals['debit_cents'], totals['credit_cents'], plan['clearing_account_id'], stamp, batch_id))
        db.execute('DELETE FROM opening_lines WHERE batch_id=?', (batch_id,))
    else:
        batch_id = db.execute('''INSERT INTO opening_batches(code,request_key,request_fingerprint,prepared_fingerprint,
            business_date,reference,notes,debit_cents,credit_cents,clearing_account_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (payables._code(db, 'opening_batch', 'OPN'), request_key, fingerprint, _prepared_hash(plan),
             plan['business_date'], plan['reference'], plan['notes'], totals['debit_cents'], totals['credit_cents'],
             plan['clearing_account_id'], stamp, stamp)).lastrowid
    _insert_lines(db, batch_id, plan)
    payables._audit(db, 'opening_batch', batch_id, 'update' if old else 'create', {'line_count': len(plan['lines'])})
    return detail(db, batch_id)


def _saved_input(db, batch):
    return {'business_date': batch['business_date'], 'reference': batch['reference'], 'notes': batch['notes'],
            'lines': [json.loads(row['prepared_json']) for row in db.execute('SELECT * FROM opening_lines WHERE batch_id=? ORDER BY line_no', (batch['id'],))]}


def approve(db, batch_id, data):
    batch = payables._require(db, 'opening_batches', batch_id)
    if not isinstance(data, dict):
        raise ValueError('بيانات اعتماد الافتتاح غير صحيحة')
    if batch['status'] == 'posted':
        if type(data.get('revision')) is not int or data['revision'] not in (batch['revision'], batch['revision'] - 1):
            raise accounting.ConflictError('راجع النسخة المعتمدة لدفعة الافتتاح')
        return detail(db, batch_id)
    if batch['status'] != 'draft':
        raise ValueError('دفعة الافتتاح ملغاة')
    payables._revision(data, batch)
    try:
        plan = _prepare(db, _saved_input(db, batch), batch_id)
    except ValueError as error:
        raise accounting.ConflictError('راجع الدفعة واحفظها مجددًا قبل الاعتماد: ' + str(error)) from None
    if _prepared_hash(plan) != batch['prepared_fingerprint']:
        raise accounting.ConflictError('تغير الربط أو تفاصيل الافتتاح؛ راجع الدفعة واحفظها مجددًا قبل الاعتماد')
    if not plan['can_approve']:
        raise ValueError(' '.join(row['message'] for row in plan['blockers']))
    accounting_periods.assert_open(db, batch['business_date'])
    for row in db.execute('SELECT * FROM opening_lines WHERE batch_id=? ORDER BY line_no', (batch_id,)).fetchall():
        line = {**json.loads(row['prepared_json']), 'id': row['id'], 'batch_id': batch_id}
        debit = line['amount_cents'] if line['side'] == 'debit' else 0
        credit = line['amount_cents'] if line['side'] == 'credit' else 0
        journal_id = payables.create_journal(db, 'opening_balance', row['id'], batch['business_date'], [
            {'account_id': line['account_id'], 'debit_cents': debit, 'credit_cents': credit,
             'supplier_id': line.get('supplier_id'), 'department_id': line.get('department_id'), 'source_line_id': row['id']},
            {'account_id': batch['clearing_account_id'], 'debit_cents': credit, 'credit_cents': debit},
        ])
        metadata = {}
        if line['kind'] != 'general':
            _, post = _adapter(line['kind'])
            metadata = post(db, line, journal_id) or {}
        db.execute('''UPDATE opening_lines SET journal_id=?,document_type=?,document_id=?,extra_json=? WHERE id=?''',
            (journal_id, metadata.get('document_type'), metadata.get('document_id'),
             _json({**line.get('extra', {}), **metadata}), row['id']))
    balance = db.execute('''SELECT COALESCE(SUM(l.debit_cents-l.credit_cents),0) FROM opening_lines o
        JOIN journal_lines l ON l.journal_id=o.journal_id WHERE o.batch_id=? AND l.account_id=?''',
        (batch_id, batch['clearing_account_id'])).fetchone()[0]
    if balance:
        raise ValueError('الحساب الوسيط للافتتاح غير متزن؛ لم تعتمد الدفعة')
    stamp = payables._now()
    db.execute("UPDATE opening_batches SET status='posted',revision=revision+1,posted_at=?,updated_at=? WHERE id=?", (stamp, stamp, batch_id))
    payables._audit(db, 'opening_batch', batch_id, 'approve', {'debit_cents': batch['debit_cents'], 'credit_cents': batch['credit_cents']})
    return detail(db, batch_id)


def detail(db, batch_id):
    result = payables._require(db, 'opening_batches', batch_id)
    result.pop('request_fingerprint', None)
    result.pop('prepared_fingerprint', None)
    lines = []
    for row in db.execute('SELECT * FROM opening_lines WHERE batch_id=? ORDER BY line_no', (batch_id,)):
        lines.append({**json.loads(row['prepared_json']), **{key: row[key] for key in ('id', 'batch_id', 'line_no', 'journal_id', 'document_type', 'document_id')}, 'extra': json.loads(row['extra_json'])})
    account = accounting._account(db, result['clearing_account_id'])
    result.update(lines=lines, journals=[payables.journal_detail(db, row['journal_id']) for row in lines if row['journal_id']],
                  totals={'debit_cents': result['debit_cents'], 'credit_cents': result['credit_cents'],
                          'difference_cents': result['debit_cents'] - result['credit_cents'], 'line_count': len(lines)},
                  clearing_account_code=account['code'], clearing_account_name=account['name'],
                  clearing_balance_cents=result['credit_cents'] - result['debit_cents'], warnings=[])
    if result['status'] == 'draft':
        try:
            result['preview'] = _prepare(db, _saved_input(db, result), batch_id)
        except ValueError as error:
            result['preview_error'] = str(error)
    return result


def list_batches(db):
    return [dict(row) for row in db.execute('''SELECT id,code,request_key,business_date,reference,notes,status,
        revision,debit_cents,credit_cents,clearing_account_id,created_at,updated_at,posted_at,
        (SELECT COUNT(*) FROM opening_lines WHERE batch_id=opening_batches.id) line_count
        FROM opening_batches ORDER BY id DESC''')]
