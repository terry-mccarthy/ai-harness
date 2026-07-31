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
