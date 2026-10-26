# Changelog

## v1.1 (October 26, 2026)

A review of v1 read the workflow against Intuit's, Google's and Anthropic's documentation and played races the first
tests did not. Four problems were confirmed and fixed, each with a test, and the open question about the PDF upload was
settled.

### Fixed

- **Bills had no `CurrencyRef`.** Intuit's Bill reference marks `CurrencyRef` "Required if multicurrency is enabled for
  the company", and the workflow never sent it, so a multicurrency company could refuse every approved bill. The bill now
  carries the vendor's own currency as QuickBooks reports it, which the checks already match to the invoice. The mock
  company now has multicurrency switched on and refuses a bill without `CurrencyRef` or in another currency than the
  vendor's, and a new test posts an EUR invoice for a vendor set up in EUR.
- **A PDF without a filename could never be approved.** Gmail reports an empty `filename` for a part that has none, as
  some scanners and billing systems send them. Intake read such a PDF and saved it as `invoice.pdf`, but Approve looked
  the PDF up by that name in the email, never found it, and said "The PDF is gone". Approve now finds the part by its
  Gmail part ID, which never changes. The test email whose bill answer gets lost now carries a PDF without a filename.
- **A run could overwrite a decision made from an alert's links.** When Slack refused an approval request, the alert
  carried the Approve and Reject links, and the next run sent the request again. If someone used those links while that
  run was working, the run saved its row over the decision: a posted invoice went back to waiting, with a fresh Slack
  request whose Reject would have marked a posted bill as rejected. A row the run read at its start is now saved with a
  conditional update that only applies while the row is unchanged; otherwise the decision stands and Slack is not asked
  again. New test.
- **The run lock could run out during a slow run.** The lock lasted 30 minutes from the start of the run, but one PDF
  whose Claude call times out three times takes about 9 minutes, so a few slow PDFs were enough for the next scheduled
  run to start beside it and send second approval requests. The lock is now renewed after every PDF. New check.

### Settled: the Attachable upload

Intuit's upload tutorial shows the file part base64 encoded with `Content-Transfer-Encoding: base64`, while n8n sends
raw binary parts. Intuit's own Java SDK (QuickBooks-V3-Java-SDK) builds the upload by appending the file's bytes
unencoded after `Content-Disposition` and `Content-Type` part headers, with no `Content-Transfer-Encoding`, which is the
form n8n sends; the tutorial's second example has no such header either. The workflow is unchanged and the mock keeps
accepting both forms, as the endpoint does.

### Checked and found correct

- Error items from n8n's error outputs carry the input item's fields (checked in n8n's `workflow-execute` source), so
  the nodes that read `stage` from a failed Gmail call get it.
- Intuit's query guide confirms backslash escapes, values compared without regard to case, and only active name list
  entities by default. Anthropic's model rules match the models the workflow refuses, and the cost section's prices
  match Anthropic's current list.

## v1 (October 26, 2026)

First build: supplier invoices from a Gmail label are read by Claude, checked in code and against QuickBooks Online,
approved or rejected by a person from Slack, then entered as QuickBooks bills with the original PDF attached. Built in
n8n, with an error alert workflow and an end to end test against strict mocks.

### Reading invoices

- A 15 minute schedule (`SCHEDULE_ENABLED`) and a Run Now webhook (`X-Run-Secret`) share one start: **Load Settings**
  checks every setting before any API call, and Run Now answers 202, or 401, or 500 with the list of problems.
- Gmail labels are looked up by name (Gmail changes labels by ID). Emails with the To Process label are read oldest
  first, at most `MAX_MESSAGES_PER_RUN` new ones per run.
- An n8n data table is the ledger: one row per PDF (or per email without a PDF). PDFs whose row is final or still
  waiting for approval are skipped, so a repeat run never reads or announces anything twice.
- Claude reads each PDF (base64 document block before the text, email details in tags) through a forced, strict tool
  call. Its answer is validated again in code. A reply without the tool call, one cut off by `max_tokens`, an
  unreadable scan, a document that is not a bill or a PDF with several invoices goes to a person.
- Checks in code: required fields, lines plus tax against the total (within a cent), the printed subtotal, invoice and
  due dates, currency against the vendor's, negative lines, the 21 character limit for QuickBooks bill numbers. The
  vendor is queried by display name (with `VENDOR_ALIASES`), and a bill with the same vendor and number makes it a
  duplicate, in QuickBooks or among invoices already waiting or posted from other emails.
- The ledger row is saved before Slack hears about the invoice, so every link points at a saved invoice. One Gmail
  label per email once its last PDF is handled.

### Approving

- Approve and Reject links are signed with HMAC SHA-256 over the invoice key, the action and the expiry
  (`APPROVAL_SECRET`, `APPROVAL_LINK_DAYS`). The signature is compared in constant time. Forged links are refused and
  raise an alert; expired links are refused; a decided invoice only shows what happened.
- Opening a link only shows a confirmation page. The decision is a POST from the button on that page, with the person's
  name (and a reason for a reject), so link previews and mail scanners cannot approve anything.
- Approve claims the ledger row in one conditional update, checks QuickBooks again (vendor added since, bill entered by
  hand since), creates the bill with a request ID, attaches the PDF through the Attachable upload, then records the
  result, moves the email label, adds a Sheets row and posts a Slack note.
- Anything that stops an approval before the bill exists puts the invoice back to waiting, so the same link works once
  the problem is fixed. The workflow never creates vendors.

### Failures

- Claude and QuickBooks calls retry inside the run with exponential backoff and honor Retry-After; a QuickBooks 429
  without Retry-After waits `QBO_RATE_LIMIT_WAIT_SECONDS` (60, as Intuit asks). QuickBooks 401 and 403 are not retried
  and fail the run with what to fix.
- A PDF that could not be finished stays in To Process with its reading saved and is tried again on the next run, up to
  `MAX_EXTRACT_ATTEMPTS` runs; then it goes to a person.
- Gmail, Google Sheets and Slack calls use n8n's built-in retry (3 tries, 5 seconds apart) and an error output.
- Every run that could not finish everything ends in a Stop and Error node, so the error workflow posts it to Slack.

### Design decisions made while building

- **OAuth stays in n8n credentials.** Gmail, Google Sheets and QuickBooks use n8n's OAuth2 credentials. Keeping refresh
  tokens in workflow memory was ruled out: Intuit rotates the QuickBooks refresh token and revokes the whole
  authorization when an already rotated token is used again, and n8n's workflow memory can lose a write when two runs
  overlap. n8n's credential store keeps the newest token, encrypted.
- **The ledger is a data table, not workflow memory.** Approvals and the 15 minute runs can overlap. n8n saves workflow
  static data as one block at the end of a run, so one run can overwrite another's changes; data table rows are
  updated one at a time, and the approval claim is a single conditional update.
- **The ledger table is read by ID.** n8n's "by name" lookup for data tables matches part of a name (an `invoice_ledger`
  setting would also find `invoice_ledger_old`), so the workflow takes `LEDGER_TABLE_ID`.

### Found and fixed while testing

- **Per email Gmail calls went to a literal path.** URLs with `{{ }}` were not marked as expressions, so every
  message, attachment and label call asked for `/messages/{{ $json.messageId }}` and got 404. The first test run caught
  it, and it also showed the failure path working: each email was retried 3 times, kept in To Process, and the run
  failed with every message ID listed.
- **The PDF upload was re-encoded by n8n.** The first version built the multipart body itself and sent it raw. n8n's
  request helper turns any body with a `multipart/form-data` content type into form fields, so QuickBooks would have
  received one field per character. The upload now uses n8n's own multipart support with two binary parts: the
  Attachable metadata as `application/json` named `attachment.json`, and the PDF.
- **Error alerts could carry Slack markup.** Failure messages quote email senders, subjects and Claude's reading, and the
  error workflow posts them to Slack unchanged. A sender name with `<!channel>` would have pinged the channel. Failure
  messages are now escaped (`&`, `<`, `>`) like the other Slack messages, and the test checks an alert.
- **An old link of an invoice that was being checked again said it went to a person.** It now says the invoice is
  not ready yet and new links will come to Slack.

### Found and fixed in a logic review

A second pass read the workflow for gaps the tests did not cover yet. The Slack resend, the run lock, the email with
two PDFs, the interrupted approval and the missing currency have their own tests now; the other fixes run inside the
existing scenarios.

- **A Slack request that never arrived was never sent again.** When Slack refused the message, the invoice was saved as
  waiting and the run failed with the links in the alert, but nothing ever re-sent it. The ledger now has a
  `notifiedAt` column, set only when Slack accepts the message; the email stays in To Process until then, and the next
  run sends the request again, marked as sent again.
- **Two runs at once could read the same PDF twice.** A Run Now call during a scheduled run could read and announce the
  same invoices again. A lock row in the ledger is now taken with one conditional update after the emails are listed
  and released at the end of the run; a run that finds it held stops without touching anything. A lock left by a
  crashed run runs out after 30 minutes.
- **An email with two PDFs could leave To Process too early.** Approving the first PDF while the second had no ledger
  row yet moved the email out of To Process, so the second PDF would never be read. Each row now records how many PDFs
  the email has, and the email keeps its label until every one has a row.
- **An approval interrupted by n8n stopping stayed stuck.** The row stayed `approving` and every link refused. After 10
  minutes the Approve link now offers to finish it; QuickBooks is searched first, so a bill that was saved gets the PDF
  instead of a second bill. The Reject link refuses such an invoice, because the bill may already exist.
- **One QuickBooks request ID for every approval attempt.** QuickBooks answers a repeated request ID with its first
  answer, errors included, so a second attempt after a fixed problem could get the old error back. The request ID now
  carries one ID per attempt; retries inside an attempt keep it.
- **The approval claim now checks the whole row.** The claim only applies while the row is exactly as the page read it
  (status and saved data), so an old link cannot claim an invoice whose links were renewed in between.
- **An invoice that prints no currency was blocked.** Many domestic invoices show only `$`. It is now treated as the
  vendor's currency (or the home currency) with a warning. An invoice in another currency from a vendor that is not in
  QuickBooks yet gets a warning, and Approve checks it against the vendor once the vendor exists.
- **Expired requests counted as duplicates.** An invoice whose links expired unused made the same invoice in a new
  email a duplicate, so nobody could approve either. Expired waiting rows no longer count.
- **Smaller fixes.** The currency is escaped in Slack text like every other value read from a PDF. A Retry-After from
  QuickBooks is capped at 120 seconds. `MAX_PDF_MB` is capped at 20, because a 30 MB PDF becomes 40 MB as base64 and
  Anthropic takes 32 MB per request. A label change is skipped when the email already has the right label.
