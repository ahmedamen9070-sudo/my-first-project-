"""Chart-of-accounts foundation. No journals, balances, posting, or tax-rate inference."""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SEED = Path(__file__).with_name('accounting_chart.json')
SIDES = {'debit', 'credit'}
STATEMENTS = {'balance_sheet', 'income_statement'}
# The current money layer stores hundredths; do not mislabel data with a 0/3-decimal currency.
CURRENCIES = {'EGP', 'USD', 'EUR', 'GBP', 'SAR', 'AED', 'CHF', 'CAD', 'AUD', 'QAR'}


class ConflictError(ValueError):
    """An optimistic revision changed, requiring a fresh read before mutation."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _text(data, key, maximum, required=False):
    value = data.get(key, '')
    if not isinstance(value, str):
        raise ValueError('صيغة غير صحيحة للحقل: ' + key)
    value = value.strip()
    if (required and not value) or len(value) > maximum:
        raise ValueError('راجع الحقل: ' + key)
    return value


def _id(value):
    if type(value) is not int or value < 1:
        raise ValueError('معرّف غير صحيح')
    return value


def _revision(data, current):
    if type(data.get('revision')) is not int or data['revision'] != current['revision']:
        raise ConflictError('تغيرت البيانات من نافذة أخرى. أعد فتحها قبل الحفظ.')


def _audit(db, entity, entity_id, action, details):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit'").fetchone():
        db.execute('INSERT INTO audit(entity,entity_id,action,details,created_at) VALUES(?,?,?,?,?)',
                   (entity, entity_id, action, json.dumps(details, ensure_ascii=False), _now()))


def _account(db, account_id):
    row = db.execute('SELECT * FROM accounts WHERE id=?', (_id(account_id),)).fetchone()
    if not row:
        raise ValueError('الحساب غير موجود')
    return row


def _bounds(code):
    prefix = code.rstrip('0')
    if not prefix or len(prefix) == 6:
        raise ValueError('لا توجد مساحة ترقيم فرعية داخل هذا التجميع')
    start = int(code)
    return prefix, start, start + 10 ** (6 - len(prefix)) - 1


def _seed_validate(seed):
    rows = seed.get('accounts')
    if not isinstance(seed.get('version'), int) or not isinstance(rows, list) or not rows:
        raise ValueError('ملف تأسيس شجرة الحسابات غير صالح')
    lookup = {}
    for row in rows:
        code = row.get('code')
        if not isinstance(code, str) or not re.fullmatch(r'[1-8][0-9]{5}', code) or code in lookup:
            raise ValueError('كود مكرر أو غير صحيح في تأسيس الشجرة')
        if not isinstance(row.get('name'), str) or not row['name'].strip():
            raise ValueError('اسم حساب مفقود في تأسيس الشجرة')
        if type(row.get('posting')) is not bool or row.get('normal_side') not in SIDES or row.get('statement') not in STATEMENTS:
            raise ValueError('خصائص حساب غير صحيحة في تأسيس الشجرة')
        if not row['posting']:
            _bounds(code)
        lookup[code] = row
    roots = {r['code'] for r in rows if r.get('parent_code') is None}
    if roots != {str(i) + '00000' for i in range(1, 9)}:
        raise ValueError('تأسيس الشجرة يحتاج المجموعات الرئيسية الثماني')
    for row in rows:
        parent = lookup.get(row.get('parent_code'))
        if row.get('parent_code') is None:
            if row['posting']:
                raise ValueError('الحساب الرئيسي لا يقبل حركة مباشرة')
        elif not parent or parent['posting'] or parent['code'] == row['code']:
            raise ValueError('تسلسل الأب في تأسيس الشجرة غير صحيح')
        elif not row['code'].startswith(parent['code'].rstrip('0')) or row['statement'] != parent['statement']:
            raise ValueError('الحساب خارج نطاق التجميع أو قائمته المالية')
        ancestors = set()
        cursor = row
        while cursor.get('parent_code') is not None:
            if cursor['code'] in ancestors:
                raise ValueError('دورة غير مسموحة في تأسيس الشجرة')
            ancestors.add(cursor['code'])
            cursor = lookup[cursor['parent_code']]
    seen = set()
    for mapping in seed.get('mappings', []):
        identity = (mapping.get('module'), mapping.get('role'))
        target = lookup.get(mapping.get('account_code'))
        if identity in seen or not all(isinstance(x, str) and x for x in identity) or not target or not target['posting']:
            raise ValueError('ربط مكرر أو حساب ربط غير صحيح في التأسيس')
        allowed = mapping.get('allowed_classes', [target['code'][0]])
        if not isinstance(allowed, list) or not allowed or any(str(x) not in '12345678' or len(str(x)) != 1 for x in allowed):
            raise ValueError('نطاق ربط محاسبي غير صحيح')
        seen.add(identity)
    return lookup


def initialize(db):
    """Create an additive schema and seed once, preserving every existing edit and account."""
    statements = [
        '''CREATE TABLE IF NOT EXISTS accounting_metadata(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, description TEXT NOT NULL, seeded_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE CHECK(length(code)=6), name TEXT NOT NULL, parent_id INTEGER REFERENCES accounts(id), posting INTEGER NOT NULL CHECK(posting IN(0,1)), normal_side TEXT NOT NULL CHECK(normal_side IN('debit','credit')), statement TEXT NOT NULL CHECK(statement IN('balance_sheet','income_statement')), active INTEGER NOT NULL DEFAULT 1 CHECK(active IN(0,1)), system INTEGER NOT NULL DEFAULT 0 CHECK(system IN(0,1)), notes TEXT NOT NULL DEFAULT '', role TEXT, revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
        '''CREATE INDEX IF NOT EXISTS accounts_parent ON accounts(parent_id)''',
        '''CREATE TABLE IF NOT EXISTS account_mappings(id INTEGER PRIMARY KEY, module TEXT NOT NULL, role TEXT NOT NULL, label TEXT NOT NULL, account_id INTEGER NOT NULL REFERENCES accounts(id), allowed_classes TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN('proposed','confirmed')), expected_side TEXT NOT NULL CHECK(expected_side IN('debit','credit')), revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL, UNIQUE(module,role))''',
        '''CREATE TABLE IF NOT EXISTS accounting_settings(id INTEGER PRIMARY KEY CHECK(id=1), country_code TEXT, base_currency TEXT, fiscal_start_month INTEGER CHECK(fiscal_start_month BETWEEN 1 AND 12), user_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(user_confirmed IN(0,1)), chart_status TEXT NOT NULL DEFAULT 'draft' CHECK(chart_status IN('draft','reviewed')), revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL)''',
    ]
    for statement in statements:
        db.execute(statement)
    mapping_columns = {row['name'] for row in db.execute('PRAGMA table_info(account_mappings)')}
    if 'expected_side' not in mapping_columns:
        db.execute("ALTER TABLE account_mappings ADD COLUMN expected_side TEXT NOT NULL DEFAULT 'debit' CHECK(expected_side IN('debit','credit'))")
        db.execute("UPDATE account_mappings SET expected_side=(SELECT normal_side FROM accounts WHERE accounts.id=account_mappings.account_id)")
    db.execute('''INSERT OR IGNORE INTO accounting_settings(id,country_code,base_currency,fiscal_start_month,user_confirmed,chart_status,updated_at) VALUES(1,'EG','EGP',1,1,'draft',?)''', (_now(),))
    if db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]:
        if not db.execute('SELECT 1 FROM accounting_metadata WHERE id=1').fetchone():
            raise ValueError('الشجرة موجودة دون سجل تأسيس؛ تحتاج مراجعة ولا يعاد تأسيسها فوق البيانات')
        return
    seed = json.loads(SEED.read_text(encoding='utf-8'))
    lookup = _seed_validate(seed)
    pending = list(seed['accounts'])
    inserted = {}
    stamp = _now()
    while pending:
        next_pending = []
        for row in pending:
            parent_code = row.get('parent_code')
            if parent_code is not None and parent_code not in inserted:
                next_pending.append(row)
                continue
            cursor = db.execute('''INSERT INTO accounts(code,name,parent_id,posting,normal_side,statement,active,system,notes,role,created_at,updated_at) VALUES(?,?,?,?,?,?,1,1,?,?,?,?)''',
                                (row['code'], row['name'].strip(), inserted.get(parent_code), int(row['posting']), row['normal_side'], row['statement'], row.get('notes', ''), row.get('role'), stamp, stamp))
            inserted[row['code']] = cursor.lastrowid
        if len(next_pending) == len(pending):
            raise ValueError('تعذر ترتيب آباء شجرة الحسابات')
        pending = next_pending
    for mapping in seed.get('mappings', []):
        allowed = [str(x) for x in mapping.get('allowed_classes', [mapping['account_code'][0]])]
        db.execute('''INSERT INTO account_mappings(module,role,label,account_id,allowed_classes,status,expected_side,updated_at) VALUES(?,?,?,?,?,'proposed',?,?)''',
                   (mapping['module'], mapping['role'], mapping['label'], inserted[mapping['account_code']], json.dumps(allowed), lookup[mapping['account_code']]['normal_side'], stamp))
    db.execute('INSERT INTO accounting_metadata(id,version,description,seeded_at) VALUES(1,?,?,?)',
               (seed['version'], seed.get('description', ''), stamp))
    _audit(db, 'chart', 1, 'initialize_chart', {'version': seed['version'], 'accounts': len(inserted), 'opening_balances_created': False})


def get_settings(db):
    row = dict(db.execute('SELECT * FROM accounting_settings WHERE id=1').fetchone())
    row['user_confirmed'] = bool(row['user_confirmed'])
    metadata = db.execute('SELECT version FROM accounting_metadata WHERE id=1').fetchone()
    row['chart_version'] = metadata['version'] if metadata else None
    return row


def snapshot(db):
    accounts = []
    for row in db.execute('''SELECT a.*,p.code parent_code FROM accounts a LEFT JOIN accounts p ON p.id=a.parent_id ORDER BY a.code'''):
        item = dict(row)
        for key in ('posting', 'active', 'system'):
            item[key] = bool(item[key])
        accounts.append(item)
    mappings = []
    for row in db.execute('''SELECT m.*,a.code account_code,a.name account_name,a.active account_active,a.posting account_posting FROM account_mappings m JOIN accounts a ON a.id=m.account_id ORDER BY m.module,m.id'''):
        item = dict(row)
        item['allowed_classes'] = json.loads(item['allowed_classes'])
        item['account_active'] = bool(item['account_active'])
        item['account_posting'] = bool(item['account_posting'])
        mappings.append(item)
    metadata = db.execute('SELECT * FROM accounting_metadata WHERE id=1').fetchone()
    return {'version': metadata['version'] if metadata else None,
            'description': metadata['description'] if metadata else '',
            'settings': get_settings(db),
            'counts': {'total': len(accounts), 'groups': sum(not a['posting'] for a in accounts), 'posting': sum(a['posting'] for a in accounts), 'active': sum(a['active'] for a in accounts), 'mappings': len(mappings), 'confirmed_mappings': sum(m['status'] == 'confirmed' for m in mappings)},
            'accounts': accounts, 'mappings': mappings}


def _auto_code(db, parent, posting):
    prefix, start, end = _bounds(parent['code'])
    records = db.execute('SELECT code,posting FROM accounts').fetchall()
    used = {int(row['code']) for row in records}
    reserved = []
    for row in records:
        if not row['posting'] and row['code'] != parent['code'] and row['code'].startswith(prefix):
            _, low, high = _bounds(row['code'])
            reserved.append((low, high))
    if posting:
        candidate = start + 1
        while candidate <= end:
            blocking = next((high for low, high in reserved if low <= candidate <= high), None)
            if blocking is not None:
                candidate = blocking + 1
            elif candidate in used or candidate % 10 == 0:
                candidate += 1
            else:
                return f'{candidate:06d}'
    else:
        zeros = 6 - len(prefix)
        if zeros < 2:
            raise ValueError('هذا التجميع يسمح بحسابات حركة فقط؛ اختر تجميعًا أعلى لإضافة فرع')
        step = 10 ** (zeros - 1)
        for candidate in range(start + step, end + 1, step):
            high = candidate + step - 1
            if candidate in used or any(candidate <= c <= high for c in used):
                continue
            if any(not (high < low or candidate > other_high) for low, other_high in reserved):
                continue
            return f'{candidate:06d}'
    raise ValueError('اكتمل نطاق الأكواد لهذا التجميع؛ اختر تجميعًا آخر أو أضف فرعًا مناسبًا')


def _result_account(db, account_id):
    row = dict(db.execute('SELECT a.*,p.code parent_code FROM accounts a LEFT JOIN accounts p ON p.id=a.parent_id WHERE a.id=?', (account_id,)).fetchone())
    for key in ('posting', 'active', 'system'):
        row[key] = bool(row[key])
    return row


def create_account(db, data):
    parent = _account(db, data.get('parent_id'))
    if parent['posting'] or not parent['active']:
        raise ValueError('أضف الحساب تحت تجميع نشط، وليس حساب حركة')
    posting = data.get('posting', True)
    if type(posting) is not bool:
        raise ValueError('حدد نوع الحساب: تجميع أو حساب حركة')
    if set(data) - {'parent_id','name','posting','normal_side','notes','statement','code','active','system','role'}:
        raise ValueError('حقول إضافة حساب غير معروفة')
    side = data.get('normal_side', parent['normal_side'])
    if not isinstance(side,str) or side not in SIDES:
        raise ValueError('حدد طبيعة الحساب مدينة أو دائنة')
    if 'statement' in data and data['statement'] != parent['statement']:
        raise ValueError('القائمة المالية تورث من التجميع ولا تغير عند الإضافة')
    if any(k in data for k in ('code', 'active', 'system', 'role')):
        raise ValueError('الكود والحالة والخصائص النظامية تحدد تلقائيًا')
    name = _text(data, 'name', 160, True)
    notes = _text(data, 'notes', 2000)
    if db.execute('SELECT 1 FROM accounts WHERE parent_id=? AND name=?', (parent['id'], name)).fetchone():
        raise ValueError('يوجد حساب بنفس الاسم داخل هذا التجميع')
    code = _auto_code(db, parent, posting)
    stamp = _now()
    cursor = db.execute('''INSERT INTO accounts(code,name,parent_id,posting,normal_side,statement,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                        (code, name, parent['id'], int(posting), side, parent['statement'], notes, stamp, stamp))
    db.execute("UPDATE accounting_settings SET chart_status='draft',revision=revision+1,updated_at=? WHERE id=1 AND chart_status='reviewed'", (stamp,))
    _audit(db, 'account', cursor.lastrowid, 'create_account', {'code': code, 'parent_code': parent['code'], 'posting': posting})
    return _result_account(db, cursor.lastrowid)


def update_account(db, account_id, data):
    old = _account(db, account_id)
    _revision(data, old)
    for key in ('code', 'parent_id', 'posting', 'normal_side', 'statement', 'system', 'role'):
        if key in data and data[key] != old[key]:
            raise ValueError('الكود والتبعية والنوع والطبيعة خصائص ثابتة؛ أضف حسابًا جديدًا بدل تغيير هيكل الحساب')
    allowed = {'revision', 'name', 'notes', 'active', 'code', 'parent_id', 'posting', 'normal_side', 'statement', 'system', 'role'}
    if set(data) - allowed:
        raise ValueError('حقول تعديل الحساب غير معروفة')
    name = _text(data, 'name', 160, True) if 'name' in data else old['name']
    notes = _text(data, 'notes', 2000) if 'notes' in data else old['notes']
    active = data.get('active', bool(old['active']))
    if type(active) is not bool:
        raise ValueError('حالة الحساب غير صحيحة')
    if db.execute('SELECT 1 FROM accounts WHERE parent_id IS ? AND name=? AND id<>?', (old['parent_id'], name, old['id'])).fetchone():
        raise ValueError('يوجد حساب بنفس الاسم داخل هذا التجميع')
    if not active:
        if old['parent_id'] is None:
            raise ValueError('لا يمكن إيقاف مجموعة رئيسية')
        if db.execute('SELECT 1 FROM accounts WHERE parent_id=? AND active=1', (old['id'],)).fetchone():
            raise ValueError('أوقف أو راجع الحسابات الفرعية النشطة أولًا')
        if db.execute('SELECT 1 FROM account_mappings WHERE account_id=?', (old['id'],)).fetchone():
            raise ValueError('الحساب مرتبط بموديول؛ غيّر الربط قبل إيقافه')
    elif old['parent_id'] is not None and not _account(db, old['parent_id'])['active']:
        raise ValueError('لا يمكن تفعيل حساب تحت تجميع موقوف')
    db.execute('UPDATE accounts SET name=?,notes=?,active=?,revision=revision+1,updated_at=? WHERE id=?',
               (name, notes, int(active), _now(), old['id']))
    db.execute("UPDATE accounting_settings SET chart_status='draft',revision=revision+1,updated_at=? WHERE id=1 AND chart_status='reviewed'", (_now(),))
    _audit(db, 'account', old['id'], 'update_account', {'code': old['code'], 'previous_name': old['name'], 'name': name, 'previous_active': bool(old['active']), 'active': active, 'previous_notes': old['notes'], 'notes': notes})
    return _result_account(db, old['id'])


def update_mapping(db, mapping_id, data):
    old = db.execute('SELECT * FROM account_mappings WHERE id=?', (_id(mapping_id),)).fetchone()
    if not old:
        raise ValueError('الربط غير موجود')
    _revision(data, old)
    if set(data) - {'revision', 'account_id', 'status'}:
        raise ValueError('خصائص ربط غير معروفة')
    target = _account(db, data.get('account_id', old['account_id']))
    if not target['active'] or not target['posting']:
        raise ValueError('الربط يحتاج حساب حركة نشطًا، وليس تجميعًا')
    if target['code'][0] not in json.loads(old['allowed_classes']):
        raise ValueError('الحساب خارج المجموعة المحاسبية المسموحة لهذا الربط')
    if target['normal_side'] != old['expected_side']:
        raise ValueError('طبيعة الحساب المختار لا تطابق طبيعة الربط المطلوب')
    status = data.get('status', old['status'])
    if status not in {'proposed', 'confirmed'}:
        raise ValueError('حالة الربط غير صحيحة')
    db.execute('UPDATE account_mappings SET account_id=?,status=?,revision=revision+1,updated_at=? WHERE id=?',
               (target['id'], status, _now(), old['id']))
    # Changing a template after review returns the chart to draft for another review.
    db.execute("UPDATE accounting_settings SET chart_status='draft',revision=revision+1,updated_at=? WHERE id=1 AND chart_status='reviewed'", (_now(),))
    _audit(db, 'account_mapping', old['id'], 'update_account_mapping', {'module': old['module'], 'role': old['role'], 'previous_account_id': old['account_id'], 'account_code': target['code'], 'previous_status': old['status'], 'status': status})
    row = dict(db.execute('SELECT m.*,a.code account_code,a.name account_name FROM account_mappings m JOIN accounts a ON a.id=m.account_id WHERE m.id=?', (old['id'],)).fetchone())
    row['allowed_classes'] = json.loads(row['allowed_classes'])
    return row


def update_settings(db, data):
    old = get_settings(db)
    _revision(data, old)
    if set(data) - {'revision', 'country_code', 'base_currency', 'fiscal_start_month', 'chart_status'}:
        raise ValueError('حقول إعدادات غير معروفة')
    country = data.get('country_code', old['country_code'])
    currency = data.get('base_currency', old['base_currency'])
    month = data.get('fiscal_start_month', old['fiscal_start_month'])
    status = data.get('chart_status', old['chart_status'])
    if not isinstance(country, str) or not re.fullmatch(r'[A-Z]{2}', country):
        raise ValueError('استخدم رمز دولة من حرفين كبيرين مثل EG')
    if not isinstance(currency,str) or currency not in CURRENCIES:
        raise ValueError('عملة غير مدعومة حاليًا؛ نظام المبالغ الحالي يدعم عملات من منزلتين عشريتين مثل EGP وUSD وEUR')
    if type(month) is not int or not 1 <= month <= 12:
        raise ValueError('حدد شهر بدء السنة المالية من 1 إلى 12')
    if status not in {'draft', 'reviewed'}:
        raise ValueError('حالة مراجعة الشجرة غير صحيحة')
    changing = any(value != old[key] for key, value in [('country_code', country), ('base_currency', currency), ('fiscal_start_month', month)])
    if currency != old['base_currency'] and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='invoices'").fetchone() and db.execute('SELECT 1 FROM invoices LIMIT 1').fetchone():
        raise ValueError('توجد فواتير محفوظة؛ تغيير العملة يحتاج خطة تحويل ولا يعيد تسمية مبالغها تلقائيًا')
    if changing:
        for table in ('journal_entries', 'journals', 'postings'):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() and db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone():
                raise ValueError('توجد قيود مالية؛ لا تغير إعدادات الأساس بعد بدء الترحيل')
        status = 'draft'
    if status == 'reviewed' and db.execute("SELECT 1 FROM account_mappings WHERE status<>'confirmed' LIMIT 1").fetchone():
        raise ValueError('راجع وأكد روابط الموديولات قبل تسجيل مراجعة الشجرة')
    db.execute('''UPDATE accounting_settings SET country_code=?,base_currency=?,fiscal_start_month=?,chart_status=?,user_confirmed=1,revision=revision+1,updated_at=? WHERE id=1''',
               (country, currency, month, status, _now()))
    _audit(db, 'accounting_settings', 1, 'update_accounting_settings', {'country_code': country, 'base_currency': currency, 'fiscal_start_month': month, 'chart_status': status, 'amount_conversion_performed': False})
    return get_settings(db)


def _safe_cell(value):
    value = '' if value is None else str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r', '\n')) else value


def export_csv(db):
    """BOM-prefixed CSV for review; contains account definitions, never invented balances."""
    stream = io.StringIO(newline='')
    stream.write('\ufeff')
    writer = csv.writer(stream)
    writer.writerow(['الكود', 'الحساب', 'كود الأب', 'النوع', 'الطبيعة', 'القائمة', 'الحالة', 'ملاحظات'])
    for account in snapshot(db)['accounts']:
        writer.writerow([_safe_cell(x) for x in [account['code'], account['name'], account['parent_code'], 'حركة' if account['posting'] else 'تجميع', 'مدين' if account['normal_side'] == 'debit' else 'دائن', 'مركز مالي' if account['statement'] == 'balance_sheet' else 'نتائج أعمال', 'نشط' if account['active'] else 'موقوف', account['notes']]])
    return stream.getvalue()
