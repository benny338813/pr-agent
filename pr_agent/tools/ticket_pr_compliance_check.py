import os
import re
import traceback

import requests

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import GithubProvider
from pr_agent.git_providers import AzureDevopsProvider
from pr_agent.log import get_logger

# Compile the regex pattern once, outside the function
GITHUB_TICKET_PATTERN = re.compile(
     r'(https://github[^/]+/[^/]+/[^/]+/issues/\d+)|(\b(\w+)/(\w+)#(\d+)\b)|(#\d+)'
)
# Option A: issue number at start of branch or after /, followed by - or end (e.g. feature/1-test-issue, 123-fix)
BRANCH_ISSUE_PATTERN = re.compile(r"(?:^|/)(\d{1,6})(?=-|$)")
JIRA_TICKET_PATTERN = re.compile(
    r"(?:https?://[^\s/]+(?:/[^\s/]+)*/browse/)?\b([A-Z][A-Z0-9]{1,9}-\d{1,7})\b"
)

def find_jira_tickets(text):
    if not text:
        return []
    return list(dict.fromkeys(JIRA_TICKET_PATTERN.findall(text)))


def _get_jira_setting(name: str, default=""):
    settings = get_settings()
    return (
        settings.get(f"jira.{name}", None) or
        settings.get(f"JIRA.{name.upper()}", None) or
        settings.get(name, None) or
        default
    )


def _as_jira_scalar(value, default=""):
    if value is None or value == "":
        return default
    if isinstance(value, (list, tuple)):
        return value[0] if value else default
    return value


def _get_jira_config():
    base_url = str(_as_jira_scalar(_get_jira_setting("jira_base_url"), os.getenv("JIRA_BASE_URL", ""))).rstrip("/")
    token = _as_jira_scalar(_get_jira_setting("jira_api_token"))
    token_env = _as_jira_scalar(_get_jira_setting("jira_api_token_env"))
    if token_env:
        token = os.getenv(str(token_env), token)
    elif not token:
        token = os.getenv("JIRA_BENNY_BOT_PAT", token)
    email = _as_jira_scalar(_get_jira_setting("jira_api_email"))
    timeout = int(_as_jira_scalar(_get_jira_setting("jira_timeout"), 30))
    return {
        "base_url": base_url,
        "token": token,
        "email": email,
        "timeout": timeout,
    }


def _get_jira_ticket_text(git_provider):
    parts = []
    title_method = getattr(git_provider, "get_title", None)
    if callable(title_method):
        try:
            title = title_method()
            if title:
                parts.append(str(title))
        except Exception as e:
            get_logger().debug(f"Failed to read PR title for Jira ticket extraction: {e}")
    for method_name in ("get_user_description", "get_pr_description"):
        method = getattr(git_provider, method_name, None)
        if not callable(method):
            continue
        try:
            value = method()
            if isinstance(value, tuple):
                value = value[0]
            if value:
                parts.append(str(value))
        except Exception as e:
            get_logger().debug(f"Failed to read PR description for Jira ticket extraction: {e}")
    try:
        branch = git_provider.get_pr_branch()
        if branch:
            parts.append(str(branch))
    except Exception as e:
        get_logger().debug(f"Failed to read PR branch for Jira ticket extraction: {e}")
    return "\n".join(parts)


def _extract_jira_ticket_keys(git_provider):
    return find_jira_tickets(_get_jira_ticket_text(git_provider))[:3]


def _jira_auth_headers(config):
    headers = {"Accept": "application/json"}
    if config["email"]:
        return headers, (config["email"], config["token"])
    headers["Authorization"] = f"Bearer {config['token']}"
    return headers, None


def _extract_jira_field_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_extract_jira_field_text(item) for item in value)))
    if isinstance(value, dict):
        if "text" in value and isinstance(value["text"], str):
            return value["text"]
        return "\n".join(filter(None, (_extract_jira_field_text(item) for item in value.values())))
    return str(value)


def _fetch_jira_ticket(ticket_key, config, max_characters):
    if not config["base_url"] or not config["token"]:
        get_logger().warning("Skipping Jira ticket fetch: jira_base_url or jira_api_token is not configured")
        return None

    headers, auth = _jira_auth_headers(config)
    url = f"{config['base_url']}/rest/api/2/issue/{ticket_key}"
    response = requests.get(url, headers=headers, auth=auth, timeout=config["timeout"])
    response.raise_for_status()
    issue = response.json()
    fields = issue.get("fields", {})
    body = _extract_jira_field_text(fields.get("description"))
    if len(body) > max_characters:
        body = body[:max_characters] + "..."
    labels = fields.get("labels") or []
    subtasks = []
    for subtask in fields.get("subtasks") or []:
        subtask_key = subtask.get("key", "")
        subtask_fields = subtask.get("fields", {})
        subtasks.append({
            "ticket_url": f"{config['base_url']}/browse/{subtask_key}" if subtask_key else "",
            "title": subtask_fields.get("summary", ""),
            "body": "",
        })
    return {
        "ticket_id": issue.get("key", ticket_key),
        "ticket_url": f"{config['base_url']}/browse/{issue.get('key', ticket_key)}",
        "title": fields.get("summary", ""),
        "body": body,
        "labels": ", ".join(labels),
        "sub_issues": subtasks,
    }


def fetch_jira_tickets(git_provider, max_characters):
    ticket_keys = _extract_jira_ticket_keys(git_provider)
    config = _get_jira_config()
    get_logger().info(
        "Jira ticket extraction completed",
        artifact={
            "ticket_keys": ticket_keys,
            "jira_base_url_configured": bool(config["base_url"]),
            "jira_token_configured": bool(config["token"]),
            "jira_auth_mode": "basic" if config["email"] else "pat",
        },
    )
    if not ticket_keys:
        return []

    tickets_content = []
    for ticket_key in ticket_keys:
        try:
            get_logger().info(f"Fetching Jira ticket {ticket_key}")
            ticket = _fetch_jira_ticket(ticket_key, config, max_characters)
            if ticket:
                tickets_content.append(ticket)
                get_logger().info(
                    f"Fetched Jira ticket {ticket_key}",
                    artifact={
                        "ticket_id": ticket.get("ticket_id"),
                        "title_present": bool(ticket.get("title")),
                        "description_present": bool(ticket.get("body")),
                        "sub_issues_count": len(ticket.get("sub_issues") or []),
                    },
                )
        except Exception as e:
            get_logger().warning(
                f"Failed to fetch Jira ticket {ticket_key}: {e}",
                artifact={"traceback": traceback.format_exc()},
            )
    get_logger().info(
        "Jira ticket fetch summary",
        artifact={"requested_count": len(ticket_keys), "fetched_count": len(tickets_content)},
    )
    return tickets_content


def extract_ticket_links_from_pr_description(pr_description, repo_path, base_url_html='https://github.com'):
    """
    Extract all ticket links from PR description
    """
    github_tickets = set()
    try:
        # Use the updated pattern to find matches
        matches = GITHUB_TICKET_PATTERN.findall(pr_description)

        for match in matches:
            if match[0]:  # Full URL match
                github_tickets.add(match[0])
            elif match[1]:  # Shorthand notation match: owner/repo#issue_number
                owner, repo, issue_number = match[2], match[3], match[4]
                github_tickets.add(f'{base_url_html.strip("/")}/{owner}/{repo}/issues/{issue_number}')
            else:  # #123 format
                issue_number = match[5][1:]  # remove #
                if issue_number.isdigit() and len(issue_number) < 5 and repo_path:
                    github_tickets.add(f'{base_url_html.strip("/")}/{repo_path}/issues/{issue_number}')

        if len(github_tickets) > 3:
            get_logger().info(f"Too many tickets found in PR description: {len(github_tickets)}")
            # Limit the number of tickets to 3
            github_tickets = set(list(github_tickets)[:3])
    except Exception as e:
        get_logger().error(f"Error extracting tickets error= {e}",
                           artifact={"traceback": traceback.format_exc()})

    return list(github_tickets)

def extract_ticket_links_from_branch_name(branch_name, repo_path, base_url_html="https://github.com"):
    """
    Extract GitHub issue URLs from branch name. Numbers are matched at start of branch or after /,
    followed by - or end (e.g. feature/1-test-issue -> #1). Respects extract_issue_from_branch
    and optional branch_issue_regex (may be under [config] in TOML).
    """
    if not branch_name or not repo_path:
        return []
    if not isinstance(branch_name, str):
        return []
    settings = get_settings()
    if not settings.get("extract_issue_from_branch", settings.get("config.extract_issue_from_branch", True)):
        return []
    github_tickets = set()
    custom_regex_str = settings.get("branch_issue_regex") or settings.get("config.branch_issue_regex", "") or ""
    if custom_regex_str:
        try:
            pattern = re.compile(custom_regex_str)
            if pattern.groups < 1:
                get_logger().error(
                    "branch_issue_regex must contain at least one capturing group for the issue number; using default pattern."
                )
                pattern = BRANCH_ISSUE_PATTERN
        except re.error as e:
            get_logger().error(f"Invalid custom regex for branch issue extraction: {e}")
            return []
    else:
        pattern = BRANCH_ISSUE_PATTERN
    for match in pattern.finditer(branch_name):
        try:
            issue_number = match.group(1)
        except IndexError:
            continue
        if issue_number and issue_number.isdigit():
            github_tickets.add(
                f"{base_url_html.strip('/')}/{repo_path}/issues/{issue_number}"
            )
    return list(github_tickets)


async def extract_tickets(git_provider):
    MAX_TICKET_CHARACTERS = 10000
    tickets_content = []
    try:
        tickets_content.extend(fetch_jira_tickets(git_provider, MAX_TICKET_CHARACTERS))

        if isinstance(git_provider, GithubProvider):
            user_description = git_provider.get_user_description()
            description_tickets = extract_ticket_links_from_pr_description(
                user_description, git_provider.repo, git_provider.base_url_html
            )
            branch_name = git_provider.get_pr_branch()
            branch_tickets = extract_ticket_links_from_branch_name(
                branch_name, git_provider.repo, git_provider.base_url_html
            )
            seen = set()
            merged = []
            for link in description_tickets + branch_tickets:
                if link not in seen:
                    seen.add(link)
                    merged.append(link)
            if len(merged) > 3:
                get_logger().info(f"Too many tickets (description + branch): {len(merged)}")
                tickets = merged[:3]
            else:
                tickets = merged
            if tickets:

                for ticket in tickets:
                    repo_name, original_issue_number = git_provider._parse_issue_url(ticket)

                    try:
                        issue_main = git_provider.repo_obj.get_issue(original_issue_number)
                    except Exception as e:
                        get_logger().error(f"Error getting main issue: {e}",
                                           artifact={"traceback": traceback.format_exc()})
                        continue

                    issue_body_str = issue_main.body or ""
                    if len(issue_body_str) > MAX_TICKET_CHARACTERS:
                        issue_body_str = issue_body_str[:MAX_TICKET_CHARACTERS] + "..."

                    # Extract sub-issues
                    sub_issues_content = []
                    try:
                        sub_issues = git_provider.fetch_sub_issues(ticket)
                        for sub_issue_url in sub_issues:
                            try:
                                sub_repo, sub_issue_number = git_provider._parse_issue_url(sub_issue_url)
                                sub_issue = git_provider.repo_obj.get_issue(sub_issue_number)

                                sub_body = sub_issue.body or ""
                                if len(sub_body) > MAX_TICKET_CHARACTERS:
                                    sub_body = sub_body[:MAX_TICKET_CHARACTERS] + "..."

                                sub_issues_content.append({
                                    'ticket_url': sub_issue_url,
                                    'title': sub_issue.title,
                                    'body': sub_body
                                })
                            except Exception as e:
                                get_logger().warning(f"Failed to fetch sub-issue content for {sub_issue_url}: {e}")

                    except Exception as e:
                        get_logger().warning(f"Failed to fetch sub-issues for {ticket}: {e}")

                    # Extract labels
                    labels = []
                    try:
                        for label in issue_main.labels:
                            labels.append(label.name if hasattr(label, 'name') else label)
                    except Exception as e:
                        get_logger().error(f"Error extracting labels error= {e}",
                                           artifact={"traceback": traceback.format_exc()})

                    tickets_content.append({
                        'ticket_id': issue_main.number,
                        'ticket_url': ticket,
                        'title': issue_main.title,
                        'body': issue_body_str,
                        'labels': ", ".join(labels),
                        'sub_issues': sub_issues_content  # Store sub-issues content
                    })

        elif isinstance(git_provider, AzureDevopsProvider):
            tickets_info = git_provider.get_linked_work_items()
            for ticket in tickets_info:
                try:
                    ticket_body_str = ticket.get("body", "")
                    if len(ticket_body_str) > MAX_TICKET_CHARACTERS:
                        ticket_body_str = ticket_body_str[:MAX_TICKET_CHARACTERS] + "..."

                    tickets_content.append(
                        {
                            "ticket_id": ticket.get("id"),
                            "ticket_url": ticket.get("url"),
                            "title": ticket.get("title"),
                            "body": ticket_body_str,
                            "requirements": ticket.get("acceptance_criteria", ""),
                            "labels": ", ".join(ticket.get("labels", [])),
                        }
                    )
                except Exception as e:
                    get_logger().error(
                        f"Error processing Azure DevOps ticket: {e}",
                        artifact={"traceback": traceback.format_exc()},
                    )
        return tickets_content

    except Exception as e:
        get_logger().error(f"Error extracting tickets error= {e}",
                           artifact={"traceback": traceback.format_exc()})


async def extract_and_cache_pr_tickets(git_provider, vars):
    if not get_settings().get('pr_reviewer.require_ticket_analysis_review', False):
        get_logger().info("Ticket analysis review is disabled; skipping ticket extraction")
        return

    related_tickets = get_settings().get('related_tickets', [])

    if not related_tickets:
        tickets_content = await extract_tickets(git_provider)

        if tickets_content:
            # Store sub-issues along with main issues
            for ticket in tickets_content:
                if "sub_issues" in ticket and ticket["sub_issues"]:
                    for sub_issue in ticket["sub_issues"]:
                        related_tickets.append(sub_issue)  # Add sub-issues content

                related_tickets.append(ticket)

            get_logger().info("Extracted tickets and sub-issues from PR description",
                              artifact={"tickets": related_tickets})

            vars['related_tickets'] = related_tickets
            get_settings().set('related_tickets', related_tickets)
        else:
            get_logger().info("No related tickets were extracted for this PR")
    else:
        get_logger().info("Using cached tickets", artifact={"tickets": related_tickets})
        vars['related_tickets'] = related_tickets


def check_tickets_relevancy():
    return True
