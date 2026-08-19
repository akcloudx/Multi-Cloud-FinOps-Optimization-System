"""
db/sessions.py — Server-side session store backing login persistence.

See db/schema.py's AppSession docstring for the full reasoning (plain
st.session_state does not survive a real browser refresh). Always uses the
Azure/live scope regardless of whether the session itself is a demo or
production login - see that same docstring for why.
"""

import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from db.schema import get_engine, init_db, AppSession

_ENGINE_PROVIDER = "Azure"
_SESSION_SCOPE = "live"
_SESSION_MAX_AGE_DAYS = 7


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def create_session(user: dict, mode: str) -> Optional[str]:
    """Creates a new session row and returns its token, or None if it
    couldn't (e.g. a transient DB hiccup right as the Serverless tier wakes
    from auto-pause) - the caller (_start_session in ui/auth_page.py) treats
    None as "this login still works for the current connection, it just
    won't survive a refresh" rather than blocking sign-in entirely on a
    session-persistence nicety."""
    try:
        init_db(_ENGINE_PROVIDER, _SESSION_SCOPE)
        token = secrets.token_urlsafe(24)
        now_iso = _now_iso()
        with Session(get_engine(_ENGINE_PROVIDER, _SESSION_SCOPE)) as session:
            session.add(AppSession(
                token=token, user_id=user["id"], username=user["username"],
                display_name=user.get("display_name"), mode=mode,
                created_at=now_iso, last_seen_at=now_iso,
            ))
            session.commit()
        return token
    except Exception as e:
        print(f"[Warning] create_session() failed - login will not survive a refresh ({e}).")
        return None


def get_session(token: str) -> Optional[dict]:
    """Returns {"id", "username", "display_name", "mode"} if the token is
    valid and not expired, else None. Touches last_seen_at and opportunistically
    prunes expired rows (no separate cleanup job needed at this scale).

    Wrapped in try/except deliberately - this runs on EVERY fresh page load,
    including right after the Azure SQL Serverless tier's auto-pause (60min
    idle, see deploy_all_resources.ps1) has kicked in, so a transient
    connection hiccup here is a real, expected condition, not a hypothetical.
    Failing this open (treat as "no valid session, show login") is safe and
    correct; the alternative - letting a DB exception propagate up through
    require_login() - would crash the whole script with a raw traceback
    instead of just asking the user to sign in again."""
    if not token:
        return None
    try:
        init_db(_ENGINE_PROVIDER, _SESSION_SCOPE)
        cutoff_iso = (datetime.utcnow() - timedelta(days=_SESSION_MAX_AGE_DAYS)).strftime("%Y-%m-%d %H:%M:%S UTC")
        with Session(get_engine(_ENGINE_PROVIDER, _SESSION_SCOPE)) as session:
            session.query(AppSession).filter(AppSession.last_seen_at < cutoff_iso).delete(synchronize_session=False)
            row = session.query(AppSession).filter(AppSession.token == token).first()
            if not row:
                session.commit()
                return None
            row.last_seen_at = _now_iso()
            result = {"id": row.user_id, "username": row.username, "display_name": row.display_name, "mode": row.mode}
            session.commit()
            return result
    except Exception as e:
        print(f"[Warning] get_session() failed - treating as no session ({e}).")
        return None


def delete_session(token: str) -> None:
    """Removes a session row - called on logout, so the token in the (now
    stale) browser URL can't be reused to sign back in. Best-effort: the
    caller (_clear_session) already drops the URL param and session_state
    regardless, so a failure here just leaves a harmless orphaned row
    (pruned later by get_session's opportunistic cleanup) rather than
    blocking logout."""
    if not token:
        return
    try:
        init_db(_ENGINE_PROVIDER, _SESSION_SCOPE)
        with Session(get_engine(_ENGINE_PROVIDER, _SESSION_SCOPE)) as session:
            session.query(AppSession).filter(AppSession.token == token).delete(synchronize_session=False)
            session.commit()
    except Exception as e:
        print(f"[Warning] delete_session() failed ({e}).")
