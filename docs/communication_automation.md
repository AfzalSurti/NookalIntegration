# Section 4.6: Nookal-Native SMS + Email Communication Automation

## 1. Executive Summary & Contractual Context

Section 4.6 of the Back to Ease Physiotherapy Phase 1 Agreement mandates:

> *Configure WhatsApp, SMS and email through approved/technically supported methods. Implement owner/admin/staff/practitioner roles and permissions. Use the Client's existing patient-facing number where technically and lawfully supported. Templates and signatures must be configurable.*

### Key Strategic & Architectural Directives
1. **Strictly Zero Third-Party Messaging Providers**: Per Section 4.6 requirements, no external communication gateways (e.g., Twilio, Vonage, MessageBird, SendGrid, Mailgun, AWS SES, or arbitrary SMTP services) are introduced. All patient-facing appointment communications leverage Nookal's official, built-in communication facilities.
2. **WhatsApp Scope**: WhatsApp is explicitly out of scope for Phase 1.
3. **Primary Patient-Facing Number**: All SMS communications originate from the clinic's registered patient-facing number configured directly within Nookal PMS settings, preserving familiar sender recognition.
4. **Guaranteed Duplicate Prevention**: Automated safeguards ensure that patients never receive duplicate reminders or conflicting booking notices from both Nookal and the application.

---

## 2. Nookal Technical Capability Assessment

A rigorous audit of the official Nookal API v2 documentation and operational platform confirms that **Nookal does not expose programmatic API endpoints for sending SMS or email messages** (e.g., no `/sendSMS`, `/sendEmail`, or `/sendMessage`).

Instead, Nookal operates on a native automation paradigm where communications are defined, scheduled, and dispatched internally by Nookal's cloud engine based on appointment events and administrative settings.

### Native PMS Capabilities vs. Official API v2 Surface

| Feature / Workflow | Nookal Native Platform | Nookal API v2 | Responsibility & Implementation |
|---|---|---|---|
| **Appointment Confirmation SMS** | ✅ Supported (`Manage > Communications`) | ❌ No API | **Nookal Native**: Dispatched automatically upon booking |
| **Appointment Confirmation Email** | ✅ Supported (`Manage > Communications`) | ❌ No API | **Nookal Native**: Dispatched automatically upon booking |
| **Appointment Reminder SMS** | ✅ Supported (`Manage > Communications > Reminders`) | ❌ No API | **Nookal Native**: Scheduled 24–48h prior to appointment |
| **Appointment Reminder Email** | ✅ Supported (`Manage > Communications > Reminders`) | ❌ No API | **Nookal Native**: Scheduled 24–48h prior to appointment |
| **Appointment Cancellation Notice** | ✅ Supported | ❌ No API | **Nookal Native**: Dispatched on appointment cancellation |
| **Reschedule Notification** | ✅ Supported (rebook triggers new confirmation) | ❌ No API | **Nookal Native**: Handled via updated appointment lifecycle |
| **Follow-up / Recall Campaign** | ✅ Supported (`Manage > Communications`) | ❌ No API | **Nookal Native**: Scheduled via recall filters |
| **SMS Credit & Auto-Top-Up** | ✅ Supported (`Setup > Account > Billing`) | ❌ No API | **Nookal Admin Portal** |
| **Patient Phone & Email Lookup** | ✅ Supported | ✅ Supported (`getPatients`, `searchPatients`) | **Application Integration**: Validates contact readiness |
| **Email Reminder Sent Observation** | ✅ Supported | ✅ Supported (`getAppointments` field: `emailReminderSent`) | **Application Integration**: Observes native reminder status |
| **No-Show Messaging via API** | ❌ Unverified | ❌ No API | **Limitation Reported**: Classified as `OFFICIAL_API_UNSUPPORTED` |
| **Two-Way Conversational SMS** | ❌ Not available | ❌ No API | **Limitation Reported**: Classified as `NOT_AVAILABLE` |

---

## 3. Duplicate Prevention Architecture

When Nookal's native reminders are active, running an external reminder engine creates a critical risk of duplicate messages (e.g., patient receives two SMS reminders for the same appointment).

To eliminate duplicate messaging:

1. **Default Deferral to Nookal Native**:
   `config/settings.yaml` sets `app_reminders_enabled: false`. When `AppointmentRemindersWorkflow` runs for SMS or Email channels, it verifies Nookal native capability and defers message delivery to Nookal, logging `appointment_reminders.nookal_native_deferred`.
2. **Per-Appointment State Observation**:
   Before processing any appointment, the application inspects the `emailReminderSent` flag and status flags (`cancelled`, `dna`, `arrived`). If Nookal has already transmitted an email reminder or if the appointment status is ineligible, the application automatically skips it (`nookal_already_handled`).
3. **Idempotent State Tracker (`CommunicationStateStore`)**:
   An append-only, thread-safe JSONL store (`data/working/communication_state/communication-state.jsonl`) tracks all processed appointment workflows, guaranteeing idempotency across repeated runs.

---

## 4. Templates, Signatures & Clinic Sender ID

### Configurable Templates
Templates are maintained within Nookal's official template editor under:
> **Manage > Communications > Communication Templates**

Supported dynamic merge placeholders include:
- `{PatientFirstName}`, `{PatientLastName}`, `{PatientTitle}`
- `{AppointmentDate}`, `{AppointmentTime}`, `{LocationName}`, `{LocationAddress}`
- `{PractitionerName}`, `{PractitionerTitle}`

### Practitioner Email Signatures
Configured per practitioner under:
> **Setup > Practice Admin > Practitioners > [Practitioner Profile] > Email Signature**

### Clinic Patient-Facing Number
Configured under:
> **Setup > Practice Admin > Locations > [Location Profile] > SMS Sender ID**
- Uses the clinic's approved mobile or registered landline number.
- No new or unfamiliar phone numbers are displayed to patients.

---

## 5. Role-Based Access Control (RBAC)

Section 4.6 requires granular permissions across `owner`, `admin`, `staff`, and `practitioner` roles:

| Permission | Description | Owner | Admin | Staff | Practitioner |
|---|---|:---:|:---:|:---:|:---:|
| `communication:view` | View communication status, capability matrix, and patient readiness | ✅ | ✅ | ✅ | ✅ |
| `communication:manage` | Modify communication config overrides and workflow bindings | ✅ | ✅ | ❌ | ❌ |

- **Practitioners & Staff** have read-only visibility to assist patients with reminder inquiries.
- **Admins & Owners** retain full management and configuration authority.
- Unauthorized roles (e.g. guests, billing clerks) are blocked with `403 Forbidden` / `AuthorizationError`.

---

## 6. Dashboard Communication Interface

A dedicated dashboard portal is available at `/communication` (accessible via **Workflows > Communication**):

1. **Status Cards**:
   - **Nookal-Native SMS**: Real-time status, sender ID verification, and credit guidance.
   - **Nookal-Native Email**: Active state, sender domain verification.
   - **Duplicate Prevention**: Confirms active protection against double sends.
   - **Zero Third-Party Guarantee**: Verifies strict exclusion of Twilio/SendGrid.
2. **Capability Matrix Table**:
   - Lists every clinical workflow (`Confirmation`, `Reminder`, `Cancellation`, `Reschedule`, `Recall`, `No-Show`).
   - Clearly labels ownership (`Nookal Native`, `Application Controlled`, `Official API Unsupported`).
3. **Interactive Patient Readiness Checker**:
   - Allows staff to enter a Patient ID (e.g., `pat_1001`) to query `/api/communication/patient/{id}/readiness`.
   - Returns instant validation of SMS readiness (valid phone present + enabled) and Email readiness (valid email present + enabled).

---

## 7. OpenClaw & Orchestration Workflows

Deterministic workflows in `app/orchestration/communication.py` provide safe, observable integration hooks for scheduled tasks (cron/launchd) and OpenClaw:

- `CheckCommunicationConfigWorkflow` (`check_communication_config`): Returns system-wide capability matrix and settings audit.
- `CheckPatientReadinessWorkflow` (`check_patient_readiness`): Validates patient contact completeness given `patient_id` or `phone`.
- `CheckAppointmentCommunicationWorkflow` (`check_appointment_communication`): Evaluates whether Nookal has sent reminders for a specific appointment.

### Safety & Compliance Boundaries
- OpenClaw never sends raw outbound SMS or email directly.
- OpenClaw never stores or bypasses patient consent flags.
- Audit events record operation metadata without storing message bodies or raw clinical content.
