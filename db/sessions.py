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


def create_session(user: dict, mode: str) -> str:
    """Creates a new session row and returns its token. Called once at
    login - the token then lives in the URL (st.query_params) for the rest
    of that browser tab's life, until logout."""
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


def get_session(token: str) -> Optional[dict]:
    """Returns {"id", "username", "display_name", "mode"} if the token is
    valid and not expired, else None. Touches last_seen_at and opportunistically
    prunes expired rows (no separate cleanup job needed at this scale)."""
    if not token:
        return None
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


def delete_session(token: str) -> None:
    """Removes a session row - called on logout, so the token in the (now
    stale) browser URL can't be reused to sign back in."""
    if not token:
        return
    init_db(_ENGINE_PROVIDER, _SESSION_SCOPE)
    with Session(get_engine(_ENGINE_PROVIDER, _SESSION_SCOPE)) as session:
        session.query(AppSession).filter(AppSession.token == token).delete(synchronize_session=False)
        session.commit()
