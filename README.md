# Care Coordination Prototype

This is a small, runnable prototype adapted from the presentation's event-driven,
long-running workflow design. The presentation itself is about **new-hire
onboarding**, not medicine. This prototype applies its reusable ideas—explicit
workflow state, persisted checkpoints, event-triggered progress, idempotency,
and audit history—to a narrow care-coordination workflow.

## Scope and safety

The workflow tracks only these operational checkpoints:

1. Referral received
2. Patient contacted
3. Appointment scheduled
4. Follow-up completed

Each checkpoint must be advanced by an authenticated coordinator or a signed
webhook, in order. A human approval is required before follow-up completion.
Cases can be assigned a staff identifier and an internal due date; the dashboard
shows due-today and overdue work without sending reminders or patient messages.
The app does not contact patients, interpret records, diagnose, triage, recommend
care, prescribe medication, or make clinical decisions.

**Demo only. Do not enter real patient information or protected health
information.** This prototype uses shared coordinator and reviewer secrets, not
individual user accounts or a production identity provider, and is not certified or represented as compliant with any
healthcare privacy/security regulation. Production use requires clinical and
privacy review, appropriate identity and access controls, encryption and key
management, retention/backups, threat modeling, incident response, and review of
all applicable legal and regulatory requirements. The reviewer ID is configured
server-side and tied to a separate shared reviewer secret; it is not individual
identity verification and must be replaced with a verified identity provider
before production use.

## Run locally

Requires Python 3.10 or later.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:CARE_API_KEY = "replace-with-a-local-demo-key"
$env:CARE_APPROVER_API_KEY = "replace-with-a-different-reviewer-key"
$env:CARE_APPROVER_ID = "demo-supervisor"
$env:CARE_WEBHOOK_SECRET = "replace-with-a-local-webhook-secret"
uvicorn app:app --reload
```

Open `http://127.0.0.1:8000/`. Enter the coordinator API key in the local UI;
the reviewer separately enters the configured reviewer key when approving.
Keys are held in page memory only. API documentation is available at
`http://127.0.0.1:8000/docs`.

The database defaults to `care-coordinator.db` next to `app.py`. Set
`CARE_DATABASE_PATH` to use another SQLite file. Existing care audit and webhook
tables are reused; the app adds a `care_cases` table without deleting existing
data.

## API

- `GET /health` — liveness check
- `POST /cases` — create an empty, pseudonymous workflow case
- `GET /cases` — list cases
- `GET /cases/{case_id}` — read checkpoint and audit history
- `GET /dashboard` — case totals, internal due/overdue worklist, and per-staff
  active/overdue/due-today/pending-approval counts
- `PATCH /cases/{case_id}/assignment` — update `assigned_to` and/or timezone-aware
  `due_at` (set either field to `null` to clear it)
- `POST /cases/{case_id}/approval` — submit
  `{"decision":"approved"}` or a rejection with a non-empty `reason`; requires
  the separate reviewer credential and is available only at the appointment
  checkpoint. The reviewer ID is assigned from server configuration, not the
  request body.
- `POST /cases/{case_id}/events` — coordinator transition; requires
  `Authorization: Bearer ...` and `Idempotency-Key`
- `POST /webhooks/events` — signed event; requires `X-Webhook-Signature`
  (hex HMAC-SHA256 of the exact request body) and `Idempotency-Key`

Supported events are `patient_contacted`, `appointment_scheduled`, and
`follow_up_completed`. Invalid ordering is rejected rather than inferred or
silently repaired. `follow_up_completed` is blocked unless the latest human
approval decision is `approved`. Rejection is auditable and blocks progression
until an authorized person records a new approval.

The due worklist is a read-time calculation over active cases with a due date
that has arrived. It does not schedule background jobs, send external alerts,
or contact patients.

The dashboard's **View audit history** control shows recorded checkpoint
transitions, assignment changes, and approval decisions for each case. Audit
entries are append-only through the prototype API; database administrators can
still modify the local SQLite file directly.

The dashboard also includes a workflow-stage summary, pending-approval and
unassigned counts, due-soon worklist, per-staff active/overdue/due-today/pending-
approval workload summary (completed cases are excluded), case search,
stage/assignee/due/approval filters,
sorting, and 25-case pagination. **Export filtered CSV** includes only
pseudonymous case IDs and workflow/assignment timestamps; protect exported files
as operational data and never add patient identifiers to them.

API responses include basic browser safety headers and are marked `no-store`.
Signed webhook bodies are streamed with a 16 KiB size limit. These safeguards
are useful for a local prototype but do not replace a production security review.

## Validation

```powershell
python -m pytest -q
```

## What this validates from the planning deck

- State is stored in SQLite and survives process restarts.
- Transition order is enforced in code, rather than left to a model prompt.
- Event delivery is authenticated and deduplicated.
- Each accepted state change has an audit entry.
- Staff assignment, due-date changes, and human approval decisions are audited.
- A recorded approval gates the final workflow checkpoint.
- Dashboard search, filters, progress summary, and internal due worklist expose
  operational workload without adding clinical data or external messaging.
- Per-staff workload totals include unassigned active cases and exclude completed
  cases.
- No periodic polling or autonomous clinical agent is required.

The prototype does not substantiate the deck's token-savings claims: it makes no
LLM calls. Any future comparison must use measured workloads and a reviewed
baseline rather than the presentation's illustrative assumptions.

## Suggested project demo

1. Start the server with the local demo credentials above and open the dashboard.
2. Create a pseudonymous case; show its persisted `referral_received` checkpoint.
3. Assign a staff ID and set a due date in the past to demonstrate the internal
   overdue worklist. No notification is sent.
4. Advance the case through patient contact and appointment scheduling.
5. Attempt follow-up before approval and show that the API rejects it.
6. Enter the separate reviewer key and approve; the server records the
   configured reviewer ID, then complete follow-up.
7. Refresh the dashboard and show the completed count, audit history, and that
   the completed case is no longer in the active due worklist.
