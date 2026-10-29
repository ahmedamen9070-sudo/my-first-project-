"""Read-only current supplier worklists and master-data preparation findings.

The review date labels future-dated work; it does not reconstruct earlier draft
states. Opening this list never initializes schema or changes a document.
"""
from __future__ import annotations

from datetime import date
import json


DRAFTS = (
    ('invoice', 'invoices', 'business_date', 'total_cents', 'فاتورة مورد'),
    ('return', 'supplier_returns', 'business_date', 'total_cents', 'مرتجع مورد'),
    ('payment', 'supplier_payments', 'payment_date', 'amount_cents', 'سداد مورد'),
    ('settlement', 'supplier_settlements', 'business_date', 'amount_cents', 'تسوية مورد'),
    ('refund', 'supplier_refunds', 'business_date', 'amount_cents', 'استرداد من مورد'),
    ('remittance', 'withholding_remittances', 'business_date', 'amount_cents', 'توريد خصم ضريبي'),
)


def _date(value):
    if not isinstance(value, str):
        raise ValueError('حدد تاريخ المراجعة بصيغة سنة-شهر-يوم')
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError('تاريخ المراجعة غير صحيح') from None
    if parsed.isoformat() != value:
        raise ValueError('حدد تاريخ المراجعة بصيغة سنة-شهر-يوم')
    return value


def _columns(db, table):
    # Identifiers are internal constants, never input from a request.
    return {row['name'] for row in db.execute('PRAGMA table_info(' + table + ')')}


def _supplier_names(db):
    columns = _columns(db, 'suppliers')
    if 'id' not in columns:
        return {}
    name = 'name' if 'name' in columns else "'' AS name"
    code = 'code' if 'code' in columns else "'' AS code"
    return {row['id']: dict(row) for row in db.execute(f'SELECT id,{name},{code} FROM suppliers')}


def _closed_months(db):
    if not {'month', 'status'} <= _columns(db, 'accounting_periods'):
        return []
    return [row['month'] for row in db.execute(
        "SELECT month FROM accounting_periods WHERE status='closed' ORDER BY month DESC")]


def _payment_reference(row):
    try:
        details = json.loads(row.get('details') or '{}')
    except (ValueError, TypeError):
        return ''
    if not isinstance(details, dict):
        return ''
    for field in ('cheque_number', 'transaction_reference', 'deposit_reference', 'receipt_reference'):
        value = details.get(field)
        if isinstance(value, str) and value:
            return value
    return ''


def _drafts(db, as_of):
    suppliers, months, drafts = _supplier_names(db), _closed_months(db), []
    has_postings = 'invoice_id' in _columns(db, 'invoice_postings')
    for kind, table, date_field, amount_field, label in DRAFTS:
        columns = _columns(db, table)
        if not {'id', 'code', 'status', date_field, amount_field} <= columns:
            continue
        posted = (' AND NOT EXISTS(SELECT 1 FROM invoice_postings p WHERE p.invoice_id=d.id)'
                  if kind == 'invoice' and has_postings else '')
        for raw in db.execute(f"SELECT d.* FROM {table} d WHERE d.status='draft'" + posted):
            original = dict(raw)
            ds = original[date_field]
            supplier_id = original.get('supplier_id')
            supplier = suppliers.get(supplier_id, {})
            locked = next((month for month in months if month >= ds[:7]), None)
            drafts.append({
                'kind': kind, 'id': original['id'], 'code': original['code'],
                'business_date': ds, 'supplier_id': supplier_id,
                'supplier_name': supplier.get('name') or '',
                'supplier_code': supplier.get('code') or '',
                'reference': original.get('reference') or (_payment_reference(original) if kind == 'payment' else ''),
                'amount_cents': int(original[amount_field] or 0),
                'future_dated': ds > as_of, 'locked_period': locked,
                'label': label, 'current_status': 'draft',
                'authority_name': original.get('authority_name') or '',
                'action': {'type': kind, 'id': original['id']},
            })
    drafts.sort(key=lambda row: (row['business_date'], row['kind'], row['id']))
    return drafts


def _accounts(db):
    columns = _columns(db, 'accounts')
    if not {'id', 'code', 'posting', 'active', 'normal_side'} <= columns:
        return {}
    return {row['id']: dict(row) for row in db.execute('SELECT * FROM accounts')}


def _account_reasons(account, allowed_classes, side):
    if account is None:
        return ['missing_account']
    reasons = []
    if not account['active']:
        reasons.append('inactive')
    if not account['posting']:
        reasons.append('not_posting')
    if account['normal_side'] != side:
        reasons.append('invalid_side')
    if not str(account['code']).startswith(tuple(allowed_classes)):
        reasons.append('invalid_class')
    return reasons


REASONS = {
    'missing_mapping': 'لم يحدد ربط الحساب',
    'unconfirmed': 'الربط يحتاج مراجعة وتأكيدًا',
    'missing_account': 'الحساب غير مربوط أو غير موجود',
    'inactive': 'الحساب موقوف',
    'not_posting': 'الحساب تجميعي ولا يقبل الترحيل',
    'invalid_side': 'طبيعة الحساب غير مناسبة لهذا الاستخدام',
    'invalid_class': 'الحساب خارج الفئات المسموح بها لهذا الاستخدام',
}


def _setup_issues(db):
    accounts, issues = _accounts(db), []
    mapping_columns = _columns(db, 'account_mappings')
    mapping = None
    if {'id', 'module', 'role', 'account_id', 'status'} <= mapping_columns:
        mapping = db.execute("SELECT * FROM account_mappings WHERE module='الموردون' AND role='payable_control'").fetchone()
    mapping = dict(mapping) if mapping else None
    account = accounts.get(mapping['account_id']) if mapping else None
    reasons = ['missing_mapping'] if mapping is None else (
        ([] if mapping['status'] == 'confirmed' else ['unconfirmed']) +
        _account_reasons(account, ('2',), 'credit'))
    if reasons:
        issues.append({'kind': 'mapping', 'entity_id': mapping['id'] if mapping else None,
                       'code': account['code'] if account else 'payable_control',
                       'label': 'حساب مراقبة الموردين',
                       'message': 'راجع حساب مراقبة الموردين: ' + '؛ '.join(REASONS[reason] for reason in reasons) + '.',
                       'severity': 'warning', 'reasons': reasons,
                       'action': {'type': 'accounting_mappings', 'id': mapping['id'] if mapping else None}})
    columns = _columns(db, 'products')
    if not {'id', 'name'} <= columns:
        return issues
    for raw in db.execute('SELECT * FROM products ORDER BY id'):
        product = dict(raw)
        account = accounts.get(product.get('expense_account_id'))
        reasons = _account_reasons(account, ('5', '6', '8'), 'debit')
        if reasons:
            issues.append({'kind': 'product', 'entity_id': product['id'],
                           'code': product.get('code') or '', 'label': product['name'],
                           'supplier_id': product.get('supplier_id'),
                           'message': 'راجع حساب تكلفة البند: ' + '؛ '.join(REASONS[reason] for reason in reasons) + '.',
                           'severity': 'warning', 'reasons': reasons,
                           'action': {'type': 'product', 'id': product['id']}})
    return issues


def current_work(db, as_of):
    """Return every current draft and scoped setup finding without writing."""
    as_of = _date(as_of)
    drafts, issues = _drafts(db, as_of), _setup_issues(db)
    by_kind = {kind: sum(row['kind'] == kind for row in drafts) for kind, *_ in DRAFTS}
    return {'drafts': drafts, 'setup_issues': issues,
            'counts': {'drafts': len(drafts), 'future_drafts': sum(row['future_dated'] for row in drafts),
                       'locked_drafts': sum(row['locked_period'] is not None for row in drafts),
                       'setup_issues': len(issues), 'drafts_by_kind': by_kind},
            'basis': 'current_worklist',
            'notes': ['قائمة المسودات والإعدادات تعكس حالتها الحالية؛ تاريخ المراجعة يميز المستند المؤرخ لاحقًا ولا يعيد بناء حالة مسودة قديمة.',
                      'الإعدادات المعروضة تخص مراقبة الموردين وحسابات تكلفة البنود؛ الضرائب والميزات الاختيارية لا تفترض تلقائيًا.']}
