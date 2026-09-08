"""Auth API — login / logout / me. Passwords and tokens are never logged."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.dashboard.auth import Session, User
from app.dashboard.container import DashboardContainer
from app.dashboard.dependencies import get_container, get_session, require_user
from app.dashboard.schemas import LoginRequest, SessionOut, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=SessionOut)
def login(
    body: LoginRequest,
    response: Response,
    container: Annotated[DashboardContainer, Depends(get_container)],
) -> SessionOut:
    user = container.auth_backend.authenticate(body.username, body.password)
    if user is None:
        # Do not reveal whether username or password failed.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")
    session = container.sessions.create(user)
    response.set_cookie(
        key=container.session_cookie_name,
        value=session.session_id,
        httponly=True,
        samesite="lax",
        secure=container.environment == "production",
        max_age=8 * 3600,
        path="/",
    )
    container.audit(
        actor=user.user_id,
        action="dashboard.login",
        target_type="system",
        target_id="auth",
        result="success",
        metadata={"role": user.role},
    )
    return SessionOut(
        user=UserOut(
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            display_name=user.display_name,
        ),
        csrf_token=session.csrf_token,
    )


@router.post("/logout")
def logout(
    response: Response,
    container: Annotated[DashboardContainer, Depends(get_container)],
    session: Annotated[Session | None, Depends(get_session)],
) -> dict[str, str]:
    if session is not None:
        container.sessions.invalidate(session.session_id)
        container.audit(
            actor=session.user_id,
            action="dashboard.logout",
            target_type="system",
            target_id="auth",
            result="success",
            metadata={"role": session.role},
        )
    response.delete_cookie(container.session_cookie_name, path="/")
    return {"status": "ok"}


@router.get("/me", response_model=SessionOut)
def me(
    user: Annotated[User, Depends(require_user)],
    session: Annotated[Session | None, Depends(get_session)],
) -> SessionOut:
    assert session is not None
    return SessionOut(
        user=UserOut(
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            display_name=user.display_name,
        ),
        csrf_token=session.csrf_token,
    )
