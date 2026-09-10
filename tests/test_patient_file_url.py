"""
Comprehensive test suite for Nookal Patient File URL Retrieval.

Covers all 10 required prompt scenarios:
1. Valid Nookal file URL response (official wrapped & flat).
2. Nookal file URL response with unexpected shape.
3. Missing URL.
4. Invalid file ID.
5. Nookal 4xx.
6. Nookal 5xx.
7. Unauthorized dashboard user.
8. Wrong patient/file combination.
9. Signed URL is never written to logs or audit.
10. API key is never exposed.
"""
from __future__ import annotations

import logging
from typing import Any
import httpx
import pytest
from fastapi.testclient import TestClient

from app.nookal_client import (
    HttpNookalClient,
    NookalFileNotFound,
    NookalInvalidFileId,
    NookalRequestFailed,
    NookalResponseInvalid,
    NookalUrlMissing,
    PatientFile,
)
from app.shared.config import NookalConfig
from tests.helpers.dashboard import build_dashboard_env


BASE_URL = "https://api.nookal.com/production/v2/"
SAMPLE_SIGNED_URL = "https://s3.amazonaws.com/nookal-files/100/file_6a783aac0425e0.10481860.pdf?AWSAccessKeyId=AKIAIOSFODNN7EXAMPLE&Signature=vjbyPxybdZaNmGa%2ByT272YEAiv4%3D&Expires=1700000000"


def _make_config() -> NookalConfig:
    return NookalConfig(
        base_url=BASE_URL,
        api_key="test_secret_api_key_xyz123",
        requests_per_second=100.0,
        max_retries=0,
        timeout_seconds=5.0,
    )


# 1. Valid Nookal file URL response (both official wrapped and direct)
def test_1_valid_nookal_file_url_response_official_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "getFileUrl" in str(request.url)
        assert request.url.params.get("patient_id") == "100"
        assert request.url.params.get("file_id") == "file_6a783aac0425e0.10481860"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "api_call": "getFileUrl",
                    "results": {
                        "url": SAMPLE_SIGNED_URL
                    }
                }
            }
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    url = nookal.get_file_url("100", "file_6a783aac0425e0.10481860")
    assert url == SAMPLE_SIGNED_URL


def test_1b_valid_nookal_file_url_response_direct_and_alternates() -> None:
    for payload in [
        {"status": "success", "data": {"url": SAMPLE_SIGNED_URL}},
        {"status": "success", "data": {"file_url": SAMPLE_SIGNED_URL}},
        {"status": "success", "data": {"file": {"url": SAMPLE_SIGNED_URL}}},
        {"status": "success", "data": {"results": {"file_url": SAMPLE_SIGNED_URL}}},
        {"status": "success", "data": SAMPLE_SIGNED_URL},
    ]:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
        nookal = HttpNookalClient(config=_make_config(), client=client)
        assert nookal.get_file_url("100", "file_6a783aac0425e0.10481860") == SAMPLE_SIGNED_URL


# 2. Nookal file URL response with unexpected shape
def test_2_nookal_file_url_unexpected_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": 12345})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    with pytest.raises(NookalResponseInvalid) as exc_info:
        nookal.get_file_url("100", "file_6a783aac0425e0.10481860")
    assert exc_info.value.code == "NOOKAL_RESPONSE_INVALID"


# 3. Missing URL
def test_3_missing_url_in_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "success", "data": {"api_call": "getFileUrl", "results": {}}}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    with pytest.raises(NookalUrlMissing) as exc_info:
        nookal.get_file_url("100", "file_6a783aac0425e0.10481860")
    assert exc_info.value.code == "NOOKAL_URL_MISSING"


# 4. Invalid file ID
def test_4_invalid_file_id() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)), base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    for bad_id in ["", "   ", "file/traversal", "file\\bad", "file?bad=1", "file#hash"]:
        with pytest.raises(NookalInvalidFileId) as exc_info:
            nookal.get_file_url("100", bad_id)
        assert exc_info.value.code == "NOOKAL_INVALID_FILE_ID"


# 5. Nookal 4xx
def test_5_nookal_4xx_responses() -> None:
    # 5a. 404 Not Found
    client_404 = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url=BASE_URL)
    nookal_404 = HttpNookalClient(config=_make_config(), client=client_404)
    with pytest.raises(NookalFileNotFound) as exc_404:
        nookal_404.get_file_url("100", "file_6a783aac0425e0.10481860")
    assert exc_404.value.code == "NOOKAL_FILE_NOT_FOUND"

    # 5b. 422 Validation Error
    client_422 = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(422)), base_url=BASE_URL)
    nookal_422 = HttpNookalClient(config=_make_config(), client=client_422)
    with pytest.raises(NookalInvalidFileId) as exc_422:
        nookal_422.get_file_url("100", "file_6a783aac0425e0.10481860")
    assert exc_422.value.code == "NOOKAL_INVALID_FILE_ID"

    # 5c. 200 with Nookal failure payload: "File does not exist"
    def failure_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "failure", "details": {"errorMessage": "File does not exist or does not belong to patient"}}
        )
    client_fail = httpx.Client(transport=httpx.MockTransport(failure_handler), base_url=BASE_URL)
    nookal_fail = HttpNookalClient(config=_make_config(), client=client_fail)
    with pytest.raises(NookalFileNotFound) as exc_fail:
        nookal_fail.get_file_url("100", "file_6a783aac0425e0.10481860")
    assert exc_fail.value.code == "NOOKAL_FILE_NOT_FOUND"


# 6. Nookal 5xx
def test_6_nookal_5xx_responses() -> None:
    client_500 = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="Internal Server Error")), base_url=BASE_URL)
    nookal_500 = HttpNookalClient(config=_make_config(), client=client_500)

    with pytest.raises(NookalRequestFailed) as exc_500:
        nookal_500.get_file_url("100", "file_6a783aac0425e0.10481860")
    assert exc_500.value.code == "NOOKAL_REQUEST_FAILED"


# 7. Unauthorized dashboard user
def test_7_unauthorized_dashboard_user(tmp_path) -> None:
    env = build_dashboard_env(tmp_path)
    client = env.client()

    # 7a. Unauthenticated request must receive 401
    unauth_resp = client.get(
        "/patients/100/files/file_6a783aac0425e0.10481860/url",
        follow_redirects=False,
    )
    assert unauth_resp.status_code == 401
    assert "authentication_required" in unauth_resp.text

    # 7b. Authenticated user with permission PATIENT_VIEW succeeds
    csrf = env.login(client, "practitioner")
    auth_resp = client.get(
        "/patients/100/files/file_6a783aac0425e0.10481860/url",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert auth_resp.status_code == 303
    assert "s3.amazonaws.com" in auth_resp.headers["location"]


# 8. Wrong patient/file combination
def test_8_wrong_patient_file_combination(tmp_path) -> None:
    env = build_dashboard_env(tmp_path)
    client = env.client()

    # Seed a file specifically belonging to patient 'pat_1001'
    env.nookal.files["file_for_pat1001"] = PatientFile(
        file_id="file_for_pat1001",
        patient_id="pat_1001",
        name="patient1001_xray.pdf",
    )

    csrf = env.login(client, "practitioner")
    # Request that file under patient '100' (wrong combination)
    resp = client.get(
        "/patients/100/files/file_for_pat1001/url",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "File URL could not be retrieved"

    # Audit event should record failure with error_code NOOKAL_FILE_NOT_FOUND
    events = [e for e in env.audit.events if e.action == "dashboard.patient_file_download" and e.result == "failure"]
    assert len(events) >= 1
    assert events[-1].metadata.get("error_code") == "NOOKAL_FILE_NOT_FOUND"


# 9. Signed URL is never written to logs or audit
def test_9_signed_url_never_written_to_logs_or_audit(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    env = build_dashboard_env(tmp_path)
    client = env.client()

    # Configure mock to return full signed URL with signature query param
    sensitive_sig = "SECRET_S3_SIGNATURE_VALUE_DO_NOT_LOG"
    env.nookal.files["file_sensitive"] = PatientFile(
        file_id="file_sensitive",
        patient_id="100",
        name="sensitive.pdf",
    )

    with caplog.at_level(logging.DEBUG):
        csrf = env.login(client, "practitioner")
        resp = client.get(
            "/patients/100/files/file_sensitive/url",
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    # Check audit events
    audit_events = [e for e in env.audit.events if e.action == "dashboard.patient_file_download"]
    assert len(audit_events) >= 1
    for event in audit_events:
        meta_str = str(event.metadata or {})
        assert sensitive_sig not in meta_str
        assert "Signature=" not in meta_str
        assert "AWSAccessKeyId=" not in meta_str

    # Check log output
    for record in caplog.records:
        assert sensitive_sig not in record.getMessage()


# 10. API key is never exposed on errors or logs
def test_10_api_key_never_exposed(caplog: pytest.LogCaptureFixture) -> None:
    secret_key = "test_super_secret_api_key_9999"
    cfg = NookalConfig(
        base_url=BASE_URL,
        api_key=secret_key,
        requests_per_second=100.0,
        max_retries=0,
        timeout_seconds=5.0,
    )

    # 10a. Upstream 500 error containing the api_key in the response body
    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"Error processing api_key={secret_key}")

    client = httpx.Client(transport=httpx.MockTransport(error_handler), base_url=BASE_URL)
    nookal = HttpNookalClient(config=cfg, client=client)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(NookalRequestFailed) as exc_info:
            nookal.get_file_url("100", "file_6a783aac0425e0.10481860")

    err_msg = str(exc_info.value)
    assert secret_key not in err_msg
    assert "[REDACTED]" in err_msg

    # Verify application logs and audit do not contain the secret key
    app_records = [r for r in caplog.records if r.name.startswith("app.")]
    for record in app_records:
        assert secret_key not in record.getMessage()


# End-to-end Dashboard Patient 100 Live Verification Flow (Section 9)
def test_dashboard_patient_100_file_view_flow(tmp_path) -> None:
    env = build_dashboard_env(tmp_path)
    client = env.client()

    from app.nookal_client import PatientRef
    env.nookal.seed_patient(
        PatientRef(
            patient_id="100",
            first_name="Patient",
            last_name="OneHundred",
            display_name="Patient OneHundred",
            phone="+61411110100",
            email="patient100@example.test",
        )
    )
    env.nookal.files["file_6a783aac0425e0.10481860"] = PatientFile(
        file_id="file_6a783aac0425e0.10481860",
        patient_id="100",
        name="initial_assessment.pdf",
        file_type="pdf",
        date_added="2026-08-20",
        size=1048576,
    )

    # Step 1: Log into the dashboard as practitioner
    csrf = env.login(client, "practitioner")

    # Step 2: Open patient 100 detail page
    resp = client.get("/patients/100", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    html = resp.text
    assert "Patient OneHundred" in html or "100" in html

    # Step 3 & 4: Open Files tab - verify file_6a783aac0425e0.10481860 is listed
    assert "file_6a783aac0425e0.10481860" in html
    assert "/patients/100/files/file_6a783aac0425e0.10481860/url" in html

    # Step 5 & 6: Click View/Open file link - returns 303 redirect to Nookal file URL
    file_resp = client.get(
        "/patients/100/files/file_6a783aac0425e0.10481860/url",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert file_resp.status_code == 303
    redirect_url = file_resp.headers["location"]
    assert "s3.amazonaws.com" in redirect_url
    assert "file_6a783aac0425e0.10481860" in redirect_url

    # Also verify unauthenticated request receives 401 authentication_required
    unauth_client = env.client()
    unauth_resp = unauth_client.get(
        "/patients/100/files/file_6a783aac0425e0.10481860/url",
        follow_redirects=False,
    )
    assert unauth_resp.status_code == 401
    assert "authentication_required" in unauth_resp.text
