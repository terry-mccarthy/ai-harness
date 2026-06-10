"""ContextForge setup init container.

Registers all MCP stub servers with ContextForge, then creates a single
virtual server (harness_all) that aggregates all discovered tools.

Runs once at stack startup; idempotent (existing registrations are re-used).

Environment variables:
  CF_URL              ContextForge base URL (default: http://contextforge:4444)
  CF_JWT_SECRET       JWT signing secret (must match ContextForge's JWT_SECRET_KEY)
  CF_ADMIN_EMAIL      Admin subject claim (default: admin@harness.local)
  CF_SERVER_NAME      Virtual server name (default: harness_all)
  STUBS               Comma-separated name=url pairs for MCP stubs
"""
import json
import logging
import os
import sys
import time
import uuid

import httpx
import jwt as pyjwt

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("cf-setup")

CF_URL = os.environ.get("CF_URL", "http://contextforge:4444").rstrip("/")
CF_JWT_SECRET = os.environ.get(
    "CF_JWT_SECRET", "cf-dev-secret-key-at-least-32-bytes-long"
)
CF_ADMIN_EMAIL = os.environ.get("CF_ADMIN_EMAIL", "admin@harness.local")
CF_SERVER_NAME = os.environ.get("CF_SERVER_NAME", "harness_all")

STUBS_RAW = os.environ.get(
    "STUBS",
    (
        "git_diff_stub=http://git-diff-stub:9001/mcp,"
        "linter_stub=http://linter-stub:9002/mcp,"
        "architect_stub=http://architect-stub:9004/mcp,"
        "sre_stub=http://sre-stub:9005/mcp"
    ),
)


def _token() -> str:
    now = int(time.time())
    return pyjwt.encode(
        {
            "sub": CF_ADMIN_EMAIL,
            "preferred_username": "admin",
            "iat": now,
            "iss": "mcpgateway",
            "aud": "mcpgateway-api",
            "jti": str(uuid.uuid4()),
            "exp": now + 3600,
        },
        CF_JWT_SECRET,
        algorithm="HS256",
    )


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


def _wait_for_cf(retries: int = 30, delay: float = 5.0) -> None:
    log.info("Waiting for ContextForge at %s …", CF_URL)
    for i in range(retries):
        try:
            r = httpx.get(f"{CF_URL}/health", timeout=5.0)
            if r.status_code == 200:
                log.info("ContextForge is up.")
                return
        except Exception:
            pass
        log.info("  attempt %d/%d — not ready yet", i + 1, retries)
        time.sleep(delay)
    sys.exit("ContextForge did not become ready in time")


def _register_gateway(client: httpx.Client, name: str, url: str) -> str | None:
    """Register a gateway; return its ID (or None on failure)."""
    # Check if already registered
    resp = client.get(f"{CF_URL}/gateways", headers=_headers())
    resp.raise_for_status()
    for gw in resp.json():
        if gw.get("name") == name:
            log.info("Gateway '%s' already registered (id=%s)", name, gw["id"])
            return gw["id"]

    log.info("Registering gateway '%s' → %s", name, url)
    resp = client.post(
        f"{CF_URL}/gateways",
        headers=_headers(),
        json={"name": name, "url": url, "transport": "STREAMABLEHTTP"},
        timeout=20.0,
    )
    if resp.status_code in (200, 201):
        gw = resp.json()
        log.info("Registered gateway '%s' (id=%s)", name, gw["id"])
        return gw["id"]
    log.warning("Failed to register '%s': %d %s", name, resp.status_code, resp.text[:200])
    return None


def _list_tools(client: httpx.Client) -> list[str]:
    """Return all tool IDs discovered across registered gateways."""
    resp = client.get(f"{CF_URL}/tools", headers=_headers())
    resp.raise_for_status()
    return [t["id"] for t in resp.json()]


def _ensure_virtual_server(client: httpx.Client, tool_ids: list[str]) -> str:
    """Create or return the harness_all virtual server UUID."""
    resp = client.get(f"{CF_URL}/servers", headers=_headers())
    resp.raise_for_status()
    for srv in resp.json():
        if srv.get("name") == CF_SERVER_NAME:
            log.info("Virtual server '%s' already exists (id=%s)", CF_SERVER_NAME, srv["id"])
            return srv["id"]

    log.info("Creating virtual server '%s' with %d tools", CF_SERVER_NAME, len(tool_ids))
    resp = client.post(
        f"{CF_URL}/servers",
        headers=_headers(),
        json={"server": {"name": CF_SERVER_NAME, "associated_tools": tool_ids}},
    )
    resp.raise_for_status()
    srv = resp.json()
    log.info("Created virtual server '%s' (id=%s)", CF_SERVER_NAME, srv["id"])
    return srv["id"]


def main() -> None:
    _wait_for_cf()

    stubs = {}
    for entry in STUBS_RAW.split(","):
        entry = entry.strip()
        if "=" in entry:
            name, url = entry.split("=", 1)
            stubs[name.strip()] = url.strip()

    with httpx.Client(timeout=30.0) as client:
        for name, url in stubs.items():
            _register_gateway(client, name, url)

        # Brief pause for ContextForge to discover tools from stubs
        time.sleep(3)

        tool_ids = _list_tools(client)
        log.info("Discovered %d tools total", len(tool_ids))

        server_uuid = _ensure_virtual_server(client, tool_ids)
        log.info("ContextForge ready. CF_SERVER_UUID=%s", server_uuid)

        # Write UUID to a known location so governance can read it at startup
        out_path = os.environ.get("CF_UUID_OUTPUT", "/tmp/cf_server_uuid.txt")
        try:
            with open(out_path, "w") as f:
                f.write(server_uuid)
            log.info("Server UUID written to %s", out_path)
        except Exception as e:
            log.warning("Could not write UUID file: %s", e)

    sys.stdout.write(f"CF_SERVER_UUID={server_uuid}\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
