"""
Context fetcher — pulls real-time data from Jira and staging before each standup.

This gives Max actual knowledge about what's happening in the sprint,
so his standup updates reference real tickets and real status.
"""

import os
from datetime import datetime

import httpx
from loguru import logger


async def fetch_jira_context() -> str:
    """Fetch current sprint data from Jira for Max's standup context."""
    jira_url = os.getenv("JIRA_URL", "https://everperform.atlassian.net")
    jira_email = os.getenv("JIRA_EMAIL")
    jira_token = os.getenv("JIRA_API_TOKEN")
    project_key = os.getenv("JIRA_PROJECT_KEY", "ESB")

    if not jira_email or not jira_token:
        logger.warning("Jira credentials not configured — Max won't have sprint context")
        return "No Jira context available."

    try:
        async with httpx.AsyncClient() as client:
            # Fetch current sprint issues
            jql = (
                f"project = {project_key} AND sprint in openSprints() "
                f"ORDER BY status ASC, priority DESC"
            )
            response = await client.get(
                f"{jira_url}/rest/api/3/search",
                params={"jql": jql, "maxResults": 30, "fields": "summary,status,assignee,priority"},
                auth=(jira_email, jira_token),
                timeout=10,
            )

            if response.status_code != 200:
                logger.error(f"Jira API returned {response.status_code}")
                return "Could not fetch Jira data."

            data = response.json()
            issues = data.get("issues", [])

            if not issues:
                return "No issues found in the current sprint."

            # Format sprint context for Max
            lines = [f"### Current Sprint ({len(issues)} issues)"]
            status_groups = {}
            for issue in issues:
                fields = issue["fields"]
                status = fields["status"]["name"]
                summary = fields["summary"]
                key = issue["key"]
                assignee = (
                    fields.get("assignee", {}).get("displayName", "Unassigned")
                    if fields.get("assignee")
                    else "Unassigned"
                )

                if status not in status_groups:
                    status_groups[status] = []
                status_groups[status].append(f"- {key}: {summary} (Assignee: {assignee})")

            for status, items in status_groups.items():
                lines.append(f"\n**{status}:**")
                lines.extend(items)

            return "\n".join(lines)

    except Exception as e:
        logger.error(f"Error fetching Jira context: {e}")
        return f"Error fetching Jira data: {str(e)}"


async def check_staging_health() -> str:
    """Quick health check on the staging environment."""
    staging_url = os.getenv("STAGING_URL", "https://v2.staging.everperform.com")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(staging_url, timeout=10, follow_redirects=True)
            if response.status_code == 200:
                return f"Staging ({staging_url}) is UP and healthy."
            else:
                return f"Staging returned HTTP {response.status_code} — may have issues."
    except Exception as e:
        return f"Staging is DOWN or unreachable: {str(e)}"


async def build_standup_context() -> str:
    """Build full context for Max before joining standup."""
    context_parts = []

    # Jira sprint data
    jira = await fetch_jira_context()
    context_parts.append(jira)

    # Staging health
    staging = await check_staging_health()
    context_parts.append(staging)

    # Today's date
    context_parts.append(f"Current date/time: {datetime.now().strftime('%A %d %B %Y, %H:%M AEST')}")

    return "\n\n".join(context_parts)
