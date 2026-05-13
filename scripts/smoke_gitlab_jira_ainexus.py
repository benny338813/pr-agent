#!/usr/bin/env python3
import argparse
import os
import sys

import requests

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.gitlab_provider import GitLabProvider
from pr_agent.tools.ticket_pr_compliance_check import fetch_jira_tickets, find_jira_tickets


DEFAULT_MR_URL = "https://fw-git2.phison.com/d9/ps5302-qlc/code/fw/x2_qlc_r5/-/merge_requests/3591"
DEFAULT_GITLAB_URL = "https://fw-git2.phison.com"
DEFAULT_JIRA_URL = "https://jira.phison.com:8443"
DEFAULT_AINEXUS_URL = "https://ainexus.phison.com/api/external/v1"
DEFAULT_MODEL = "openai/qwen/qwen3.6-35b-a3b-fp8"


def mask_state(name):
    return "set" if os.getenv(name) else "missing"


def configure(args):
    os.environ.setdefault("GITLAB_URL", args.gitlab_url)
    os.environ.setdefault("GITLAB_AUTH_TYPE", args.gitlab_auth_type)
    os.environ.setdefault("CONFIG__GIT_PROVIDER", "gitlab")

    settings = get_settings()
    settings.set("CONFIG.GIT_PROVIDER", "gitlab")
    settings.set("CONFIG.MODEL", args.model)
    settings.set("CONFIG.FALLBACK_MODELS", [])
    settings.set("CONFIG.CUSTOM_MODEL_MAX_TOKENS", args.max_tokens)
    settings.set("CONFIG.MAX_MODEL_TOKENS", args.max_tokens)
    settings.set("CONFIG.TOKEN_ENCODING", "cl100k_base")
    settings.set("OPENAI.API_BASE", args.ainexus_url)
    settings.set("OPENAI.KEY_ENV", "AI_NEXUUS_PAT")
    settings.set("LITELLM.DROP_PARAMS", True)
    settings.set("JIRA.JIRA_BASE_URL", args.jira_url)
    settings.set("JIRA.JIRA_API_TOKEN_ENV", "JIRA_BENNY_BOT_PAT")


def check_env():
    print("Environment:")
    for name in ("GITLAB_TOKEN", "AI_NEXUUS_PAT", "JIRA_BENNY_BOT_PAT"):
        print(f"  {name}: {mask_state(name)}")
    print()


def check_gitlab(mr_url):
    print("GitLab MR:")
    provider = GitLabProvider(mr_url)
    files = provider.get_files()
    text = "\n".join([
        provider.mr.title or "",
        provider.mr.description or "",
        provider.mr.source_branch or "",
    ])
    jira_keys = find_jira_tickets(text)
    print("  status: ok")
    print(f"  project: {provider.id_project}")
    print(f"  mr: {provider.id_mr}")
    print(f"  title: {provider.mr.title}")
    print(f"  branch: {provider.mr.source_branch} -> {provider.mr.target_branch}")
    print(f"  changed files: {len(files)}")
    print("  first files: " + ", ".join(getattr(f, "filename", str(f)) for f in files[:5]))
    print("  jira keys: " + (", ".join(jira_keys) if jira_keys else "none"))
    print()
    return provider, jira_keys


def check_jira(provider, skip):
    print("Jira:")
    if skip:
        print("  status: skipped")
        print()
        return
    if not os.getenv("JIRA_BENNY_BOT_PAT"):
        print("  status: skipped, JIRA_BENNY_BOT_PAT is missing")
        print()
        return
    tickets = fetch_jira_tickets(provider, 10000)
    print(f"  status: ok, tickets={len(tickets)}")
    for ticket in tickets:
        print(f"  - {ticket.get('ticket_id')}: {ticket.get('title')}")
        print(f"    {ticket.get('ticket_url')}")
    print()


def check_ainexus(args):
    print("Ainexus:")
    token = os.getenv("AI_NEXUUS_PAT")
    if not token:
        print("  status: skipped, AI_NEXUUS_PAT is missing")
        print()
        return
    url = args.ainexus_url.rstrip("/") + "/models"
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=30,
        verify=not args.insecure,
    )
    print(f"  GET {url}")
    print(f"  status_code: {response.status_code}")
    if response.ok:
        payload = response.json()
        data = payload.get("data", payload if isinstance(payload, list) else [])
        model_ids = [
            item.get("id", str(item))
            for item in data[:10]
            if isinstance(item, dict) or item
        ]
        print("  first models: " + (", ".join(model_ids) if model_ids else "none"))
    else:
        print("  response: " + response.text[:500])
    print()


def main():
    parser = argparse.ArgumentParser(description="Smoke test GitLab MR, Jira ticket fetch, and Ainexus auth.")
    parser.add_argument("--mr-url", default=DEFAULT_MR_URL)
    parser.add_argument("--gitlab-url", default=DEFAULT_GITLAB_URL)
    parser.add_argument("--gitlab-auth-type", default="private_token", choices=["private_token", "oauth_token"])
    parser.add_argument("--jira-url", default=DEFAULT_JIRA_URL)
    parser.add_argument("--ainexus-url", default=DEFAULT_AINEXUS_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-tokens", type=int, default=32768)
    parser.add_argument("--skip-jira", action="store_true")
    parser.add_argument("--skip-ainexus", action="store_true")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification for smoke HTTP checks.")
    args = parser.parse_args()

    configure(args)
    check_env()
    provider, _ = check_gitlab(args.mr_url)
    check_jira(provider, args.skip_jira)
    if not args.skip_ainexus:
        check_ainexus(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
