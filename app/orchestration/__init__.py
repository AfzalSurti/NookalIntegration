"""
Orchestration layer — clinic workflow business logic.

OpenClaw / launchd / webhooks / dashboard should *call* into this package.
They must not embed clinic rules. No OpenClaw SDK imports belong here.
"""
from __future__ import annotations

from app.orchestration.treatment_letters import TreatmentCompletionLetterWorkflow
from app.orchestration.appointment_commands import (
    CancelAppointmentWorkflow,
    CheckAppointmentWorkflow,
    CreateAppointmentWorkflow,
    RescheduleAppointmentWorkflow,
)
from app.orchestration.appointment_reminders import AppointmentRemindersWorkflow
from app.orchestration.base import BaseWorkflow
from app.orchestration.certificates import CertificateRequestWorkflow
from app.orchestration.communication import (
    CheckAppointmentCommunicationWorkflow,
    CheckCommunicationConfigWorkflow,
    CheckPatientReadinessWorkflow,
)
from app.orchestration.context import WorkflowContext
from app.orchestration.correlation import new_correlation_id
from app.orchestration.errors import WorkflowExecutionError
from app.orchestration.referral_letters import ReferralThankYouWorkflow
from app.orchestration.referrer_sync import ReferrerConflictStore, ReferrerSyncWorkflow
from app.orchestration.results import WorkflowErrorInfo, WorkflowResult, WorkflowStatus

__all__ = [
    "AppointmentRemindersWorkflow",
    "BaseWorkflow",
    "CancelAppointmentWorkflow",
    "CertificateRequestWorkflow",
    "CheckAppointmentCommunicationWorkflow",
    "CheckAppointmentWorkflow",
    "CheckCommunicationConfigWorkflow",
    "CheckPatientReadinessWorkflow",
    "CreateAppointmentWorkflow",
    "ReferralThankYouWorkflow",
    "ReferrerConflictStore",
    "ReferrerSyncWorkflow",
    "RescheduleAppointmentWorkflow",
    "TreatmentCompletionLetterWorkflow",
    "WorkflowContext",
    "WorkflowErrorInfo",
    "WorkflowExecutionError",
    "WorkflowResult",
    "WorkflowStatus",
    "new_correlation_id",
]

