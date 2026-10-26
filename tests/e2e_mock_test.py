"""End-to-end test for the invoice capture to QuickBooks workflow against MOCK APIs.

No real Gmail, QuickBooks, Google Sheets, Slack or Claude account is used. The script:
  1. starts strict, stateful mocks of the Gmail, QuickBooks Online, Google Sheets, Slack and Anthropic APIs on this
     machine, and fills the mock mailbox with fictional supplier emails (the PDFs are in tests/fixtures),
  2. creates a temporary n8n data table for the invoice ledger, imports and activates a temporary copy of
     workflow/error-alert-slack.json (n8n only runs an active error workflow), then a temporary copy of the invoice
     workflow that reports to it. In the copy, API hosts point at the mocks, settings are test values, and the n8n
     OAuth credentials are replaced by fixed bearer tokens (the mocks cannot run Google's or Intuit's OAuth flow),
  3. runs intake (Run Now) several times, opens the approval links from the Slack messages like a person would
     (GET shows the confirmation page, POST is the button on it), tries forged, expired, reused and racing links,
     and plays API failures: QuickBooks 429, 401 and lost answers, Claude 529, Gmail and Slack errors,
  4. checks every request the workflow made, the ledger rows, the Gmail labels, the QuickBooks bills and
     attachments, the Sheets rows, the Slack messages, the node each run ended on, and that every failed run raised
     exactly one error alert linked to it,
  5. imports a copy with broken settings (Run Now must answer 500 and name the problems) and a copy with the
     schedule switched on (it must pick up a new email by itself),
  6. prints PASS or FAIL per check and deletes every temporary workflow and the temporary data table, also after a
     crash.
Temporary workflows are named "TEST invoice-capture-to-quickbooks (temporary)" and the data table
"TEST invoice-capture-to-quickbooks ledger (temporary)". Leftovers with exactly those names from an interrupted run are
deleted at the start. Nothing else in n8n is touched.

Requirements: Python 3.9+, a running n8n 2.x (with data tables) and the public API enabled.
Environment:
  N8N_URL      default http://localhost:5678
  N8N_API_KEY  your n8n API key (Settings > n8n API)
  MOCK_HOST    how n8n reaches this machine; default host.docker.internal (n8n in Docker).
               Use 127.0.0.1 if n8n runs directly on this machine.
  VERBOSE      optional; 1 prints each check as soon as it is made
  SAVE_REQUESTS  optional file path; every request the workflow made to the mocks is saved there as JSON
                 (headers are left out, PDF data is shortened)
Usage: python tests/e2e_mock_test.py
"""
import base64, copy, hashlib, hmac, json, os, re, sys, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

N8N = os.environ.get('N8N_URL', 'http://localhost:5678').rstrip('/')
KEY = os.environ.get('N8N_API_KEY', '')
MOCK_PORT = 8801
MOCK = f"http://{os.environ.get('MOCK_HOST', 'host.docker.internal')}:{MOCK_PORT}"
ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / 'workflow' / 'invoice-capture-to-quickbooks.json'
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
SLUG = 'invoice-capture-to-quickbooks'
TEMP_NAME = f'TEST {SLUG} (temporary)'
TABLE_NAME = f'TEST {SLUG} ledger (temporary)'

REALM = '9130350000000001'
TOKENS = {'gmailOAuth2': 'mock-gmail-token', 'googleSheetsOAuth2Api': 'mock-sheets-token', 'quickBooksOAuth2Api': 'mock-qbo-token'}
APPROVAL_SECRET = 'test-approval-secret-7f3a91c2d4e5b6a7c8d9e0f1'
ENV = {
    'QBO_BASE_URL': MOCK + '/qbo', 'QBO_APP_URL': 'https://app.sandbox.qbo.intuit.com', 'QBO_REALM_ID': REALM,
    'QBO_EXPENSE_ACCOUNT_ID': '7', 'QBO_TAX_ACCOUNT_ID': '31', 'HOME_CURRENCY': 'USD',
    'VENDOR_ALIASES': json.dumps({'Bluepeak Electrical': 'Bluepeak Electrical Services'}),
    'ANTHROPIC_API_KEY': 'mock-anthropic-key', 'ANTHROPIC_MODEL': 'claude-sonnet-5', 'BUSINESS_NAME': 'Maple Street Bakery',
    'APPROVAL_SECRET': APPROVAL_SECRET, 'APPROVAL_BASE_URL': f'{N8N}/webhook/{SLUG}-e2e-approval', 'APPROVAL_LINK_DAYS': '7',
    'SLACK_WEBHOOK_URL': MOCK + '/slack/hook', 'LEDGER_TABLE_ID': '', 'GOOGLE_SHEET_ID': 'sheet-mock-1', 'GOOGLE_SHEET_TAB': 'Invoice log',
    'RUN_NOW_SECRET': 'test-run-secret', 'SCHEDULE_ENABLED': 'false', 'MAX_MESSAGES_PER_RUN': '20', 'MAX_PDF_MB': '10',
    'MAX_EXTRACT_ATTEMPTS': '2', 'API_MAX_ATTEMPTS': '3', 'RETRY_BASE_SECONDS': '1', 'QBO_RATE_LIMIT_WAIT_SECONDS': '1',
    'DUE_DATE_MAX_DAYS': '120', 'INVOICE_MAX_AGE_DAYS': '3650', 'BUSINESS_TZ': 'America/Chicago',
}
HOSTS = {'https://gmail.googleapis.com': MOCK, 'https://sheets.googleapis.com': MOCK, 'https://api.anthropic.com': MOCK}
LEDGER_COLUMNS = [('key', 'string'), ('messageId', 'string'), ('status', 'string'), ('vendor', 'string'), ('invoiceNumber', 'string'),
                  ('total', 'number'), ('currency', 'string'), ('attempts', 'number'), ('billId', 'string'), ('decidedBy', 'string'),
                  ('note', 'string'), ('data', 'string'), ('notifiedAt', 'string')]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def b64url(data):
    return base64.urlsafe_b64encode(data).decode()


# ----------------------------------------------------------------------------------------------------------------------
# Test data (all made up: example.com addresses, fictional companies). What the mock Claude "reads" from each PDF.
# ----------------------------------------------------------------------------------------------------------------------
def inv(vendor, number, date, due, lines, tax, total, cur='USD', subtotal=None, problem='', confidence='high'):
    return {'readable': True, 'document_type': 'invoice', 'multiple_invoices': False, 'vendor_name': vendor, 'invoice_number': number,
            'invoice_date': date, 'due_date': due, 'currency': cur, 'line_items': [{'description': d, 'amount': a} for d, a in lines],
            'subtotal': round(sum(a for _, a in lines), 2) if subtotal is None else subtotal, 'tax': tax, 'total': total,
            'confidence': confidence, 'problem': problem}


READ = {
    'northwind-20417': inv('Northwind Office Supply', 'INV-20417', '2026-10-20', '2026-11-19',
                           [('Copy paper, letter, 40 cases', 1000.00), ('Toner cartridges, black, 2 pack', 190.00)], 94.50, 1284.50),
    'northwind-20388': inv('Northwind Office Supply', 'INV-20388', '2026-10-02', '2026-11-01', [('Printer paper, 10 cases', 250.00)], 19.85, 269.85),
    'bluepeak-7781': inv('Bluepeak Electrical', 'BP-7781', '2026-10-18', '2026-11-17',
                         [('Labor, panel upgrade (8 hours)', 600.00), ('Materials (wire, breakers)', 150.00)], 60.00, 820.00,
                         problem='The total does not equal the lines plus tax <!channel>'),
    'cedar-1042': inv('Cedar Ridge Landscaping', 'CRL-1042', '2026-10-15', '2026-11-14', [('October lawn care, 4 visits', 340.00)], 0, 340.00),
    'metro-5521': inv('Metro Courier Services', 'MCS-5521', '2026-10-19', '2026-11-02', [('Same day deliveries (14)', 210.00), ('Fuel surcharge', 18.90)], 0, 228.90),
    'summit-3310': inv('Summit Coffee Roasters', 'SCR-3310', '2026-10-21', '2026-11-20', [('Whole bean coffee, house blend, 20 lb', 260.00)], 0, 260.00, cur=''),
    'pinecrest-88213': inv('Pinecrest Utilities', 'PU-88213', '2026-10-12', '2026-11-05', [('Electricity, Sep 10 to Oct 10', 412.37)], 0, 412.37),
    'lakeside-4410': inv('Lakeside Printing', 'LP-4410', '2026-10-16', '2026-11-15', [('Menus, 500 copies', 180.00)], 9.90, 189.90),
    'oakline-207': inv('Oakline Janitorial', 'OJ-207', '2026-10-17', '2026-11-16', [('Nightly cleaning, October', 950.00)], 0, 950.00),
    'granite-221': inv('Granite Office Furniture', 'GOF-221', '2026-10-14', '2026-11-13', [('Office chairs, ergonomic (4)', 1196.00)], 122.59, 1318.59),
    'granite-222': inv('Granite Office Furniture', 'GOF-222', '2026-10-14', '2026-11-13', [('Standing desk, 60 inch', 649.00)], 66.52, 715.52),
    'atlas-9001': inv('Atlas Freight Partners', 'AF-9001', '2026-10-13', '2026-11-12', [('Sea freight, container MSKU 000000-1', 1450.00)], 0, 1450.00, cur='EUR'),
    'ridgeway-310': inv('Ridgeway Pest Control', 'RPC-310', '2026-10-20', '2026-11-19', [('Quarterly pest treatment', 145.00)], 0, 145.00),
    'evergreen-4471': inv('Evergreen Water Co', 'EW-4471', '2026-10-22', '2026-11-21',
                          [('Water cooler rental, October', 39.00), ('Bottled water, 12 x 5 gal', 84.00)], 0, 123.00),
    'brightpath-118': inv('Bright Path Cleaning', 'BPC-118', '2026-10-23', '2026-11-22', [('Window cleaning, storefront', 120.00)], 0, 120.00),
    'fjord-5120': inv('Fjord Coffee Imports', 'FCI-5120', '2026-10-22', '2026-11-21',
                      [('Green coffee, single origin, 60 kg', 840.00), ('Freight to Springfield', 95.00)], 0, 935.00, cur='EUR'),
    'harbor-7702': inv('Harbor Linen Service', 'HLS-7702', '2026-10-23', '2026-11-22', [('Apron and towel service, October', 186.00)], 0, 186.00),
    'blurry-scan': {'readable': False, 'document_type': 'other', 'multiple_invoices': False, 'vendor_name': '', 'invoice_number': '',
                    'invoice_date': '', 'due_date': '', 'currency': '', 'line_items': [], 'subtotal': None, 'tax': None, 'total': None,
                    'confidence': 'low', 'problem': 'The scan is too blurry to read any text or amounts'},
}
CLAUDE_BEHAVIOR = {'lakeside-4410': 529, 'oakline-207': 'text-only'}

# ----------------------------------------------------------------------------------------------------------------------
# Mock state
# ----------------------------------------------------------------------------------------------------------------------
LOCK = threading.RLock()
LOG = []            # dict(service, method, path, query, headers, body, status, response, t)
COUNTER = {'n': 0}
FAIL = {'qbo_401': False, 'vendor_429_once': set(), 'create_429_once': set(), 'create_lost': set(), 'upload_fail': set(),
        'modify_fail': {}, 'slack_fail': set(), 'gmail_get_fail': set(),
        'during_vendor_query': {}}     # vendor name (lower case) -> function run once when that vendor is queried (a race)
LOCK_WATCH = ('northwind-20417', 'atlas-9001')
LOCK_SNAPSHOTS = {}  # fixture -> the run lock's expiry when Claude was asked to read that PDF


def next_id(prefix=''):
    COUNTER['n'] += 1
    return f'{prefix}{COUNTER["n"]}'


# Gmail
LABELS = [
    {'id': 'INBOX', 'name': 'INBOX', 'type': 'system', 'messageListVisibility': 'hide', 'labelListVisibility': 'labelShow'},
    {'id': 'UNREAD', 'name': 'UNREAD', 'type': 'system'},
    {'id': 'Label_101', 'name': 'Invoices/To Process', 'type': 'user', 'messageListVisibility': 'show', 'labelListVisibility': 'labelShow'},
    {'id': 'Label_102', 'name': 'Invoices/Awaiting Approval', 'type': 'user', 'messageListVisibility': 'show', 'labelListVisibility': 'labelShow'},
    {'id': 'Label_103', 'name': 'Invoices/Processed', 'type': 'user', 'messageListVisibility': 'show', 'labelListVisibility': 'labelShow'},
    {'id': 'Label_104', 'name': 'Invoices/Rejected', 'type': 'user', 'messageListVisibility': 'show', 'labelListVisibility': 'labelShow'},
    {'id': 'Label_105', 'name': 'Invoices/Needs Review', 'type': 'user', 'messageListVisibility': 'show', 'labelListVisibility': 'labelShow'},
    {'id': 'Label_200', 'name': 'Receipts', 'type': 'user', 'messageListVisibility': 'show', 'labelListVisibility': 'labelShow'},
]
L_TODO, L_WAIT, L_DONE, L_REJ, L_REVIEW = 'Label_101', 'Label_102', 'Label_103', 'Label_104', 'Label_105'
LABEL_IDS = {l['id'] for l in LABELS}
MESSAGES = {}       # id -> dict(id, threadId, labels:set, internalDate, headers, text, pdfs:[(filename, bytes, mime)])
ATTACHMENTS = {}    # attachmentId -> (messageId, pdf index)


def add_message(mid, sender, subject, pdf_files, minutes_ago, text='Please find our invoice attached.', mime='application/pdf'):
    """pdf_files: fixture names, or (fixture name, filename shown in the email) where '' is a PDF part without a filename."""
    pdfs = [(f if isinstance(f, str) else f[1], (FIXTURES / (f if isinstance(f, str) else f[0])).read_bytes(), mime) for f in pdf_files]
    MESSAGES[mid] = {'id': mid, 'threadId': 't' + mid[1:], 'labels': {'INBOX', L_TODO}, 'pdfs': pdfs, 'text': text,
                     'internalDate': str(int((datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).timestamp() * 1000)),
                     'headers': {'From': sender, 'To': 'ap@maplestreetbakery.example.com', 'Subject': subject,
                                 'Date': (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime('%a, %d %b %Y %H:%M:%S +0000')}}
    return mid


def message_resource(m, fmt):
    res = {'id': m['id'], 'threadId': m['threadId'], 'labelIds': sorted(m['labels']), 'snippet': m['text'][:100],
           'historyId': next_id(), 'internalDate': m['internalDate'], 'sizeEstimate': 2048 + sum(len(p[1]) for p in m['pdfs'])}
    if fmt == 'minimal':
        return res
    parts = [{'partId': '0', 'mimeType': 'text/plain', 'filename': '', 'headers': [{'name': 'Content-Type', 'value': 'text/plain; charset="UTF-8"'}],
              'body': {'size': len(m['text']), 'data': b64url(m['text'].encode())}}]
    for i, (name, data, mime) in enumerate(m['pdfs']):
        aid = 'ANGjdJ' + uuid.uuid4().hex + uuid.uuid4().hex      # Gmail issues a new attachment ID on every read
        ATTACHMENTS[aid] = (m['id'], i)
        headers = ([{'name': 'Content-Type', 'value': f'{mime}; name="{name}"'}, {'name': 'Content-Disposition', 'value': f'attachment; filename="{name}"'}] if name
                   else [{'name': 'Content-Type', 'value': mime}, {'name': 'Content-Disposition', 'value': 'attachment'}])   # no filename at all
        parts.append({'partId': str(i + 1), 'mimeType': mime, 'filename': name, 'headers': headers, 'body': {'attachmentId': aid, 'size': len(data)}})
    res['payload'] = {'partId': '', 'mimeType': 'multipart/mixed', 'filename': '',
                      'headers': [{'name': k, 'value': v} for k, v in m['headers'].items()], 'body': {'size': 0}, 'parts': parts}
    return res


def gerr(code, message, reason, status, domain='global'):
    return code, {'error': {'code': code, 'message': message, 'errors': [{'message': message, 'domain': domain, 'reason': reason}], 'status': status}}


def gmail(method, path, query, headers, body):
    """users.labels.list, users.messages.list/get/modify and users.messages.attachments.get, as documented."""
    if headers.get('authorization') != 'Bearer ' + TOKENS['gmailOAuth2']:
        return gerr(401, 'Invalid Credentials', 'authError', 'UNAUTHENTICATED')
    q = {k: v for k, v in query.items()}
    if method == 'GET' and path == '/gmail/v1/users/me/labels':
        return 200, {'labels': copy.deepcopy(LABELS)}
    if method == 'GET' and path == '/gmail/v1/users/me/messages':
        unknown = set(q) - {'labelIds', 'maxResults', 'pageToken', 'q', 'includeSpamTrash'}
        if unknown:
            return gerr(400, f'Invalid parameter: {sorted(unknown)[0]}', 'badRequest', 'INVALID_ARGUMENT')
        size = int(q.get('maxResults', ['100'])[0])
        if not 1 <= size <= 500:
            return gerr(400, 'Invalid maxResults', 'invalid', 'INVALID_ARGUMENT')
        want = set(q.get('labelIds', []))
        if want - LABEL_IDS:
            return gerr(400, 'Invalid label: ' + sorted(want - LABEL_IDS)[0], 'invalidArgument', 'INVALID_ARGUMENT')
        rows = sorted((m for m in MESSAGES.values() if want <= m['labels']), key=lambda m: -int(m['internalDate']))
        out = {'resultSizeEstimate': len(rows)}
        if rows:
            out['messages'] = [{'id': m['id'], 'threadId': m['threadId']} for m in rows[:size]]
        return 200, out
    m = re.fullmatch(r'/gmail/v1/users/me/messages/([^/]+)', path)
    if method == 'GET' and m:
        msg = MESSAGES.get(m.group(1))
        if not msg:
            return gerr(404, 'Requested entity was not found.', 'notFound', 'NOT_FOUND')
        if m.group(1) in FAIL['gmail_get_fail']:
            return gerr(500, 'Backend Error', 'backendError', 'INTERNAL')
        fmt = q.get('format', ['full'])[0].lower()
        if fmt not in ('minimal', 'full', 'raw', 'metadata') or set(q) - {'format', 'metadataHeaders'}:
            return gerr(400, 'Invalid value at format', 'invalid', 'INVALID_ARGUMENT')
        return 200, message_resource(msg, fmt)
    m = re.fullmatch(r'/gmail/v1/users/me/messages/([^/]+)/attachments/([^/]+)', path)
    if method == 'GET' and m:
        hit = ATTACHMENTS.get(m.group(2))
        if not hit or hit[0] != m.group(1):
            return gerr(400, 'Invalid attachment token', 'invalidArgument', 'INVALID_ARGUMENT')
        data = MESSAGES[hit[0]]['pdfs'][hit[1]][1]
        return 200, {'size': len(data), 'data': b64url(data)}
    m = re.fullmatch(r'/gmail/v1/users/me/messages/([^/]+)/modify', path)
    if method == 'POST' and m:
        msg = MESSAGES.get(m.group(1))
        if not msg:
            return gerr(404, 'Requested entity was not found.', 'notFound', 'NOT_FOUND')
        if not str(headers.get('content-type', '')).startswith('application/json') or not isinstance(body, dict):
            return gerr(400, 'Invalid JSON payload received.', 'badRequest', 'INVALID_ARGUMENT')
        extra = set(body) - {'addLabelIds', 'removeLabelIds', 'addClassificationLabels', 'removeClassificationLabelIds'}
        if extra:
            return gerr(400, f'Invalid JSON payload received. Unknown name "{sorted(extra)[0]}": Cannot find field.', 'badRequest', 'INVALID_ARGUMENT')
        add, rem = body.get('addLabelIds', []), body.get('removeLabelIds', [])
        if not isinstance(add, list) or not isinstance(rem, list) or len(add) > 100 or len(rem) > 100:
            return gerr(400, 'Invalid label lists', 'invalidArgument', 'INVALID_ARGUMENT')
        bad = [x for x in add + rem if x not in LABEL_IDS]
        if bad:
            return gerr(400, f'Invalid label: {bad[0]}', 'invalidArgument', 'INVALID_ARGUMENT')
        left = FAIL['modify_fail'].get(msg['id'], 0)
        if left:
            FAIL['modify_fail'][msg['id']] = left - 1
            return gerr(500, 'Backend Error', 'backendError', 'INTERNAL')
        msg['labels'] = (msg['labels'] - set(rem)) | set(add)
        return 200, {'id': msg['id'], 'threadId': msg['threadId'], 'labelIds': sorted(msg['labels'])}
    return gerr(404, f'Not found: {method} {path}', 'notFound', 'NOT_FOUND')


# Google Sheets
SHEET_ID, SHEET_TAB = 'sheet-mock-1', 'Invoice log'
SHEET_ROWS = [['Logged at', 'Event', 'Status', 'Vendor', 'Invoice number', 'Invoice date', 'Due date', 'Currency', 'Total', 'Problems',
               'QuickBooks bill', 'Decided by', 'Note', 'From', 'Subject', 'Ledger key']]


def sheets(method, raw_path, query, headers, body):
    """spreadsheets.values.append: POST /v4/spreadsheets/{id}/values/{range}:append"""
    if headers.get('authorization') != 'Bearer ' + TOKENS['googleSheetsOAuth2Api']:
        return 401, {'error': {'code': 401, 'message': 'Request had invalid authentication credentials.', 'status': 'UNAUTHENTICATED'}}
    m = re.fullmatch(r'/v4/spreadsheets/([^/]+)/values/(.+):append', raw_path)
    if method != 'POST' or not m:
        return 404, {'error': {'code': 404, 'message': 'Not found', 'status': 'NOT_FOUND'}}
    if m.group(1) != SHEET_ID:
        return 404, {'error': {'code': 404, 'message': 'Requested entity was not found.', 'status': 'NOT_FOUND'}}
    rng = urllib.parse.unquote(m.group(2))
    if rng != f"'{SHEET_TAB}'!A:P":
        return 400, {'error': {'code': 400, 'message': f'Unable to parse range: {rng}', 'status': 'INVALID_ARGUMENT'}}
    q = {k: v[0] for k, v in query.items()}
    if q.get('valueInputOption') not in ('RAW', 'USER_ENTERED'):
        return 400, {'error': {'code': 400, 'message': "'valueInputOption' is required but not specified", 'status': 'INVALID_ARGUMENT'}}
    if q.get('insertDataOption', 'OVERWRITE') not in ('OVERWRITE', 'INSERT_ROWS') or set(q) - {'valueInputOption', 'insertDataOption', 'includeValuesInResponse', 'responseValueRenderOption', 'responseDateTimeRenderOption'}:
        return 400, {'error': {'code': 400, 'message': 'Invalid query parameter', 'status': 'INVALID_ARGUMENT'}}
    if not isinstance(body, dict) or set(body) - {'range', 'majorDimension', 'values'}:
        extra = sorted(set(body or {}) - {'range', 'majorDimension', 'values'})
        return 400, {'error': {'code': 400, 'message': f'Invalid JSON payload received. Unknown name "{extra[0] if extra else "?"}"', 'status': 'INVALID_ARGUMENT'}}
    values = body.get('values')
    if body.get('majorDimension', 'ROWS') != 'ROWS' or not isinstance(values, list) or not values or not all(
            isinstance(r, list) and len(r) <= 16 and all(isinstance(c, (str, int, float, bool)) for c in r) for r in values):
        return 400, {'error': {'code': 400, 'message': 'Invalid values: rows of up to 16 plain values are expected', 'status': 'INVALID_ARGUMENT'}}
    start = len(SHEET_ROWS) + 1
    SHEET_ROWS.extend(copy.deepcopy(values))
    end = len(SHEET_ROWS)
    cols = max(len(r) for r in values)
    return 200, {'spreadsheetId': SHEET_ID, 'tableRange': f"'{SHEET_TAB}'!A1:P{start - 1}",
                 'updates': {'spreadsheetId': SHEET_ID, 'updatedRange': f"'{SHEET_TAB}'!A{start}:{chr(64 + cols)}{end}",
                             'updatedRows': len(values), 'updatedColumns': cols, 'updatedCells': sum(len(r) for r in values)}}


# QuickBooks Online
ACCOUNTS = {'7': 'Office Expenses', '31': 'Sales Tax Expense'}
# The mock company has multicurrency switched on (Preferences.CurrencyPrefs.MultiCurrencyEnabled). Intuit's Bill reference:
# CurrencyRef is "Required if multicurrency is enabled for the company".
PREFS = {'MultiCurrencyEnabled': True, 'HomeCurrency': 'USD'}
VENDORS = {}
BILLS = {}
ATTACHABLES = []
REQUEST_IDS = {}    # (endpoint, requestid) -> (status, body)


def add_vendor(vid, name, currency='USD', active=True):
    VENDORS[vid] = {'Id': vid, 'SyncToken': '0', 'DisplayName': name, 'CompanyName': name, 'PrintOnCheckName': name, 'Active': active,
                    'Balance': 0, 'Vendor1099': False, 'CurrencyRef': {'value': currency, 'name': {'USD': 'United States Dollar', 'EUR': 'Euro'}.get(currency, currency)},
                    'domain': 'QBO', 'sparse': False, 'MetaData': {'CreateTime': '2025-01-10T09:00:00-08:00', 'LastUpdatedTime': '2025-01-10T09:00:00-08:00'}}


def add_bill(vendor_id, doc, txn, total, note='', lines=None):
    bid = str(100 + len(BILLS) + 30)
    BILLS[bid] = {'Id': bid, 'SyncToken': '0', 'DocNumber': doc, 'TxnDate': txn, 'TotalAmt': total, 'Balance': total,
                  'VendorRef': {'value': vendor_id, 'name': VENDORS[vendor_id]['DisplayName']}, 'APAccountRef': {'value': '33', 'name': 'Accounts Payable (A/P)'},
                  'CurrencyRef': copy.deepcopy(VENDORS[vendor_id]['CurrencyRef']), 'PrivateNote': note, 'Line': lines or [],
                  'domain': 'QBO', 'sparse': False, 'MetaData': {'CreateTime': now_iso(), 'LastUpdatedTime': now_iso()}}
    return bid


def fault(status, code, message, detail, ftype='ValidationFault', element=''):
    return status, {'Fault': {'Error': [{'Message': message, 'Detail': detail, 'code': str(code), 'element': element}], 'type': ftype}, 'time': now_iso()}


# Property lists from Intuit's minor version 75 XSD (Finance.xsd): Bill (with Transaction and PurchaseByVendor), Line,
# AccountBasedExpenseLineDetail, Attachable and AttachableRef. Anything else gets error 2010 like the real API.
BILL_KEYS = {'Id', 'SyncToken', 'MetaData', 'CustomField', 'AttachableRef', 'domain', 'status', 'sparse', 'DocNumber', 'TxnDate', 'DepartmentRef',
             'CurrencyRef', 'ExchangeRate', 'PrivateNote', 'TxnStatus', 'LinkedTxn', 'Line', 'TxnTaxDetail', 'TxnSource', 'TaxFormType', 'TaxFormNum',
             'TransactionLocationType', 'Tag', 'TxnApprovalInfo', 'RecurDataRef', 'RecurringInfo', 'ProjectRef', 'TotalCostAmount', 'HomeTotalCostAmount',
             'VendorRef', 'APAccountRef', 'TotalAmt', 'BillEmail', 'ReplyEmail', 'Memo', 'GlobalTaxCalculation', 'PayerRef', 'SalesTermRef', 'DueDate',
             'RemitToAddr', 'ShipAddr', 'VendorAddr', 'Balance', 'HomeBalance', 'BillEx', 'LessCIS', 'IncludeInAnnualTPAR'}
LINE_KEYS = {'Id', 'LineNum', 'Description', 'Amount', 'Received', 'LinkedTxn', 'DetailType', 'PaymentLineDetail', 'DiscountLineDetail', 'TaxLineDetail',
             'SalesItemLineDetail', 'DescriptionLineDetail', 'ItemBasedExpenseLineDetail', 'AccountBasedExpenseLineDetail', 'ReimburseLineDetail',
             'DepositLineDetail', 'PurchaseOrderItemLineDetail', 'SalesOrderItemLineDetail', 'ItemReceiptLineDetail', 'JournalEntryLineDetail',
             'GroupLineDetail', 'SubTotalLineDetail', 'TDSLineDetail', 'ItemAdjustmentLineDetail', 'CustomField', 'LineEx', 'ProjectRef', 'CostAmount', 'HomeCostAmount'}
ABE_KEYS = {'CustomerRef', 'ClassRef', 'AccountRef', 'BillableStatus', 'MarkupInfo', 'TaxAmount', 'TaxCodeRef', 'TaxInclusiveAmt', 'ExpenseDetailLineDetailEx'}
ATTACHABLE_KEYS = {'Id', 'SyncToken', 'MetaData', 'CustomField', 'AttachableRef', 'domain', 'status', 'sparse', 'FileName', 'FileAccessUri', 'TempDownloadUri',
                   'Size', 'ContentType', 'Category', 'Lat', 'Long', 'PlaceName', 'Note', 'Tag', 'ThumbnailFileAccessUri', 'ThumbnailTempDownloadUri',
                   'AttachableEx', 'documentId'}
ATTACHABLE_REF_KEYS = {'EntityRef', 'LineInfo', 'IncludeOnSend', 'CustomField', 'AttachableRefEx'}
REF_KEYS = {'value', 'name', 'type'}
QUERY_RE = re.compile(r"^select \* from (Vendor|Bill) where (DisplayName|Id|DocNumber) = '((?:[^'\\]|\\.)*)'$", re.I)


def is_date(s):
    try:
        return isinstance(s, str) and datetime.strptime(s, '%Y-%m-%d').strftime('%Y-%m-%d') == s
    except ValueError:
        return False


def money_ok(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and round(v, 2) == v


def unknown(obj, allowed, where):
    extra = sorted(set(obj) - allowed)
    return fault(400, 2010, 'Request has invalid or unsupported property', f'Property Name:{extra[0]} specified is unsupported or invalid', element=f'{where}.{extra[0]}') if extra else None


def bad_ref(ref, where):
    if not isinstance(ref, dict) or set(ref) - REF_KEYS or not isinstance(ref.get('value'), str) or not ref['value'].strip():
        return fault(400, 2020, 'Required param missing, need to supply the required value for the API', f'Required parameter {where}.value is missing in the request')
    return None


def qbo_bill(body, requestid):
    for check in (unknown(body, BILL_KEYS, 'Bill'), bad_ref(body.get('VendorRef'), 'VendorRef')):
        if check:
            return check
    vendor = VENDORS.get(body['VendorRef']['value'])
    if not vendor or not vendor['Active']:
        return fault(400, 2500, 'Invalid Reference Id', 'Invalid Reference Id : Vendor assigned to this transaction has been deleted or made inactive.', element='VendorRef')
    if 'CurrencyRef' not in body:
        if PREFS['MultiCurrencyEnabled']:
            return fault(400, 2020, 'Required param missing, need to supply the required value for the API', 'Required parameter CurrencyRef is missing in the request', element='CurrencyRef')
    else:
        check = bad_ref(body['CurrencyRef'], 'CurrencyRef')
        if check:
            return check
        cur, vcur = body['CurrencyRef']['value'], vendor['CurrencyRef']['value']
        if cur != vcur or (not PREFS['MultiCurrencyEnabled'] and cur != PREFS['HomeCurrency']):
            return fault(400, 6000, 'A business validation error has occurred while processing your request',
                         f'Business Validation Error: The transaction currency {cur} does not match the currency {vcur} of the vendor.', element='CurrencyRef')
    lines = body.get('Line')
    if not isinstance(lines, list) or not lines:
        return fault(400, 2020, 'Required param missing, need to supply the required value for the API', 'Required parameter Line is missing in the request')
    total = 0
    for n, line in enumerate(lines):
        if not isinstance(line, dict):
            return fault(400, 2010, 'Request has invalid or unsupported property', f'Line.{n}')
        check = unknown(line, LINE_KEYS, f'Line.{n}')
        if check:
            return check
        if line.get('DetailType') not in ('AccountBasedExpenseLineDetail', 'ItemBasedExpenseLineDetail'):
            return fault(400, 2170, 'Invalid Enumeration', f'Invalid Enumeration : {line.get("DetailType")}', element='DetailType')
        detail = line.get(line['DetailType'])
        if line['DetailType'] != 'AccountBasedExpenseLineDetail' or not isinstance(detail, dict):
            return fault(400, 2020, 'Required param missing, need to supply the required value for the API', f'Required parameter Line.{n}.AccountBasedExpenseLineDetail is missing')
        check = unknown(detail, ABE_KEYS, f'Line.{n}.AccountBasedExpenseLineDetail') or bad_ref(detail.get('AccountRef'), 'AccountRef')
        if check:
            return check
        if detail['AccountRef']['value'] not in ACCOUNTS:
            return fault(400, 2500, 'Invalid Reference Id', 'Invalid Reference Id : Accounts element id is not valid', element='AccountRef')
        if not money_ok(line.get('Amount')):
            return fault(400, 2100, 'Invalid Decimal Format', f'Line.{n}.Amount must be a decimal with at most 2 places')
        if line['Amount'] < 0:
            return fault(400, 2290, 'NegativeAmount', 'Negative amounts are not supported on transaction lines.')
        if not isinstance(line.get('Description', ''), str) or len(line.get('Description', '')) > 4000:
            return fault(400, 2050, 'Invalid String Length', 'Description: Max length 4000')
        total += round(line['Amount'] * 100)
    doc = body.get('DocNumber', '')
    if not isinstance(doc, str) or len(doc) > 21:
        return fault(400, 2050, 'Invalid String Length', f'String length is either shorter or longer than supported by specification. Min:0 Max:21 supplied:{len(str(doc))}', element='DocNumber')
    for f in ('TxnDate', 'DueDate'):
        if f in body and not is_date(body[f]):
            return fault(400, 2060, 'Invalid Date Format', f'{f}: {body[f]}', element=f)
    if not isinstance(body.get('PrivateNote', ''), str) or len(body.get('PrivateNote', '')) > 4000:
        return fault(400, 2050, 'Invalid String Length', 'PrivateNote: Max length 4000')
    bid = add_bill(vendor['Id'], doc, body.get('TxnDate', now_iso()[:10]), total / 100, body.get('PrivateNote', ''),
                   [dict(l, Id=str(i + 1), LineNum=i + 1) for i, l in enumerate(lines)])
    b = BILLS[bid]
    if 'DueDate' in body:
        b['DueDate'] = body['DueDate']
    b['requestid'] = requestid
    return 200, {'Bill': {k: v for k, v in copy.deepcopy(b).items() if k != 'requestid'}, 'time': now_iso()}


def parse_multipart(ctype, raw):
    m = re.search(r'boundary=("?)([^";]+)\1', ctype or '')
    if not ctype.startswith('multipart/form-data') or not m:
        return None, 'Content-Type must be multipart/form-data with a boundary'
    boundary = ('--' + m.group(2)).encode()
    chunks = raw.split(boundary)
    if len(chunks) < 3 or not chunks[-1].startswith(b'--'):
        return None, 'multipart body is not closed with the boundary'
    parts = []
    for chunk in chunks[1:-1]:
        chunk = chunk[2:] if chunk.startswith(b'\r\n') else chunk
        if b'\r\n\r\n' not in chunk:
            return None, 'part without headers'
        head, data = chunk.split(b'\r\n\r\n', 1)
        if data.endswith(b'\r\n'):
            data = data[:-2]
        hdrs = {}
        for line in head.decode('utf-8', 'replace').split('\r\n'):
            k, _, v = line.partition(':')
            hdrs[k.strip().lower()] = v.strip()
        disp = hdrs.get('content-disposition', '')
        name = re.search(r'\bname="([^"]*)"', disp)
        fname = re.search(r'filename="([^"]*)"', disp)
        parts.append({'name': name.group(1) if name else '', 'filename': fname.group(1) if fname else '', 'headers': hdrs, 'data': data})
    return parts, ''


def qbo_upload(headers, raw):
    parts, problem = parse_multipart(headers.get('content-type', ''), raw)
    if problem:
        return fault(400, 1050, 'Invalid Content Type', problem)
    meta = [p for p in parts if re.fullmatch(r'file_metadata_\d\d', p['name'])]
    files = [p for p in parts if re.fullmatch(r'file_content_\d\d', p['name'])]
    if len(meta) != 1 or len(files) != 1 or meta[0]['name'][-2:] != files[0]['name'][-2:]:
        return fault(400, 2020, 'Required param missing', 'Expected one file_metadata_nn part and one file_content_nn part with the same nn')
    m, f = meta[0], files[0]
    if not m['headers'].get('content-type', '').startswith('application/json'):
        return fault(400, 1050, 'Invalid Content Type', 'The file_metadata part must be application/json')
    try:
        att = json.loads(m['data'].decode('utf-8'))
    except ValueError:
        return fault(400, 2010, 'Request has invalid or unsupported property', 'file_metadata is not valid JSON')
    check = unknown(att, ATTACHABLE_KEYS, 'Attachable')
    if check:
        return check
    refs = att.get('AttachableRef')
    if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], dict):
        return fault(400, 2020, 'Required param missing', 'AttachableRef with one EntityRef is required')
    check = unknown(refs[0], ATTACHABLE_REF_KEYS, 'AttachableRef')
    if check:
        return check
    ent = refs[0].get('EntityRef') or {}
    if set(ent) - REF_KEYS or ent.get('type') != 'Bill' or ent.get('value') not in BILLS:
        return fault(400, 2500, 'Invalid Reference Id', f'Invalid Reference Id : EntityRef {ent}')
    if att.get('FileName') != f['filename'] or not f['filename'].lower().endswith('.pdf') or att.get('ContentType') != 'application/pdf':
        return fault(400, 2010, 'Request has invalid or unsupported property', 'FileName and ContentType must match the file part')
    if f['headers'].get('content-type') != 'application/pdf':
        return fault(400, 1050, 'Invalid Content Type', 'The file part must be application/pdf')
    data = f['data']
    if f['headers'].get('content-transfer-encoding', '').lower() == 'base64':
        try:
            data = base64.b64decode(re.sub(rb'\s+', b'', data), validate=True)
        except ValueError:
            return fault(400, 2010, 'Request has invalid or unsupported property', 'file content is not valid base64')
    if not data.startswith(b'%PDF-'):
        return fault(400, 2010, 'Request has invalid or unsupported property', 'file content is not a PDF')
    doc = BILLS[ent['value']]['DocNumber']
    if doc in FAIL['upload_fail']:
        return fault(500, 10000, 'An application error has occurred while processing your request', 'System Failure Error: An unexpected error occurred', 'SystemFault')
    aid = str(5000000000 + len(ATTACHABLES) + 1)
    a = {'Id': aid, 'SyncToken': '0', 'FileName': f['filename'], 'Size': len(data), 'ContentType': 'application/pdf', 'AttachableRef': refs,
         'FileAccessUri': f'/v3/company/{REALM}/download/{aid}', 'TempDownloadUri': f'https://intuit-qbo-mock.example.com/{aid}', 'domain': 'QBO', 'sparse': False,
         'MetaData': {'CreateTime': now_iso(), 'LastUpdatedTime': now_iso()}}
    ATTACHABLES.append({**a, 'bytes': data})
    return 200, {'AttachableResponse': [{'Attachable': a}], 'time': now_iso()}


def qbo(method, path, query, headers, body, raw):
    """QuickBooks Online Accounting API v3: query, bill create and upload, minor version 75."""
    if headers.get('authorization') != 'Bearer ' + TOKENS['quickBooksOAuth2Api'] or FAIL['qbo_401']:
        return fault(401, 3200, 'message=AuthenticationFailed; errorCode=003200; statusCode=401', 'Token expired: eyJlbmMiOi...', 'AuthenticationFault')
    m = re.fullmatch(r'/qbo/v3/company/([^/]+)/(query|bill|upload)', path)
    if not m:
        return fault(400, 500, 'Unsupported Operation', f'Operation Could not find resource for {path}')
    if m.group(1) != REALM:
        return fault(403, 120, 'Authorization Failure', 'AuthorizationFailure: The app is not connected to this company', 'AuthorizationFault')
    q = {k: v[0] for k, v in query.items()}
    if q.get('minorversion') != '75':
        return fault(400, 2010, 'Request has invalid or unsupported property', 'minorversion 75 is expected by this test')
    if headers.get('accept') != 'application/json':
        return fault(400, 1050, 'Invalid Content Type', 'Accept: application/json is required for JSON responses')
    op = m.group(2)
    if op == 'query':
        if method != 'GET' or set(q) - {'query', 'minorversion'}:
            return fault(400, 4000, 'The query cannot be parsed, refer to detailed message for reasons', 'Unexpected parameters')
        sql = q.get('query', '')
        mq = QUERY_RE.match(sql)
        if not mq:
            return fault(400, 4000, 'Error parsing query', f'QueryParserError: Encountered an unexpected token in "{sql[:80]}"')
        entity, field, value = mq.group(1).capitalize(), mq.group(2), re.sub(r'\\(.)', r'\1', mq.group(3))
        if entity == 'Vendor' and field not in ('DisplayName', 'Id') or entity == 'Bill' and field != 'DocNumber':
            return fault(400, 4001, 'Invalid query', f'QueryValidationError: property {field} is not queryable for {entity}')
        if entity == 'Vendor' and field == 'DisplayName' and value.lower() in {n.lower() for n in FAIL['vendor_429_once']}:
            FAIL['vendor_429_once'] = {n for n in FAIL['vendor_429_once'] if n.lower() != value.lower()}
            status, resp = fault(429, 3001, 'message=ThrottleExceeded; errorCode=003001; statusCode=429', 'The request limit was reached.', 'SystemFault')
            return status, resp, {'Retry-After': '1'}
        race = FAIL['during_vendor_query'].pop(value.lower(), None) if entity == 'Vendor' and field == 'DisplayName' else None
        if race:
            race()
        if entity == 'Vendor':
            # Values are not case sensitive (Intuit's query guide); inactive vendors are left out unless asked for
            rows = [v for v in VENDORS.values() if v['Active'] and (v['Id'] == value if field == 'Id' else v['DisplayName'].lower() == value.lower())]
        else:
            rows = [{k: v for k, v in b.items() if k != 'requestid'} for b in BILLS.values() if b['DocNumber'].lower() == value.lower()]
        qr = {entity: copy.deepcopy(rows), 'startPosition': 1, 'maxResults': len(rows)} if rows else {}
        return 200, {'QueryResponse': qr, 'time': now_iso()}
    if method != 'POST':
        return fault(405, 500, 'Unsupported Operation', 'Method not allowed')
    rid = q.get('requestid', '')
    if set(q) - {'minorversion', 'requestid'} or (rid and (len(rid) > 50 or not re.fullmatch(r'[A-Za-z0-9_.:-]+', rid))):
        return fault(400, 2130, 'Invalid Request Id Format', 'The request ID is invalid, at most 50 characters')
    if op == 'bill':
        if not str(headers.get('content-type', '')).startswith('application/json') or not isinstance(body, dict):
            return fault(400, 1050, 'Invalid Content Type', 'Content-Type application/json is required')
        doc = body.get('DocNumber', '')
        if rid and ('bill', rid) in REQUEST_IDS:
            if doc in FAIL['create_lost']:
                return fault(503, 10000, 'Service Unavailable', 'The service is temporarily unavailable', 'SystemFault')
            return REQUEST_IDS[('bill', rid)]
        if doc in FAIL['create_429_once']:
            FAIL['create_429_once'].discard(doc)
            status, resp = fault(429, 3001, 'message=ThrottleExceeded; errorCode=003001; statusCode=429', 'The request limit was reached.', 'SystemFault')
            return status, resp, {'Retry-After': '1'}
        result = qbo_bill(body, rid)
        if result[0] == 200 and rid:
            REQUEST_IDS[('bill', rid)] = result
        if result[0] == 200 and doc in FAIL['create_lost']:
            return fault(503, 10000, 'Service Unavailable', 'The service is temporarily unavailable', 'SystemFault')
        return result
    if rid and ('upload', rid) in REQUEST_IDS:
        return REQUEST_IDS[('upload', rid)]
    result = qbo_upload(headers, raw)
    if result[0] == 200 and rid:
        REQUEST_IDS[('upload', rid)] = result
    return result


# Claude (Messages API)
CLAUDE_REQUEST_KEYS = {'model', 'max_tokens', 'messages', 'system', 'tools', 'tool_choice', 'thinking', 'metadata', 'stop_sequences', 'stream',
                       'temperature', 'top_p', 'top_k', 'output_config', 'service_tier', 'container', 'mcp_servers', 'context_management', 'inference_geo'}
DOCUMENT_KEYS = {'type', 'source', 'title', 'context', 'citations', 'cache_control'}
NO_FORCED_TOOL = re.compile(r'^claude-(fable-5-1|mythos-5-1|opus-5-5)\b')
NO_THINKING_OFF = re.compile(r'^claude-(fable|mythos|opus-5-5)\b')
UNSUPPORTED_STRICT = {'minLength', 'maxLength', 'minimum', 'maximum', 'multipleOf', 'pattern', 'minItems', 'maxItems'}


def strict_schema_problems(s, where='input_schema'):
    """Strict tool use: every object needs additionalProperties false, required fields that exist, no unsupported constraints."""
    p = []
    if not isinstance(s, dict):
        return [f'{where} must be an object']
    for k in UNSUPPORTED_STRICT & set(s):
        p.append(f'{where}.{k} is not supported with strict tools')
    if 'anyOf' in s:
        for i, sub in enumerate(s['anyOf']):
            p += strict_schema_problems(sub, f'{where}.anyOf.{i}')
        return p
    t = s.get('type')
    if t == 'object':
        if s.get('additionalProperties') is not False:
            p.append(f'{where}: additionalProperties must be false for strict tools')
        props = s.get('properties', {})
        if not set(s.get('required', [])) <= set(props):
            p.append(f'{where}: required lists unknown properties')
        for k, sub in props.items():
            p += strict_schema_problems(sub, f'{where}.{k}')
    elif t == 'array':
        p += strict_schema_problems(s.get('items'), f'{where}.items')
    elif t not in ('string', 'number', 'integer', 'boolean', 'null'):
        p.append(f'{where}: unknown type {t!r}')
    return p


def claude_problems(headers, body):
    p = []
    if headers.get('x-api-key') != ENV['ANTHROPIC_API_KEY']:
        p.append('missing or wrong x-api-key')
    if headers.get('anthropic-version') != '2023-06-01':
        p.append('missing anthropic-version')
    if not str(headers.get('content-type', '')).startswith('application/json'):
        p.append('content-type must be application/json')
    for key in sorted(set(body) - CLAUDE_REQUEST_KEYS):
        p.append(f'{key}: Extra inputs are not permitted')
    if not isinstance(body.get('model'), str) or not isinstance(body.get('max_tokens'), int) or body.get('max_tokens', 0) < 1:
        p.append('model and max_tokens are required')
    for key in ('temperature', 'top_p', 'top_k'):
        if key in body:
            p.append(f'{key} is not supported on this model')
    thinking = body.get('thinking')
    if thinking is not None and thinking not in ({'type': 'disabled'}, {'type': 'adaptive'}):
        p.append('thinking must be disabled or adaptive')
    forced = isinstance(body.get('tool_choice'), dict) and body['tool_choice'].get('type') in ('tool', 'any')
    if forced and NO_FORCED_TOOL.search(str(body.get('model', ''))):
        p.append('tool_choice: type "tool" and "any" are not supported for this model.')
    if thinking == {'type': 'disabled'} and NO_THINKING_OFF.search(str(body.get('model', ''))):
        p.append('thinking: type "disabled" is not supported for this model.')
    if not isinstance(body.get('system'), str) or not body['system'].strip():
        p.append('system must be a non empty string')
    msgs = body.get('messages') or []
    if len(msgs) != 1 or msgs[0].get('role') != 'user' or not isinstance(msgs[0].get('content'), list) or len(msgs[0]['content']) != 2:
        return p + ['expected one user message with a document block and a text block']
    doc, text = msgs[0]['content']
    if doc.get('type') != 'document' or set(doc) - DOCUMENT_KEYS:
        p.append('the first block must be a document block (PDFs go before the text)')
    src = doc.get('source') or {}
    if set(src) != {'type', 'media_type', 'data'} or src.get('type') != 'base64' or src.get('media_type') != 'application/pdf':
        p.append('document source must be {type: base64, media_type: application/pdf, data}')
    data = src.get('data', '')
    if not isinstance(data, str) or not re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', data or ''):
        p.append('document data must be standard base64 without line breaks')
    else:
        pdf = base64.b64decode(data)
        if not pdf.startswith(b'%PDF-'):
            p.append('document data is not a PDF')
        if len(data) > 32 * 1024 * 1024:
            p.append('request too large')
    if text.get('type') != 'text' or not str(text.get('text', '')).strip():
        p.append('the second block must be a non empty text block')
    tools = body.get('tools') or []
    if len(tools) != 1 or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', tools[0].get('name', '')) or set(tools[0]) - {'name', 'description', 'input_schema', 'strict', 'cache_control', 'type'}:
        p.append('expected one tool with a valid name')
    else:
        if tools[0].get('strict') is not True:
            p.append('tool must be strict')
        p += strict_schema_problems(tools[0].get('input_schema'))
        if body.get('tool_choice') != {'type': 'tool', 'name': tools[0]['name']}:
            p.append('tool must be forced')
    return p


def claude(headers, body):
    problems = claude_problems(headers, body)
    if problems:
        return 400, {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': '; '.join(problems)}, 'request_id': 'req_mock_bad'}
    pdf = base64.b64decode(body['messages'][0]['content'][0]['source']['data'])
    m = re.search(rb'fixture:([a-z0-9-]+)', pdf)
    fid = m.group(1).decode() if m else ''
    if fid in LOCK_WATCH and fid not in LOCK_SNAPSHOTS:
        try:
            LOCK_SNAPSHOTS[fid] = ledger(ENV['LEDGER_TABLE_ID'], with_lock=True).get(LOCK_KEY, {}).get('attempts')
        except Exception as e:     # the check that reads the snapshots reports it
            LOCK_SNAPSHOTS[fid] = repr(e)
    behavior = CLAUDE_BEHAVIOR.get(fid)
    if isinstance(behavior, int):
        return behavior, {'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}, 'request_id': 'req_mock_529'}
    usage = {'input_tokens': 2200, 'output_tokens': 160}
    if behavior == 'text-only' or fid not in READ:
        return 200, {'id': 'msg_mock_' + fid, 'type': 'message', 'role': 'assistant', 'model': body['model'], 'stop_reason': 'end_turn', 'stop_sequence': None,
                     'content': [{'type': 'text', 'text': 'Here is what I found on the invoice.'}], 'usage': usage}
    answer = copy.deepcopy(READ[fid])
    assert set(answer) == set(body['tools'][0]['input_schema']['properties']), 'the mock answer must match the tool schema'
    return 200, {'id': 'msg_mock_' + fid, 'type': 'message', 'role': 'assistant', 'model': body['model'], 'stop_reason': 'tool_use', 'stop_sequence': None,
                 'content': [{'type': 'tool_use', 'id': 'toolu_mock_' + fid, 'name': body['tools'][0]['name'], 'input': answer}], 'usage': usage}


# ----------------------------------------------------------------------------------------------------------------------
# Mock HTTP server
# ----------------------------------------------------------------------------------------------------------------------
class Mock(BaseHTTPRequestHandler):
    # HTTP/1.1 keep-alive: n8n reuses connections instead of opening a new one for every call, which keeps the
    # Docker Desktop host.docker.internal proxy from dropping the odd new connection during a long run.
    protocol_version = 'HTTP/1.1'
    timeout = 120

    def log_message(self, *args):
        pass

    def _send(self, code, obj, extra_headers=None):
        data = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain' if isinstance(obj, str) else 'application/json;charset=UTF-8')
        if self.path.startswith('/qbo/'):
            self.send_header('intuit_tid', '1-mock-' + uuid.uuid4().hex[:12])
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        url = urllib.parse.urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        headers = {k.lower(): v for k, v in self.headers.items()}
        path, query = url.path, urllib.parse.parse_qs(url.query, keep_blank_values=True)
        service = ('claude' if path == '/v1/messages' else 'alert' if path.startswith('/slack/errors') else 'slack' if path.startswith('/slack')
                   else 'gmail' if path.startswith('/gmail/') else 'sheets' if path.startswith('/v4/spreadsheets/') else 'qbo' if path.startswith('/qbo/') else 'unknown')
        body = None
        if raw and not headers.get('content-type', '').startswith('multipart/'):
            try:
                body = json.loads(raw)
            except ValueError:
                return self._send(400, {'error': 'Body is not valid JSON'})
        extra = None
        with LOCK:
            if service == 'claude':
                status, resp = claude(headers, body or {})
            elif service in ('slack', 'alert'):
                ok = (method == 'POST' and str(headers.get('content-type', '')).startswith('application/json') and isinstance(body, dict)
                      and isinstance(body.get('text'), str) and body['text'].strip() and not set(body) - {'text', 'blocks', 'attachments', 'thread_ts', 'mrkdwn'})
                status, resp = (200, 'ok') if ok else (400, 'invalid_payload' if isinstance(body, dict) and body.get('text') else 'no_text')
                if ok and service == 'slack' and any(s in body['text'] for s in FAIL['slack_fail']):
                    status, resp = 500, 'internal_error'
            elif service == 'gmail':
                status, resp = gmail(method, path, query, headers, body)
            elif service == 'sheets':
                status, resp = sheets(method, path, query, headers, body)
            elif service == 'qbo':
                out = qbo(method, path, query, headers, body, raw)
                status, resp = out[0], out[1]
                extra = out[2] if len(out) > 2 else None
            else:
                status, resp = 404, {'error': f'Unknown mock path {path}'}
            LOG.append({'service': service, 'method': method, 'path': path, 'query': {k: v if len(v) > 1 else v[0] for k, v in query.items()},
                        'headers': headers, 'body': body if body is not None else (raw.decode('utf-8', 'replace') if raw else None),
                        'status': status, 'response': resp, 't': time.time()})
        return self._send(status, resp, extra)

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')


CONNECTION_ERRORS = []


class MockServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        CONNECTION_ERRORS.append(repr(sys.exc_info()[1]))


# ----------------------------------------------------------------------------------------------------------------------
# n8n helpers
# ----------------------------------------------------------------------------------------------------------------------
def api(method, path, body=None):
    req = urllib.request.Request(N8N + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'X-N8N-API-KEY': KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
        return json.loads(text) if text else {}


def test_copy(env, suffix=''):
    """The workflow with $env values replaced by test values, API hosts pointed at the mocks, the n8n OAuth credentials
    replaced by fixed bearer tokens the mocks check, and test webhook paths."""
    wf = json.loads(WORKFLOW.read_text(encoding='utf-8'))

    def fix(value):
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if not isinstance(value, str):
            return value
        value = re.sub(r'\$env\.([A-Z0-9_]+)', lambda m: json.dumps(env.get(m.group(1), '')), value)
        for real, mock in HOSTS.items():
            value = value.replace(real, mock)
        return value

    for node in wf['nodes']:
        node['parameters'] = fix(node.get('parameters', {}))
        p = node['parameters']
        if p.get('authentication') == 'predefinedCredentialType':
            token = TOKENS[p.pop('nodeCredentialType')]
            p['authentication'] = 'none'
            node.pop('credentials', None)
            p['sendHeaders'] = True
            p.setdefault('headerParameters', {'parameters': []})['parameters'].append({'name': 'Authorization', 'value': 'Bearer ' + token})
        if 'webhookId' in node:
            node['webhookId'] = str(uuid.uuid4())
        if node['type'] == 'n8n-nodes-base.webhook':
            p['path'] = f"{SLUG}-e2e-{'run' if node['name'] == 'Run Now' else 'approval'}{suffix}"
        if node['type'] == 'n8n-nodes-base.scheduleTrigger':
            p['rule'] = {'interval': [{'field': 'minutes', 'minutesInterval': 1}]}   # so the test does not wait 15 minutes
    return wf


def import_active(env, suffix, error_wid):
    wf = test_copy(env, suffix)
    settings = {**wf.get('settings', {}), 'errorWorkflow': error_wid}
    wid = api('POST', '/api/v1/workflows', {'name': TEMP_NAME, 'nodes': wf['nodes'], 'connections': wf['connections'], 'settings': settings})['id']
    api('POST', f'/api/v1/workflows/{wid}/activate')
    time.sleep(2)
    return wid


def import_error_workflow():
    wf = json.loads((WORKFLOW.parent / 'error-alert-slack.json').read_text(encoding='utf-8'))
    for node in wf['nodes']:
        node['parameters'] = json.loads(json.dumps(node.get('parameters', {})).replace('$env.SLACK_WEBHOOK_URL', json.dumps(MOCK + '/slack/errors').replace('"', '\\"')))
    wid = api('POST', '/api/v1/workflows', {'name': TEMP_NAME, 'nodes': wf['nodes'], 'connections': wf['connections'], 'settings': wf.get('settings', {})})['id']
    api('POST', f'/api/v1/workflows/{wid}/activate')
    return wid


def remove(wid):
    try:
        api('POST', f'/api/v1/workflows/{wid}/deactivate')
    finally:
        api('DELETE', f'/api/v1/workflows/{wid}')


def remove_leftovers():
    """Deletes temporary workflows and the temporary data table left by an interrupted run (exact names only)."""
    cursor, found = None, []
    while True:
        page = api('GET', '/api/v1/workflows?limit=250' + (f'&cursor={cursor}' if cursor else ''))
        found += [w['id'] for w in page.get('data', []) if w.get('name') == TEMP_NAME]
        cursor = page.get('nextCursor')
        if not cursor:
            break
    for wid in found:
        remove(wid)
    tables = [t['id'] for t in api('GET', '/api/v1/data-tables?limit=250').get('data', []) if t.get('name') == TABLE_NAME]
    for tid in tables:
        api('DELETE', f'/api/v1/data-tables/{tid}')
    return len(found), len(tables)


def create_table():
    return api('POST', '/api/v1/data-tables', {'name': TABLE_NAME, 'columns': [{'name': n, 'type': t} for n, t in LEDGER_COLUMNS]})['id']


LOCK_KEY = '__intake_lock__'


def ledger(tid, with_lock=False):
    rows, cursor = [], None
    while True:
        page = api('GET', f'/api/v1/data-tables/{tid}/rows?limit=250' + (f'&cursor={cursor}' if cursor else ''))
        rows += page.get('data', [])
        cursor = page.get('nextCursor')
        if not cursor:
            break
    return {r['key']: r for r in rows if with_lock or r['key'] != LOCK_KEY}


def ledger_delete(tid, key):
    flt = urllib.parse.quote(json.dumps({'type': 'and', 'filters': [{'columnName': 'key', 'condition': 'eq', 'value': key}]}))
    api('DELETE', f'/api/v1/data-tables/{tid}/rows/delete?filter={flt}')


def ledger_update(tid, key, data):
    api('PATCH', f'/api/v1/data-tables/{tid}/rows/update', {'filter': {'type': 'and', 'filters': [{'columnName': 'key', 'condition': 'eq', 'value': key}]},
                                                             'data': data, 'returnData': False})


def alerts_for(ids, timeout=90):
    end = time.time() + timeout
    while True:
        got = [e for e in LOG if e['service'] == 'alert']
        linked = {i: [e for e in got if re.search(rf'/executions/{i}(?!\d)', e['body']['text'])] for i in ids}
        if all(linked.values()) or time.time() > end:
            return linked, got
        time.sleep(1)


class Runner:
    def __init__(self, wid, suffix=''):
        self.wid, self.suffix, self.sent, self.results, self.mark = wid, suffix, 0, [], 0
        self.ids = {}

    def executions(self):
        out, cursor = [], None
        while True:
            page = api('GET', f'/api/v1/executions?workflowId={self.wid}&limit=250' + (f'&cursor={cursor}' if cursor else ''))
            out += page.get('data', [])
            cursor = page.get('nextCursor')
            if not cursor:
                return out

    def wait(self, count=1, timeout=240):
        end = time.time() + timeout
        while time.time() < end:
            done = [e for e in self.executions() if e.get('mode') == 'webhook' and e.get('stoppedAt')]
            if len(done) >= self.sent:
                return sorted(done, key=lambda e: int(e['id']))[-count:]
            time.sleep(0.4)
        raise TimeoutError('the workflow did not finish in time')

    def request(self, method, url, data=None, headers=None):
        req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def run(self, secret=ENV['RUN_NOW_SECRET']):
        self.mark = len(LOG)
        status, text = self.request('POST', f'{N8N}/webhook/{SLUG}-e2e-run{self.suffix}', b'{}',
                                    {'Content-Type': 'application/json', **({'X-Run-Secret': secret} if secret is not None else {})})
        self.sent += 1
        return status, text, self.wait()[0]

    def open_link(self, url):
        """A person clicks a link from Slack (GET)."""
        self.mark = len(LOG)
        status, text = self.request('GET', url)
        self.sent += 1
        return status, text, self.wait()[0]

    def press(self, url, name='Dana Lee', reason='', extra=None):
        """The button on the confirmation page: a form POST with the link's fields, the name and a reason."""
        self.mark = len(LOG)
        status, text = self.request('POST', url.split('?')[0], form_body(url, name, reason, extra), {'Content-Type': 'application/x-www-form-urlencoded'})
        self.sent += 1
        return status, text, self.wait()[0]

    def stopped_at(self, ex):
        data = api('GET', f"/api/v1/executions/{ex['id']}?includeData=true").get('data') or {}
        result = data.get('resultData') or {}
        return result.get('lastNodeExecuted'), (result.get('error') or {}).get('message', '')

    def new(self, service=None, method=None, path=None, contains=None):
        out = []
        for e in LOG[self.mark:]:
            if e['service'] == 'alert' and service != 'alert':
                continue
            if service and e['service'] != service or method and e['method'] != method:
                continue
            if path and not re.fullmatch(path, e['path']):
                continue
            if contains and contains not in json.dumps([e['path'], e['body'], e['query']]):
                continue
            out.append(e)
        return out

    def check(self, scenario, label, ok, detail=''):
        self.results.append((scenario, label, bool(ok), detail))
        if os.environ.get('VERBOSE'):
            print(f"{'PASS' if ok else 'FAIL'}  {scenario}: {label}  ({detail})", flush=True)


def form_body(url, name, reason='', extra=None):
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
    fields = {k: q.get(k, '') for k in ('k', 'a', 'e', 's')}
    fields['name'] = name
    if reason:
        fields['reason'] = reason
    fields.update(extra or {})
    return urllib.parse.urlencode(fields).encode()


def sign(key, action, exp):
    return hmac.new(APPROVAL_SECRET.encode(), f'{key}.{action}.{exp}'.encode(), hashlib.sha256).hexdigest()


def link_for(key, action, exp, base=None):
    return f"{base or ENV['APPROVAL_BASE_URL']}?k={key}&a={action}&e={exp}&s={sign(key, action, exp)}"


def slack_for(entries, *fragments):
    return [e['body']['text'] for e in entries if e['service'] == 'slack' and all(f in e['body']['text'] for f in fragments)]


def links_in(text):
    found = dict(re.findall(r'<(http[^|>]+)\|(Approve and post to QuickBooks|Reject[^>]*)>', text))
    return ({'approve': next((u for u, t in found.items() if t.startswith('Approve')), None),
             'reject': next((u for u, t in found.items() if t.startswith('Reject')), None)})


def claude_calls(entries):
    return [e for e in entries if e['service'] == 'claude']


def fixture_of(e):
    pdf = base64.b64decode(e['body']['messages'][0]['content'][0]['source']['data'])
    m = re.search(rb'fixture:([a-z0-9-]+)', pdf)
    return m.group(1).decode() if m else ''


def creates(entries, doc=None):
    return [e for e in entries if e['service'] == 'qbo' and e['path'].endswith('/bill') and (doc is None or (e['body'] or {}).get('DocNumber') == doc)]


def uploads(entries):
    return [e for e in entries if e['service'] == 'qbo' and e['path'].endswith('/upload')]


def modifies(entries, mid=None):
    return [e for e in entries if e['service'] == 'gmail' and e['path'].endswith('/modify') and (mid is None or f'/messages/{mid}/' in e['path'])]


def labels_of(mid):
    return {l for l in MESSAGES[mid]['labels'] if l.startswith('Label_')}


def page_title(html):
    m = re.search(r'<h1>(.*?)</h1>', html or '')
    return m.group(1) if m else (html or '')[:80]


def bills_for(doc):
    return [b for b in BILLS.values() if b['DocNumber'] == doc]


# ----------------------------------------------------------------------------------------------------------------------
# The mailbox: fictional supplier emails (subjects and senders made up)
# ----------------------------------------------------------------------------------------------------------------------
M = {
    'clean': 'a1b2c3d4e5f60001', 'dup': 'a1b2c3d4e5f60002', 'mismatch': 'a1b2c3d4e5f60003', 'newvendor': 'a1b2c3d4e5f60004',
    'scan': 'a1b2c3d4e5f60005', 'reject': 'a1b2c3d4e5f60006', 'throttle': 'a1b2c3d4e5f60007', 'auth': 'a1b2c3d4e5f60008',
    'claude529': 'a1b2c3d4e5f60009', 'notool': 'a1b2c3d4e5f6000a', 'twopdfs': 'a1b2c3d4e5f6000b', 'currency': 'a1b2c3d4e5f6000c',
    'nopdf': 'a1b2c3d4e5f6000d', 'lost': 'a1b2c3d4e5f6000e', 'attachfail': 'a1b2c3d4e5f6000f', 'schedule': 'a1b2c3d4e5f60010',
    'eur': 'a1b2c3d4e5f60021', 'race': 'a1b2c3d4e5f60022',
}


def seed():
    for vid, name, cur in (('56', 'Northwind Office Supply', 'USD'), ('57', 'Bluepeak Electrical Services', 'USD'), ('58', 'Metro Courier Services', 'USD'),
                           ('59', 'Summit Coffee Roasters', 'USD'), ('60', 'Pinecrest Utilities', 'USD'), ('61', 'Granite Office Furniture', 'USD'),
                           ('62', 'Atlas Freight Partners', 'USD'), ('63', 'Ridgeway Pest Control', 'USD'), ('64', 'Evergreen Water Co', 'USD'),
                           ('65', 'Bright Path Cleaning', 'USD'), ('66', 'Lakeside Printing', 'USD'), ('67', 'Oakline Janitorial', 'USD')):
        add_vendor(vid, name, cur)
    add_vendor('70', 'Cedar Ridge Lawn Care Old', 'USD', active=False)
    add_bill('56', 'INV-20388', '2026-10-02', 269.85, 'Entered by hand')
    add_message(M['clean'], 'Northwind Billing <billing@northwind.example.com>', 'Invoice INV-20417 for October', ['northwind-20417.pdf'], 300)
    add_message(M['dup'], 'Northwind Billing <billing@northwind.example.com>', 'Reminder: invoice INV-20388', ['northwind-20388.pdf'], 290)
    add_message(M['mismatch'], 'Bluepeak Office <office@bluepeak.example.com>', 'BP-7781 panel upgrade', ['bluepeak-7781.pdf'], 280)
    add_message(M['newvendor'], 'Cedar Ridge <hello@cedarridge.example.com>', 'Your October lawn care invoice', ['cedar-1042.pdf'], 270)
    add_message(M['scan'], 'Sam Ortiz <sam.ortiz@example.com>', 'receipt scan', ['blurry-scan.pdf'], 260)
    add_message(M['reject'], 'Metro Courier <accounts@metrocourier.example.com>', 'Invoice MCS-5521', ['metro-5521.pdf'], 250)
    add_message(M['throttle'], 'Summit Coffee <wholesale@summitcoffee.example.com>', 'Invoice SCR-3310', ['summit-3310.pdf'], 240)
    add_message(M['claude529'], 'Lakeside Printing <orders@lakesideprint.example.com>', 'Invoice LP-4410', ['lakeside-4410.pdf'], 220)
    add_message(M['notool'], 'Oakline Janitorial <billing@oakline.example.com>', 'October cleaning', ['oakline-207.pdf'], 210)
    add_message(M['twopdfs'], 'Granite Furniture AR <ar@granitefurniture.example.com>', 'Invoices GOF-221 and GOF-222', ['granite-221.pdf', 'granite-222.pdf'], 200)
    add_message(M['currency'], 'Atlas Freight <invoices@atlasfreight.example.com>', 'Rechnung / Invoice AF-9001', ['atlas-9001.pdf'], 190)
    add_message(M['nopdf'], 'Pat Kim <pat.kim@example.com>', 'Invoice link for the catering order', [], 180,
                text='Hi, you can download our invoice here: https://invoices.example.com/abc123')
    # This PDF part has no filename at all (some scanners and billing systems send them like that)
    add_message(M['lost'], 'Ridgeway Pest Control <office@ridgewaypest.example.com>', 'Invoice RPC-310', [('ridgeway-310.pdf', '')], 170)
    add_message(M['attachfail'], 'Evergreen Water <service@evergreenwater.example.com>', 'October invoice EW-4471', ['evergreen-4471.pdf'], 160)
    FAIL['vendor_429_once'].add('Summit Coffee Roasters')
    FAIL['create_429_once'].add('SCR-3310')
    FAIL['modify_fail'][M['reject']] = 3           # the label change after intake fails 3 times (all of n8n's tries)


# ----------------------------------------------------------------------------------------------------------------------
# Scenarios
# ----------------------------------------------------------------------------------------------------------------------
EXPECTED_SHEET_HEADER = SHEET_ROWS[0][:]


def run_main(r, tid):
    S = {}      # Slack messages by scenario
    # ------------------------------------------------------------ refused Run Now
    status, body, ex = r.run(secret='wrong-secret')
    node, error = r.stopped_at(ex)
    r.check('Run Now, wrong secret', 'refused with 401, nothing read, run ends as failed for the alert',
            status == 401 and json.loads(body) == {'started': False, 'error': 'unauthorized'} and not r.new() and node == 'Stop: Bad Run Secret', f'{status}, ended at {node}')
    r.ids['bad_secret'] = ex['id']

    # ------------------------------------------------------------ intake run 1
    FAIL['qbo_401'] = False
    status, body, ex = r.run()
    run1 = LOG[r.mark:]
    node, error = r.stopped_at(ex)
    r.ids['run1'] = ex['id']
    calls = claude_calls(run1)
    by_fixture = {}
    for e in calls:
        by_fixture.setdefault(fixture_of(e), []).append(e['status'])
    r.check('Intake run 1', 'Run Now answers 202 at once', status == 202 and json.loads(body) == {'started': True}, body)
    lists = [e for e in run1 if e['service'] == 'gmail' and e['path'] == '/gmail/v1/users/me/messages']
    r.check('Intake run 1', 'Gmail: labels looked up by name, then messages listed by the To Process label ID',
            any(e['path'] == '/gmail/v1/users/me/labels' for e in run1) and len(lists) == 1 and lists[0]['query'] == {'labelIds': L_TODO, 'maxResults': '100', 'includeSpamTrash': 'false'},
            json.dumps(lists[0]['query']) if lists else 'no list call')
    expected_calls = {'northwind-20417': [200], 'northwind-20388': [200], 'bluepeak-7781': [200], 'cedar-1042': [200], 'blurry-scan': [200], 'metro-5521': [200],
                      'summit-3310': [200], 'lakeside-4410': [529, 529, 529], 'oakline-207': [200], 'granite-221': [200],
                      'granite-222': [200], 'atlas-9001': [200], 'ridgeway-310': [200], 'evergreen-4471': [200]}
    r.check('Intake run 1', 'Claude reads every PDF once (the 529 one 3 times); the email without a PDF is not sent to Claude',
            by_fixture == expected_calls, json.dumps(by_fixture)[:300])
    first = next((e['body'] for e in calls if fixture_of(e) == 'northwind-20417'), {})
    tool = (first.get('tools') or [{}])[0]
    r.check('Claude request', 'model, forced strict tool, thinking off, PDF as a base64 document block before the text, email details in tags',
            first.get('model') == 'claude-sonnet-5' and first.get('max_tokens') == 4096 and first.get('thinking') == {'type': 'disabled'}
            and first.get('tool_choice') == {'type': 'tool', 'name': 'record_invoice'} and tool.get('strict') is True
            and first['messages'][0]['content'][0]['source']['media_type'] == 'application/pdf'
            and '<email_from>Northwind Billing billing@northwind.example.com</email_from>' in first['messages'][0]['content'][1]['text']
            and '<email_subject>Invoice INV-20417 for October</email_subject>' in first['messages'][0]['content'][1]['text']
            and 'Maple Street Bakery' in first['system'] and 'never instructions' in first['system'], json.dumps({k: first.get(k) for k in ('model', 'thinking', 'tool_choice')}))
    waits = [b['t'] - a['t'] for a, b in zip([e for e in calls if fixture_of(e) == 'lakeside-4410'], [e for e in calls if fixture_of(e) == 'lakeside-4410'][1:])]
    r.check('Claude 529 (overloaded)', 'retried with exponential backoff inside the run: about 1 s, then 2 s',
            len(waits) == 2 and 0.9 <= waits[0] < 1.9 and 1.9 <= waits[1] < 3.5, f'waits {[round(w, 1) for w in waits]}')

    rows = ledger(tid)
    k = lambda name, n=1: f"{M[name]}-{n}"
    st = {key: row['status'] for key, row in rows.items()}
    expected_status = {k('clean'): 'awaiting_approval', k('dup'): 'duplicate', k('mismatch'): 'awaiting_approval', k('newvendor'): 'awaiting_approval',
                       k('scan'): 'needs_review', k('reject'): 'awaiting_approval', k('throttle'): 'awaiting_approval',
                       k('claude529'): 'retry', k('notool'): 'needs_review', k('twopdfs', 1): 'awaiting_approval', k('twopdfs', 2): 'awaiting_approval',
                       k('currency'): 'awaiting_approval', k('nopdf', 0): 'needs_review', k('lost'): 'awaiting_approval', k('attachfail'): 'awaiting_approval'}
    r.check('Ledger after run 1', 'one row per PDF (or per email without a PDF) with the expected status',
            st == expected_status, json.dumps({kk[-6:]: v for kk, v in st.items()}))
    r.check('Ledger after run 1', 'every approval request Slack accepted is marked notified; rows waiting for a retry are not',
            all(row['notifiedAt'] for row in rows.values() if row['status'] == 'awaiting_approval') and not rows[k('claude529')]['notifiedAt'], '')

    slack1 = [e['body']['text'] for e in run1 if e['service'] == 'slack']
    clean = slack_for(run1, 'Northwind Office Supply, invoice INV-20417')
    S['clean'] = clean[0] if clean else ''
    L = links_in(S['clean'])
    r.check('Clean invoice', 'Slack approval request with the details, all checks passed, and signed Approve and Reject links',
            len(clean) == 1 and clean[0].startswith(':page_facing_up: *Invoice to approve*: Northwind Office Supply, invoice INV-20417')
            and 'Total: USD 1,284.50 (lines 1,190.00 + tax 94.50)' in clean[0] and 'Dated Oct 20, 2026, due Nov 19, 2026' in clean[0]
            and 'QuickBooks vendor: Northwind Office Supply (Id 56)' in clean[0] and '- Copy paper, letter, 40 cases: 1,000.00' in clean[0]
            and 'Checks: all passed' in clean[0] and L['approve'] and L['reject'], clean[0].splitlines()[0] if clean else 'no Slack message')
    q = urllib.parse.parse_qs(urllib.parse.urlparse(L['approve'] or '').query)
    exp = int(q.get('e', ['0'])[0])
    r.check('Clean invoice', 'the link is HMAC SHA-256 of key, action and expiry with APPROVAL_SECRET, and expires in APPROVAL_LINK_DAYS',
            L['approve'] == link_for(k('clean'), 'approve', exp) and L['reject'] == link_for(k('clean'), 'reject', exp)
            and abs(exp - (time.time() + 7 * 86400)) < 600, (L['approve'] or '')[-90:])
    qbo1 = [e for e in run1 if e['service'] == 'qbo']
    vq = [e['query'].get('query') for e in qbo1 if 'Northwind' in json.dumps(e['query'])]
    r.check('Clean invoice', 'QuickBooks: Vendor queried by name and Bill by DocNumber (minorversion 75), nothing created at intake',
            "select * from Vendor where DisplayName = 'Northwind Office Supply'" in vq and any("DocNumber = 'INV-20417'" in (e['query'].get('query') or '') for e in qbo1)
            and all(e['query'].get('minorversion') == '75' and e['method'] == 'GET' for e in qbo1) and not creates(run1) and not uploads(run1), f'{len(qbo1)} QuickBooks calls')
    r.check('Clean invoice', 'the email moves from To Process to Awaiting Approval', labels_of(M['clean']) == {L_WAIT}, str(labels_of(M['clean'])))

    dup = slack_for(run1, 'Duplicate invoice skipped', 'INV-20388')
    r.check('Duplicate invoice number', 'same vendor and number already in QuickBooks: no links, Slack says it was skipped, email labeled Processed',
            len(dup) == 1 and 'QuickBooks already has this bill (Id 130' in dup[0] and 'Nothing was posted' in dup[0] and '<http' not in dup[0]
            and labels_of(M['dup']) == {L_DONE} and len(bills_for('INV-20388')) == 1, dup[0].splitlines()[0] if dup else 'no Slack message')

    mm = slack_for(run1, 'Bluepeak Electrical, invoice BP-7781')
    S['mismatch'] = mm[0] if mm else ''
    r.check('Total mismatch', 'goes to approval clearly marked: lines plus tax 810.00, total 820.00, Approve still offered',
            len(mm) == 1 and mm[0].startswith(':warning: *Invoice to approve, with problems*') and 'Lines plus tax come to 810.00, but the invoice total is 820.00 (off by 10.00)' in mm[0]
            and links_in(mm[0])['approve'], mm[0].splitlines()[0] if mm else 'no Slack message')
    r.check('Total mismatch', "VENDOR_ALIASES maps the invoice's vendor name to the QuickBooks vendor; Claude's text is escaped for Slack (<!channel> cannot ping)",
            any("DisplayName = 'Bluepeak Electrical Services'" in (e['query'].get('query') or '') for e in qbo1) and mm
            and 'QuickBooks vendor: Bluepeak Electrical Services (Id 57)' in mm[0] and '&lt;!channel&gt;' in mm[0] and '<!channel>' not in mm[0], '')

    nv = slack_for(run1, 'Cedar Ridge Landscaping, invoice CRL-1042')
    S['newvendor'] = nv[0] if nv else ''
    r.check('Unknown vendor', 'goes to approval marked: vendor not found, never created, Approve offered (it checks again when pressed)',
            len(nv) == 1 and 'QuickBooks vendor: *not found*' in nv[0] and 'QuickBooks has no active vendor named "Cedar Ridge Landscaping"' in nv[0]
            and 'The workflow never creates vendors' in nv[0] and links_in(nv[0])['approve'] and not any(e['method'] == 'POST' and 'vendor' in e['path'].lower() for e in qbo1),
            nv[0].splitlines()[3] if nv else 'no Slack message')

    scan = slack_for(run1, 'Invoice needs a person', 'blurry-scan.pdf')
    r.check('Unreadable scan', 'goes to a person: Slack says why, no links, email labeled Needs Review, ledger needs_review',
            len(scan) == 1 and 'Claude could not read the PDF: The scan is too blurry to read any text or amounts' in scan[0] and '|Approve' not in scan[0]
            and labels_of(M['scan']) == {L_REVIEW} and st.get(k('scan')) == 'needs_review', scan[0].splitlines()[1] if scan else 'no Slack message')
    nt = slack_for(run1, 'Invoice needs a person', 'oakline-207.pdf')
    r.check('Claude answers without the tool call', 'treated as unusable: a person enters it (no retry loop, no guessing)',
            len(nt) == 1 and 'Claude answered without the record_invoice tool call' in nt[0] and labels_of(M['notool']) == {L_REVIEW}, nt[0].splitlines()[1] if nt else '')
    np_ = slack_for(run1, 'Invoice needs a person', 'Invoice link for the catering order')
    r.check('Email without a PDF', 'goes to a person, never to Claude', len(np_) == 1 and 'The email has no PDF attachment' in np_[0] and labels_of(M['nopdf']) == {L_REVIEW}, '')
    cur = slack_for(run1, 'Atlas Freight Partners, invoice AF-9001')
    S['currency'] = cur[0] if cur else ''
    r.check('Currency does not match the vendor', 'blocking problem: only a Reject link (enter it by hand), no Approve link',
            len(cur) == 1 and cur[0].startswith(':no_entry: *Invoice that cannot be approved as read*') and 'The invoice is in EUR, but the vendor is set up in USD' in cur[0]
            and links_in(cur[0])['approve'] is None and links_in(cur[0])['reject'], cur[0].splitlines()[0] if cur else '')
    two = slack_for(run1, 'Granite Office Furniture')
    r.check('Two PDFs in one email', 'each PDF gets its own approval request; the email waits in Awaiting Approval',
            len(two) == 2 and {'GOF-221', 'GOF-222'} == {re.search(r'invoice (GOF-22\d)', t).group(1) for t in two} and labels_of(M['twopdfs']) == {L_WAIT}, f'{len(two)} messages')
    S['granite221'] = next((t for t in two if 'invoice GOF-221' in t.splitlines()[0]), '')
    S['granite222'] = next((t for t in two if 'invoice GOF-222' in t.splitlines()[0]), '')
    S['reject'] = next(iter(slack_for(run1, 'Metro Courier Services, invoice MCS-5521')), '')
    S['throttle'] = next(iter(slack_for(run1, 'Summit Coffee Roasters, invoice SCR-3310')), '')
    S['lost'] = next(iter(slack_for(run1, 'Ridgeway Pest Control, invoice RPC-310')), '')
    S['attachfail'] = next(iter(slack_for(run1, 'Evergreen Water Co, invoice EW-4471')), '')
    vend = [e for e in qbo1 if "Summit Coffee Roasters" in (e['query'].get('query') or '')]
    r.check('QuickBooks 429 at intake', 'the vendor query gets 429 with Retry-After 1, waits and succeeds on the second try',
            [e['status'] for e in vend] == [429, 200] and S['throttle'] and (vend[1]['t'] - vend[0]['t']) >= 0.9, f"statuses {[e['status'] for e in vend]}")
    r.check('No currency printed', "the invoice is treated as the vendor's currency with a warning, and can still be approved",
            'No currency is printed on the invoice, so it is treated as USD' in S['throttle'] and 'Total: USD 260.00' in S['throttle']
            and links_in(S['throttle'])['approve'], S['throttle'].splitlines()[0] if S['throttle'] else 'no message')
    r.check('Claude failure (run 1 of 2)', 'after 3 tries the PDF stays in To Process for the next run (try 1 of 2), no Slack message',
            st.get(k('claude529')) == 'retry' and rows[k('claude529')]['attempts'] == 1 and labels_of(M['claude529']) == {L_TODO} and not slack_for(run1, 'Lakeside'), '')
    r.check('Gmail label change fails', 'retried 3 times by n8n; the email stays in To Process but its ledger row is saved',
            len(modifies(run1, M['reject'])) == 3 and labels_of(M['reject']) == {L_TODO} and st.get(k('reject')) == 'awaiting_approval', '')
    r.check('Intake run 1', 'the run ends as failed, naming the Claude outage and the label change',
            node == 'Stop: Run Incomplete' and 'Claude did not read it (529' in error and 'try 1 of 2' in error
            and 'Gmail did not move email a1b2c3d4e5f60006' in error and 'QuickBooks' not in error, f'ended at {node}')
    lock = ledger(tid, with_lock=True).get(LOCK_KEY, {})
    r.check('Run lock', 'the run took the lock and released it at the end', lock.get('status') == 'lock' and lock.get('attempts') == 0 and lock.get('note') == 'free',
            json.dumps({k2: lock.get(k2) for k2 in ('attempts', 'note')}))
    snaps = dict(LOCK_SNAPSHOTS)
    r.check('Run lock', 'the lock is renewed after every PDF: when Claude read the 12th PDF it ran out later than when Claude read the first',
            all(isinstance(snaps.get(f), (int, float)) for f in LOCK_WATCH) and snaps['atlas-9001'] > snaps['northwind-20417'] > time.time() * 1000,
            json.dumps(snaps))
    new_rows = SHEET_ROWS[1:]
    r.check('Google Sheets log', 'one RAW row per invoice of the run (16 columns), appended in a single call',
            len([e for e in run1 if e['service'] == 'sheets']) == 1 and run1 and [e for e in run1 if e['service'] == 'sheets'][0]['query'] == {'valueInputOption': 'RAW', 'insertDataOption': 'INSERT_ROWS'}
            and len(new_rows) == 15 and all(len(row) == 16 for row in new_rows) and any(row[1] == 'Duplicate' and row[4] == 'INV-20388' for row in new_rows), f'{len(new_rows)} rows')

    # ------------------------------------------------------------ another run still holds the lock
    ledger_update(tid, LOCK_KEY, {'attempts': int(time.time() * 1000) + 600000, 'note': '999999'})
    status, body, ex = r.run()
    node, _ = r.stopped_at(ex)
    flow = r.new()
    r.check('Run lock', 'while another run holds the lock, a run lists the emails and stops: nothing is read, posted or relabeled',
            status == 202 and not claude_calls(flow) and not [e for e in flow if e['service'] == 'gmail' and re.search(r'/messages/[^/]+$', e['path'])]
            and not r.new('qbo') and not r.new('slack') and not modifies(flow) and node == 'Lock Route' and ex['status'] == 'success', f'ended at {node}')
    ledger_update(tid, LOCK_KEY, {'attempts': int(time.time() * 1000) - 1000, 'note': '999999'})

    # ------------------------------------------------------------ intake run 2: the QuickBooks connection is revoked
    add_message(M['auth'], 'Pinecrest Utilities <billing@pinecrest.example.com>', 'Your electricity bill PU-88213', ['pinecrest-88213.pdf'], 5)
    FAIL['qbo_401'] = True
    status, body, ex = r.run()
    run2 = LOG[r.mark:]
    node, error = r.stopped_at(ex)
    r.ids['run2'] = ex['id']
    calls = [fixture_of(e) for e in claude_calls(run2)]
    rows = ledger(tid)
    qbo2 = [e for e in run2 if e['service'] == 'qbo']
    r.check('QuickBooks auth failure (401)', 'the new email is read once; the 401 is not retried inside the run (n8n already tried to refresh the token)',
            calls.count('pinecrest-88213') == 1 and len(qbo2) == 1 and qbo2[0]['status'] == 401, f'{len(qbo2)} QuickBooks call(s), Claude {calls}')
    r.check('QuickBooks auth failure (401)', 'the invoice waits in To Process with its reading saved; nobody gets an approval request yet',
            rows[k('auth')]['status'] == 'retry' and rows[k('auth')]['attempts'] == 0 and json.loads(rows[k('auth')]['data'])['invoice']['invoiceNumber'] == 'PU-88213'
            and labels_of(M['auth']) == {L_TODO} and not slack_for(run2, 'Pinecrest'), rows[k('auth')]['status'])
    r.check('QuickBooks auth failure (401)', 'the run fails with a clear reason: reconnect the QuickBooks credential',
            node == 'Stop: Run Incomplete' and 'QuickBooks refused the connection (401, code 3200)' in error and 'open it in n8n and reconnect it' in error, f'ended at {node}')
    lk = slack_for(run2, 'Invoice needs a person', 'lakeside-4410.pdf')
    r.check('Claude failure fallback', 'second run still fails, so the PDF goes to a person (MAX_EXTRACT_ATTEMPTS=2), and the run is flagged',
            calls.count('lakeside-4410') == 3 and rows[k('claude529')]['status'] == 'needs_review' and len(lk) == 1 and 'It was tried 2 times, so it now goes to a person' in lk[0]
            and labels_of(M['claude529']) == {L_REVIEW} and 'could not be read after 2 tries and went to a person' in error, lk[0].splitlines()[1] if lk else 'no Slack message')
    heal = modifies(run2, M['reject'])
    r.check('Label repair', 'the email whose label change failed is only relabeled (Awaiting Approval), never read or announced again',
            len(heal) == 1 and heal[0]['body'] == {'addLabelIds': [L_WAIT], 'removeLabelIds': [L_TODO, L_DONE, L_REJ, L_REVIEW]}
            and labels_of(M['reject']) == {L_WAIT} and not slack_for(run2, 'Metro') and 'metro-5521' not in calls, json.dumps(heal[0]['body']) if heal else 'no relabel')

    # ------------------------------------------------------------ intake run 3: QuickBooks reconnected
    FAIL['qbo_401'] = False
    status, body, ex = r.run()
    run3 = LOG[r.mark:]
    node, error = r.stopped_at(ex)
    rows = ledger(tid)
    r.check('Repeat run', 'only the unfinished invoice is touched, from its saved reading: no Claude call, one approval request, nothing else read or relabeled',
            not claude_calls(run3) and len([e for e in run3 if e['service'] == 'slack']) == 1 and slack_for(run3, 'Pinecrest Utilities, invoice PU-88213')
            and [e['path'].split('/')[-2] for e in modifies(run3)] == [M['auth']] and node == 'Run Failed?' and ex['status'] == 'success', f'ended at {node}')
    r.check('QuickBooks auth failure (401)', 'after reconnecting, the next run finishes it: waiting for approval, email in Awaiting Approval',
            rows[k('auth')]['status'] == 'awaiting_approval' and labels_of(M['auth']) == {L_WAIT}, rows[k('auth')]['status'])
    S['auth'] = next(iter(slack_for(run3, 'Pinecrest Utilities, invoice PU-88213')), '')

    # ------------------------------------------------------------ intake run 4: nothing left
    status, body, ex = r.run()
    node, _ = r.stopped_at(ex)
    r.check('Repeat run with nothing new', 'only the Gmail lookups: no Claude, QuickBooks, Slack or Sheets call, nothing relabeled',
            not claude_calls(r.new()) and not r.new('qbo') and not r.new('slack') and not r.new('sheets') and not modifies(r.new())
            and node == 'Any Emails?' and ex['status'] == 'success', f'ended at {node}')

    # ------------------------------------------------------------ approvals
    L = links_in(S['clean'])
    status, html, ex = r.open_link(L['approve'])
    node, _ = r.stopped_at(ex)
    r.check('Opening the Approve link', 'shows a confirmation page (200) with the details and a form; nothing is posted or changed by a GET',
            status == 200 and page_title(html) == 'Approve this bill?' and 'Northwind Office Supply' in html and '1,284.50' in html
            and 'name="name"' in html and f'name="s" value="{urllib.parse.parse_qs(urllib.parse.urlparse(L["approve"]).query)["s"][0]}"' in html
            and not r.new('qbo') and ledger(tid)[k('clean')]['status'] == 'awaiting_approval' and node == 'Approval Failed?', f'{status} {page_title(html)}')
    status, html, ex = r.press(L['approve'], name='')
    r.check('Pressing Approve without a name', 'asked again for the name (400), nothing posted', status == 400 and 'Please type your name' in html and not r.new('qbo'), f'{status}')
    status, html, ex = r.press(L['approve'], name='Dana Lee')
    node, error = r.stopped_at(ex)
    flow = r.new()
    bill_calls = creates(flow)
    bill = bill_calls[0]['body'] if bill_calls else {}
    r.check('Approve: clean invoice', "posts the Bill: VendorRef, the vendor's CurrencyRef, DocNumber, TxnDate, DueDate, one expense line per invoice line, tax as its own line",
            status == 200 and page_title(html) == 'Posted to QuickBooks' and len(bill_calls) == 1 and bill_calls[0]['status'] == 200
            and set(bill_calls[0]['query']) == {'minorversion', 'requestid'} and bill_calls[0]['query']['requestid'].startswith(f"bill-{k('clean')}-")
            and bill.get('VendorRef') == {'value': '56'} and bill.get('CurrencyRef') == {'value': 'USD'}
            and bill.get('DocNumber') == 'INV-20417' and bill.get('TxnDate') == '2026-10-20' and bill.get('DueDate') == '2026-11-19'
            and [(l['Amount'], l['AccountBasedExpenseLineDetail']['AccountRef']['value']) for l in bill.get('Line', [])] == [(1000.0, '7'), (190.0, '7'), (94.5, '31')]
            and bill['Line'][2]['Description'] == 'Tax on invoice INV-20417' and bill.get('PrivateNote', '').endswith(f"[ledger:{k('clean')}]")
            and 'Approved by Dana Lee' in bill.get('PrivateNote', ''), json.dumps(bill)[:200])
    up = uploads(flow)
    att = ATTACHABLES[-1] if ATTACHABLES else {}
    r.check('Approve: clean invoice', 'attaches the original PDF to the Bill (Attachable upload, multipart, linked to the Bill)',
            len(up) == 1 and up[0]['status'] == 200 and att.get('bytes') == (FIXTURES / 'northwind-20417.pdf').read_bytes()
            and att.get('AttachableRef') == [{'EntityRef': {'type': 'Bill', 'value': bill_calls[0]['response']['Bill']['Id']}, 'IncludeOnSend': False}]
            and att.get('FileName') == 'northwind-20417.pdf', f'{len(up)} upload(s)')
    row = ledger(tid)[k('clean')]
    r.check('Approve: clean invoice', 'ledger posted with the bill Id and the approver, email labeled Processed, Sheets row and Slack note',
            row['status'] == 'posted' and row['billId'] == bill_calls[0]['response']['Bill']['Id'] and row['decidedBy'] == 'Dana Lee'
            and labels_of(M['clean']) == {L_DONE} and SHEET_ROWS[-1][1] == 'Approved' and SHEET_ROWS[-1][11] == 'Dana Lee'
            and slack_for(flow, 'Posted to QuickBooks', 'INV-20417', 'approved by Dana Lee') and node == 'Approval Failed?' and ex['status'] == 'success', f'ended at {node}')
    r.check('Approve: clean invoice', 'QuickBooks checked again before posting (vendor by Id, bill by number), and the PDF found again in Gmail',
            [e['query'].get('query') for e in flow if e['service'] == 'qbo' and e['method'] == 'GET'] == ["select * from Vendor where Id = '56'", "select * from Bill where DocNumber = 'INV-20417'"]
            and len([e for e in flow if e['service'] == 'gmail' and '/attachments/' in e['path']]) == 1, '')

    # Reuse
    mark = len(LOG)
    status1, html1, _ = r.open_link(L['approve'])
    status2, html2, _ = r.press(L['approve'], name='Sam Ortiz')
    status3, html3, _ = r.press(L['reject'], name='Sam Ortiz', reason='Changed my mind')
    r.check('Reused links', 'after the approval every link of that invoice only shows what happened (409); nothing is posted twice',
            status1 == 409 and page_title(html1) == 'Already approved' and status2 == 409 and status3 == 409 and 'approved by Dana Lee' in html2
            and not creates(LOG[mark:]) and len(bills_for('INV-20417')) == 1 and ledger(tid)[k('clean')]['status'] == 'posted', f'{status1} {status2} {status3}')

    # Forged, swapped, expired, malformed
    bad = L['approve'][:-1] + ('0' if L['approve'][-1] != '0' else '1')
    status, html, ex = r.open_link(bad)
    node, error = r.stopped_at(ex)
    r.ids['forged'] = ex['id']
    r.check('Forged link', 'a changed signature is refused (403), nothing read or changed, and the run fails for an alert',
            status == 403 and page_title(html) == 'This link is not valid' and not r.new('qbo') and node == 'Stop: Approval Problem' and 'wrong signature' in error, f'{status}, ended at {node}')
    q = {kk: v[0] for kk, v in urllib.parse.parse_qs(urllib.parse.urlparse(S['currency'] and links_in(S['currency'])['reject']).query).items()}
    swapped = f"{ENV['APPROVAL_BASE_URL']}?k={q['k']}&a=approve&e={q['e']}&s={q['s']}"
    status, html, ex = r.press(swapped, name='Mallory')
    node, _ = r.stopped_at(ex)
    r.ids['swapped'] = ex['id']
    r.check('Forged link', 'turning a Reject link into an Approve link fails the signature check too (the action is signed)',
            status == 403 and node == 'Stop: Approval Problem' and ledger(tid)[k('currency')]['status'] == 'awaiting_approval', f'{status}')
    old = int(time.time()) - 3600
    status, html, ex = r.open_link(link_for(k('mismatch'), 'approve', old))
    node, _ = r.stopped_at(ex)
    r.check('Expired link', 'a correctly signed link past its expiry is refused (410) and explains how to get new links',
            status == 410 and page_title(html) == 'This link has expired' and 'move the email back to the' in html and node == 'Approval Failed?' and ex['status'] == 'success', f'{status}')
    status, html, ex = r.open_link(f"{ENV['APPROVAL_BASE_URL']}?k={k('mismatch')}&a=approve")
    r.check('Incomplete link', 'refused with 400, nothing read', status == 400 and page_title(html) == 'This link is incomplete' and not r.new('qbo'), f'{status}')

    # Reject
    R = links_in(S['reject'])
    status, html, ex = r.open_link(R['reject'])
    r.check('Reject', 'the Reject link shows a confirmation page with a reason field', status == 200 and page_title(html) == 'Reject this invoice?' and 'name="reason"' in html, f'{status}')
    status, html, ex = r.press(R['reject'], name='Dana Lee', reason='Not our account <b>')
    node, _ = r.stopped_at(ex)
    flow = r.new()
    row = ledger(tid)[k('reject')]
    r.check('Reject', 'nothing posted; ledger rejected with the reason, email labeled Rejected, Sheets row and Slack note (escaped)',
            status == 200 and page_title(html) == 'Rejected' and not r.new('qbo') and row['status'] == 'rejected' and row['note'] == 'Rejected by Dana Lee: Not our account <b>'
            and labels_of(M['reject']) == {L_REJ} and SHEET_ROWS[-1][1] == 'Rejected' and slack_for(flow, 'Invoice rejected', 'Not our account &lt;b&gt;')
            and 'Not our account &lt;b&gt;' in html and node == 'Approval Failed?', f'{status}')
    status, html, ex = r.press(R['approve'], name='Sam Ortiz')
    r.check('Reject', 'the Approve link of a rejected invoice is refused (409)', status == 409 and page_title(html) == 'Already rejected' and not creates(r.new()), f'{status}')

    # Total mismatch approved
    status, html, ex = r.press(links_in(S['mismatch'])['approve'], name='Dana Lee')
    bc = creates(r.new())
    b = bc[0]['body'] if bc else {}
    r.check('Approve: total mismatch', 'posted as read (lines plus tax = 810.00) with the warning in the private note; the page shows both totals',
            status == 200 and len(bc) == 1 and sum(l['Amount'] for l in b.get('Line', [])) == 810.0 and 'off by 10.00' in b.get('PrivateNote', '')
            and 'Total printed on the invoice' in html and '820.00' in html, f'{status}')

    # Unknown vendor: refused until the vendor exists, then posted with the same link
    N = links_in(S['newvendor'])
    status, html, ex = r.press(N['approve'], name='Dana Lee')
    node, _ = r.stopped_at(ex)
    row = ledger(tid)[k('newvendor')]
    r.check('Approve: unknown vendor', 'refused (409): no vendor is created, nothing posted, the invoice waits again and the same link keeps working',
            status == 409 and page_title(html) == 'Add the vendor first' and not creates(r.new()) and not any(e['method'] == 'POST' for e in r.new('qbo'))
            and row['status'] == 'awaiting_approval' and 'vendor is not in QuickBooks' in row['note'] and node == 'Approval Failed?', f'{status}, {row["status"]}')
    add_vendor('68', 'Cedar Ridge Landscaping', 'USD')
    status, html, ex = r.press(N['approve'], name='Dana Lee')
    bc = creates(r.new())
    r.check('Approve: unknown vendor', 'after the vendor is added in QuickBooks, the same link posts the bill to that vendor',
            status == 200 and len(bc) == 1 and bc[0]['body']['VendorRef'] == {'value': '68'} and ledger(tid)[k('newvendor')]['status'] == 'posted', f'{status}')

    # An approval that never finished: the row is left "approving" (as if n8n stopped in the middle)
    row = ledger(tid)[k('throttle')]
    data = json.loads(row['data'])
    data['claimedAt'] = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    ledger_update(tid, k('throttle'), {'status': 'approving', 'data': json.dumps(data)})
    status_r, html_r, _ = r.open_link(links_in(S['throttle'])['reject'])
    status_g, html_g, _ = r.open_link(links_in(S['throttle'])['approve'])
    r.check('Approval that never finished', 'after 10 minutes the Reject link refuses (409) and the Approve link offers to finish it',
            status_r == 409 and page_title(html_r) == 'An approval did not finish' and status_g == 200 and 'did not finish' in html_g, f'{status_r} {status_g}')

    # QuickBooks 429 on the bill create (and finishing the stale approval)
    status, html, ex = r.press(links_in(S['throttle'])['approve'], name='Dana Lee')
    bc = creates(r.new())
    r.check('QuickBooks 429 on the bill', 'the stale approval is claimed again; 429 retried after Retry-After with the same request ID; exactly one bill',
            status == 200 and ledger(tid)[k('throttle')]['status'] == 'posted' and [e['status'] for e in bc] == [429, 200]
            and bc[0]['query']['requestid'] == bc[1]['query']['requestid'] and len(bills_for('SCR-3310')) == 1 and bc[1]['t'] - bc[0]['t'] >= 0.9, f"statuses {[e['status'] for e in bc]}")

    # Blocked invoice: an Approve link the workflow never sent
    status, html, ex = r.open_link(link_for(k('currency'), 'approve', int(time.time()) + 3600))
    r.check('Blocked invoice', 'even a correctly signed Approve link is refused (409) for an invoice with a blocking problem',
            status == 409 and page_title(html) == 'This invoice cannot be approved as read' and not r.new('qbo'), f'{status}')

    # The second PDF of the email has no ledger row yet (as if a run were still reading it) when the first is approved
    ledger_delete(tid, k('twopdfs', 2))
    status, html, ex = r.press(links_in(S['granite221'])['approve'], name='Dana Lee')
    r.check('Two PDFs in one email', 'approving one PDF while the other has no ledger row yet keeps the email in To Process',
            status == 200 and ledger(tid)[k('twopdfs', 1)]['status'] == 'posted' and labels_of(M['twopdfs']) == {L_TODO}, str(labels_of(M['twopdfs'])))
    status, body, ex = r.run()
    flow = r.new()
    again = slack_for(flow, 'Granite Office Furniture, invoice GOF-222')
    r.check('Two PDFs in one email', 'the next run reads only the missing PDF and asks for its approval; the email waits in Awaiting Approval',
            [fixture_of(e) for e in claude_calls(flow)] == ['granite-222'] and len(again) == 1 and labels_of(M['twopdfs']) == {L_WAIT}, str(labels_of(M['twopdfs'])))
    S['granite222'] = again[0] if again else S['granite222']

    # Bill entered by hand while waiting
    add_bill('61', 'GOF-222', '2026-10-14', 715.52, 'Entered by hand')
    status, html, ex = r.press(links_in(S['granite222'])['approve'], name='Dana Lee')
    flow = r.new()
    r.check('Duplicate found at approval', 'someone entered the bill by hand meanwhile: nothing posted, ledger duplicate, and the email moves to Processed',
            status == 200 and page_title(html) == 'Already in QuickBooks' and not creates(flow) and ledger(tid)[k('twopdfs', 2)]['status'] == 'duplicate'
            and labels_of(M['twopdfs']) == {L_DONE} and len(bills_for('GOF-222')) == 1, f'{status}')

    # The answer to the bill create is lost: approving again adopts the saved bill
    FAIL['create_lost'].add('RPC-310')
    status, html, ex = r.press(links_in(S['lost'])['approve'], name='Dana Lee')
    node, error = r.stopped_at(ex)
    r.ids['lost'] = ex['id']
    bc = creates(r.new())
    r.check('Bill saved but the answer lost', 'QuickBooks saved the bill but kept answering 503: 3 tries with one request ID, page says it may be saved, invoice waits again',
            status == 502 and len(bc) == 3 and len({e['query']['requestid'] for e in bc}) == 1 and len(bills_for('RPC-310')) == 1
            and ledger(tid)[k('lost')]['status'] == 'awaiting_approval' and 'may still have saved it' in html and node == 'Stop: Approval Problem', f'{status}, ended at {node}')
    FAIL['create_lost'].discard('RPC-310')
    status, html, ex = r.press(links_in(S['lost'])['approve'], name='Dana Lee')
    flow = r.new()
    r.check('Bill saved but the answer lost', 'opening the link again finds that bill by the ledger key in its note and attaches the PDF: still one bill',
            status == 200 and not creates(flow) and len(uploads(flow)) == 1 and len(bills_for('RPC-310')) == 1
            and ledger(tid)[k('lost')]['status'] == 'posted' and ledger(tid)[k('lost')]['billId'] == bills_for('RPC-310')[0]['Id']
            and 'already saved by an earlier approval' in ledger(tid)[k('lost')]['note'], f'{status}')
    att = ATTACHABLES[-1] if ATTACHABLES else {}
    r.check('PDF without a filename', 'the PDF part had no filename: both approvals found it again by its Gmail part ID, and it is attached as invoice.pdf',
            att.get('FileName') == 'invoice.pdf' and att.get('bytes') == (FIXTURES / 'ridgeway-310.pdf').read_bytes()
            and att.get('AttachableRef', [{}])[0].get('EntityRef', {}).get('value') == (bills_for('RPC-310') or [{}])[0].get('Id'), json.dumps({k2: att.get(k2) for k2 in ('FileName', 'Size')}))

    # The PDF upload fails after the bill was created
    FAIL['upload_fail'].add('EW-4471')
    status, html, ex = r.press(links_in(S['attachfail'])['approve'], name='Dana Lee')
    node, error = r.stopped_at(ex)
    r.ids['attachfail'] = ex['id']
    flow = r.new()
    r.check('PDF upload fails', 'the bill stays posted (no undo, no second bill); the page, the ledger and an alert say to attach the PDF by hand',
            status == 200 and page_title(html) == 'Posted to QuickBooks' and len(creates(flow)) == 1 and len(uploads(flow)) == 3
            and ledger(tid)[k('attachfail')]['status'] == 'posted' and 'PDF was NOT attached' in ledger(tid)[k('attachfail')]['note']
            and 'Attach it in QuickBooks by hand' in html and node == 'Stop: Approval Problem' and 'The PDF was not attached to the bill' in error, f'{status}, ended at {node}')

    # Two clicks at the same moment
    A = links_in(S['auth'])['approve']
    mark = len(LOG)
    results = []

    def click():
        results.append(r.request('POST', A.split('?')[0], form_body(A, 'Dana Lee'), {'Content-Type': 'application/x-www-form-urlencoded'}))
    threads = [threading.Thread(target=click) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    r.sent += 2
    r.wait(2)
    codes = sorted(s for s, _ in results)
    r.check('Two clicks at the same moment', 'only one claims the invoice: one bill, the other click sees it is already being handled',
            codes == [200, 409] and len(creates(LOG[mark:])) == 1 and len(bills_for('PU-88213')) == 1 and ledger(tid)[k('auth')]['status'] == 'posted', f'codes {codes}')

    # Needs review: nothing to approve
    status, html, ex = r.open_link(link_for(k('claude529'), 'approve', int(time.time()) + 3600))
    r.check('Invoice that went to a person', 'an Approve link is refused (409): nothing to approve', status == 409 and page_title(html) == 'Nothing to approve', f'{status}')

    # ------------------------------------------------------------ renewal of expired links
    row = ledger(tid)[k('currency')]
    data = json.loads(row['data'])
    data['expiresAt'] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    ledger_update(tid, k('currency'), {'data': json.dumps(data)})
    MESSAGES[M['currency']]['labels'] = (MESSAGES[M['currency']]['labels'] - {L_WAIT}) | {L_TODO}
    status, body, ex = r.run()
    flow = r.new()
    renew = slack_for(flow, 'Atlas Freight Partners, invoice AF-9001')
    r.check('Expired approval renewed', 'moving the email back to To Process posts new links from the saved reading: no Claude call',
            not claude_calls(flow) and len(renew) == 1 and 'New links, the earlier ones expired' in renew[0] and labels_of(M['currency']) == {L_WAIT}
            and json.loads(ledger(tid)[k('currency')]['data'])['expiresAt'] > now_iso(), renew[0].splitlines()[-1] if renew else 'no message')

    # ------------------------------------------------------------ Slack is down for one approval request
    add_message('a1b2c3d4e5f60020', 'Bright Path Billing <billing@brightpath.example.com>', 'Invoice BPC-118', ['brightpath-118.pdf'], 5)
    FAIL['slack_fail'].add('BPC-118')
    status, body, ex = r.run()
    node, error = r.stopped_at(ex)
    r.ids['slackdown'] = ex['id']
    FAIL['slack_fail'].discard('BPC-118')
    row = ledger(tid)['a1b2c3d4e5f60020-1']
    r.check('Slack is down', 'retried 3 times; the ledger keeps the invoice (not marked notified), the email stays in To Process, and the run fails with the links',
            len([e for e in r.new('slack') if 'BPC-118' in e['body']['text']]) == 3 and row['status'] == 'awaiting_approval' and not row['notifiedAt']
            and labels_of('a1b2c3d4e5f60020') == {L_TODO} and node == 'Stop: Run Incomplete' and 'the next run sends it again' in error and 'Approve: http' in error, f'ended at {node}')
    status, body, ex = r.run()
    node, _ = r.stopped_at(ex)
    flow = r.new()
    again = slack_for(flow, 'Bright Path Cleaning, invoice BPC-118')
    row = ledger(tid)['a1b2c3d4e5f60020-1']
    r.check('Slack is down', 'the next run sends the approval request again from the saved reading (no Claude call), then labels the email',
            not claude_calls(flow) and len(again) == 1 and 'Sent again: the first message did not reach Slack' in again[0] and row['notifiedAt']
            and labels_of('a1b2c3d4e5f60020') == {L_WAIT} and node == 'Run Failed?' and ex['status'] == 'success', f'ended at {node}')

    # ------------------------------------------------------------ a vendor in another currency; Slack down for another invoice
    add_vendor('69', 'Fjord Coffee Imports', 'EUR')
    add_vendor('71', 'Harbor Linen Service', 'USD')
    add_message(M['eur'], 'Fjord Coffee Imports <billing@fjordcoffee.example.com>', 'Invoice FCI-5120', ['fjord-5120.pdf'], 4)
    add_message(M['race'], 'Harbor Linen <accounts@harborlinen.example.com>', 'Invoice HLS-7702', ['harbor-7702.pdf'], 3)
    FAIL['slack_fail'].add('HLS-7702')
    status, body, ex = r.run()
    node, error = r.stopped_at(ex)
    r.ids['racerun'] = ex['id']
    FAIL['slack_fail'].discard('HLS-7702')
    flow = r.new()
    eur = slack_for(flow, 'Fjord Coffee Imports, invoice FCI-5120')
    S['eur'] = eur[0] if eur else ''
    race_row = ledger(tid)[k('race')]
    r.check('Vendor in another currency', 'an EUR invoice from a vendor set up in EUR in QuickBooks passes every check and goes to approval',
            len(eur) == 1 and 'Total: EUR 935.00' in eur[0] and 'Checks: all passed' in eur[0] and links_in(eur[0])['approve'] and labels_of(M['eur']) == {L_WAIT},
            eur[0].splitlines()[0] if eur else 'no Slack message')
    status, html, ex = r.press(links_in(S['eur'])['approve'], name='Dana Lee')
    bc = creates(r.new())
    b = bc[0]['body'] if bc else {}
    r.check('Vendor in another currency', "the bill carries the vendor's currency (CurrencyRef EUR), which QuickBooks requires when multicurrency is on",
            status == 200 and page_title(html) == 'Posted to QuickBooks' and len(bc) == 1 and bc[0]['status'] == 200 and b.get('CurrencyRef') == {'value': 'EUR'}
            and ledger(tid)[k('eur')]['status'] == 'posted' and labels_of(M['eur']) == {L_DONE}, f"{status} {json.dumps(b.get('CurrencyRef'))}")

    # ------------------------------------------------------------ approved from the alert's links while the next run sends the request again
    r.check('Approved while a run sends the request again', 'setup: Slack refused the request, so the invoice waits unconfirmed and the alert carries its links',
            race_row['status'] == 'awaiting_approval' and not race_row['notifiedAt'] and node == 'Stop: Run Incomplete' and 'HLS-7702' in error and 'Approve: http' in error, f'ended at {node}')

    def approved_meanwhile():
        # What the Approve link from the alert leaves behind, landing after the run read the ledger
        ledger_update(tid, k('race'), {'status': 'posted', 'billId': '777', 'decidedBy': 'Sam Ortiz', 'note': 'Posted as bill Id 777 with the PDF attached'})
    FAIL['during_vendor_query']['harbor linen service'] = approved_meanwhile
    status, body, ex = r.run()
    node, _ = r.stopped_at(ex)
    flow = r.new()
    row = ledger(tid)[k('race')]
    r.check('Approved while a run sends the request again', 'the run saves nothing over the decision: the row stays posted, Slack is not asked again, the email moves to Processed',
            not FAIL['during_vendor_query'] and not [e for e in flow if e['service'] == 'slack'] and row['status'] == 'posted' and row['billId'] == '777'
            and row['decidedBy'] == 'Sam Ortiz' and labels_of(M['race']) == {L_DONE} and not [s for s in SHEET_ROWS[1:] if s[-1] == k('race') and s[1] == 'Sent to Slack again']
            and node == 'Run Failed?' and ex['status'] == 'success', f"{row['status']}, ended at {node}")

    # ------------------------------------------------------------ the error alert workflow
    static_failed = sorted((e['id'] for e in r.executions() if e.get('status') == 'error'), key=int)
    linked, got = alerts_for(static_failed)
    expected = sorted([r.ids[n] for n in ('bad_secret', 'run1', 'run2', 'forged', 'swapped', 'lost', 'attachfail', 'slackdown', 'racerun')], key=int)
    r.check('Error alert workflow', 'every failed run raised exactly one Slack alert with its execution link, and no other alerts',
            static_failed == expected and all(len(v) == 1 for v in linked.values()) and len(got) == len(static_failed), f'{len(static_failed)} failed runs, {len(got)} alerts')
    a1 = (linked.get(r.ids['run1']) or [{'body': {'text': ''}}])[0]['body']['text']
    a2 = (linked.get(r.ids['run2']) or [{'body': {'text': ''}}])[0]['body']['text']
    r.check('Error alert workflow', 'the alerts name the node and the reasons (Claude outage and label change; QuickBooks reconnect)',
            f'*{TEMP_NAME}* failed at *Stop: Run Incomplete*' in a1 and 'Claude did not read it' in a1 and 'Gmail did not move email' in a1
            and 'open it in n8n and reconnect it' in a2, a2.splitlines()[1][:100] if a2.count(chr(10)) else 'no alert')
    r.check('Error alert workflow', 'email senders and subjects in an alert are escaped for Slack, so a crafted name cannot ping the channel',
            '&lt;orders@lakesideprint.example.com&gt;' in a1 and '<orders@' not in a1, '')

    # ------------------------------------------------------------ schedule switched off in this copy
    timed = [e for e in r.executions() if e.get('mode') == 'trigger' and e.get('stoppedAt')]
    end = time.time() + 75
    while not timed and time.time() < end:
        time.sleep(2)
        timed = [e for e in r.executions() if e.get('mode') == 'trigger' and e.get('stoppedAt')]
    ends = [r.stopped_at(e)[0] for e in timed]
    r.check('Schedule with SCHEDULE_ENABLED=false', 'scheduled runs stop at "Schedule On?" and read nothing',
            timed and all(n == 'Schedule On?' for n in ends) and all(e['status'] == 'success' for e in timed), f'{len(timed)} scheduled run(s)')


def run_bad_settings(results, error_wid):
    env = {**ENV, 'APPROVAL_SECRET': 'too-short', 'QBO_EXPENSE_ACCOUNT_ID': '', 'ANTHROPIC_MODEL': 'claude-opus-5-5', 'VENDOR_ALIASES': '{not json'}
    wid = import_active(env, '-3', error_wid)
    r = Runner(wid, '-3')
    try:
        status, body, ex = r.run()
        node, _ = r.stopped_at(ex)
        try:
            answer = json.loads(body)
        except ValueError:
            answer = body
        problems = ['QBO_EXPENSE_ACCOUNT_ID must be the ID of a QuickBooks expense account (digits only)',
                    'ANTHROPIC_MODEL claude-opus-5-5 does not accept the forced tool call with thinking turned off that this workflow uses (use claude-sonnet-5, claude-opus-5 or claude-haiku-4-5)',
                    'APPROVAL_SECRET must be a random string of at least 32 characters',
                    'VENDOR_ALIASES must be a JSON object such as {"Name on invoices": "QuickBooks vendor name"}']
        results.append(('Run Now with broken settings', 'answered 500 with the exact list of problems, no API call, run ends as failed',
                        status == 500 and answer == {'started': False, 'error': 'settings', 'problems': problems} and not r.new() and node == 'Stop: Bad Settings' and ex['status'] == 'error',
                        f'{status}, ended at {node}' if answer == {'started': False, 'error': 'settings', 'problems': problems} else json.dumps(answer)[:700]))
        linked, _ = alerts_for([ex['id']])
        text = (linked[ex['id']] or [{'body': {'text': ''}}])[0]['body']['text']
        results.append(('Run Now with broken settings', 'the error alert names every problem',
                        len(linked[ex['id']]) == 1 and 'failed at *Stop: Bad Settings*' in text and 'Settings are not valid, so no email was read. ' + '. '.join(problems) + '.' in text,
                        text.splitlines()[1][:90] if text.count('\n') else 'no alert'))
        status, html, ex = r.open_link(link_for('a1b2c3d4e5f60001-1', 'approve', int(time.time()) + 3600, f'{N8N}/webhook/{SLUG}-e2e-approval-3'))
        node, _ = r.stopped_at(ex)
        results.append(('Approval link with broken settings', 'refused with a clear page (503), nothing read, run fails for an alert',
                        status == 503 and page_title(html) == 'Approvals are not set up correctly' and not r.new() and node == 'Stop: Approval Problem', f'{status}'))
    finally:
        remove(wid)


def run_schedule(results, error_wid, tid):
    add_message(M['schedule'], 'Northwind Billing <billing@northwind.example.com>', 'Invoice INV-20455', ['northwind-20417.pdf'], 1)
    mark = len(LOG)
    wid = import_active({**ENV, 'SCHEDULE_ENABLED': 'true', 'LEDGER_TABLE_ID': tid}, '-2', error_wid)
    r = Runner(wid, '-2')
    try:
        end, timed = time.time() + 150, []
        while time.time() < end:
            timed = [e for e in r.executions() if e.get('mode') == 'trigger' and e.get('stoppedAt')]
            if timed:
                break
            time.sleep(2)
        node, _ = r.stopped_at(timed[0]) if timed else ('none', '')
        flow = LOG[mark:]
        rows = ledger(tid)
        results.append(('Schedule with SCHEDULE_ENABLED=true', 'the scheduled run picks up the new email by itself; nothing else is read again',
                        len(timed) >= 1 and rows.get(f"{M['schedule']}-1", {}).get('status') in ('awaiting_approval', 'duplicate')
                        and [fixture_of(e) for e in claude_calls(flow)] == ['northwind-20417'] and labels_of(M['schedule']) == {L_DONE},
                        f'{len(timed)} scheduled run(s), ended at {node}'))
        dup = slack_for(flow, 'INV-20417')
        results.append(('Schedule with SCHEDULE_ENABLED=true', 'the same invoice arriving again in a new email is caught as a duplicate (it is already posted)',
                        rows.get(f"{M['schedule']}-1", {}).get('status') == 'duplicate' and dup and 'Duplicate invoice skipped' in dup[0], dup[0].splitlines()[0] if dup else 'no message'))
    finally:
        remove(wid)


def main():
    if not KEY:
        raise SystemExit('Set N8N_API_KEY first.')
    wfs, tables = remove_leftovers()
    if wfs or tables:
        print(f'Removed {wfs} temporary workflow(s) and {tables} data table(s) left by an interrupted run.')
    seed()
    server = MockServer(('127.0.0.1', MOCK_PORT), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    results = []
    tid = create_table()
    try:
        ENV['LEDGER_TABLE_ID'] = tid
        error_wid = import_error_workflow()
        try:
            wid = import_active(ENV, '', error_wid)
            r = Runner(wid)
            try:
                run_main(r, tid)
            finally:
                results = r.results
                remove(wid)
            run_bad_settings(results, error_wid)
            run_schedule(results, error_wid, tid)
        finally:
            remove(error_wid)
    finally:
        api('DELETE', f'/api/v1/data-tables/{tid}')
        server.shutdown()
        if CONNECTION_ERRORS:
            print(f'Mock connection problems: {CONNECTION_ERRORS[:5]}', flush=True)
        if os.environ.get('SAVE_REQUESTS'):
            def short(e):
                body = copy.deepcopy(e['body'])
                out = {}
                if e['service'] == 'claude' and isinstance(body, dict):
                    out['fixture'] = fixture_of(e) if e['status'] != 400 else ''
                    for block in body.get('messages', [{}])[0].get('content', []):
                        if block.get('type') == 'document':
                            block['source']['data'] = block['source']['data'][:60] + '...'
                if isinstance(body, str) and len(body) > 1500:
                    body = body[:1500] + '...'
                return {'service': e['service'], 'method': e['method'], 'path': e['path'], 'query': e['query'], 'body': body, 'status': e['status'], **out}
            Path(os.environ['SAVE_REQUESTS']).write_text(json.dumps([short(e) for e in LOG], indent=1), encoding='utf-8')

    results = [(sc, lb, bool(ok), dt) for sc, lb, ok, dt in results]
    passed = sum(ok for *_, ok, _ in results)
    last = None
    for scenario, label_text, ok, detail in results:
        if scenario != last:
            print(f'\n{scenario}')
            last = scenario
        print(f"  {'PASS' if ok else 'FAIL'}  {label_text}" + (f'  ({detail})' if detail else ''))
    print(f'\n{passed}/{len(results)} checks passed')
    sys.exit(0 if results and passed == len(results) else 1)


if __name__ == '__main__':
    main()
