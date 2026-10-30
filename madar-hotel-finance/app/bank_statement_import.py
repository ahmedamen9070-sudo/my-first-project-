"""Read-only CSV inspection and exact EGP statement normalization.

The browser supplies decoded text. No file paths, database writes, dates, money
directions or missing balances are inferred from operating records here.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from datetime import date


MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 2000
MAX_COLUMNS = 64
MAX_CENTS = 10 ** 13
DELIMITERS = {'comma': ',', 'semicolon': ';', 'tab': '\t'}
FORMATS = ('YYYY-MM-DD', 'DD/MM/YYYY', 'DD-MM-YYYY', 'MM/DD/YYYY')
COLUMN_KEYS = {'date', 'value_date', 'reference', 'description',
               'deposit', 'withdrawal', 'amount', 'balance'}
DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')
ALIASES = {
    'date': ('date', 'transaction date', 'booking date', 'posting date',
             'التاريخ', 'تاريخ', 'تاريخ الحركة', 'تاريخ العملية', 'تاريخ القيد'),
    'value_date': ('value date', 'تاريخ القيمة'),
    'reference': ('reference', 'ref', 'transaction reference', 'transaction id',
                  'cheque no', 'cheque number', 'document number', 'المرجع',
                  'مرجع', 'رقم العملية', 'رقم المرجع', 'رقم الشيك'),
    'description': ('description', 'details', 'narrative', 'transaction description',
                    'البيان', 'بيان', 'الوصف', 'وصف', 'تفاصيل', 'تفاصيل الحركة'),
    'deposit': ('deposit', 'deposits', 'credit', 'credit amount', 'paid in',
                'دائن', 'إيداع', 'ايداع', 'إيداعات', 'ايداعات', 'الائتمان'),
    'withdrawal': ('withdrawal', 'withdrawals', 'debit', 'debit amount', 'paid out',
                   'مدين', 'سحب', 'مسحوبات'),
    'amount': ('amount', 'signed amount', 'net amount', 'المبلغ', 'مبلغ',
               'قيمة الحركة', 'القيمة', 'قيمة'),
    'balance': ('balance', 'running balance', 'الرصيد', 'رصيد', 'الرصيد الجاري'),
}


def _key(value):
    value = unicodedata.normalize('NFKC', value).casefold()
    value = value.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا').replace('ى', 'ي')
    return ''.join(char for char in value if char.isalnum())


def _text(data):
    if not isinstance(data, dict) or not isinstance(data.get('csv_text'), str):
        raise ValueError('اختر ملف CSV نصيًا وأعد معاينة الاستيراد')
    value = data['csv_text']
    try:
        length = len(value.encode('utf-8'))
    except UnicodeEncodeError:
        raise ValueError('ترميز الملف غير صحيح؛ اختر UTF-8 أو Windows-1256') from None
    if not value.strip('\ufeff \t\r\n'):
        raise ValueError('ملف الكشف فارغ')
    if length > MAX_BYTES:
        raise ValueError('ملف الكشف يتجاوز الحد المسموح: 2 ميجابايت')
    if any(unicodedata.category(char) == 'Cc' and char not in '\t\r\n' for char in value):
        raise ValueError('الملف يحتوي محارف تحكم غير صالحة؛ صدّره كنص CSV')
    return value.lstrip('\ufeff')


def _delimiter(data, value):
    choice = data.get('delimiter', 'auto')
    if not isinstance(choice, str) or choice != 'auto' and choice not in DELIMITERS:
        raise ValueError('فاصل أعمدة CSV غير صحيح')
    if choice != 'auto':
        return choice, DELIMITERS[choice]
    try:
        selected = csv.Sniffer().sniff(value[:65536], delimiters=',;\t').delimiter
        return next((key, char) for key, char in DELIMITERS.items() if char == selected)
    except csv.Error:
        # The header still gives a deterministic choice for ragged files, whose
        # individual row widths are rejected below rather than silently shifted.
        first = value.splitlines()[0] if value.splitlines() else ''
        counts = [(first.count(char), key, char) for key, char in DELIMITERS.items()]
        best = max(counts)
        if best[0] == 0:
            return 'comma', ','
        return best[1], best[2]


def _read(data):
    value = _text(data)
    delimiter_name, delimiter = _delimiter(data, value)
    found, source_rows = [], []
    try:
        reader = csv.reader(io.StringIO(value, newline=''), delimiter=delimiter, strict=True)
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            if len(row) > MAX_COLUMNS:
                raise ValueError('عدد أعمدة الكشف يتجاوز 64 عمودًا')
            if any(len(cell) > 10000 for cell in row):
                raise ValueError('سطر الكشف ' + str(reader.line_num) + ' يحتوي حقلًا نصيًا طويلًا جدًا')
            found.append(row)
            source_rows.append(reader.line_num)
            if len(found) > MAX_ROWS + 1:
                raise ValueError('الكشف يتجاوز 2000 حركة؛ قسّمه إلى فترات أصغر')
    except csv.Error:
        raise ValueError('صيغة CSV غير مكتملة؛ راجع علامات الاقتباس والفاصل المحدد') from None
    if not found or len(found[0]) < 2:
        raise ValueError('يحتاج الكشف صف عناوين وعمودَي التاريخ والمبلغ على الأقل؛ راجع الفاصل')
    if any(len(header.strip()) > 200 for header in found[0]):
        raise ValueError('عنوان العمود أطول من 200 حرف')
    return delimiter_name, found[0], found[1:], source_rows[1:]


def _proposed(headers):
    result = {}
    for field, names in ALIASES.items():
        aliases = {_key(name) for name in names}
        matches = [index for index, header in enumerate(headers) if _key(header) in aliases]
        if len(matches) == 1:
            result[field] = matches[0]
    return result


def inspect(data):
    """Inspect headings and samples only; all data rows remain uncommitted."""
    delimiter, headers, rows, numbers = _read(data)
    ragged = [numbers[index] for index, row in enumerate(rows) if len(row) != len(headers)]
    notes = ['اختيار الأعمدة اقتراح للمراجعة؛ الإيداع موجب والسحب سالب من منظور الفندق.',
             'النصوص والمراجع تحفظ كنص فقط؛ لا تنفذ صيغ الجداول الحسابية.',
             'إذا ظهرت العربية غير مقروءة فاختر ترميز الملف الصحيح قبل الاستيراد.']
    if ragged:
        notes.append('يوجد اختلاف في عدد الأعمدة، بداية من السطر ' + str(ragged[0]) + '؛ راجع الفاصل أو الملف قبل الاستيراد.')
    proposal = _proposed(headers)
    return {'delimiter': delimiter,
            'headers': [{'index': index, 'name': header.strip() or 'عمود ' + str(index + 1)}
                        for index, header in enumerate(headers)],
            'sample_rows': rows[:5], 'row_count': len(rows), 'proposed_mapping': proposal,
            'proposed_amount_mode': 'columns' if 'deposit' in proposal or 'withdrawal' in proposal else 'signed',
            'notes': notes}


def _columns(data, width, mode):
    columns = data.get('columns')
    if not isinstance(columns, dict) or set(columns) - COLUMN_KEYS:
        raise ValueError('راجع ربط أعمدة الكشف')
    result = {}
    for name, index in columns.items():
        if index is None or index == '':
            continue
        if type(index) is not int or not 0 <= index < width:
            raise ValueError('اختيار عمود غير صحيح: ' + name)
        result[name] = index
    if 'date' not in result:
        raise ValueError('حدد عمود تاريخ الحركة')
    if mode == 'signed' and 'amount' not in result:
        raise ValueError('حدد عمود المبلغ الموجب للإيداع والسالب للسحب')
    if mode == 'columns' and not {'deposit', 'withdrawal'} & result.keys():
        raise ValueError('حدد عمود الإيداع أو السحب على الأقل')
    if mode == 'columns' and 'deposit' in result and result.get('withdrawal') == result['deposit']:
        raise ValueError('عمود السحب يختلف عن عمود الإيداع')
    return result


def _date(value, date_format):
    value = value.translate(DIGITS).strip()
    separator = '/' if '/' in date_format else '-'
    parts = value.split(separator)
    if len(parts) != 3 or any(not part.isascii() or not part.isdecimal() for part in parts):
        raise ValueError('التاريخ لا يطابق الصيغة المحددة ' + date_format)
    if date_format == 'YYYY-MM-DD':
        year, month, day = parts
    elif date_format == 'MM/DD/YYYY':
        month, day, year = parts
    else:
        day, month, year = parts
    if len(year) != 4 or not 1 <= len(month) <= 2 or not 1 <= len(day) <= 2:
        raise ValueError('التاريخ لا يطابق الصيغة المحددة ' + date_format)
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        raise ValueError('تاريخ غير صالح في الكشف') from None


def _money(value, decimal_separator, *, blank_zero=False):
    value = value.translate(DIGITS).replace('\u2212', '-').strip()
    if not value:
        if blank_zero:
            return 0
        raise ValueError('المبلغ أو الرصيد فارغ')
    negative = value.startswith('(') and value.endswith(')')
    if negative:
        value = value[1:-1].strip()
        if value.startswith(('+', '-')):
            raise ValueError('علامة المبلغ السالب مكررة')
    elif '(' in value or ')' in value:
        raise ValueError('أقواس المبلغ غير صحيحة')
    sign = -1 if negative else 1
    if value.startswith(('+', '-')):
        if value[0] == '-':
            sign = -1
        value = value[1:]
    decimal = '.' if decimal_separator == 'point' else ','
    grouping = ',' if decimal_separator == 'point' else '.'
    value = value.replace('\u066b', decimal).replace('\u066c', grouping)
    value = value.replace('\u00a0', ' ').replace('\u202f', ' ')
    if value.count(decimal) > 1:
        raise ValueError('فاصل الكسور مكرر؛ راجع اختيار الفاصل العشري')
    parts = value.split(decimal)
    integer = parts[0]
    fraction = parts[1] if len(parts) == 2 else ''
    if len(parts) == 2 and (not fraction or len(fraction) > 2 or not re.fullmatch('[0-9]+', fraction)):
        raise ValueError('المبلغ بحد أقصى منزلتين عشريتين؛ لا يجري تقريب عند الاستيراد')
    if not integer and fraction:
        integer = '0'
    if grouping in integer and ' ' in integer:
        raise ValueError('فواصل الآلاف مختلطة في المبلغ')
    if grouping in integer or ' ' in integer:
        sep = grouping if grouping in integer else ' '
        if not re.fullmatch(r'[0-9]{1,3}(?:' + re.escape(sep) + r'[0-9]{3})+', integer):
            raise ValueError('تجميع الآلاف غير صحيح؛ راجع الفاصل العشري والأرقام')
        integer = integer.replace(sep, '')
    if not re.fullmatch('[0-9]+', integer):
        raise ValueError('المبلغ يحتوي أحرفًا أو رمز عملة أو علامة غير صحيحة')
    # Limit digit processing before int() to avoid expensive huge inputs.
    if len(integer) > 12:
        raise ValueError('المبلغ خارج النطاق المسموح')
    cents = sign * (int(integer) * 100 + int(fraction.ljust(2, '0') or '0'))
    if abs(cents) > MAX_CENTS:
        raise ValueError('المبلغ خارج النطاق المسموح')
    return cents


def parse(data):
    """Validate every CSV movement before returning an all-or-nothing preview."""
    delimiter, headers, source_rows, numbers = _read(data)
    mode = data.get('amount_mode', 'columns')
    if mode not in ('signed', 'columns'):
        raise ValueError('اختر مبلغًا بإشارته أو عمودَي السحب والإيداع')
    date_format = data.get('date_format', 'YYYY-MM-DD')
    if date_format not in FORMATS:
        raise ValueError('صيغة التاريخ المحددة غير مدعومة')
    decimal_separator = data.get('decimal_separator', 'point')
    if decimal_separator not in ('point', 'comma'):
        raise ValueError('اختر الفاصل العشري نقطة أو فاصلة')
    columns = _columns(data, len(headers), mode)
    lines, errors, previous_balance, implied_opening = [], [], None, None
    for row, number in zip(source_rows, numbers):
        try:
            if len(row) != len(headers):
                raise ValueError('عدد الأعمدة لا يطابق صف العناوين؛ راجع الفاصل')
            get = lambda key: row[columns[key]].strip() if key in columns else ''
            movement_date = _date(get('date'), date_format)
            value_date = _date(get('value_date'), date_format) if get('value_date') else movement_date
            reference, description = get('reference'), get('description')
            if len(reference) > 200 or len(description) > 1000:
                raise ValueError('المرجع بحد أقصى 200 حرف والبيان بحد أقصى 1000 حرف')
            if mode == 'signed':
                amount = _money(get('amount'), decimal_separator)
                if amount == 0:
                    raise ValueError('الحركة الصفرية لا تستورد؛ راجع سطور الأرصدة والعناوين داخل الملف')
            else:
                deposit = _money(get('deposit'), decimal_separator, blank_zero=True)
                withdrawal = _money(get('withdrawal'), decimal_separator, blank_zero=True)
                if deposit < 0 or withdrawal < 0:
                    raise ValueError('عمودا السحب والإيداع بقيم موجبة؛ اختر المبلغ بإشارته إذا لزم')
                if (deposit > 0) == (withdrawal > 0):
                    raise ValueError('الحركة تحتاج إيداعًا أو سحبًا واحدًا موجبًا، دون جمعهما في السطر نفسه')
                amount = deposit - withdrawal
            line = {'date': movement_date, 'value_date': value_date, 'reference': reference,
                    'description': description, 'amount_cents': amount}
            if 'balance' in columns:
                balance = _money(get('balance'), decimal_separator)
                if previous_balance is not None and previous_balance + amount != balance:
                    raise ValueError('الرصيد الجاري لا يساوي رصيد السطر السابق مع الحركة؛ راجع اتجاه المبالغ وترتيب السطور')
                if previous_balance is None:
                    implied_opening = balance - amount
                    if abs(implied_opening) > MAX_CENTS:
                        raise ValueError('رصيد بداية الكشف خارج النطاق المسموح')
                previous_balance = balance
                line['balance_cents'] = balance
            lines.append(line)
        except ValueError as error:
            errors.append('السطر ' + str(number) + ': ' + str(error))
            if len(errors) == 10:
                break
    if errors:
        raise ValueError('لم يستورد أي سطر. ' + ' | '.join(errors))
    deposit = sum(max(line['amount_cents'], 0) for line in lines)
    withdrawal = sum(max(-line['amount_cents'], 0) for line in lines)
    if max(deposit, withdrawal) > MAX_CENTS:
        raise ValueError('إجمالي الكشف يتجاوز النطاق المسموح؛ قسّمه إلى فترات أصغر')
    result = {'lines': lines, 'count': len(lines), 'delimiter': delimiter,
              'totals': {'deposit_cents': deposit, 'withdrawal_cents': withdrawal, 'net_cents': deposit - withdrawal},
              'warnings': ['هذه معاينة استيراد فقط؛ لا تحفظ كشفًا أو تطابق حركة أو تنشئ قيدًا.',
                           'راجع الفترة ورصيدي بداية ونهاية كشف البنك قبل حفظ التسوية.']}
    if 'balance' in columns and lines:
        result['balance_check'] = {'implied_opening_cents': implied_opening,
                                   'closing_cents': previous_balance, 'consistent': True}
    return result
