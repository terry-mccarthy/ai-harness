"""JWT decoding helpers."""
import jwt
from fastapi import HTTPException

from .config import PUBLIC_KEY


def decode_jwt(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing_token")
    raw_token = authorization[7:]
    try:
        return jwt.decode(raw_token, PUBLIC_KEY, algorithms=["RS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "token_expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "invalid_token")


def validate_approval_token(token: str, thread_id: str, tool_name: str, secret: str) -> bool:
    """Returns True if the human-approval token is valid, unexpired, and scoped
    to this thread+tool.

    Deliberately duplicated (not imported) from
    packages/harness-supervisor/harness_supervisor/approval.py: importing that
    package here would pull langgraph/psycopg/etc. into governance's slim
    container as transitive dependencies. This function is small, self-contained,
    and governance already depends on pyjwt — see ADR-0012 (short-lived JWTs
    scoped to thread_id + tool_name) and issue #01.
    """
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"])
        return claims.get("thread_id") == thread_id and claims.get("tool_name") == tool_name
    except jwt.ExpiredSignatureError:
        return False
    except jwt.InvalidTokenError:
        return False
