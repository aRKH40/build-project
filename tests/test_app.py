import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import create_app


API_KEY = "test-api-key"
WEBHOOK_SECRET = "test-webhook-secret"
APPROVER_API_KEY = "test-approver-api-key"
APPROVER_ID = "supervisor-verified"


def make_client(tmp_path):
    app = create_app(
        database_path=tmp_path / "care.db",
        api_key=API_KEY,
        webhook_secret=WEBHOOK_SECRET,
        approver_api_key=APPROVER_API_KEY,
        approver_id=APPROVER_ID,
    )
    return TestClient(app)


def auth_headers():
    return {"Authorization": f"Bearer {API_KEY}"}


def approver_headers():
    return {"Authorization": f"Bearer {APPROVER_API_KEY}"}


def test_health_and_home_page_are_available(tmp_path):
    client = make_client(tmp_path)

    health = client.get("/health")
    home = client.get("/")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert health.headers["cache-control"] == "no-store"
    assert health.headers["x-content-type-options"] == "nosniff"
    assert health.headers["x-frame-options"] == "DENY"
    assert home.status_code == 200
    assert "Care Coordination" in home.text


def create_case(client):
    response = client.post("/cases", headers=auth_headers())
    assert response.status_code == 201
    return response.json()["case_id"]


def test_case_workflow_enforces_order_and_records_audit(tmp_path):
    client = make_client(tmp_path)
    case_id = create_case(client)

    out_of_order = client.post(
        f"/cases/{case_id}/events",
        headers={**auth_headers(), "Idempotency-Key": "early-appointment"},
        json={"case_id": case_id, "event": "appointment_scheduled"},
    )
    assert out_of_order.status_code == 409

    transition = client.post(
        f"/cases/{case_id}/events",
        headers={**auth_headers(), "Idempotency-Key": "contacted-1"},
        json={"case_id": case_id, "event": "patient_contacted"},
    )
    assert transition.status_code == 200
    assert transition.json()["current_step"] == "patient_contacted"

    detail = client.get(f"/cases/{case_id}", headers=auth_headers())
    assert detail.status_code == 200
    assert [entry["event"] for entry in detail.json()["audit_log"]] == [
        "case_created",
        "patient_contacted",
    ]


def test_replayed_event_is_idempotent(tmp_path):
    client = make_client(tmp_path)
    case_id = create_case(client)
    headers = {**auth_headers(), "Idempotency-Key": "contacted-1"}
    body = {"case_id": case_id, "event": "patient_contacted"}

    first = client.post(f"/cases/{case_id}/events", headers=headers, json=body)
    replay = client.post(f"/cases/{case_id}/events", headers=headers, json=body)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    detail = client.get(f"/cases/{case_id}", headers=auth_headers()).json()
    assert len(detail["audit_log"]) == 2


def test_workflow_persists_after_app_restart(tmp_path):
    first_client = make_client(tmp_path)
    case_id = create_case(first_client)

    restarted_client = make_client(tmp_path)
    response = restarted_client.get(f"/cases/{case_id}", headers=auth_headers())

    assert response.status_code == 200
    assert response.json()["current_step"] == "referral_received"


def test_signed_webhook_requires_valid_signature_and_is_idempotent(tmp_path):
    client = make_client(tmp_path)
    case_id = create_case(client)
    body = json.dumps(
        {"case_id": case_id, "event": "patient_contacted"},
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": signature,
        "Idempotency-Key": "webhook-contact-1",
    }

    invalid = client.post(
        "/webhooks/events",
        content=body,
        headers={**headers, "X-Webhook-Signature": "0" * 64},
    )
    accepted = client.post("/webhooks/events", content=body, headers=headers)
    replay = client.post("/webhooks/events", content=body, headers=headers)

    assert invalid.status_code == 401
    assert accepted.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True


def test_oversized_signed_webhook_is_rejected(tmp_path):
    client = make_client(tmp_path)
    body = b" " * 16_385
    signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()

    response = client.post(
        "/webhooks/events",
        content=body,
        headers={
            "X-Webhook-Signature": signature,
            "Idempotency-Key": "oversized-event",
        },
    )

    assert response.status_code == 413


def test_missing_api_key_is_rejected(tmp_path):
    client = make_client(tmp_path)

    response = client.post("/cases")

    assert response.status_code == 401


def test_mismatched_case_id_is_rejected(tmp_path):
    client = make_client(tmp_path)
    case_id = create_case(client)

    response = client.post(
        f"/cases/{case_id}/events",
        headers={**auth_headers(), "Idempotency-Key": str(uuid.uuid4())},
        json={"case_id": str(uuid.uuid4()), "event": "patient_contacted"},
    )

    assert response.status_code == 422


def advance_to_appointment(client, case_id):
    for event in ("patient_contacted", "appointment_scheduled"):
        response = client.post(
            f"/cases/{case_id}/events",
            headers={**auth_headers(), "Idempotency-Key": f"setup-{event}"},
            json={"case_id": case_id, "event": event},
        )
        assert response.status_code == 200


def test_assignment_due_date_and_dashboard_reminder(tmp_path):
    client = make_client(tmp_path)
    case_id = create_case(client)

    assigned = client.patch(
        f"/cases/{case_id}/assignment",
        headers=auth_headers(),
        json={"assigned_to": "coordinator-01", "due_at": "2020-01-01T10:00:00Z"},
    )
    dashboard = client.get("/dashboard", headers=auth_headers())

    assert assigned.status_code == 200
    assert assigned.json()["assigned_to"] == "coordinator-01"
    assert dashboard.status_code == 200
    assert dashboard.json()["active_cases"] == 1
    assert dashboard.json()["overdue_count"] == 1
    assert dashboard.json()["reminders"][0]["case_id"] == case_id
    assert dashboard.json()["step_counts"]["referral_received"] == 1


def test_dashboard_tracks_upcoming_due_work_and_pending_approval(tmp_path):
    client = make_client(tmp_path)
    case_id = create_case(client)
    advance_to_appointment(client, case_id)
    due_at = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()

    assigned = client.patch(
        f"/cases/{case_id}/assignment",
        headers=auth_headers(),
        json={"due_at": due_at},
    )
    dashboard = client.get("/dashboard", headers=auth_headers())

    assert assigned.status_code == 200
    assert dashboard.status_code == 200
    assert dashboard.json()["pending_approvals"] == 1
    assert dashboard.json()["due_soon_count"] == 1
    assert dashboard.json()["reminders"][0]["status"] == "upcoming"
    assert dashboard.json()["step_counts"]["appointment_scheduled"] == 1
    assert dashboard.json()["step_counts"]["referral_received"] == 0


def test_dashboard_summarizes_active_workload_by_assignee(tmp_path):
    client = make_client(tmp_path)
    overdue_case = create_case(client)
    pending_approval_case = create_case(client)
    unassigned_case = create_case(client)
    completed_case = create_case(client)

    client.patch(
        f"/cases/{overdue_case}/assignment",
        headers=auth_headers(),
        json={"assigned_to": "nurse-a", "due_at": "2020-01-01T10:00:00Z"},
    )
    client.patch(
        f"/cases/{pending_approval_case}/assignment",
        headers=auth_headers(),
        json={
            "assigned_to": "nurse-a",
            "due_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        },
    )
    client.patch(
        f"/cases/{unassigned_case}/assignment",
        headers=auth_headers(),
        json={"due_at": datetime.now(timezone.utc).replace(hour=23, minute=59, second=0, microsecond=0).isoformat()},
    )
    advance_to_appointment(client, pending_approval_case)
    advance_to_appointment(client, completed_case)
    client.post(
        f"/cases/{completed_case}/approval",
        headers=approver_headers(),
        json={"decision": "approved"},
    )
    client.post(
        f"/cases/{completed_case}/events",
        headers={**auth_headers(), "Idempotency-Key": "complete-workload-case"},
        json={"case_id": completed_case, "event": "follow_up_completed"},
    )
    client.patch(
        f"/cases/{completed_case}/assignment",
        headers=auth_headers(),
        json={"assigned_to": "nurse-a"},
    )

    response = client.get("/dashboard", headers=auth_headers())

    assert response.status_code == 200
    workload = response.json()["staff_workload"]
    assert workload == [
        {
            "assigned_to": "nurse-a",
            "active_cases": 2,
            "overdue_count": 1,
            "due_today_count": 0,
            "pending_approvals": 1,
        },
        {
            "assigned_to": None,
            "active_cases": 1,
            "overdue_count": 0,
            "due_today_count": 1,
            "pending_approvals": 0,
        },
    ]


def test_assignment_rejects_unqualified_dates_and_invalid_staff_ids(tmp_path):
    client = make_client(tmp_path)
    case_id = create_case(client)

    no_timezone = client.patch(
        f"/cases/{case_id}/assignment",
        headers=auth_headers(),
        json={"due_at": "2030-01-01T10:00:00"},
    )
    invalid_staff = client.patch(
        f"/cases/{case_id}/assignment",
        headers=auth_headers(),
        json={"assigned_to": "staff name"},
    )

    assert no_timezone.status_code == 422
    assert invalid_staff.status_code == 422


def test_human_approval_blocks_follow_up_until_approved(tmp_path):
    client = make_client(tmp_path)
    case_id = create_case(client)
    advance_to_appointment(client, case_id)
    headers = {**auth_headers(), "Idempotency-Key": "followup-before-approval"}
    event = {"case_id": case_id, "event": "follow_up_completed"}

    blocked = client.post(f"/cases/{case_id}/events", headers=headers, json=event)
    missing_reason = client.post(
        f"/cases/{case_id}/approval",
        headers=approver_headers(),
        json={"decision": "rejected"},
    )
    rejected = client.post(
        f"/cases/{case_id}/approval",
        headers=approver_headers(),
        json={
            "decision": "rejected",
            "reason": "Needs coordinator review",
        },
    )
    blocked_after_rejection = client.post(
        f"/cases/{case_id}/events",
        headers={**auth_headers(), "Idempotency-Key": "followup-after-rejection"},
        json=event,
    )
    approved = client.post(
        f"/cases/{case_id}/approval",
        headers=approver_headers(),
        json={"decision": "approved"},
    )
    completed = client.post(
        f"/cases/{case_id}/events",
        headers={**auth_headers(), "Idempotency-Key": "followup-after-approval"},
        json=event,
    )

    assert blocked.status_code == 409
    assert missing_reason.status_code == 422
    assert rejected.status_code == 200
    assert blocked_after_rejection.status_code == 409
    assert approved.status_code == 200
    assert completed.status_code == 200
    detail = client.get(f"/cases/{case_id}", headers=auth_headers()).json()
    assert detail["current_step"] == "follow_up_completed"
    assert detail["approval_status"] == "approved"
    assert [entry["event"] for entry in detail["audit_log"]].count("approval_rejected") == 1
    assert [entry["event"] for entry in detail["audit_log"]].count("approval_approved") == 1
    dashboard = client.get("/dashboard", headers=auth_headers()).json()
    assert dashboard["completed_cases"] == 1
    assert dashboard["reminders"] == []


def test_approval_is_only_available_at_appointment_checkpoint(tmp_path):
    client = make_client(tmp_path)
    case_id = create_case(client)

    response = client.post(
        f"/cases/{case_id}/approval",
        headers=approver_headers(),
        json={"decision": "approved"},
    )

    assert response.status_code == 409


def test_coordinator_cannot_approve_and_reviewer_identity_is_authenticated(tmp_path):
    client = make_client(tmp_path)
    case_id = create_case(client)
    advance_to_appointment(client, case_id)

    coordinator_attempt = client.post(
        f"/cases/{case_id}/approval",
        headers=auth_headers(),
        json={"decision": "approved"},
    )
    forged_identity = client.post(
        f"/cases/{case_id}/approval",
        headers=approver_headers(),
        json={"decision": "approved", "approver_id": "forged-supervisor"},
    )
    accepted = client.post(
        f"/cases/{case_id}/approval",
        headers=approver_headers(),
        json={"decision": "approved"},
    )

    assert coordinator_attempt.status_code == 401
    assert forged_identity.status_code == 422
    assert accepted.status_code == 200
    assert accepted.json()["approver_id"] == APPROVER_ID

    detail = client.get(f"/cases/{case_id}", headers=auth_headers()).json()
    approval_audit = [
        entry for entry in detail["audit_log"] if entry["event"] == "approval_approved"
    ]
    assert len(approval_audit) == 1
    assert approval_audit[0]["actor"] == APPROVER_ID


def test_approval_is_unavailable_without_configured_reviewer(tmp_path):
    app = create_app(
        database_path=tmp_path / "care.db",
        api_key=API_KEY,
        webhook_secret=WEBHOOK_SECRET,
    )
    client = TestClient(app)
    case_id = create_case(client)

    response = client.post(
        f"/cases/{case_id}/approval",
        headers=auth_headers(),
        json={"decision": "approved"},
    )

    assert response.status_code == 503


def test_reviewer_and_coordinator_credentials_must_be_distinct(tmp_path):
    app = create_app(
        database_path=tmp_path / "care.db",
        api_key=API_KEY,
        webhook_secret=WEBHOOK_SECRET,
        approver_api_key=API_KEY,
        approver_id=APPROVER_ID,
    )
    client = TestClient(app)
    case_id = create_case(client)

    response = client.post(
        f"/cases/{case_id}/approval",
        headers=auth_headers(),
        json={"decision": "approved"},
    )

    assert response.status_code == 503
