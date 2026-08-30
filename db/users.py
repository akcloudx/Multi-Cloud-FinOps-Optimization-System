"""
db/users.py — Simple shared login store (bcrypt-hashed passwords).

Deliberately minimal: no roles, no per-user tenant scoping (the tenant
registry in db/tenants.py always lives in the "live" scope, by design - see
PROJECT_CONTEXT.md). This exists to gate the app behind *something* before
real Entra ID / OIDC login (via Streamlit's native st.login()) replaces it -
not intended as a long-term identity system.

Always stored in the "Azure" engine's database regardless of which cloud
provider is later selected inside the dashboard - app users aren't
cloud-provider-scoped.

2026-08: demo and production accounts are genuinely separate tables now (one
per mode scope, see db/schema.py), not just distinguished by username within
one shared table - matches the login screen's own Mode toggle, which is
read before any of these functions are ever called (see ui/auth_page.py:
the toggle picks Demo or Production, THEN the matching form renders directly
beneath it). Every function here takes an explicit `mode` ("demo" | "live")
for exactly that reason - it should always be
`st.session_state["_login_mode_widget"]` translated to "demo"/"live", never
guessed.
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


def user_count(mode: str) -> int:
    init_db(_ENGINE_PROVIDER, mode)
    with Session(get_engine(_ENGINE_PROVIDER, mode)) as session:
        return session.query(AppUser).count()


def production_user_count() -> int:
    """Count of real production accounts. Always the "live" scope - the demo
    account can't exist there at all now that demo/live are separate tables,
    so (unlike before this split) there's no need to filter it out by name."""
    init_db(_ENGINE_PROVIDER, "live")
    with Session(get_engine(_ENGINE_PROVIDER, "live")) as session:
        return session.query(AppUser).count()


def ensure_demo_user() -> None:
    """Creates the shared demo account on first use. Always the "demo" scope.
    Idempotent - safe to call every time the login screen's Demo Mode path
    renders."""
    init_db(_ENGINE_PROVIDER, "demo")
    with Session(get_engine(_ENGINE_PROVIDER, "demo")) as session:
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


def create_user(username: str, password: str, display_name: str = "", mode: str = "live") -> int:
    """Creates a new user with a bcrypt-hashed password. Raises ValueError if
    the username is already taken or either field is blank. mode defaults to
    "live" since every real call site (production bootstrap, User Management's
    "add a user") only ever creates real accounts - never the demo one."""
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("Username and password are both required.")

    init_db(_ENGINE_PROVIDER, mode)
    now_iso = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    with Session(get_engine(_ENGINE_PROVIDER, mode)) as session:
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


def verify_login(username: str, password: str, mode: str) -> Optional[dict]:
    """Returns {"id", "username", "display_name"} on success, None on failure.
    Never raises on bad credentials - only on unexpected DB errors. mode is
    required (not defaulted) - the demo login form must check the "demo"
    scope and the production login form must check "live"; a wrong default
    here would mean a login form silently checking the wrong account list."""
    username = (username or "").strip()
    if not username or not password:
        return None

    init_db(_ENGINE_PROVIDER, mode)
    with Session(get_engine(_ENGINE_PROVIDER, mode)) as session:
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


def list_users(mode: str = "live") -> list:
    """Defaults to "live" - the User Management admin page manages real
    accounts; the demo scope only ever has the one fixed seeded account."""
    init_db(_ENGINE_PROVIDER, mode)
    with Session(get_engine(_ENGINE_PROVIDER, mode)) as session:
        rows = session.query(AppUser).order_by(AppUser.id).all()
        session.expunge_all()
        return rows


def update_user(user_id: int, mode: str, username: str = None, display_name: str = None) -> None:
    """Renames a user and/or changes their display name - added 2026-08-30,
    real gap the user caught: User Management could list accounts and add
    new ones, but never edit an existing one at all. username is optional
    (None leaves it unchanged) since the Manage dialog's Save button always
    submits both fields together, matching Manage Tenant's own "leave a
    field as-is by not touching it" convention elsewhere in this app.
    Raises ValueError on a blank/duplicate username, same rule as
    create_user()."""
    init_db(_ENGINE_PROVIDER, mode)
    with Session(get_engine(_ENGINE_PROVIDER, mode)) as session:
        user = session.query(AppUser).filter(AppUser.id == user_id).first()
        if not user:
            raise ValueError("User not found.")
        if username is not None:
            username = username.strip()
            if not username:
                raise ValueError("Username can't be blank.")
            existing = session.query(AppUser).filter(AppUser.username == username, AppUser.id != user_id).first()
            if existing:
                raise ValueError(f"Username '{username}' is already taken.")
            user.username = username
        if display_name is not None:
            user.display_name = display_name.strip() or user.username
        session.commit()


def update_user_password(user_id: int, new_password: str, mode: str) -> None:
    """Sets a new password for an existing user - same bcrypt hashing as
    create_user(). Raises ValueError if the password is too short, matching
    the same 8-char minimum already enforced on the Add User form."""
    if len(new_password or "") < 8:
        raise ValueError("Use at least 8 characters for the password.")
    init_db(_ENGINE_PROVIDER, mode)
    with Session(get_engine(_ENGINE_PROVIDER, mode)) as session:
        user = session.query(AppUser).filter(AppUser.id == user_id).first()
        if not user:
            raise ValueError("User not found.")
        user.password_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        session.commit()


def set_user_active(user_id: int, is_active: bool, mode: str) -> None:
    """Activates/deactivates a user - added alongside update_user() above:
    the Active/Inactive badge already existed on User Management's own list
    (list_users() above), but nothing ever actually SET is_active to False
    anywhere in the codebase before this, a real pre-existing gap found
    while wiring up the new Manage dialog. A deactivated user's is_active
    check in verify_login() above already correctly blocks their login -
    that logic was always correct, it just had no way to ever be reached.

    Real safety gap closed 2026-08-30: raises ValueError rather than
    deactivating the LAST active account. Without this, deactivating your
    own (or the only other) account left every row in the table with
    is_active=False - verify_login() correctly refuses all of them, but
    unlike deleting every account down to zero rows (which correctly falls
    back to _render_bootstrap_form()'s "create the first account" screen,
    since that checks a plain row COUNT, not is_active), rows still exist
    here - production_user_count() > 0 - so the bootstrap screen never
    reappears either. A real, unrecoverable-without-direct-DB-access
    lockout, not a hypothetical."""
    init_db(_ENGINE_PROVIDER, mode)
    with Session(get_engine(_ENGINE_PROVIDER, mode)) as session:
        user = session.query(AppUser).filter(AppUser.id == user_id).first()
        if not user:
            raise ValueError("User not found.")
        if not is_active and user.is_active:
            other_active = session.query(AppUser).filter(
                AppUser.is_active == True, AppUser.id != user_id  # noqa: E712
            ).count()
            if other_active == 0:
                raise ValueError("Can't deactivate the last active account - you'd be locked out.")
        user.is_active = is_active
        session.commit()


def delete_user(user_id: int, mode: str) -> None:
    """Permanently removes a user - added 2026-08-30 alongside the
    deactivate-lockout guard above, same underlying risk: deleting the last
    ACTIVE account while other (inactive) rows still exist produces the
    identical lockout set_user_active() now blocks - production_user_count()
    stays > 0, so the bootstrap "create the first account" screen never
    reappears. Deleting the truly LAST row overall (0 remaining) is fine and
    deliberately still allowed - that genuinely does fall back to bootstrap."""
    init_db(_ENGINE_PROVIDER, mode)
    with Session(get_engine(_ENGINE_PROVIDER, mode)) as session:
        user = session.query(AppUser).filter(AppUser.id == user_id).first()
        if not user:
            raise ValueError("User not found.")
        if user.is_active:
            other_active = session.query(AppUser).filter(
                AppUser.is_active == True, AppUser.id != user_id  # noqa: E712
            ).count()
            other_total = session.query(AppUser).filter(AppUser.id != user_id).count()
            if other_active == 0 and other_total > 0:
                raise ValueError("Can't delete the last active account while other accounts still exist - you'd be locked out.")
        session.delete(user)
        session.commit()
