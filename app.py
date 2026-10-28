import hashlib
import hmac
import json
import os
import re
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


APP_NAME = "care-coordinator"
INITIAL_STEP = "referral_received"
WORKFLOW_STEPS = (
    "referral_received",
    "patient_contacted",
    "appointment_scheduled",
    "follow_up_completed",
)
TRANSITIONS: dict[str, tuple[str, str]] = {
    "patient_contacted": (INITIAL_STEP, "patient_contacted"),
    "appointment_scheduled": ("patient_contacted", "appointment_scheduled"),
    "follow_up_completed": ("appointment_scheduled", "follow_up_completed"),
}
ALLOWED_EVENTS = Literal[
    "patient_contacted",
    "appointment_scheduled",
    "follow_up_completed",
]


class EventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: uuid.UUID
    event: ALLOWED_EVENTS


class CaseCreated(BaseModel):
    case_id: str
    current_step: str
    created_at: str


class AssignmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assigned_to: str | None = Field(default=None, max_length=64)
    due_at: datetime | None = None

    @field_validator("assigned_to")
    @classmethod
    def validate_assigned_to(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
            raise ValueError("assigned_to must be a short staff identifier")
        return value

    @field_validator("due_at")
    @classmethod
    def validate_due_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("due_at must include a timezone")
        return value


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=500)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def initialize_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS care_cases (
                case_id TEXT PRIMARY KEY,
                current_step TEXT NOT NULL CHECK (
                    current_step IN (
                        'referral_received',
                        'patient_contacted',
                        'appointment_scheduled',
                        'follow_up_completed'
                    )
                ),
                assigned_to TEXT,
                due_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS care_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id VARCHAR(128) NOT NULL,
                event VARCHAR(64) NOT NULL,
                actor VARCHAR(128) NOT NULL,
                old_step VARCHAR(32) NOT NULL,
                new_step VARCHAR(32) NOT NULL,
                created_at DATETIME NOT NULL,
                details TEXT
            )
            """
        )
        case_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(care_cases)")
        }
        if "assigned_to" not in case_columns:
            connection.execute("ALTER TABLE care_cases ADD COLUMN assigned_to TEXT")
        if "due_at" not in case_columns:
            connection.execute("ALTER TABLE care_cases ADD COLUMN due_at TEXT")
        audit_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(care_audit_log)")
        }
        if "details" not in audit_columns:
            connection.execute("ALTER TABLE care_audit_log ADD COLUMN details TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS care_webhook_deliveries (
                session_id VARCHAR(128) NOT NULL,
                idempotency_key VARCHAR(128) NOT NULL,
                event VARCHAR(64) NOT NULL,
                status VARCHAR(16) NOT NULL,
                actor VARCHAR(128) NOT NULL,
                old_step VARCHAR(32) NOT NULL,
                new_step VARCHAR(32) NOT NULL,
                message VARCHAR(500) NOT NULL,
                claimed_at DATETIME NOT NULL,
                PRIMARY KEY (session_id, idempotency_key),
                CONSTRAINT uq_care_session_event UNIQUE (session_id, event)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS care_approvals (
                case_id TEXT PRIMARY KEY,
                decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
                approver_id TEXT NOT NULL,
                reason TEXT,
                decided_at TEXT NOT NULL
            )
            """
        )


def create_case(database_path: Path) -> dict[str, str]:
    case_id = str(uuid.uuid4())
    now = utc_now()
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO care_cases (case_id, current_step, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (case_id, INITIAL_STEP, now, now),
        )
        connection.execute(
            """
            INSERT INTO care_audit_log
                (session_id, event, actor, old_step, new_step, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (case_id, "case_created", "coordinator", "START", INITIAL_STEP, now),
        )
    return {
        "case_id": case_id,
        "current_step": INITIAL_STEP,
        "created_at": now,
    }


def list_cases(database_path: Path) -> list[dict[str, object]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT c.case_id, c.current_step, c.assigned_to, c.due_at,
                   c.created_at, c.updated_at,
                   COALESCE(
                       a.decision,
                       CASE WHEN c.current_step = 'appointment_scheduled'
                            THEN 'pending' ELSE 'not_required' END
                   ) AS approval_status
            FROM care_cases AS c
            LEFT JOIN care_approvals AS a ON a.case_id = c.case_id
            ORDER BY c.updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_case(database_path: Path, case_id: str) -> dict[str, object] | None:
    with connect(database_path) as connection:
        case = connection.execute(
            """
            SELECT case_id, current_step, assigned_to, due_at, created_at, updated_at
            FROM care_cases
            WHERE case_id = ?
            """,
            (case_id,),
        ).fetchone()
        if case is None:
            return None
        audit = connection.execute(
            """
            SELECT event, actor, old_step, new_step, created_at, details
            FROM care_audit_log
            WHERE session_id = ?
            ORDER BY id
            """,
            (case_id,),
        ).fetchall()
        approval = connection.execute(
            """
            SELECT decision, approver_id, reason, decided_at
            FROM care_approvals
            WHERE case_id = ?
            """,
            (case_id,),
        ).fetchone()
    current_step = case["current_step"]
    approval_status = (
        approval["decision"]
        if approval is not None
        else "pending" if current_step == "appointment_scheduled" else "not_required"
    )
    return {
        **dict(case),
        "approval_status": approval_status,
        "approval": dict(approval) if approval is not None else None,
        "audit_log": [dict(row) for row in audit],
    }


def update_assignment(
    database_path: Path,
    case_id: str,
    update: AssignmentUpdate,
) -> dict[str, object]:
    if not update.model_fields_set:
        raise HTTPException(status_code=422, detail="Provide an assignment or due date to update")
    now = utc_now()
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        case = connection.execute(
            "SELECT current_step, assigned_to, due_at FROM care_cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if case is None:
            raise HTTPException(status_code=404, detail="Care case not found")
        assigned_to = (
            update.assigned_to
            if "assigned_to" in update.model_fields_set
            else case["assigned_to"]
        )
        due_at = (
            update.due_at.astimezone(timezone.utc).isoformat()
            if update.due_at is not None
            else None
        ) if "due_at" in update.model_fields_set else case["due_at"]
        connection.execute(
            """
            UPDATE care_cases SET assigned_to = ?, due_at = ?, updated_at = ?
            WHERE case_id = ?
            """,
            (assigned_to, due_at, now, case_id),
        )
        details = json.dumps(
            {"assigned_to": assigned_to, "due_at": due_at},
            separators=(",", ":"),
        )
        connection.execute(
            """
            INSERT INTO care_audit_log
                (session_id, event, actor, old_step, new_step, created_at, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case_id,
                "assignment_updated",
                "coordinator",
                case["current_step"],
                case["current_step"],
                now,
                details,
            ),
        )
    return {
        "case_id": case_id,
        "current_step": case["current_step"],
        "assigned_to": assigned_to,
        "due_at": due_at,
    }


def decide_approval(
    database_path: Path,
    case_id: str,
    decision: ApprovalDecision,
    approver_id: str,
) -> dict[str, object]:
    if decision.decision == "rejected" and not (decision.reason or "").strip():
        raise HTTPException(status_code=422, detail="A reason is required when rejecting")
    now = utc_now()
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        case = connection.execute(
            "SELECT current_step FROM care_cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if case is None:
            raise HTTPException(status_code=404, detail="Care case not found")
        if case["current_step"] != "appointment_scheduled":
            raise HTTPException(
                status_code=409,
                detail="Approval decisions are only available at the appointment checkpoint",
            )
        connection.execute(
            """
            INSERT INTO care_approvals (case_id, decision, approver_id, reason, decided_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(case_id) DO UPDATE SET
                decision = excluded.decision,
                approver_id = excluded.approver_id,
                reason = excluded.reason,
                decided_at = excluded.decided_at
            """,
            (case_id, decision.decision, approver_id, decision.reason, now),
        )
        connection.execute(
            """
            INSERT INTO care_audit_log
                (session_id, event, actor, old_step, new_step, created_at, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case_id,
                f"approval_{decision.decision}",
                approver_id,
                case["current_step"],
                case["current_step"],
                now,
                json.dumps(
                    {"reason": decision.reason},
                    separators=(",", ":"),
                ),
            ),
        )
    return {
        "case_id": case_id,
        "approval_status": decision.decision,
        "approver_id": approver_id,
        "reason": decision.reason,
        "decided_at": now,
    }


def get_dashboard(database_path: Path) -> dict[str, object]:
    cases = list_cases(database_path)
    now = datetime.now(timezone.utc)
    today = now.date()
    due_soon_limit = now.timestamp() + 7 * 24 * 60 * 60
    active_cases = [case for case in cases if case["current_step"] != "follow_up_completed"]
    reminders: list[dict[str, str]] = []
    for case in active_cases:
        due_at = case["due_at"]
        if due_at is None:
            continue
        due = datetime.fromisoformat(due_at)
        if due.date() < today:
            due_status = "overdue"
        elif due.date() == today:
            due_status = "due_today"
        elif due.timestamp() <= due_soon_limit:
            due_status = "upcoming"
        else:
            continue
        reminders.append(
            {
                "case_id": case["case_id"],
                "current_step": case["current_step"],
                "assigned_to": case["assigned_to"] or "unassigned",
                "due_at": due_at,
                "status": due_status,
            }
        )
    reminders.sort(key=lambda item: item["due_at"])
    reminder_status_by_case = {
        reminder["case_id"]: reminder["status"] for reminder in reminders
    }
    staff_workload: dict[str | None, dict[str, object]] = {}
    for case in active_cases:
        assigned_to = case["assigned_to"]
        workload = staff_workload.setdefault(
            assigned_to,
            {
                "assigned_to": assigned_to,
                "active_cases": 0,
                "overdue_count": 0,
                "due_today_count": 0,
                "pending_approvals": 0,
            },
        )
        workload["active_cases"] += 1
        due_status = reminder_status_by_case.get(case["case_id"])
        if due_status == "overdue":
            workload["overdue_count"] += 1
        elif due_status == "due_today":
            workload["due_today_count"] += 1
        if case["approval_status"] == "pending":
            workload["pending_approvals"] += 1
    sorted_staff_workload = sorted(
        staff_workload.values(),
        key=lambda item: (
            -int(item["active_cases"]),
            item["assigned_to"] is None,
            item["assigned_to"] or "",
        ),
    )
    step_counts = {
        step: sum(case["current_step"] == step for case in cases)
        for step in WORKFLOW_STEPS
    }
    pending_approvals = sum(
        case["approval_status"] == "pending" for case in active_cases
    )
    due_today_count = sum(item["status"] == "due_today" for item in reminders)
    overdue_count = sum(item["status"] == "overdue" for item in reminders)
    return {
        "total_cases": len(cases),
        "active_cases": len(active_cases),
        "completed_cases": len(cases) - len(active_cases),
        "unassigned_cases": sum(case["assigned_to"] is None for case in active_cases),
        "pending_approvals": pending_approvals,
        "overdue_count": overdue_count,
        "due_today_count": due_today_count,
        "due_soon_count": sum(item["status"] == "upcoming" for item in reminders),
        "step_counts": step_counts,
        "staff_workload": sorted_staff_workload,
        "reminders": reminders,
        "cases": cases,
    }


async def read_webhook_body(request: Request, max_bytes: int = 16_384) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(status_code=413, detail="Webhook payload is too large")
        body.extend(chunk)
    return bytes(body)


def apply_event(
    database_path: Path,
    case_id: str,
    event: str,
    idempotency_key: str,
    actor: str,
) -> dict[str, object]:
    if event not in TRANSITIONS:
        raise HTTPException(status_code=422, detail="Unsupported workflow event")

    expected_step, next_step = TRANSITIONS[event]
    now = utc_now()
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        case = connection.execute(
            "SELECT current_step FROM care_cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if case is None:
            raise HTTPException(status_code=404, detail="Care case not found")

        prior_delivery = connection.execute(
            """
            SELECT event, status, old_step, new_step, message
            FROM care_webhook_deliveries
            WHERE session_id = ? AND idempotency_key = ?
            """,
            (case_id, idempotency_key),
        ).fetchone()
        if prior_delivery is not None:
            if prior_delivery["event"] != event:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key was already used for a different event",
                )
            return {
                "case_id": case_id,
                "event": event,
                "current_step": prior_delivery["new_step"],
                "message": prior_delivery["message"],
                "duplicate": True,
            }

        prior_event = connection.execute(
            """
            SELECT idempotency_key
            FROM care_webhook_deliveries
            WHERE session_id = ? AND event = ?
            """,
            (case_id, event),
        ).fetchone()
        if prior_event is not None:
            raise HTTPException(
                status_code=409,
                detail="This workflow event was already recorded with another idempotency key",
            )

        old_step = case["current_step"]
        if old_step != expected_step:
            raise HTTPException(
                status_code=409,
                detail=f"Event '{event}' requires the case to be at '{expected_step}'",
            )
        if event == "follow_up_completed":
            approval = connection.execute(
                "SELECT decision FROM care_approvals WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            if approval is None or approval["decision"] != "approved":
                raise HTTPException(
                    status_code=409,
                    detail="A human approval is required before follow-up completion",
                )

        message = f"Workflow advanced to {next_step}"
        connection.execute(
            """
            UPDATE care_cases
            SET current_step = ?, updated_at = ?
            WHERE case_id = ? AND current_step = ?
            """,
            (next_step, now, case_id, expected_step),
        )
        connection.execute(
            """
            INSERT INTO care_audit_log
                (session_id, event, actor, old_step, new_step, created_at, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (case_id, event, actor, old_step, next_step, now, None),
        )
        connection.execute(
            """
            INSERT INTO care_webhook_deliveries
                (session_id, idempotency_key, event, status, actor,
                 old_step, new_step, message, claimed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case_id,
                idempotency_key,
                event,
                "processed",
                actor,
                old_step,
                next_step,
                message,
                now,
            ),
        )
    return {
        "case_id": case_id,
        "event": event,
        "current_step": next_step,
        "message": message,
        "duplicate": False,
    }


def create_app(
    database_path: Path | str | None = None,
    api_key: str | None = None,
    webhook_secret: str | None = None,
    approver_api_key: str | None = None,
    approver_id: str | None = None,
) -> FastAPI:
    resolved_database_path = Path(
        database_path or os.environ.get(
            "CARE_DATABASE_PATH",
            str(Path(__file__).with_name("care-coordinator.db")),
        )
    )
    configured_api_key = api_key if api_key is not None else os.environ.get("CARE_API_KEY")
    configured_webhook_secret = (
        webhook_secret
        if webhook_secret is not None
        else os.environ.get("CARE_WEBHOOK_SECRET")
    )
    configured_approver_api_key = (
        approver_api_key
        if approver_api_key is not None
        else os.environ.get("CARE_APPROVER_API_KEY")
    )
    configured_approver_id = (
        approver_id if approver_id is not None else os.environ.get("CARE_APPROVER_ID")
    )
    initialize_database(resolved_database_path)

    app = FastAPI(
        title="Care Coordination Prototype",
        description=(
            "A human-supervised workflow prototype. It does not provide clinical advice."
        ),
        version="0.1.0",
    )
    bearer = HTTPBearer(auto_error=False)

    @app.middleware("http")
    async def add_response_safety_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def require_api_key(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Depends(bearer),
        ],
    ) -> None:
        if not configured_api_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="CARE_API_KEY is not configured",
            )
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(credentials.credentials, configured_api_key)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def require_approver(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Depends(bearer),
        ],
    ) -> str:
        if not configured_approver_api_key or not configured_approver_id:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Reviewer credentials are not configured",
            )
        if configured_api_key and hmac.compare_digest(
            configured_approver_api_key,
            configured_api_key,
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Reviewer and coordinator credentials must be different",
            )
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(
                credentials.credentials,
                configured_approver_api_key,
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Valid reviewer credentials are required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return configured_approver_id

    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(Path(__file__).with_name("index.html"))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/cases", response_model=CaseCreated, status_code=201)
    def new_case(_: Annotated[None, Depends(require_api_key)]) -> dict[str, str]:
        return create_case(resolved_database_path)

    @app.get("/cases")
    def cases(_: Annotated[None, Depends(require_api_key)]) -> list[dict[str, object]]:
        return list_cases(resolved_database_path)

    @app.get("/dashboard")
    def dashboard(
        _: Annotated[None, Depends(require_api_key)],
    ) -> dict[str, object]:
        return get_dashboard(resolved_database_path)

    @app.get("/cases/{case_id}")
    def case_detail(
        case_id: uuid.UUID,
        _: Annotated[None, Depends(require_api_key)],
    ) -> dict[str, object]:
        result = get_case(resolved_database_path, str(case_id))
        if result is None:
            raise HTTPException(status_code=404, detail="Care case not found")
        return result

    @app.patch("/cases/{case_id}/assignment")
    def set_assignment(
        case_id: uuid.UUID,
        update: AssignmentUpdate,
        _: Annotated[None, Depends(require_api_key)],
    ) -> dict[str, object]:
        return update_assignment(resolved_database_path, str(case_id), update)

    @app.post("/cases/{case_id}/approval")
    def record_approval(
        case_id: uuid.UUID,
        decision: ApprovalDecision,
        authenticated_approver: Annotated[str, Depends(require_approver)],
    ) -> dict[str, object]:
        return decide_approval(
            resolved_database_path,
            str(case_id),
            decision,
            authenticated_approver,
        )

    @app.post("/cases/{case_id}/events")
    def record_event(
        case_id: uuid.UUID,
        payload: EventPayload,
        _: Annotated[None, Depends(require_api_key)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    ) -> dict[str, object]:
        if payload.case_id != case_id:
            raise HTTPException(
                status_code=422,
                detail="The case_id in the URL must match the request body",
            )
        return apply_event(
            resolved_database_path,
            str(case_id),
            payload.event,
            idempotency_key,
            "coordinator",
        )

    @app.post("/webhooks/events")
    async def receive_webhook(
        request: Request,
        signature: Annotated[str | None, Header(alias="X-Webhook-Signature")] = None,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=128),
        ] = None,
    ) -> dict[str, object]:
        if not configured_webhook_secret:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="CARE_WEBHOOK_SECRET is not configured",
            )
        if signature is None or idempotency_key is None:
            raise HTTPException(
                status_code=401,
                detail="Webhook signature and idempotency key are required",
            )
        raw_body = await read_webhook_body(request)
        expected_signature = hmac.new(
            configured_webhook_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
        try:
            payload = EventPayload.model_validate_json(raw_body)
        except ValidationError as error:
            raise HTTPException(status_code=422, detail=error.errors()) from error
        return apply_event(
            resolved_database_path,
            str(payload.case_id),
            payload.event,
            idempotency_key,
            "signed_webhook",
        )

    return app


app = create_app()
