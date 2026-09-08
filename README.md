# Back to Ease — Phase 1 core

Python packages for the clinic Mac automation stack (days 3–10 scaffolding).

| Package | Role |
|---|---|
| `app.nookal_client` | Nookal read/write wrapper + mock |
| `app.approval` | Human approval queue (sole gate for sends/writes from drafts) |
| `app.shared.audit` | Append-only audit log |
| `app.llm_service` | Local LLM draft + intent parse (no write/send imports) |
| `app.messaging` | WhatsApp / SMS / email adapters + idempotency |
| `app.shared.kill_switch` | Immediate halt for writes and outbound sends |

## Layout

```
app/
  nookal_client/     # generic — retained IP
  approval/
  llm_service/
    prompts/         # editable prompt text, not buried in code
  messaging/
    adapters/
    templates/       # client-approved message templates
  shared/            # config, audit, kill-switch
config/
  settings.yaml
  .env.example
```

## Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS:  source .venv/bin/activate
pip install -e ".[dev]"
cp config/.env.example config/.env
pytest
```

Kill switch:

```bash
python -m app.shared.kill_switch on --reason "incident"
python -m app.shared.kill_switch status
python -m app.shared.kill_switch off
```

## Design rules baked into the code

1. `llm_service` does not import `nookal_client` or `messaging`.
2. `approval.approve()` is the only path that runs registered side-effect handlers.
3. Every Nookal write and every `messaging.send` checks the kill switch first.
4. Audit metadata rejects names, bodies, and long free-text — IDs and action types only.
5. Nookal HTTP paths marked `TODO` until official docs are confirmed — the mock client is what tests run against.

## Phase A test foundation

- Synthetic seed: `data/testdata/clinic_seed.json` via `app.nookal_client.seed`
- Controllable messaging: `app.messaging.adapters.fake.FakeAdapter`
- Injectable clock: `app.shared.clock` (`FrozenClock` / `SystemClock`)
- Workflow fixtures: `tests/helpers` + `conftest.py` (`workflow_ctx`, `seeded_nookal`, …)
- Default `pytest` is offline (`-m "not external"`); live integrations use `@pytest.mark.external`

## Phase B orchestration

- Package: `app/orchestration` — `WorkflowContext`, `BaseWorkflow`, `WorkflowResult` / `WorkflowStatus`
- Correlation IDs on every workflow audit event
- Depends on injected services (nookal / messaging / approval / optional llm) — not globals
- No OpenClaw imports in business logic; schedulers/agents call into this layer later

## Phase C workflows (offline / mock)

- Reminders, check/reschedule/cancel/create appointments, certificates, referral thank-you drafts, referrer sync
- Explicit confirmation for appointment-changing actions; idempotent reminders; retry on transient send failures
- E2E tests under `tests/e2e/` (still offline; `@pytest.mark.external` reserved for live providers)

## Phase D documents (offline)

- Package `app/letters` — templates, validation, deterministic PDF renderer, in-memory store/delivery
- `DocumentApprovalHandler` registers on ApprovalQueue for certificate + letter tasks
- Treatment completion / progress letters use the same pipeline
- Real Nookal document upload and live messaging delivery remain deferred

## Phase E dashboard (offline)

Human control plane: FastAPI API + server-rendered pages under `app/dashboard`.

```bash
# credentials via env (never hardcoded in source)
set DASHBOARD_DEV_USER=admin
set DASHBOARD_DEV_PASSWORD=choose-a-local-password
python -m app.dashboard
# → http://127.0.0.1:8080
```

Browser → Dashboard API → auth → authorization → application services → existing Phase A–D components.
Security notes: `docs/dashboard_security.md`. Not production-ready IAM.

## Still blocked on client deps (Section 15)

Nookal credentials/tier, WhatsApp Business API on the clinic number, SMS/email accounts, letter templates, and written India-access authorisation. Do not invent those payloads; fill TODOs from real docs when they arrive.
