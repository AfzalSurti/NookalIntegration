"""
Phase E dashboard security review — limitations (not a compliance claim).

Reviewed areas:
- Cookie session auth with opaque server-side session ids
- CSRF required on unsafe API methods via X-CSRF-Token
- Central AuthorizationPolicy enforced on every protected API route
- Direct API privilege escalation covered by tests
- Audit scrubbing + viewer sanitization
- Kill switch uses existing app.shared.kill_switch only
- Approvals go only through ApprovalQueue.approve/reject
- No OpenClaw / live provider imports in dashboard package
- Error responses avoid stack traces

Known limitations (deferred):
- MemoryAuthBackend + in-memory sessions are for offline/dev — not production IAM
- No MFA, password rotation, lockout, or SSO
- Session cookie Secure flag only in environment=production
- HTML form posts (login) are CSRF-exempt by necessity (no session yet); post-login
  HTML mutating forms are not fully wired — prefer API + CSRF header
- AuditLog.query has no dedicated correlation_id index (filtered in service)
- Rate limiting / brute-force protection on login not implemented
- HTTPS termination expected at reverse proxy; app does not enforce TLS
- IDOR: patient IDs are guessable synthetic ids; authz is role-based not
  per-patient ACL (Phase 1 operational assumption)
- Production credential store / hashed user directory not built
- Marketing is a placeholder page only
"""
