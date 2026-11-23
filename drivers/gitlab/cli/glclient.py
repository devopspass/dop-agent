#!/usr/bin/env python3
"""
glclient.py — wait for GitLab CI/CD and merge-request events to happen.

Polling shapes:

  pipeline    Wait for a pipeline (by ID, or the latest one for an MR/ref)
              to reach a terminal status.
  job         Wait for a specific job to reach a terminal status, optionally
              printing its log once it finishes.
  mr-notes    Wait for a new note/comment to appear on a merge request
              (e.g. a bot or reviewer reply).

Credentials: pass base_url/token explicitly (the dop-agent integration instance does), or set the
GITLAB_URL + GITLAB_TOKEN env vars. No host is built in.

Examples:
    glclient.py pipeline group/sub/project --pipeline-id 487105
    glclient.py pipeline group/sub/project --mr 355
    glclient.py pipeline my/project --ref feature/PROJ-1234-fix --sha abc1234
    glclient.py job my/project --job-id 3084802 --output
    glclient.py mr-notes my/project --mr 731 --since now
    glclient.py mr-notes my/project --mr 731 --match "PR-Agent" --author pr-agent-bot

Always prints a single self-describing "RESULT: ..." line (plus details)
to stdout — read that directly, no need to inspect the exit code:
    RESULT: SUCCESS  — pipeline/job reached a successful terminal status
    RESULT: FOUND    — a matching MR note appeared
    RESULT: FAILED   — reached a failure-like terminal status (failed/canceled; manual/scheduled/waiting_for_resource)
    RESULT: TIMEOUT  — no terminal state within --timeout
    RESULT: ERROR    — configuration or GitLab API error
Pass --json for the same information as a JSON object instead.
"""

import argparse
import json
import re
import sys
import logging
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

PIPELINE_TERMINAL = {"success", "failed", "canceled", "skipped"}
PIPELINE_NEEDS_ACTION = {"manual", "scheduled", "waiting_for_resource"}
JOB_TERMINAL = {"success", "failed", "canceled", "skipped"}
FAILURE_LIKE = {"failed", "canceled"}


class ConfigError(RuntimeError):
    pass


class PollTimeout(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Config / HTTP
# --------------------------------------------------------------------------

def _load_gitlab_config():
    """Resolve (base_url, token) from GITLAB_URL/GITLAB_API_URL + GITLAB_TOKEN/GITLAB_PERSONAL_ACCESS_TOKEN."""
    import os

    url = os.environ.get("GITLAB_URL") or os.environ.get("GITLAB_API_URL")
    token = os.environ.get("GITLAB_TOKEN") or os.environ.get("GITLAB_PERSONAL_ACCESS_TOKEN")
    if url and token:
        return url.rstrip("/"), token
    raise ConfigError("No GitLab credentials: pass --instance <name> (a gitlab integration in config.yaml) "
                      "or set GITLAB_URL and GITLAB_TOKEN.")


class GitLabClient:
    def __init__(self, base_url=None, token=None):
        """Explicit base_url/token (from the integration config) win; with neither,
        falls back to the GITLAB_URL/GITLAB_TOKEN env vars."""
        if base_url and token:
            base_url = base_url.rstrip("/")
        else:
            logging.getLogger(__name__).warning("GitLabClient: no explicit base_url/token; using GITLAB_URL/GITLAB_TOKEN env")
            base_url, token = _load_gitlab_config()
        self.api = f"{base_url}/api/v4"
        self.session = requests.Session()
        self.session.headers["PRIVATE-TOKEN"] = token
        adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _get(self, path, params=None):
        resp = self.session.get(f"{self.api}{path}", params=params, timeout=30)
        if resp.status_code == 401:
            raise ConfigError("GitLab API returned 401 Unauthorized — token may be expired/invalid.")
        if not resp.ok:
            raise ConfigError(f"GitLab API error {resp.status_code} for {path}: {resp.text[:300]}")
        return resp

    def project_path(self, project):
        return urllib.parse.quote(str(project), safe="")

    def get_project(self, project):
        return self._get(f"/projects/{self.project_path(project)}").json()

    def get_merge_request(self, project, mr_iid):
        return self._get(f"/projects/{self.project_path(project)}/merge_requests/{mr_iid}").json()

    def get_pipeline(self, project, pipeline_id):
        return self._get(f"/projects/{self.project_path(project)}/pipelines/{pipeline_id}").json()

    def get_mr_approvals(self, project, mr_iid):
        return self._get(
            f"/projects/{self.project_path(project)}/merge_requests/{mr_iid}/approvals"
        ).json()

    def list_mr_pipelines(self, project, mr_iid):
        return self._get(
            f"/projects/{self.project_path(project)}/merge_requests/{mr_iid}/pipelines"
        ).json()

    def list_pipelines_for_ref(self, project, ref=None, sha=None):
        params = {"per_page": 5, "order_by": "id", "sort": "desc"}
        if ref:
            params["ref"] = ref
        if sha:
            params["sha"] = sha
        return self._get(f"/projects/{self.project_path(project)}/pipelines", params=params).json()

    def get_job(self, project, job_id):
        return self._get(f"/projects/{self.project_path(project)}/jobs/{job_id}").json()

    def get_job_trace(self, project, job_id):
        return self._get(f"/projects/{self.project_path(project)}/jobs/{job_id}/trace").text

    def list_pipeline_jobs(self, project, pipeline_id):
        return self._get(
            f"/projects/{self.project_path(project)}/pipelines/{pipeline_id}/jobs",
            params={"per_page": 100},
        ).json()

    def list_pipeline_bridges(self, project, pipeline_id):
        return self._get(
            f"/projects/{self.project_path(project)}/pipelines/{pipeline_id}/bridges",
            params={"per_page": 50},
        ).json()

    def get_job_trace_tail(self, project, job_id, max_bytes=16384, hard_cap=2_000_000):
        """Last ~max_bytes of a job log without downloading all of it: ask for
        a Range suffix; if the server ignores it and streams the full log,
        stop reading at hard_cap and keep only the tail."""
        resp = self.session.get(
            f"{self.api}/projects/{self.project_path(project)}/jobs/{job_id}/trace",
            headers={"Range": f"bytes=-{max_bytes}"}, stream=True, timeout=30,
        )
        try:
            if not resp.ok:
                raise ConfigError(f"GitLab API error {resp.status_code} for job {job_id} trace")
            buf = bytearray()
            for chunk in resp.iter_content(65536):
                buf += chunk
                if len(buf) > hard_cap:
                    break
        finally:
            resp.close()
        return bytes(buf[-max_bytes:]).decode("utf-8", "replace")

    def list_mr_notes(self, project, mr_iid):
        return self._get(
            f"/projects/{self.project_path(project)}/merge_requests/{mr_iid}/notes",
            params={"per_page": 50, "order_by": "created_at", "sort": "desc"},
        ).json()


# --------------------------------------------------------------------------
# Backoff / progress helpers
# --------------------------------------------------------------------------

def backoff_sleep(elapsed_start, deadline, interval, max_interval, quiet, message):
    now = time.time()
    remaining = deadline - now
    if remaining <= 0:
        raise PollTimeout(message)
    sleep_for = min(interval, max_interval, remaining)
    if not quiet:
        mins = int(now - elapsed_start) // 60
        secs = int(now - elapsed_start) % 60
        print(f"[{mins:02d}:{secs:02d}] {message} — waiting {sleep_for:.0f}s", file=sys.stderr)
    time.sleep(sleep_for)
    return min(interval * 1.5, max_interval)


def now_utc():
    return datetime.now(timezone.utc)


def parse_since(value):
    if value is None or value.lower() == "now":
        return now_utc()
    # accept plain ISO timestamps (as returned by GitLab, e.g. 2026-08-04T07:41:02.004Z)
    v = value.replace("Z", "+00:00")
    return datetime.fromisoformat(v)


def parse_gitlab_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_pipeline(client, args):
    deadline = time.time() + args.timeout
    start = time.time()
    interval = args.interval
    pipeline_id = args.pipeline_id

    if pipeline_id is None:
        # Resolve the pipeline id first: via MR, or via ref/sha lookup.
        while True:
            if args.mr:
                pipelines = client.list_mr_pipelines(args.project, args.mr)
                label = f"pipeline for MR !{args.mr}"
            else:
                pipelines = client.list_pipelines_for_ref(args.project, ref=args.ref, sha=args.sha)
                label = f"pipeline for ref={args.ref or '-'} sha={args.sha or '-'}"
            if pipelines:
                pipeline_id = pipelines[0]["id"]
                break
            interval = backoff_sleep(start, deadline, interval, args.max_interval, args.quiet,
                                      f"{label} not created yet")

    while True:
        pipeline = client.get_pipeline(args.project, pipeline_id)
        status = pipeline["status"]
        if status in PIPELINE_TERMINAL:
            outcome = "FAILED" if status in FAILURE_LIKE else "SUCCESS"
            return _finish(
                args, outcome,
                f"pipeline {pipeline_id} finished with status={status}",
                id=pipeline["id"], gitlab_status=status, web_url=pipeline.get("web_url"),
            )
        if status in PIPELINE_NEEDS_ACTION:
            return _finish(
                args, "FAILED",
                f"pipeline {pipeline_id} requires manual action / is waiting for a resource (status={status})",
                id=pipeline["id"], gitlab_status=status, web_url=pipeline.get("web_url"),
            )
        interval = backoff_sleep(start, deadline, interval, args.max_interval, args.quiet,
                                  f"pipeline {pipeline_id} status={status}")


def cmd_job(client, args):
    deadline = time.time() + args.timeout
    start = time.time()
    interval = args.interval

    while True:
        job = client.get_job(args.project, args.job_id)
        status = job["status"]
        if status in JOB_TERMINAL:
            outcome = "FAILED" if status in FAILURE_LIKE else "SUCCESS"
            details = {"id": job["id"], "gitlab_status": status, "web_url": job.get("web_url")}
            log = client.get_job_trace(args.project, args.job_id) if args.output else None
            return _finish(
                args, outcome,
                f"job {args.job_id} finished with status={status}",
                log=log, **details,
            )
        interval = backoff_sleep(start, deadline, interval, args.max_interval, args.quiet,
                                  f"job {args.job_id} status={status}")


def cmd_mr_notes(client, args):
    deadline = time.time() + args.timeout
    start = time.time()
    interval = args.interval
    since = parse_since(args.since)
    pattern = re.compile(args.match, re.IGNORECASE) if args.match else None

    while True:
        notes = client.list_mr_notes(args.project, args.mr)
        for note in notes:  # already sorted desc by created_at
            created = parse_gitlab_ts(note["created_at"])
            if created <= since:
                break
            if args.author and note.get("author", {}).get("username") != args.author:
                continue
            if pattern and not pattern.search(note.get("body", "")):
                continue
            author = note.get("author", {}).get("username")
            body_preview = (note.get("body") or "").splitlines()[0][:120]
            return _finish(
                args, "FOUND",
                f"new note {note['id']} on MR !{args.mr} by {author}: {body_preview}",
                id=note["id"], author=author, created_at=note["created_at"], body=note.get("body"),
            )
        interval = backoff_sleep(start, deadline, interval, args.max_interval, args.quiet,
                                  f"MR !{args.mr} — waiting for new note")


def _finish(args, status, message, **details):
    """Print a single self-describing RESULT line (plus details) and return the exit code.

    The printed message carries the outcome; the exit code is secondary.
    """
    log = details.pop("log", None)
    if args.json:
        payload = {"status": status, "message": message, **details}
        print(json.dumps(payload, indent=2))
    else:
        print(f"RESULT: {status} — {message}")
        for k, v in details.items():
            if v is not None:
                print(f"  {k}: {v}")
    if log is not None:
        print("--- log ---")
        print(log)
    return {"SUCCESS": 0, "FOUND": 0, "FAILED": 1, "TIMEOUT": 2, "ERROR": 3, "INTERRUPTED": 130}.get(status, 1)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def add_common_args(p):
    p.add_argument("project", help="Project ID or numeric/URL-encoded path, e.g. group/subgroup/repo")
    p.add_argument("--timeout", type=float, default=1800, help="Max seconds to wait (default: 1800)")
    p.add_argument("--interval", type=float, default=10, help="Initial poll interval in seconds (default: 10)")
    p.add_argument("--max-interval", type=float, default=60, help="Max poll interval in seconds (default: 60)")
    p.add_argument("--json", action="store_true", help="Print result as JSON")
    p.add_argument("--quiet", "-q", action="store_true", help="Suppress progress output on stderr")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Wait for GitLab pipeline/job/MR-note events instead of hand-rolled sleep loops.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_pipeline = sub.add_parser("pipeline", help="Wait for a pipeline to reach a terminal status")
    add_common_args(p_pipeline)
    p_pipeline.add_argument("--pipeline-id", help="Poll this specific pipeline ID")
    p_pipeline.add_argument("--mr", help="Resolve & poll the latest pipeline for this MR IID")
    p_pipeline.add_argument("--ref", help="Resolve & poll the latest pipeline for this branch/tag")
    p_pipeline.add_argument("--sha", help="Resolve & poll the pipeline for this commit SHA (with --ref, or alone)")
    p_pipeline.set_defaults(func=cmd_pipeline)

    p_job = sub.add_parser("job", help="Wait for a job to reach a terminal status")
    add_common_args(p_job)
    p_job.add_argument("--job-id", required=True, help="Job ID to poll")
    p_job.add_argument("--output", action="store_true", help="Print the job log once it finishes")
    p_job.set_defaults(func=cmd_job)

    p_notes = sub.add_parser("mr-notes", help="Wait for a new note/comment on a merge request")
    add_common_args(p_notes)
    p_notes.add_argument("--mr", required=True, help="Merge request IID")
    p_notes.add_argument("--since", default="now",
                          help="Only match notes created after this ISO timestamp (default: now)")
    p_notes.add_argument("--author", help="Only match notes from this username")
    p_notes.add_argument("--match", help="Only match notes whose body matches this regex")
    p_notes.set_defaults(func=cmd_mr_notes)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "pipeline":
        selectors = [args.pipeline_id, args.mr, args.ref or args.sha]
        if sum(1 for s in selectors if s) == 0:
            parser.error("pipeline: one of --pipeline-id, --mr, --ref/--sha is required")

    try:
        client = GitLabClient()
        return args.func(client, args)
    except ConfigError as exc:
        return _finish(args, "ERROR", str(exc))
    except PollTimeout as exc:
        return _finish(args, "TIMEOUT", str(exc))
    except KeyboardInterrupt:
        return _finish(args, "INTERRUPTED", "polling was interrupted by the user")


if __name__ == "__main__":
    sys.exit(main())
