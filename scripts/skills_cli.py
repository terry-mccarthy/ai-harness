#!/usr/bin/env python3
"""CLI for managing the AI Harness skill-learning pipeline.

Global options (before subcommand):
  --url URL       Governance base URL (default: $GOVERNANCE_URL or http://localhost:8090)
  --client CLIENT OAuth client ID used to obtain a token (default: sre)
  --secret SECRET OAuth client secret (default: ${CLIENT}_SECRET env var or {client}-secret)

Commands:
  token                         Print the raw token response for the given client
  pipeline                      Pipeline state: episode/candidate/skill counts
  episodes list                 List recent episodes
  episodes label ID             Label an episode outcome
  candidates list               List candidates
  candidates propose            Propose a candidate from labeled episodes
  candidates promote ID         Promote a candidate to an active skill
  candidates reject ID          Reject a candidate
  skills list                   List skills
  skills get ID                 Get a single skill by ID
  skills select                 Run skill selection
  skills revoke ID              Revoke an active skill
"""

import argparse
import json
import os
import sys

import httpx


class SkillsClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    @classmethod
    def from_client_credentials(cls, base_url: str, client_id: str, secret: str) -> "SkillsClient":
        resp = httpx.post(
            f"{base_url.rstrip('/')}/oauth/token",
            data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret},
            timeout=10.0,
        )
        resp.raise_for_status()
        return cls(base_url, resp.json()["access_token"])

    def _get(self, path: str, **params) -> object:
        resp = httpx.get(f"{self._base}{path}", headers=self._headers, params=params or None, timeout=10.0)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict | None = None) -> object:
        resp = httpx.post(f"{self._base}{path}", headers=self._headers, json=body or {}, timeout=10.0)
        resp.raise_for_status()
        return resp.json()

    # --- token ---

    @staticmethod
    def get_token_response(base_url: str, client_id: str, secret: str) -> dict:
        resp = httpx.post(
            f"{base_url.rstrip('/')}/oauth/token",
            data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()

    # --- episodes ---

    def list_episodes(self, limit: int = 20, unlabeled: bool = False) -> list[dict]:
        return self._get("/episodes", limit=limit, unlabeled=str(unlabeled).lower())

    def label_episode(self, episode_id: str, outcome: str, signal: dict, labeler: str = "human-operator") -> dict:
        return self._post(
            f"/episodes/{episode_id}/label",
            {"outcome": outcome, "outcome_signal": signal, "labeler_principal": labeler},
        )

    # --- candidates ---

    def list_candidates(self, status: str | None = None) -> list[dict]:
        params = {"status": status} if status else {}
        return self._get("/candidates", **params)

    def propose_candidate(self, cluster_key: str, episode_ids: list[str], procedure: dict | None = None) -> dict:
        return self._post("/candidates", {
            "cluster_key": cluster_key,
            "episode_ids": episode_ids,
            "proposed_procedure": procedure or {},
        })

    def promote_candidate(self, candidate_id: str) -> dict:
        return self._post(f"/candidates/{candidate_id}/promote")

    def reject_candidate(self, candidate_id: str, reason: str) -> dict:
        return self._post(f"/candidates/{candidate_id}/reject", {"reason": reason})

    # --- skills ---

    def list_skills(self, status: str | None = None) -> list[dict]:
        params = {"status": status} if status else {}
        return self._get("/skills", **params)

    def get_skill(self, skill_id: str) -> dict:
        return self._get(f"/skills/{skill_id}")

    def select_skill(
        self,
        fingerprint: dict | None = None,
        alert_sig: str = "cli.select",
        service_class: str = "stateless-api",
    ) -> dict:
        return self._post("/skills/select", {
            "alert_signature": alert_sig,
            "service_class": service_class,
            "env_fingerprint": fingerprint or {},
            "invoking_principal": "skills-cli",
        })

    def revoke_skill(self, skill_id: str, reason: str) -> dict:
        return self._post(f"/skills/{skill_id}/revoke", {"reason": reason})

    # --- pipeline summary ---

    def _count_by_status(self, items: list[dict], key: str) -> dict[str, int]:
        by_status: dict[str, int] = {}
        for item in items:
            s = (item.get(key) or "unknown").lower()
            by_status[s] = by_status.get(s, 0) + 1
        return by_status

    def pipeline_summary(self) -> dict:
        episodes = self.list_episodes(limit=10000)
        unlabeled = sum(1 for e in episodes if not e.get("outcome_labeled_at"))
        candidates = self.list_candidates()
        skills = self.list_skills()
        return {
            "episodes": {"total": len(episodes), "unlabeled": unlabeled},
            "candidates": self._count_by_status(candidates, "status"),
            "skills": self._count_by_status(skills, "status"),
        }


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def _resolve_secret(client_id: str, secret_arg: str | None) -> str:
    if secret_arg:
        return secret_arg
    env_key = f"{client_id.upper().replace('-', '_')}_SECRET"
    return os.environ.get(env_key, f"{client_id}-secret")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="AI Harness skills pipeline CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--url", default=os.environ.get("GOVERNANCE_URL", "http://localhost:8090"))
    p.add_argument("--client", default="sre")
    p.add_argument("--secret", default=None)

    sub = p.add_subparsers(dest="command", required=True)

    # token
    tok = sub.add_parser("token", help="Print OAuth token response")
    tok.add_argument("--client", dest="tok_client", default=None)
    tok.add_argument("--secret", dest="tok_secret", default=None)

    # pipeline
    sub.add_parser("pipeline", help="Pipeline state summary")

    # episodes
    ep = sub.add_parser("episodes")
    ep_sub = ep.add_subparsers(dest="ep_cmd", required=True)

    ep_list = ep_sub.add_parser("list")
    ep_list.add_argument("--limit", type=int, default=20)
    ep_list.add_argument("--unlabeled", action="store_true")

    ep_label = ep_sub.add_parser("label")
    ep_label.add_argument("id")
    ep_label.add_argument("--outcome", required=True,
                          choices=["RESOLVED", "FAILED", "ROLLED_BACK", "HUMAN_OVERRIDE", "INCONCLUSIVE"])
    ep_label.add_argument("--signal", default="{}")
    ep_label.add_argument("--labeler", default="human-operator")

    # candidates
    cand = sub.add_parser("candidates")
    cand_sub = cand.add_subparsers(dest="cand_cmd", required=True)

    cand_list = cand_sub.add_parser("list")
    cand_list.add_argument("--status", default=None)

    cand_prop = cand_sub.add_parser("propose")
    cand_prop.add_argument("--cluster-key", required=True)
    cand_prop.add_argument("--episodes", required=True, help="Comma-separated episode IDs")
    cand_prop.add_argument("--procedure", default="{}")

    cand_prom = cand_sub.add_parser("promote")
    cand_prom.add_argument("id")

    cand_rej = cand_sub.add_parser("reject")
    cand_rej.add_argument("id")
    cand_rej.add_argument("--reason", required=True)

    # skills
    sk = sub.add_parser("skills")
    sk_sub = sk.add_subparsers(dest="sk_cmd", required=True)

    sk_list = sk_sub.add_parser("list")
    sk_list.add_argument("--status", default=None)

    sk_get = sk_sub.add_parser("get")
    sk_get.add_argument("id")

    sk_sel = sk_sub.add_parser("select")
    sk_sel.add_argument("--fingerprint", default="{}")
    sk_sel.add_argument("--alert-sig", default="cli.select")
    sk_sel.add_argument("--service-class", default="stateless-api")

    sk_rev = sk_sub.add_parser("revoke")
    sk_rev.add_argument("id")
    sk_rev.add_argument("--reason", required=True)

    return p


def _out(data: object) -> None:
    print(json.dumps(data, indent=2))


def _handle_episodes(args, client: SkillsClient) -> None:
    if args.ep_cmd == "list":
        _out(client.list_episodes(limit=args.limit, unlabeled=args.unlabeled))
    elif args.ep_cmd == "label":
        _out(client.label_episode(args.id, args.outcome, json.loads(args.signal), args.labeler))


def _handle_candidates(args, client: SkillsClient) -> None:
    if args.cand_cmd == "list":
        _out(client.list_candidates(status=args.status))
    elif args.cand_cmd == "propose":
        eids = [e.strip() for e in args.episodes.split(",")]
        _out(client.propose_candidate(args.cluster_key, eids, json.loads(args.procedure)))
    elif args.cand_cmd == "promote":
        _out(client.promote_candidate(args.id))
    elif args.cand_cmd == "reject":
        _out(client.reject_candidate(args.id, args.reason))


def _handle_skills(args, client: SkillsClient) -> None:
    if args.sk_cmd == "list":
        _out(client.list_skills(status=args.status))
    elif args.sk_cmd == "get":
        _out(client.get_skill(args.id))
    elif args.sk_cmd == "select":
        _out(client.select_skill(json.loads(args.fingerprint), args.alert_sig, args.service_class))
    elif args.sk_cmd == "revoke":
        _out(client.revoke_skill(args.id, args.reason))


_HANDLERS = {
    "episodes": _handle_episodes,
    "candidates": _handle_candidates,
    "skills": _handle_skills,
}


def main() -> None:
    p = _build_parser()
    args = p.parse_args()

    if args.command == "token":
        url = args.url
        client_id = args.tok_client or args.client
        secret = _resolve_secret(client_id, args.tok_secret or args.secret)
        _out(SkillsClient.get_token_response(url, client_id, secret))
        return

    secret = _resolve_secret(args.client, args.secret)
    client = SkillsClient.from_client_credentials(args.url, args.client, secret)

    if args.command == "pipeline":
        _out(client.pipeline_summary())
    elif args.command in _HANDLERS:
        _HANDLERS[args.command](args, client)


if __name__ == "__main__":
    main()
