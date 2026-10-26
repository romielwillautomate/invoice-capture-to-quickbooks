# Setup: Gmail, QuickBooks Online, Claude, Slack, Google Sheets and n8n

You need self-hosted n8n 2.x (it has data tables) reachable over HTTPS, because the approval links in Slack open n8n
pages. You also need a Google account for the invoice mailbox, a QuickBooks Online company where you are an admin, an
Intuit developer account, an Anthropic API key and a Slack channel. Plan about an hour, most of it for the two OAuth apps.

Try everything on a QuickBooks **sandbox** company first. The workflow only creates bills when a person presses Approve,
but a sandbox is where you want to see the first ones.

## 1. Gmail

### Labels

Create these five labels in Gmail (nested labels use a slash, so "Invoices/To Process" sits under "Invoices"). You can
use other names; then set them in the `GMAIL_LABEL_` settings.

| Label | Who sets it | Meaning |
|---|---|---|
| `Invoices/To Process` | you or a Gmail filter | Emails the workflow should read. It is the only label the workflow looks at |
| `Invoices/Awaiting Approval` | the workflow | Read, at least one invoice waits for Approve or Reject in Slack |
| `Invoices/Processed` | the workflow | Every invoice in it is posted, or was a duplicate |
| `Invoices/Rejected` | the workflow | Every invoice in it was rejected |
| `Invoices/Needs Review` | the workflow | Something needs a person (unreadable scan, no PDF, not a bill) |

To label supplier emails automatically, add a Gmail filter (**Settings > Filters and blocked addresses > Create a new
filter**), for example `has:attachment filename:pdf to:(ap@yourcompany.com)` or a list of supplier addresses, with
**Apply the label: Invoices/To Process**. You can also add the label by hand.

### Gmail credential in n8n

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project (or use one you have).
2. **APIs & Services > Library**: enable the **Gmail API** and the **Google Sheets API**.
3. **APIs & Services > OAuth consent screen**: set it up (Internal is fine for a Google Workspace account; for a
   personal Gmail account choose External and add yourself as a test user).
4. **APIs & Services > Credentials > Create credentials > OAuth client ID**, type **Web application**. Under
   **Authorized redirect URIs** paste the OAuth redirect URL that n8n shows when you create the credential in the next
   step (it looks like `https://YOUR-N8N-DOMAIN/rest/oauth2-credential/callback`).
5. In n8n, **Credentials > Create credential > Gmail OAuth2 API**. Paste the client ID and client secret, press
   **Sign in with Google** and pick the invoice mailbox.

The workflow reads messages and attachments and changes labels. It never sends, deletes or archives mail. (n8n's Gmail
credential asks Google for wide scopes by default; the calls this workflow makes are covered by `gmail.modify`.)

## 2. Google Sheets (optional log)

1. Create a spreadsheet with a tab named **Invoice log** (or set `GOOGLE_SHEET_TAB`) and this header row in row 1:

   `Logged at | Event | Status | Vendor | Invoice number | Invoice date | Due date | Currency | Total | Problems | QuickBooks bill | Decided by | Note | From | Subject | Ledger key`

2. Copy the spreadsheet ID from its URL (`https://docs.google.com/spreadsheets/d/THIS_PART/edit`) into `GOOGLE_SHEET_ID`.
3. In n8n, create a **Google Sheets OAuth2 API** credential with the same Google OAuth client and sign in with an
   account that can edit the sheet.

Rows are written with `valueInputOption=RAW`, so a vendor name or line that starts with `=` stays text and never runs
as a formula. Leave `GOOGLE_SHEET_ID` empty to skip the sheet; the ledger in n8n still has every result.

## 3. QuickBooks Online

### Intuit app and credential

1. Sign in at [developer.intuit.com](https://developer.intuit.com/) and create an app with the
   **QuickBooks Online Accounting** scope (`com.intuit.quickbooks.accounting`).
2. Under **Keys and credentials**, use the **Development** keys for a sandbox company. Production keys need Intuit's app
   assessment first.
3. Add the n8n OAuth redirect URL (`https://YOUR-N8N-DOMAIN/rest/oauth2-credential/callback`) to the app's redirect URIs.
4. In n8n, **Credentials > Create credential > QuickBooks Online OAuth2 API**. Paste the client ID and secret, pick
   **Sandbox** or **Production** to match, press **Connect my account** and choose the company. The connecting user
   must be a QuickBooks admin.

n8n refreshes the access token by itself and keeps Intuit's rotating refresh token in its encrypted credential store.
If the connection is ever revoked, the run fails with "QuickBooks refused the connection (401)... reconnect it": open the
credential and press **Connect my account** again.

### Settings

| Setting | Where to find it |
|---|---|
| `QBO_BASE_URL` | `https://sandbox-quickbooks.api.intuit.com` for a sandbox, `https://quickbooks.api.intuit.com` for production. It must match the credential's environment |
| `QBO_APP_URL` | `https://app.sandbox.qbo.intuit.com` or `https://app.qbo.intuit.com`, used for "Open the bill" links |
| `QBO_REALM_ID` | The company ID. Sandbox: shown on the developer dashboard under **Sandbox**. Production: in QuickBooks, **Settings (gear) > Account and settings > Billing & subscription > Company ID** |
| `QBO_EXPENSE_ACCOUNT_ID` | The expense account bills go to. Open it from **Chart of accounts** (**Account history**) and copy `accountId=` from the address bar. Or run `select Id, Name from Account where AccountType = 'Expense'` in Intuit's API Explorer |
| `QBO_TAX_ACCOUNT_ID` | The account for the invoice's tax amount, for example an expense account called "Sales tax paid". Leave it empty to use the expense account |
| `HOME_CURRENCY` | Your company's home currency (default USD) |
| `VENDOR_ALIASES` | Optional JSON: `{"Name as printed on invoices": "QuickBooks vendor display name"}` |

### Vendors

The workflow looks vendors up by **display name** (QuickBooks compares the text without regard to case) and never
creates one. An invoice from a vendor that is not in QuickBooks still goes to Slack, marked "not found"; Approve then
stops and asks you to add the vendor first, and the same link works once you have. If suppliers print a longer legal
name than your QuickBooks display name, map it in `VENDOR_ALIASES`.

Each bill is entered in the vendor's currency as QuickBooks has it (the bill's `CurrencyRef`, which QuickBooks requires
once multicurrency is on). An invoice in another currency than its vendor's cannot be approved and goes to a person.

## 4. Claude

Create a key at **console.anthropic.com > API keys** and put it in `ANTHROPIC_API_KEY`. The default model is
`claude-sonnet-5`. `claude-haiku-4-5` (cheaper) and `claude-opus-5` also work. The request forces a tool call with
thinking turned off, so the Claude Fable and Mythos models and `claude-opus-5-5` cannot be used: `claude-opus-5-5` and
`claude-fable-5-1` refuse the forced tool call, and the Fable and Mythos models and `claude-opus-5-5` refuse thinking
turned off. A run refuses to start with one of them and says why.

PDFs are sent as base64 document blocks. Anthropic accepts up to 32 MB per request and 100 pages on models with a
context window under 1M tokens (600 pages otherwise). Base64 makes a file about a third larger, so `MAX_PDF_MB` (default
10) can be set to 20 at most.

## 5. Slack

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) (**From scratch**), open **Incoming
   Webhooks**, switch them on and **Add New Webhook to Workspace** for the channel where approvals should appear.
2. Copy the webhook URL into `SLACK_WEBHOOK_URL`. The error alert workflow posts to the same URL.

Anyone in that channel can approve, because the links are the approval. Use a private channel for the people who may
approve bills. Opening a link (or a link preview fetching it) only shows a confirmation page; the bill is posted when
someone types their name and presses the button on that page.

## 6. n8n

1. **The ledger table.** In n8n, open **Overview > Data tables > Create data table** (any name, for example
   `invoice_ledger`) with these columns:

   | Column | Type |
   |---|---|
   | `key` | String |
   | `messageId` | String |
   | `status` | String |
   | `vendor` | String |
   | `invoiceNumber` | String |
   | `total` | Number |
   | `currency` | String |
   | `attempts` | Number |
   | `billId` | String |
   | `decidedBy` | String |
   | `note` | String |
   | `data` | String |
   | `notifiedAt` | String |

   Open the table and copy its ID from the address bar (the part after `/datatables/`) into `LEDGER_TABLE_ID`. The
   workflow reads the table by ID, not by name, because n8n's "by name" lookup matches parts of names.

   The workflow adds one extra row with the key `__intake_lock__`: it stops two runs from reading the same emails at the
   same time. Leave it there. If n8n stops in the middle of a run, the lock runs out by itself after 30 minutes.
2. **Environment.** Copy `.env.example` into your n8n environment, fill it in and restart n8n. The nodes read the
   settings with `$env`, so self-hosted n8n needs `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`. (On n8n Cloud, `$env` is not
   available: replace `$env.` with `$vars.` and create the same names under Variables.)
3. Import `workflow/error-alert-slack.json` and **activate it**. n8n 2.x only runs an active error workflow.
4. Import `workflow/invoice-capture-to-quickbooks.json`. Open each node that shows a credential warning and pick yours:
   - **Gmail OAuth2**: Gmail: List Labels, Gmail: List Messages, Gmail: Get Message, Gmail: Get Attachment,
     Gmail: Relabel, Gmail: Reload Email, Gmail: Download PDF, Gmail: Relabel After Decision
   - **QuickBooks Online OAuth2**: QuickBooks: Query, QuickBooks: Create Bill, QuickBooks: Attach PDF
   - **Google Sheets OAuth2**: Sheets: Append Log, Sheets: Log Decision
5. In the workflow settings, choose the error alert workflow as its **Error Workflow**, then activate the workflow.
6. Set `APPROVAL_BASE_URL` to the production URL of the approval webhook, `https://YOUR-N8N-DOMAIN/webhook/invoice-approval`
   (restart n8n after changing the environment).

The workflow now listens on:

- `https://YOUR-N8N-DOMAIN/webhook/invoice-capture-run` (POST, Run Now)
- `https://YOUR-N8N-DOMAIN/webhook/invoice-approval` (GET opens a confirmation page, POST is its button)

## 7. First run

1. Forward one real (or sample) supplier invoice PDF to the mailbox and give it the `Invoices/To Process` label.
2. Start a run by hand:

   ```bash
   curl -X POST "https://YOUR-N8N-DOMAIN/webhook/invoice-capture-run" \
     -H "X-Run-Secret: YOUR_RUN_NOW_SECRET" -H "Content-Type: application/json" -d '{}'
   ```

   You get `{"started":true}` right away (or a 500 that lists every setting that is wrong).
3. Slack shows the invoice with Approve and Reject links, and the email moves to `Invoices/Awaiting Approval`.
4. Open **Approve**, check the page, type your name and press **Approve and post to QuickBooks**. The page shows the new
   bill with a link to it in QuickBooks, the PDF is attached to the bill, the email moves to `Invoices/Processed`, and
   Slack and the sheet get a note.
5. Leave `SCHEDULE_ENABLED=true` (or unset) and the workflow reads the label every 15 minutes. Change the interval in the
   **Schedule: Every 15 Minutes** node. `SCHEDULE_ENABLED=false` pauses reading; approval links keep working.

## Troubleshooting

Open the workflow's **Executions** list in n8n:

| You see | Likely cause |
|---|---|
| Run stopped at "Stop: Bad Settings" or Run Now answered 500 | The message lists every problem, for example a missing Gmail label, an empty `QBO_REALM_ID` or a short `APPROVAL_SECRET` |
| Run stopped at "Stop: Gmail Failed" | The Gmail credential needs to be reconnected, or the Gmail API is not enabled in the Google project |
| "QuickBooks refused the connection (401)" | The QuickBooks credential expired or was disconnected. Reconnect it in n8n. The invoices wait and are finished by the next run without reading the PDFs again |
| "QuickBooks refused access (403)" | The connected user is not an admin, or `QBO_REALM_ID` is a different company than the credential |
| "Claude did not read it (401 ...)" | Check `ANTHROPIC_API_KEY` and your Anthropic credit balance |
| A run ended at "Lock Route" and did nothing | Another run still held the lock; the Check Lock node names that execution. Two runs never read the inbox at the same time, and a lock left by a stopped run runs out after 30 minutes |
| An invoice is in `Needs Review` | The Slack message says why: an unreadable scan, no PDF, not a bill, more than one invoice in a PDF, or Claude failing on several runs. Enter it in QuickBooks by hand |
| Approve says "Add the vendor first" | No active vendor has that display name. Add it in QuickBooks or map the name in `VENDOR_ALIASES`, then open the same link again |
| "This link has expired" | Move the email back to `Invoices/To Process`. The next run posts new links without reading the PDF again |
| "Posted, but not recorded" | The bill exists, but the ledger update failed. Set that row's `status` to `posted` in the data table so nobody approves it again |
| A run failed but Slack stayed quiet | The error alert workflow is not active, is not chosen as the Error Workflow, or `SLACK_WEBHOOK_URL` is wrong |
