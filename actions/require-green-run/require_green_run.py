#!/usr/bin/env python3
# Generated from faheem-ai-app/f-infra@d7fa3b6e — edit there, not here
"""require-green-run: refuse a deploy unless this exact commit already has a green full test run.

Test once, promote the result (f-infra docs/plans/test-once-promote-2026-11-11.md). Prod only takes commits that
were fast-forwarded from the source branch, so the commit is byte-identical to one the source branch already
tested. The prod workflow reads that earlier run instead of re-running the suite.

Evidence: one spec per line, `<workflow file>: <job>[, <job>...]`. A spec passes when one run of that workflow
  - is for exactly the SHA (head_sha), on the source branch, from this repository (never a fork),
  - was started by `push` or `workflow_dispatch` (a pull_request run tests a merge ref, not the SHA),
  - completed with conclusion `success` (GitHub reports the latest attempt, so a green re-run counts),
  - and every named job's latest attempt concluded `success` (`skipped` never counts).
Every spec must pass. Anything else, including any failed or truncated API read, exits 1 (fail closed).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
WORKFLOW_RE = re.compile(r"^[A-Za-z0-9._-]+\.ya?ml$")
ACCEPTED_EVENTS = ("push", "workflow_dispatch")
PER_PAGE = 100


class EvidenceError(Exception):
    """The evidence is missing, red, or unreadable; the deploy must not proceed."""


def parse_evidence(text: str) -> list[tuple[str, list[str]]]:
    specs: list[tuple[str, list[str]]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        workflow, sep, jobs_text = line.partition(":")
        workflow = workflow.strip()
        jobs = [job.strip() for job in jobs_text.split(",") if job.strip()]
        if not sep or not WORKFLOW_RE.fullmatch(workflow) or not jobs:
            raise EvidenceError(
                f"bad evidence line {line!r}: expected '<workflow>.yml: <job>[, <job>...]'"
            )
        specs.append((workflow, jobs))
    if not specs:
        raise EvidenceError(
            "no evidence configured: at least one '<workflow>.yml: <job>' line is required"
        )
    return specs


def fetch_json(api_url: str, path: str, token: str) -> dict:
    if not api_url.startswith("https://"):
        raise EvidenceError(f"GITHUB_API_URL must be https, got {api_url!r}")
    request = urllib.request.Request(  # noqa: S310 -- https enforced above
        f"{api_url}/{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "require-green-run",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 -- https enforced above
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise EvidenceError(f"could not read {path}: {error}") from error


def describe(run: dict) -> str:
    return (
        f"run {run.get('id')} attempt {run.get('run_attempt', 1)} "
        f"({run.get('event')} on {run.get('head_branch')}, {run.get('status')}/{run.get('conclusion')})"
    )


def check_spec(fetch, *, repo: str, sha: str, branch: str, workflow: str, jobs: list[str]) -> str:
    query = urllib.parse.urlencode({"head_sha": sha, "branch": branch, "per_page": PER_PAGE})
    payload = fetch(
        f"repos/{repo}/actions/workflows/{urllib.parse.quote(workflow, safe='')}/runs?{query}"
    )
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise EvidenceError(f"{workflow}: the runs response has no workflow_runs list")
    if payload.get("total_count", len(runs)) > len(runs):
        raise EvidenceError(
            f"{workflow}: {payload.get('total_count')} runs for {sha[:12]}, more than one page; refusing"
        )

    seen: list[str] = []
    for run in sorted(
        runs, key=lambda r: (r.get("run_number", 0), r.get("run_attempt", 1)), reverse=True
    ):
        head_repo = (run.get("head_repository") or {}).get("full_name")
        if run.get("head_sha") != sha or run.get("head_branch") != branch or head_repo != repo:
            seen.append(f"{describe(run)}: not this commit on {branch} in {repo}")
            continue
        if run.get("event") not in ACCEPTED_EVENTS:
            seen.append(f"{describe(run)}: event is not push or workflow_dispatch")
            continue
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            seen.append(f"{describe(run)}: not a completed success")
            continue
        job_payload = fetch(
            f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page={PER_PAGE}"
        )
        job_list = job_payload.get("jobs")
        if not isinstance(job_list, list):
            raise EvidenceError(f"{workflow}: run {run['id']} has no readable job list")
        if job_payload.get("total_count", len(job_list)) > len(job_list):
            raise EvidenceError(
                f"{workflow}: run {run['id']} has more than {PER_PAGE} jobs; refusing"
            )
        green = {
            job.get("name")
            for job in job_list
            if job.get("status") == "completed" and job.get("conclusion") == "success"
        }
        missing = [job for job in jobs if job not in green]
        if missing:
            seen.append(f"{describe(run)}: job(s) not green: {', '.join(missing)}")
            continue
        return run.get("html_url") or f"run {run['id']}"

    detail = "; ".join(seen) if seen else "no runs at all"
    raise EvidenceError(
        f"{workflow}: no green run of job(s) {', '.join(jobs)} for {sha} on {branch} ({detail})"
    )


def require_green_run(fetch, repo: str, sha: str, branch: str, evidence: str) -> list[str]:
    if not SHA_RE.fullmatch(sha):
        raise EvidenceError(f"sha must be a full 40-character lowercase commit SHA, got {sha!r}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise EvidenceError(f"repository must be owner/name, got {repo!r}")
    if not branch:
        raise EvidenceError("source branch is required")
    return [
        check_spec(fetch, repo=repo, sha=sha, branch=branch, workflow=workflow, jobs=jobs)
        for workflow, jobs in parse_evidence(evidence)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument(
        "--sha", default=os.environ.get("GREEN_RUN_SHA") or os.environ.get("GITHUB_SHA", "")
    )
    parser.add_argument("--source-branch", default=os.environ.get("GREEN_RUN_SOURCE_BRANCH", ""))
    parser.add_argument("--evidence", default=os.environ.get("GREEN_RUN_EVIDENCE", ""))
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    try:
        if not token:
            raise EvidenceError("GITHUB_TOKEN is required to read the evidence runs")
        urls = require_green_run(
            lambda path: fetch_json(api_url, path, token),
            args.repo,
            args.sha,
            args.source_branch,
            args.evidence,
        )
    except EvidenceError as refusal:
        sys.stderr.write(f"::error::Refusing to deploy {args.sha[:12] or '<no sha>'}: {refusal}\n")
        return 1

    sys.stdout.write("".join(f"Green evidence for {args.sha}: {url}\n" for url in urls))
    for name in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
        target = os.environ.get(name)
        if target:
            with Path(target).open("a", encoding="utf-8") as handle:
                if name == "GITHUB_OUTPUT":
                    handle.write(f"run-urls={' '.join(urls)}\n")
                else:
                    handle.write(
                        f"Reused green test evidence for `{args.sha}`:\n"
                        + "".join(f"- {u}\n" for u in urls)
                    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
