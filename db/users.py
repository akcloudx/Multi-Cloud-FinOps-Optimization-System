"""
db/users.py — Simple shared login store (bcrypt-hashed passwords).

Deliberately minimal: one shared user list for the whole app, no roles, no
per-user tenant scoping (the tenant registry in db/tenants.py stays shared
across everyone, by design - see PROJECT_CONTEXT.md). This exists to gate the
app behind *something* before real Entra ID / OIDC login (via Streamlit's
native st.login()) replaces it - not intended as a long-term identity system.

Always stored in the "Azure" engine's database regardless of which cloud
provider is selected in the UI - app users aren't cloud-provider-scoped.
"""

from datetime import datetime
from typing import Optional

import bcrypt
from sqlalchemy.orm import Session

from db.schema import get_engine, init_db, AppUser

_ENGINE_PROVIDER = "Azure"

# Shared, fixed-credential account behind the login screen's "Demo Mode"
# button - lets anyone explore the dashboard with zero setup. Not meant to be
# a secret (it's shown right on the login screen), so never gate anything
# sensitive behind "is this the demo user".
DEMO_USERNAME = "demo"
DEMO_PASSWORD = "Demo@2026"


def user_count() -> int:
    init_db(_ENGINE_PROVIDER)
    with Session(get_engine(_ENGINE_PROVIDER)) as session:
        return session.query(AppUser).count()


def production_user_count() -> int:
    """Count of real (non-demo) accounts. Used to decide whether Production
    Mode should show the first-time "create an account" bootstrap form or a
    normal login form - counting the auto-created demo account here would
    wrongly skip the bootstrap form for a genuinely first-time production
    user who happened to try Demo Mode first."""
    init_db(_ENGINE_PROVIDER)
    with Session(get_engine(_ENGINE_PROVIDER)) as session:
        return session.query(AppUser).filter(AppUser.username != DEMO_USERNAME).count()


def ensure_demo_user() -> None:
    """Creates the shared demo account on first use. Idempotent - safe to
    call every time the login screen's Demo Mode path renders."""
    init_db(_ENGINE_PROVIDER)
    with Session(get_engine(_ENGINE_PROVIDER)) as session:
        if session.query(AppUser).filter(AppUser.username == DEMO_USERNAME).first():
            return
        now_iso = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        pw_hash = bcrypt.hashpw(DEMO_PASSWORD.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        session.add(AppUser(
            username=DEMO_USERNAME,
            password_hash=pw_hash,
            display_name="Demo User",
            is_active=True,
            created_at=now_iso,
        ))
        session.commit()


def create_user(username: str, password: str, display_name: str = "") -> int:
    """Creates a new user with a bcrypt-hashed password. Raises ValueError if
    the username is already taken or either field is blank."""
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("Username and password are both required.")

    init_db(_ENGINE_PROVIDER)
    now_iso = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    with Session(get_engine(_ENGINE_PROVIDER)) as session:
        existing = session.query(AppUser).filter(AppUser.username == username).first()
        if existing:
            raise ValueError(f"Username '{username}' is already taken.")
        user = AppUser(
            username=username,
            password_hash=pw_hash,
            display_name=display_name.strip() or username,
            is_active=True,
            created_at=now_iso,
        )
        session.add(user)
        session.commit()
        return user.id


def verify_login(username: str, password: str) -> Optional[dict]:
    """Returns {"id", "username", "display_name"} on success, None on failure.
    Never raises on bad credentials - only on unexpected DB errors."""
    username = (username or "").strip()
    if not username or not password:
        return None

    init_db(_ENGINE_PROVIDER)
    with Session(get_engine(_ENGINE_PROVIDER)) as session:
        user = session.query(AppUser).filter(
            AppUser.username == username, AppUser.is_active == True  # noqa: E712
        ).first()
        if not user:
            return None
        if not bcrypt.checkpw(password.encode("utf-8"), user.password_hash.encode("utf-8")):
            return None
        user.last_login_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        session.commit()
        return {"id": user.id, "username": user.username, "display_name": user.display_name}


def list_users() -> list:
    init_db(_ENGINE_PROVIDER)
    with Session(get_engine(_ENGINE_PROVIDER)) as session:
        rows = session.query(AppUser).order_by(AppUser.id).all()
        session.expunge_all()
        return rows
