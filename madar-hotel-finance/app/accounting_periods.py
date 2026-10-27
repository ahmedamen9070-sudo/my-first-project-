"""Monthly accounting locks with immutable, reviewed close/reopen events.

The caller owns the transaction. A lock is not a year-end closing journal, a tax
filing, or proof that opening balances have been entered.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import re
from datetime import date

import accounting

DRAFTS = (
    ('invoice', 'invoices', 'business_date', 'فاتورة'),
    ('return', 'supplier_returns', 'business_date', 'مرتجع'),
    ('payment', 'supplier_payments', 'payment_date', 'سداد'),
    ('settlement', 'supplier_settlements', 'business_date', 'تسوية'),
    ('refund', 'supplier_refunds', 'business_date', 'استرداد'),
)


def _has(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _month(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}', value):
        raise ValueError('حدد الفترة بصيغة سنة-شهر مثل 2026-01')
    try:
        start = date.fromisoformat(value + '-01')
    except ValueError:
        raise ValueError('الفترة المحاسبية غير صحيحة') from None
    return value, start.isoformat(), date(start.year, start.month, calendar.monthrange(start.year, start.month)[1]).isoformat()


def _date(value):
    if not isinstance(value, str):
        raise ValueError('تاريخ المستند غير صحيح')
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError('تاريخ المستند غير صحيح') from None
    if parsed.isoformat() != value:
        raise ValueError('صيغة تاريخ المستند غير صحيحة')
    return value


def initialize(db):
    for statement in (
        'CREATE TABLE IF NOT EXISTS sequences(kind TEXT PRIMARY KEY,value INTEGER NOT NULL)',
        '''CREATE TABLE IF NOT EXISTS accounting_periods(
            month TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT 'open' CHECK(status IN('open','closed')),
            revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),last_event_id INTEGER,
            updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS accounting_period_events(
            id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,request_key TEXT NOT NULL UNIQUE,
            request_fingerprint TEXT NOT NULL,month TEXT NOT NULL REFERENCES accounting_periods(month),
            action TEXT NOT NULL CHECK(action IN('close','reopen')),reference TEXT NOT NULL,reason TEXT NOT NULL,
            snapshot TEXT NOT NULL,created_at TEXT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS accounting_period_events_month ON accounting_period_events(month,id)',
    ):
        db.execute(statement)
    for operation in ('UPDATE', 'DELETE'):
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_period_events_{operation.lower()}
            BEFORE {operation} ON accounting_period_events
            BEGIN SELECT RAISE(ABORT,'accounting period event is immutable'); END''')


def assert_open(db, business_date):
    """No mutation; old installations without this additive module remain open."""
    business_date = _date(business_date)
    if not _has(db, 'accounting_periods'):
        return
    row = db.execute("SELECT month FROM accounting_periods WHERE status='closed' AND month>=? ORDER BY month DESC LIMIT 1", (business_date[:7],)).fetchone()
    if row:
        if row['month'] == business_date[:7]:
            raise ValueError('الفترة ' + business_date[:7] + ' مقفولة؛ أعد فتحها بسبب مسجل قبل تعديل أو ترحيل مستند فيها')
        raise ValueError('الفترة اللاحقة ' + row['month'] + ' مقفولة؛ أعد فتحها أولًا قبل تعديل مستند بتاريخ ' + business_date)


def assert_document_dates(db, new_date, old_date=None):
    """A draft cannot escape review by moving from a locked month to an open one."""
    if old_date is not None:
        assert_open(db, old_date)
    assert_open(db, new_date)


def _event(db, ident):
    row = db.execute('SELECT * FROM accounting_period_events WHERE id=?', (ident,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result['snapshot'] = json.loads(result['snapshot'])
    result.pop('request_fingerprint', None)
    return result


def _period(db, month):
    month, start, end = _month(month)
    row = db.execute('SELECT * FROM accounting_periods WHERE month=?', (month,)).fetchone() if _has(db, 'accounting_periods') else None
    result = dict(row) if row else {'month': month, 'status': 'open', 'revision': 1, 'last_event_id': None, 'updated_at': None}
    later = db.execute("SELECT month FROM accounting_periods WHERE status='closed' AND month>? ORDER BY month DESC LIMIT 1", (month,)).fetchone() if _has(db, 'accounting_periods') else None
    result.update(start_date=start, end_date=end, last_event=_event(db, result['last_event_id']) if result['last_event_id'] else None,
                  blocked_by_later_closed=later['month'] if later else None, effective_locked=result['status']=='closed' or later is not None)
    return result


def snapshot(db, year=None):
    if year is None:
        year = date.today().year
    if isinstance(year, str) and re.fullmatch(r'[0-9]{4}', year):
        year = int(year)
    if type(year) is not int or not 1 <= year <= 9999:
        raise ValueError('السنة المالية غير صحيحة')
    return {'year': year, 'periods': [_period(db, f'{year:04d}-{month:02d}') for month in range(1, 13)],
            'policy': 'monthly_lock', 'settings': {'fiscal_start_month': accounting.get_settings(db)['fiscal_start_month']},
            'notes': ['قفل شهري يمنع تعديل أو ترحيل مستند مؤرخ داخل الفترة؛ لا ينشئ قيد إقفال سنوي.',
                      'إعادة فتح فترة تحتاج مرجعًا وسببًا مسجلين؛ الفترات المقفولة اللاحقة تفتح أولًا.']}


def _pending(db, start, end):
    rows, counts = [], {kind: 0 for kind, *_ in DRAFTS}
    for kind, table, field, label in DRAFTS:
        if not _has(db, table):
            continue
        extra = ''
        if kind == 'invoice' and _has(db, 'invoice_postings'):
            extra = ' AND NOT EXISTS(SELECT 1 FROM invoice_postings ip WHERE ip.invoice_id=d.id)'
        for row in db.execute(f"SELECT id,code,{field} date FROM {table} d WHERE status='draft' AND {field}>=? AND {field}<=?" + extra + ' ORDER BY date,id', (start, end)):
            rows.append({'type': kind, 'label': label, **dict(row)})
            counts[kind] += 1
    rows.sort(key=lambda row: (row['date'], row['type'], row['id']))
    return rows, counts


def preview(db, month):
    period = _period(db, month)
    start, end = period['start_date'], period['end_date']
    totals = {'count': 0, 'debit_cents': 0, 'credit_cents': 0}
    unbalanced, unsealed, earlier_unbalanced, earlier_unsealed = [], [], [], []
    if _has(db, 'journal_entries') and _has(db, 'journal_lines'):
        for raw in db.execute('''SELECT j.id,j.code,j.sealed,j.entry_date,COALESCE(SUM(l.debit_cents),0) debit_cents,
            COALESCE(SUM(l.credit_cents),0) credit_cents,COUNT(l.id) line_count
            FROM journal_entries j LEFT JOIN journal_lines l ON l.journal_id=j.id
            WHERE j.entry_date<=? GROUP BY j.id ORDER BY j.id''', (end,)):
            row = dict(raw)
            earlier_journal = row['entry_date'] < start
            if not row['sealed']:
                (earlier_unsealed if earlier_journal else unsealed).append(row)
                continue
            if not earlier_journal:
                totals['count'] += 1
                totals['debit_cents'] += row['debit_cents']
                totals['credit_cents'] += row['credit_cents']
            if row['debit_cents'] != row['credit_cents'] or row['line_count'] < 2 or row['debit_cents'] <= 0:
                (earlier_unbalanced if earlier_journal else unbalanced).append(row)
    all_pending, _ = _pending(db, '0001-01-01', end)
    pending = [row for row in all_pending if row['date'] >= start]
    earlier = [row for row in all_pending if row['date'] < start]
    counts = {kind: sum(row['type']==kind for row in pending) for kind, *_ in DRAFTS}
    import supplier_reconciliation
    report = supplier_reconciliation.report(db, end)
    # Only compact diagnostics are saved in immutable close snapshots. Detailed
    # dated supplier/journal drilldown stays available from reconciliation.
    reconciliation = {key: report[key] for key in ('matched', 'totals', 'counts')}
    blockers = []
    if period['status'] == 'closed':
        blockers.append({'code': 'already_closed', 'message': 'الفترة مقفولة بالفعل.', 'count': 1})
    if pending:
        blockers.append({'code': 'pending_drafts', 'message': 'راجع المسودات المؤرخة داخل الفترة واعتمدها أو ألغها قبل القفل.', 'count': len(pending)})
    if earlier:
        blockers.append({'code': 'earlier_pending_drafts', 'message': 'توجد مسودات أقدم من الفترة؛ راجعها قبل قفل فترة لاحقة.', 'count': len(earlier)})
    if unbalanced:
        blockers.append({'code': 'unbalanced_journals', 'message': 'توجد قيود غير متوازنة أو غير مكتملة الأطراف داخل الفترة.', 'count': len(unbalanced)})
    if unsealed:
        blockers.append({'code': 'unsealed_journals', 'message': 'توجد قيود لم يكتمل ترحيلها داخل الفترة.', 'count': len(unsealed)})
    if earlier_unbalanced:
        blockers.append({'code': 'earlier_unbalanced_journals', 'message': 'توجد قيود أقدم غير متوازنة أو غير مكتملة؛ راجعها قبل قفل فترة لاحقة.', 'count': len(earlier_unbalanced)})
    if earlier_unsealed:
        blockers.append({'code': 'earlier_unsealed_journals', 'message': 'توجد قيود أقدم لم يكتمل ترحيلها؛ راجعها قبل قفل فترة لاحقة.', 'count': len(earlier_unsealed)})
    if not report['matched']:
        blockers.append({'code': 'supplier_reconciliation', 'message': 'مطابقة الموردين والدفعات المقدمة مع القيود تحتاج مراجعة.',
                         'count': max(report['counts'].get('exception_count', 0), report['counts'].get('mismatched_suppliers', 0), report['counts'].get('mismatched_accounts', 0), 1)})
    return {'period': period, 'journal_totals': totals, 'unbalanced_journals': unbalanced, 'unsealed_journals': unsealed,
            'earlier_unbalanced_journals': earlier_unbalanced, 'earlier_unsealed_journals': earlier_unsealed,
            'pending_drafts': pending, 'earlier_pending_drafts': earlier, 'draft_counts': counts, 'reconciliation': reconciliation,
            'events': [_event(db, row['id']) for row in db.execute('SELECT id FROM accounting_period_events WHERE month=? ORDER BY id', (month,))] if _has(db, 'accounting_period_events') else [],
            'blockers': blockers, 'can_close': not blockers,
            'warnings': ['الأرصدة الافتتاحية لم تؤكد في هذه المرحلة؛ القفل مراجعة للحركات المسجلة فقط.',
                         'القفل الشهري لا ينشئ قيود إقفال أرباح وخسائر ولا يمثل إقرارًا ضريبيًا.']}


def _payload(month, action, data):
    _month(month)
    return {'month': month, 'action': action,
            'reference': accounting._text(data, 'reference', 200, True),
            'reason': accounting._text(data, 'reason', 2000, True)}


def _retry(db, month, action, data):
    key = accounting._text(data, 'request_key', 100, True)
    value = _payload(month, action, data)
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    row = db.execute('SELECT * FROM accounting_period_events WHERE request_key=?', (key,)).fetchone()
    if row:
        if row['request_fingerprint'] != digest:
            raise accounting.ConflictError('مفتاح الطلب مستخدم لإجراء فترة ببيانات مختلفة')
        return key, digest, _event(db, row['id'])
    return key, digest, None


def _append(db, month, action, data, key, digest, review):
    period = _period(db, month)
    stamp = accounting._now()
    db.execute('INSERT OR IGNORE INTO accounting_periods(month,status,revision,updated_at) VALUES(?,\'open\',1,?)', (month, stamp))
    db.execute("INSERT INTO sequences(kind,value) VALUES('accounting_period_event',1) ON CONFLICT(kind) DO UPDATE SET value=value+1")
    number = db.execute("SELECT value FROM sequences WHERE kind='accounting_period_event'").fetchone()[0]
    code = f'PER-{number:06d}'
    value = _payload(month, action, data)
    stored_review = {key: value for key, value in review.items() if key != 'events'}
    if 'period' in stored_review:
        stored_review['period'] = dict(stored_review['period'])
        last_event = stored_review['period'].get('last_event')
        if last_event:
            stored_review['period']['last_event'] = {key: value for key, value in last_event.items() if key != 'snapshot'}
    event_id = db.execute('''INSERT INTO accounting_period_events(code,request_key,request_fingerprint,month,action,reference,reason,snapshot,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', (code, key, digest, month, action, value['reference'], value['reason'], json.dumps(stored_review, ensure_ascii=False, separators=(',', ':')), stamp)).lastrowid
    db.execute('UPDATE accounting_periods SET status=?,revision=revision+1,last_event_id=?,updated_at=? WHERE month=?',
               ('closed' if action == 'close' else 'open', event_id, stamp, month))
    accounting._audit(db, 'accounting_period', event_id, action, {'month': month, 'code': code, 'reference': value['reference'], 'reason': value['reason']})
    return {'period': _period(db, month), 'event': _event(db, event_id), 'preview': review}


def close(db, month, data):
    key, digest, retry = _retry(db, month, 'close', data)
    if retry:
        return {'period': _period(db, month), 'event': retry, 'preview': retry['snapshot']}
    period = _period(db, month)
    accounting._revision(data, period)
    review = preview(db, month)
    if not review['can_close']:
        raise ValueError('لا يمكن قفل الفترة: ' + ' '.join(item['message'] for item in review['blockers']))
    return _append(db, month, 'close', data, key, digest, review)


def reopen(db, month, data):
    key, digest, retry = _retry(db, month, 'reopen', data)
    if retry:
        return {'period': _period(db, month), 'event': retry, 'preview': retry['snapshot']}
    period = _period(db, month)
    accounting._revision(data, period)
    if period['status'] != 'closed':
        raise ValueError('الفترة مفتوحة بالفعل')
    later = db.execute("SELECT month FROM accounting_periods WHERE status='closed' AND month>? ORDER BY month", (month,)).fetchall()
    if later:
        raise ValueError('أعد فتح الفترات المقفولة اللاحقة أولًا: ' + '، '.join(row['month'] for row in later))
    return _append(db, month, 'reopen', data, key, digest,
                   {'period': period, 'later_closed_months': [], 'reason': accounting._text(data, 'reason', 2000, True)})
