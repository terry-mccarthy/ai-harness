"""Environment configuration, RSA key loading, and OAuth client registry."""
import base64
import hashlib
import os

from cryptography.hazmat.primitives.serialization import load_pem_private_key


# Tripwire: refuse to start with the committed test key unless ENV=test
_TEST_KEY_FINGERPRINT = "sha256:f51572658f267e254a18caf6d2320581aacfbaee028a2a875a8a47af4630ffb5"

_key_file = os.environ.get("JWT_PRIVATE_KEY_FILE")
if _key_file:
    with open(_key_file, "rb") as _f:
        _jwt_private_key_pem = _f.read()
else:
    _jwt_private_key_pem = os.environ["JWT_PRIVATE_KEY"].encode()

_key_fingerprint = "sha256:" + hashlib.sha256(_jwt_private_key_pem).hexdigest()
if _key_fingerprint == _TEST_KEY_FINGERPRINT and os.environ.get("ENV") != "test":
    raise RuntimeError(
        "Governance is configured with the committed test key. "
        "Set ENV=test or supply a production key via JWT_PRIVATE_KEY_FILE."
    )

PRIVATE_KEY = load_pem_private_key(_jwt_private_key_pem, password=None)
PUBLIC_KEY = PRIVATE_KEY.public_key()


def b64url(n: int) -> str:
    byte_len = (n.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(n.to_bytes(byte_len, "big")).rstrip(b"=").decode()


OPA_URL = os.environ.get("OPA_URL", "http://opa:8181")
DOLT_HOST = os.environ.get("DOLT_HOST", "dolt")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))
DOLT_USER = os.environ.get("DOLT_USER", "harness")
DOLT_PASSWORD = os.environ.get("DOLT_PASSWORD", "harness")
DOLT_DB = os.environ.get("DOLT_DB", "harness")
TOKEN_TTL = int(os.environ.get("TOKEN_TTL", "900"))
EXPIRY_PASS_INTERVAL = int(os.environ.get("EXPIRY_PASS_INTERVAL", "1000"))


CLIENTS = {
    "architect": {
        "secret": os.environ.get("ARCHITECT_SECRET", "architect-secret"),
        "role": "architect",
    },
    "code-reviewer": {
        "secret": os.environ["CODE_REVIEWER_SECRET"],
        "role": "code_reviewer",
    },
    "sre": {
        "secret": os.environ.get("SRE_SECRET", "sre-secret"),
        "role": "sre",
    },
    "human-operator": {
        "secret": os.environ.get("HUMAN_OPERATOR_SECRET", "human-operator-secret"),
        "role": "human_operator",
    },
}
