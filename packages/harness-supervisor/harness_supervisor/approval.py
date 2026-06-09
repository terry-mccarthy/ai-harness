"""Short-lived human approval JWTs scoped to a specific thread + tool."""
import time
import jwt


def issue_approval_token(
    thread_id: str,
    tool_name: str,
    secret: str,
    ttl_seconds: int = 600,
) -> str:
    now = int(time.time())
    payload = {
        "thread_id": thread_id,
        "tool_name": tool_name,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def validate_approval_token(
    token: str,
    thread_id: str,
    tool_name: str,
    secret: str,
) -> bool:
    """Returns True if token is valid, unexpired, and scoped to this thread+tool."""
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"])
        return claims.get("thread_id") == thread_id and claims.get("tool_name") == tool_name
    except jwt.ExpiredSignatureError:
        return False
    except jwt.InvalidTokenError:
        return False
