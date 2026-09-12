# Nookal-Native Messaging Architecture & Compliance

## 1. Executive Summary & Policy Compliance
Back to Ease Physiotherapy uses **Nookal Practice Management Software (PMS)** as its sole communication provider for patient SMS and email.

Strict Business Rules:
- **No Third-Party Messaging Providers**: Twilio, SendGrid, Mailgun, external SMTP, MessageBird, Vonage, AWS SNS, or any other third-party messaging services are strictly prohibited.
- **No Reverse Engineering / Web Scraping**: Private Nookal endpoints, session-cookie scraping, browser automation, or authentication bypasses are forbidden.
- **No Fabricated Endpoints**: The application will never invent endpoints (such as `/sendSMS`, `/sendMessage`, or `/sendEmail`) or report "Sent" unless an official Nookal API operation has genuinely received and accepted the message.

---

## 2. Official Nookal API v2 Capabilities vs. Native PMS Automations

### A. Nookal REST API v2 (`https://api.nookal.com/production/v2/`)
The official Nookal REST API supports integration operations for:
- **Patients**: `getPatients`, `searchPatients`, `getPatient`, `findPatientByPhone`, `addPatient`, `editPatient`
- **Appointments**: `getAppointments`, `getAppointment`, `addAppointment`, `editAppointment`, `cancelAppointment`, `rebookAppointment`, `getAvailabilities`
- **Cases & Clinical**: `getCases`, `getAllCases`, `getTreatmentNotes`, `getAllTreatmentNotes`, `addTreatmentNote`, `getExtras`, `addPatientExtra`
- **Practice Configuration**: `getLocations`, `getLocationLogo`, `getPractitioners`, `getPractitionerPhoto`, `getAppointmentTypes`, `getClassTypes`
- **Files & Financials**: `getPatientFiles`, `getFileUrl`, `uploadFile`, `setFileActive`, `getInvoice`, `getInvoices`, `addInvoice`, `addItemToInvoice`, etc.
- **Referrals**: `getReferrers`, `addReferrer`, `getReferrals`, `addReferral`

**Key Fact**: The official Nookal REST API does **NOT** provide an ad-hoc direct SMS or direct Email dispatch endpoint.

### B. Nookal Native Communication Engine
Nookal PMS possesses a built-in automated messaging engine configured by clinic administrators via the Nookal PMS Admin UI (**Manage > Communications**):
- **Appointment Confirmations**: Automatically dispatched by Nookal PMS upon booking.
- **Appointment Reminders**: Automatically dispatched by Nookal PMS 24h or 48h prior to scheduled consultations.
- **Recalls & Follow-ups**: Automated recall sequences based on clinical timelines.

---

## 3. Integration Handling

1. **Native Automation Ownership**:
   - The integration actively tracks and observes Nookal's native communication states (e.g. `email_reminder_sent`, SMS reminder flags).
   - In `config/settings.yaml`, `app_reminders_enabled: false` defers reminder dispatch to Nookal's native automation to eliminate any possibility of duplicate messaging to patients.

2. **Dashboard Direct-Send Action**:
   - In production, `SMSAdapter` and `EmailAdapter` are wired into `MessagingService`.
   - Direct ad-hoc dispatch attempts raise `NookalCommunicationUnavailable`, returning status `"unavailable"` to the dashboard.
   - The UI clearly informs clinic staff that automated appointment messaging is managed natively in Nookal PMS (*Manage > Communications*), preventing false reports of messages being "Sent".

3. **Configuration Hygiene**:
   - Configuration models (`MessagingConfig` and `CommunicationConfig`) maintain one authoritative structure.
   - Obsolete third-party references (`SMS_API_KEY`, `SMS_SENDER_ID`, `SMS_BASE_URL`, `SMTP_*`) are purged.
