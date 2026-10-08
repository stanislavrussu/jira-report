"""Jira report: counts issues created, resolved and currently open, then emails the result.

Runs as an AWS Lambda handler or directly from a terminal. Standard library only, so the
deployment package is this single file and no build step is required.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
# Imported by name: "html" is also the natural name for a rendered message, and shadowing hurts.
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

LOG = logging.getLogger("jira_report")
HTTP_TIMEOUT_SECONDS = 30
HTTP_ATTEMPTS = 3
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
ISSUE_LINKS = 5
PROJECT_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
CLOUD_ID_PATTERN = re.compile(r"^[0-9a-f-]{36}$")
# Issue types and priorities are free text in Jira, so bound them before they reach a query.
JQL_VALUE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,48}$")

# JQL bucket key and the label shown to the reader.
BUCKETS = (("created", "Created"), ("resolved", "Resolved"), ("open", "Open now"))

CAVEATS = (
    "Open now is counted when the report runs, not at the end of the window.",
    "An issue created and resolved inside the window appears in both figures.",
)


class JiraError(RuntimeError):
    """Jira returned a response the report cannot be built from."""


class ConfigError(ValueError):
    """The supplied configuration is unusable."""


@dataclass(frozen=True)
class Config:
    site_url: str  # the site itself; the email links here, because a person clicks them
    api_base: str  # the api.atlassian.com gateway, the only base a scoped token is accepted at
    email: str
    api_token: str = field(repr=False)  # keeps the token out of tracebacks and debug logging
    project_key: str
    timezone: str
    window_days: int
    sender: str
    recipient: str
    issue_types: tuple[str, ...] = ()
    priorities: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> Config:
        site_url = _require("JIRA_BASE_URL").rstrip("/")
        if not site_url.startswith("https://"):
            raise ConfigError("JIRA_BASE_URL must start with https://")

        project_key = _require("JIRA_PROJECT_KEY")
        if not PROJECT_KEY_PATTERN.match(project_key):
            raise ConfigError(
                f"JIRA_PROJECT_KEY {project_key!r} is not a valid Jira key "
                "(2-10 chars, uppercase letters and digits, starting with a letter)"
            )

        tz = os.environ.get("REPORT_TIMEZONE", "Europe/Berlin")
        try:
            ZoneInfo(tz)
        except (KeyError, ValueError) as exc:
            raise ConfigError(f"REPORT_TIMEZONE {tz!r} is not a known IANA time zone") from exc

        email, api_token = _credentials()

        # A zero or negative window would report zeroes rather than fail, so reject it here.
        window_days = os.environ.get("REPORT_WINDOW_DAYS", "7")
        if not window_days.isdigit() or int(window_days) < 1:
            raise ConfigError(f"REPORT_WINDOW_DAYS {window_days!r} must be a whole number, 1 or more")

        return cls(
            site_url=site_url,
            api_base=resolve_api_base(site_url),
            email=email,
            api_token=api_token,
            project_key=project_key,
            timezone=tz,
            window_days=int(window_days),
            sender=_require("REPORT_SENDER"),
            recipient=_require("REPORT_RECIPIENT"),
            issue_types=_jql_list("JIRA_ISSUE_TYPES"),
            priorities=_jql_list("JIRA_PRIORITIES"),
        )


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"environment variable {name} is required")
    return value


def _jql_list(name: str) -> tuple[str, ...]:
    """Read an optional comma-separated filter, rejecting anything that could alter the query."""
    values = tuple(v.strip() for v in os.environ.get(name, "").split(",") if v.strip())
    for value in values:
        if not JQL_VALUE_PATTERN.match(value):
            raise ConfigError(f"{name} contains an unusable value {value!r}")
    return values


def _credentials() -> tuple[str, str]:
    """Read Jira credentials from Secrets Manager when available, otherwise from the environment."""
    secret_id = os.environ.get("JIRA_SECRET_ID", "").strip()
    if not secret_id:
        return _require("JIRA_EMAIL"), _require("JIRA_API_TOKEN")

    import boto3  # type: ignore  # from the Lambda runtime

    payload = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)
    secret = json.loads(payload["SecretString"])
    try:
        return secret["jira_email"], secret["jira_api_token"]
    except KeyError as exc:
        raise ConfigError(
            f"secret {secret_id!r} must contain the keys 'jira_email' and 'jira_api_token'"
        ) from exc


def reporting_window(now: datetime, tz_name: str, window_days: int) -> tuple[datetime, datetime]:
    """Return [start, end) covering the last `window_days` full days in the given time zone."""
    tz = ZoneInfo(tz_name)
    end = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    # An absolute timedelta shifts the wall clock across a DST change, so re-apply midnight.
    start = (end - timedelta(days=window_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end


def parse_window(since: str, until: str, tz_name: str) -> tuple[datetime, datetime]:
    """Turn a caller-supplied since/until pair into the midnight pair reporting_window produces."""
    tz = ZoneInfo(tz_name)
    try:
        start, end = (datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=tz) for d in (since, until))
    except ValueError as exc:
        raise ConfigError("since and until must both be dates in YYYY-MM-DD form") from exc
    if start >= end:
        raise ConfigError(f"since {since} is not earlier than until {until}")
    return start, end


def build_jql(bucket: str, config: Config, start: datetime, end: datetime) -> str:
    """Build a bounded JQL query. The enhanced search API rejects unbounded queries with HTTP 400."""
    clauses = [f'project = "{config.project_key}"']
    if config.issue_types:
        clauses.append(_in_clause("issuetype", config.issue_types))
    if config.priorities:
        clauses.append(_in_clause("priority", config.priorities))

    if bucket == "open":
        # Status categories survive workflow customisation; literal status names do not.
        clauses.append("statusCategory != Done")
    else:
        # The remaining bucket keys are also the names of the JQL date fields they filter on.
        clauses.append(f'{bucket} >= "{start:%Y-%m-%d %H:%M}"')
        clauses.append(f'{bucket} < "{end:%Y-%m-%d %H:%M}"')
    return " AND ".join(clauses)


def _in_clause(field: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f'"{value}"' for value in values)
    return f"{field} IN ({quoted})"


def _http_json(url: str, headers: dict | None = None, payload: dict | None = None) -> dict:
    """Fetch JSON, POST when a payload is given, retrying the failures worth retrying."""
    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Accept": "application/json", **(headers or {})},
        method="POST" if payload is not None else "GET",
    )
    for remaining in reversed(range(HTTP_ATTEMPTS)):
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            problem = f"HTTP {exc.code} from {url}: {body}"
            retryable = exc.code in RETRY_STATUSES
            # Jira names its own pause when it rate-limits; prefer it over our guess.
            pause = float(exc.headers.get("Retry-After") or 0)
        except urllib.error.URLError as exc:
            problem, retryable, pause = f"could not reach {url}: {exc.reason}", True, 0.0
        except json.JSONDecodeError:
            problem, retryable, pause = f"{url} answered with something that is not JSON", False, 0.0

        if not remaining or not retryable:
            raise JiraError(problem) from None
        pause = pause or 2.0 ** (HTTP_ATTEMPTS - remaining)
        LOG.warning("%s - retrying in %.0fs", problem, pause)
        time.sleep(pause)


def resolve_api_base(site_url: str) -> str:
    """A scoped token is rejected by the site address; only the api.atlassian.com gateway takes it."""
    cloud_id = _http_json(f"{site_url}/_edge/tenant_info").get("cloudId", "")
    if not CLOUD_ID_PATTERN.match(cloud_id):
        raise ConfigError(f"{site_url} returned {cloud_id!r} instead of a cloud ID; check JIRA_BASE_URL")
    return f"https://api.atlassian.com/ex/jira/{cloud_id}"


def jira_request(config: Config, path: str, payload: dict | None = None) -> dict:
    token = base64.b64encode(f"{config.email}:{config.api_token}".encode()).decode()
    return _http_json(
        f"{config.api_base}{path}",
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
        payload=payload,
    )


def verify_account(config: Config) -> None:
    """JQL dates resolve in the API account's time zone, so a mismatch silently shifts the window."""
    account = jira_request(config, "/rest/api/3/myself")
    actual = account.get("timeZone")
    if actual != config.timezone:
        raise ConfigError(
            f"the Jira account's time zone is {actual!r} but REPORT_TIMEZONE is "
            f"{config.timezone!r}. JQL interprets date literals in the account's zone, so the "
            "reporting window would be wrong. Change one of them to match: in Jira, gear icon "
            "> Personal Jira settings > General settings."
        )
    LOG.info("account=%s timezone=%s", account.get("displayName"), actual)


def count_issues(config: Config, jql: str) -> int:
    result = jira_request(config, "/rest/api/3/search/approximate-count", {"jql": jql})
    try:
        return int(result["count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise JiraError(f"unexpected count response for JQL {jql!r}: {result!r}") from exc


def navigator_url(config: Config, jql: str) -> str:
    """A link for a human to click, so it points at the site, not the API gateway."""
    return f"{config.site_url}/issues/?jql={urllib.parse.quote(jql)}"


def fetch_issues(config: Config, jql: str, limit: int) -> list[dict]:
    """The newest issues matching a query. /search/jql returns IDs only unless fields are named."""
    payload = {"jql": f"{jql} ORDER BY created DESC", "maxResults": limit, "fields": ["summary"]}
    found = jira_request(config, "/rest/api/3/search/jql", payload).get("issues", [])
    # A record without a key would render as a link to nowhere, so drop it rather than show it.
    return [
        {
            "key": issue["key"],
            "summary": issue.get("fields", {}).get("summary", ""),
            "url": f"{config.site_url}/browse/{issue['key']}",
        }
        for issue in found
        if issue.get("key")
    ]


def build_report(
    config: Config, now: datetime, window: tuple[datetime, datetime] | None = None
) -> dict:
    verify_account(config)
    start, end = window or reporting_window(now, config.timezone, config.window_days)
    buckets = {}
    for key, _ in BUCKETS:
        jql = build_jql(key, config, start, end)
        count = count_issues(config, jql)
        LOG.info("bucket=%s count=%s jql=%s", key, count, jql)
        buckets[key] = {
            "count": count,
            "jql": jql,
            "url": navigator_url(config, jql),
            "issues": fetch_issues(config, jql, ISSUE_LINKS) if count else [],
        }
    return {
        "project_key": config.project_key,
        "start": start,
        "end": end,
        "last_day": end - timedelta(days=1),
        "generated_at": now.astimezone(ZoneInfo(config.timezone)),
        "timezone": config.timezone,
        "buckets": buckets,
    }


def subject_line(report: dict) -> str:
    return (
        f"[{report['project_key']}] Jira report "
        f"{report['start']:%-d %b} to {report['last_day']:%-d %b %Y}"
    )


def render_text(report: dict) -> str:
    b = report["buckets"]
    return "\n".join(
        [
            subject_line(report),
            "",
            f"Project:  {report['project_key']}",
            f"Window:   {report['start']:%Y-%m-%d %H:%M} to {report['end']:%Y-%m-%d %H:%M} "
            f"({report['timezone']})",
            "",
            *(f"{label + ':':<10} {b[key]['count']}" for key, label in BUCKETS),
            "",
            *_text_issues(b),
            *CAVEATS,
            "",
            "JQL used:",
            *(f"  {key}: {b[key]['jql']}" for key, _ in BUCKETS),
            "",
            f"Generated {report['generated_at']:%Y-%m-%d %H:%M %Z}",
        ]
    )


def _issue_sections(buckets: dict):
    """Yield label, issues, how many were left out, and where to see them, per non-empty figure."""
    for key, label in BUCKETS:
        issues = buckets[key]["issues"]
        if issues:
            # The figure comes from approximate-count, so it can land below the number listed.
            hidden = max(0, buckets[key]["count"] - len(issues))
            yield label, issues, hidden, buckets[key]["url"]


def _text_issues(buckets: dict) -> list[str]:
    lines: list[str] = []
    for label, issues, hidden, more_url in _issue_sections(buckets):
        lines.append(f"{label}:")
        lines += [f"  {issue['url']}  {issue['summary']}" for issue in issues]
        if hidden:
            lines.append(f"  and {hidden} more: {more_url}")
        lines.append("")
    return lines


def _html_issues(buckets: dict) -> str:
    """Everything Jira returns is escaped here; summaries in particular are typed by people."""
    row = '<div style="padding:3px 0"><a href="{}" style="{}">{}</a>{}</div>'
    key_style = "color:#0052cc;text-decoration:none;font-weight:600"
    blocks = []
    for label, issues, hidden, more_url in _issue_sections(buckets):
        rows = "".join(
            row.format(escape(i["url"]), key_style, escape(i["key"]), " " + escape(i["summary"]))
            for i in issues
        )
        if hidden:
            rows += row.format(escape(more_url), "color:#6b778c", f"and {hidden} more", "")
        blocks.append(
            f'<p style="margin:22px 0 2px;font-size:13px;font-weight:600">{label}</p>'
            f'<div style="font-size:13px;line-height:1.45;color:#172b4d">{rows}</div>'
        )
    return "".join(blocks)


def render_html(report: dict) -> str:
    b = report["buckets"]
    cards = "".join(
        f"""
        <td style="padding:0 8px;width:33%">
          <a href="{b[key]['url']}" style="display:block;padding:20px 12px;border:1px solid #dfe1e6;
             border-radius:6px;text-decoration:none;text-align:center;color:#172b4d">
            <div style="font-size:34px;font-weight:600;line-height:1.1">{b[key]['count']}</div>
            <div style="font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#6b778c;
                 padding-top:6px">{label}</div>
          </a>
        </td>"""
        for key, label in BUCKETS
    )
    rows = "".join(
        f'<tr><td style="padding:3px 10px 3px 0;color:#6b778c;white-space:nowrap">{key}</td>'
        f'<td style="padding:3px 0"><code>{b[key]["jql"]}</code></td></tr>'
        for key, _ in BUCKETS
    )
    caveats = " ".join(CAVEATS)

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:24px;background:#f4f5f7;
  font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#172b4d">
<table role="presentation" style="max-width:680px;margin:0 auto;background:#fff;border-radius:6px;
  border:1px solid #dfe1e6;border-collapse:separate"><tr><td style="padding:28px">

  <h1 style="margin:0;font-size:19px">{report['project_key']} &mdash; Jira report</h1>
  <p style="margin:6px 0 24px;color:#6b778c;font-size:13px">
    {report['start']:%-d %B %Y} to {report['last_day']:%-d %B %Y} &middot; {report['timezone']}
  </p>

  <table role="presentation" style="width:100%;border-collapse:separate;border-spacing:0">
    <tr>{cards}</tr>
  </table>

  {_html_issues(b)}

  <p style="margin:24px 0 0;font-size:12px;color:#6b778c;line-height:1.5">
    Each figure links to the same query in Jira. {caveats}
  </p>

  <table role="presentation" style="margin-top:14px;font-size:11px;color:#6b778c;
    border-collapse:collapse">{rows}</table>

  <p style="margin:18px 0 0;font-size:11px;color:#97a0af">
    Generated {report['generated_at']:%Y-%m-%d %H:%M %Z}
  </p>

</td></tr></table></body></html>"""


def send_email(config: Config, subject: str, html: str, text: str) -> str:
    import boto3  # type: ignore  # from the Lambda runtime

    response = boto3.client("sesv2").send_email(
        FromEmailAddress=config.sender,
        Destination={"ToAddresses": [config.recipient]},
        Content={
            "Simple": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": text, "Charset": "UTF-8"},
                    "Html": {"Data": html, "Charset": "UTF-8"},
                },
            }
        },
    )
    return response["MessageId"]


def run(
    config: Config,
    *,
    send: bool,
    now: datetime | None = None,
    window: tuple[datetime, datetime] | None = None,
) -> dict:
    report = build_report(config, now or datetime.now(timezone.utc), window)
    subject = subject_line(report)
    html, text = render_html(report), render_text(report)

    message_id = None
    if send:
        message_id = send_email(config, subject, html, text)
        LOG.info("sent to=%s message_id=%s", config.recipient, message_id)

    return {
        "subject": subject,
        "counts": {name: data["count"] for name, data in report["buckets"].items()},
        "message_id": message_id,
        "html": html,
        "text": text,
    }


def lambda_handler(event, context):  # noqa: ARG001 - signature fixed by AWS
    logging.getLogger().setLevel(logging.INFO)
    config = Config.from_env()
    # An invocation may name its own window; the schedule sends {} and gets the rolling one.
    payload = event if isinstance(event, dict) else {}
    since, until = payload.get("since", ""), payload.get("until", "")
    window = parse_window(since, until, config.timezone) if since or until else None
    result = run(config, send=True, window=window)
    return {
        "status": "ok",
        "subject": result["subject"],
        "counts": result["counts"],
        "messageId": result["message_id"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--send", action="store_true", help="send the email instead of printing it")
    parser.add_argument("--out", metavar="PATH", help="write the rendered HTML to a file")
    parser.add_argument("--since", metavar="DATE", help="YYYY-MM-DD, replacing the rolling window")
    parser.add_argument("--until", metavar="DATE", help="YYYY-MM-DD, excluded from the window")
    args = parser.parse_args(argv)
    if bool(args.since) != bool(args.until):
        parser.error("--since and --until must be given together")

    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")

    try:
        config = Config.from_env()
        window = parse_window(args.since, args.until, config.timezone) if args.since else None
        result = run(config, send=args.send, window=window)
    except (ConfigError, JiraError) as exc:
        LOG.error("%s", exc)
        return 1

    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result["html"], encoding="utf-8")
        LOG.info("wrote %s", path)

    if not args.send:
        print("\n" + result["text"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
