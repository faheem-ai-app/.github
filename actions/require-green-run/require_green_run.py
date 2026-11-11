#!/usr/bin/env python3
# Generated from faheem-ai-app/f-infra@d873b579 — edit there, not here
"""require-green-run: reuse an earlier green test run instead of testing the same code again.

Test once, promote the result (f-infra docs/plans/test-once-promote-2026-11-11.md). Two modes:

exact (prod gate, default): prod only takes commits fast-forwarded from the source branch, so the commit is
byte-identical to one the source branch already tested. A spec passes when one run of that workflow
  - is for exactly the SHA (head_sha), on the source branch, from this repository (never a fork),
  - was started by `push` or `workflow_dispatch` (a pull_request run tests a merge ref, not the SHA),
  - completed with conclusion `success` (GitHub reports the latest attempt, so a green re-run counts),
  - and every named job's latest attempt concluded `success` (`skipped` never counts);
and the SHA is in the source branch's history (compare API), so a run on a tag or stray ref named like the
branch cannot stand in. Every spec must pass; anything else, including a failed or truncated read, exits 1.

tree (after a merge on a landing branch): the push of a squash merge re-tests a tree the PR's run already tested.
The PR run is reused only when ALL hold:
  - exactly one merged PR from this repository has this SHA as its merge commit, into this branch;
  - the PR head has the same tree as this SHA, and contains this SHA's first parent (the branch's previous tip);
  - every PR ever opened from that head branch targets this branch and never changed its base (a run's own PR link is
    cleared on merge, so this is what ties the run to this branch);
  - the branch has GitHub's force-push protection (a `non_fast_forward` rule), so it really only moves forward. The branch only moves forward, so every base those runs were tested
    against is an ancestor of that parent, which makes GitHub's PR merge ref tree equal to the head tree: the PR
    run tested exactly this tree;
  - a `pull_request` run of each named workflow on the PR head completed `success` with every named job green.
Tree mode never fails the job on a miss or a read error: it reports `reused=false` and the tests run as usual.

Evidence: one spec per line, `<workflow file>: <job>[, <job>...]`.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
WORKFLOW_RE = re.compile(r"^[A-Za-z0-9._-]+\.ya?ml$")
EXACT_EVENTS = ("push", "workflow_dispatch")
TREE_EVENTS = ("pull_request",)
PER_PAGE = 100
ATTEMPTS = 2


class EvidenceError(Exception):
    """The evidence is missing, red, or unreadable."""


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


def fetch_json(api_url: str, path: str, token: str) -> dict | list:
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
    error: Exception | None = None
    for _attempt in range(ATTEMPTS):  # one retry: a cut connection is usually transient
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 -- https enforced above
                return json.load(response)
        # OSError covers URLError and timeouts; HTTPException covers a truncated body (IncompleteRead); ValueError
        # covers bad JSON. Each ends as an EvidenceError: exact mode refuses, tree mode runs the tests.
        except (OSError, http.client.HTTPException, ValueError) as caught:
            error = caught
    raise EvidenceError(f"could not read {path}: {error}") from error


def describe(run: dict) -> str:
    return (
        f"run {run.get('id')} attempt {run.get('run_attempt', 1)} "
        f"({run.get('event')} on {run.get('head_branch')}, {run.get('status')}/{run.get('conclusion')})"
    )


def check_spec(
    fetch,
    *,
    repo: str,
    sha: str,
    branch: str,
    events: tuple[str, ...],
    workflow: str,
    jobs: list[str],
) -> str:
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
        if run.get("event") not in events:
            seen.append(f"{describe(run)}: event is not {' or '.join(events)}")
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


def contains(fetch, repo: str, ancestor: str, descendant: str) -> bool:
    """True when `ancestor` is in `descendant`'s history (descendant is a SHA or a branch name)."""
    base = urllib.parse.quote(ancestor, safe="")
    head = urllib.parse.quote(descendant, safe="")
    status = fetch(f"repos/{repo}/compare/{base}...{head}?per_page=1").get("status")
    return status in {"ahead", "identical"}


def validate(repo: str, sha: str, branch: str) -> None:
    if not SHA_RE.fullmatch(sha):
        raise EvidenceError(f"sha must be a full 40-character lowercase commit SHA, got {sha!r}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise EvidenceError(f"repository must be owner/name, got {repo!r}")
    if not branch:
        raise EvidenceError("source branch is required")


def require_green_run(fetch, repo: str, sha: str, branch: str, evidence: str) -> list[str]:
    """Exact mode: every spec has a green push/dispatch run of exactly `sha` on `branch`, and `branch` holds `sha`."""
    validate(repo, sha, branch)
    specs = parse_evidence(evidence)
    if not contains(fetch, repo, sha, branch):
        raise EvidenceError(f"{sha} is not in the history of {branch}")
    return [
        check_spec(
            fetch,
            repo=repo,
            sha=sha,
            branch=branch,
            events=EXACT_EVENTS,
            workflow=workflow,
            jobs=jobs,
        )
        for workflow, jobs in specs
    ]


def reusable_pr_run(fetch, repo: str, sha: str, branch: str, evidence: str) -> list[str]:
    """Tree mode: the merged PR's green run tested exactly this tree. Raises EvidenceError on any miss."""
    validate(repo, sha, branch)
    specs = parse_evidence(evidence)
    # The soundness argument needs a branch that only moves forward: require GitHub to enforce it.
    rules = fetch(
        f"repos/{repo}/rules/branches/{urllib.parse.quote(branch, safe='')}?per_page={PER_PAGE}"
    )
    if not isinstance(rules, list) or not any(
        rule.get("type") == "non_fast_forward" for rule in rules
    ):
        raise EvidenceError(
            f"{branch} has no force-push protection (non_fast_forward rule); it could be rewound"
        )
    pulls = fetch(f"repos/{repo}/commits/{sha}/pulls?per_page={PER_PAGE}")
    if not isinstance(pulls, list):
        raise EvidenceError("the pulls-for-commit response is not a list")
    merged = [
        pr
        for pr in pulls
        if pr.get("merged_at")
        and pr.get("merge_commit_sha") == sha
        and (pr.get("base") or {}).get("ref") == branch
        and ((pr.get("head") or {}).get("repo") or {}).get("full_name") == repo
    ]
    if len(merged) != 1:
        raise EvidenceError(
            f"{len(merged)} merged PRs from {repo} into {branch} have {sha[:12]} as their merge commit"
        )
    head = merged[0]["head"]
    head_sha, head_ref = head.get("sha", ""), head.get("ref", "")
    if not SHA_RE.fullmatch(head_sha) or not head_ref:
        raise EvidenceError(f"PR #{merged[0].get('number')} has no usable head")

    commit = fetch(f"repos/{repo}/git/commits/{sha}")
    parents = [parent.get("sha") for parent in commit.get("parents") or []]
    tree = (commit.get("tree") or {}).get("sha")
    head_tree = (fetch(f"repos/{repo}/git/commits/{head_sha}").get("tree") or {}).get("sha")
    if not tree or tree != head_tree:
        raise EvidenceError(
            f"PR #{merged[0].get('number')} head {head_sha[:12]} has a different tree than {sha[:12]}"
        )
    if not parents or not contains(fetch, repo, parents[0], head_sha):
        raise EvidenceError(
            f"PR #{merged[0].get('number')} head does not contain {branch}'s previous tip"
        )
    # A pull_request run tests the PR's merge with ITS base, and GitHub clears a run's pull_requests list once the PR
    # merges, so a run cannot be tied to one PR. Instead: every PR ever opened from this head branch must target this
    # branch. Then any pull_request run on it was tested against an earlier tip of this branch, which the head
    # contains (checked above), so the tested tree is the head tree.
    owner = repo.split("/", 1)[0]
    query = urllib.parse.urlencode(
        {"state": "all", "head": f"{owner}:{head_ref}", "per_page": PER_PAGE}
    )
    siblings = fetch(f"repos/{repo}/pulls?{query}")
    if not isinstance(siblings, list) or len(siblings) >= PER_PAGE:
        raise EvidenceError(f"cannot list every PR from {head_ref}")
    if merged[0].get("number") not in {pr.get("number") for pr in siblings}:
        raise EvidenceError(
            f"the PR list from {head_ref} does not include PR #{merged[0].get('number')}"
        )
    other_bases = sorted({(pr.get("base") or {}).get("ref") for pr in siblings} - {branch}, key=str)
    if other_bases:
        raise EvidenceError(
            f"{head_ref} also had PRs into {', '.join(map(str, other_bases))}; its runs may test other bases"
        )
    # The list above shows each PR's CURRENT base only, and changing a base starts no new run: a PR first opened
    # against another branch may have been tested there. Any base change on any of them is a miss.
    for sibling in siblings:
        events = fetch(f"repos/{repo}/issues/{sibling.get('number')}/events?per_page={PER_PAGE}")
        if not isinstance(events, list) or len(events) >= PER_PAGE:
            raise EvidenceError(f"cannot read every event of PR #{sibling.get('number')}")
        if any(event.get("event") == "base_ref_changed" for event in events):
            raise EvidenceError(
                f"PR #{sibling.get('number')} changed its base; its runs may have tested another one"
            )
    return [
        check_spec(
            fetch,
            repo=repo,
            sha=head_sha,
            branch=head_ref,
            events=TREE_EVENTS,
            workflow=workflow,
            jobs=jobs,
        )
        for workflow, jobs in specs
    ]


CHECK_PROMOTION_RE = re.compile(r"check-promotion\.py\b[^\n]*?--source-workflow\s+(\S+)([^\n]*)")
REQUIRED_JOBS_RE = re.compile(r"--required-evidence-jobs\s+(\S+)")
USES_RE = re.compile(r"^(\s*)-?\s*uses:\s*\S*require-green-run(@\S+)?\s*(#.*)?$")


def _step_block(lines: list[str], index: int, step_indent: int) -> list[str]:
    """The lines of the step whose `uses:` is at lines[index], up to the next step or a shallower line."""
    block: list[str] = []
    for follow in lines[index + 1 :]:
        indent = len(follow) - len(follow.lstrip())
        if follow.strip() and (
            indent < step_indent or (follow.lstrip().startswith("- ") and indent <= step_indent)
        ):
            break
        block.append(follow)
    return block


def _gate_specs(block: list[str]) -> list[str]:
    """An exact-mode step's evidence lines; refuses inputs the pre-flight cannot honour (it checks dev, at the SHA)."""
    for row in block:
        setting = re.match(r"\s*(source-branch|sha):\s*(.*)$", row)
        if setting and not (
            setting.group(1) == "source-branch" and setting.group(2).strip(" '\"") == "dev"
        ):
            raise EvidenceError(
                f"a prod gate sets {setting.group(1)}: {setting.group(2).strip()}; the pre-flight only checks dev"
            )
    specs: list[str] = []
    for pos, row in enumerate(block):
        key = re.match(r"(\s*)evidence:\s*\|", row)
        if not key:
            continue
        for spec in block[pos + 1 :]:
            if spec.strip() and len(spec) - len(spec.lstrip()) <= len(key.group(1)):
                break
            if spec.strip():
                specs.append(spec.strip())
    if not specs:
        raise EvidenceError(
            "a prod gate's evidence is not an `evidence: |` block; the pre-flight cannot read it"
        )
    return specs


def evidence_from_workflows(texts: list[str]) -> str:
    """The prod gates' evidence specs declared in workflow files, for a pre-flight (promote.sh).

    Reads every exact-mode `require-green-run` step's `evidence: |` block, and f-brain's `check-promotion.py
    --source-workflow X [--required-evidence-jobs a,b]` (default jobs cd,verify). Tree-mode steps are not gates.
    A gate the parser cannot read faithfully (another evidence form, a non-dev source, a fixed sha) raises.
    """
    specs: list[str] = []
    for text in texts:
        lines = text.splitlines()
        for index, line in enumerate(lines):
            match = USES_RE.match(line)
            if not match:
                continue
            block = _step_block(lines, index, len(match.group(1)))
            if not any(re.match(r"\s*mode:\s*['\"]?tree\b", row) for row in block):
                specs.extend(_gate_specs(block))
        for match in CHECK_PROMOTION_RE.finditer(text):
            jobs = REQUIRED_JOBS_RE.search(match.group(2))
            names = (jobs.group(1) if jobs else "cd,verify").split(",")
            specs.append(f"{match.group(1)}: {', '.join(name for name in names if name)}")
    return "\n".join(dict.fromkeys(specs))


def write_outputs(pairs: dict[str, str], summary: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with Path(target).open("a", encoding="utf-8") as handle:
            handle.write("".join(f"{key}={value}\n" for key, value in pairs.items()))
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as handle:
            handle.write(summary)


def run_tree(fetch, args, token: str) -> int:
    """Tree mode: a config error is loud; a miss or any read problem reports reused=false so the tests run."""
    try:
        parse_evidence(args.evidence)
    except EvidenceError as error:
        sys.stderr.write(f"::error::require-green-run tree mode is misconfigured: {error}\n")
        return 1
    try:
        if not token:
            raise EvidenceError("GITHUB_TOKEN is required to read the PR run")
        urls = reusable_pr_run(fetch, args.repo, args.sha, args.source_branch, args.evidence)
    except Exception as miss:  # any surprise (odd payload shape too): run the tests
        sys.stdout.write(
            f"::notice::Running the tests: no reusable PR run for {args.sha[:12]} ({miss})\n"
        )
        write_outputs(
            {"reused": "false", "run-urls": ""}, f"No reusable PR run for `{args.sha}`: {miss}\n"
        )
        return 0
    sys.stdout.write(
        "".join(f"Reusing the PR run that tested this exact tree: {url}\n" for url in urls)
    )
    write_outputs(
        {"reused": "true", "run-urls": " ".join(urls)},
        f"Tests skipped: the merged PR's green run tested this exact tree (`{args.sha}`):\n"
        + "".join(f"- {u}\n" for u in urls),
    )
    return 0


def run_exact(fetch, args, token: str) -> int:
    """Exact mode: any miss, red run or read problem refuses (exit 1)."""
    try:
        if not token:
            raise EvidenceError("GITHUB_TOKEN is required to read the evidence runs")
        urls = require_green_run(fetch, args.repo, args.sha, args.source_branch, args.evidence)
    except Exception as refusal:  # any surprise refuses the deploy, with a reason
        sys.stderr.write(f"::error::Refusing to deploy {args.sha[:12] or '<no sha>'}: {refusal}\n")
        return 1
    sys.stdout.write("".join(f"Green evidence for {args.sha}: {url}\n" for url in urls))
    write_outputs(
        {"reused": "true", "run-urls": " ".join(urls)},
        f"Reused green test evidence for `{args.sha}`:\n" + "".join(f"- {u}\n" for u in urls),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", default=os.environ.get("GREEN_RUN_MODE") or "exact")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument(
        "--sha", default=os.environ.get("GREEN_RUN_SHA") or os.environ.get("GITHUB_SHA", "")
    )
    parser.add_argument("--source-branch", default=os.environ.get("GREEN_RUN_SOURCE_BRANCH", ""))
    parser.add_argument("--evidence", default=os.environ.get("GREEN_RUN_EVIDENCE", ""))
    parser.add_argument(
        "--evidence-from",
        nargs="+",
        metavar="WORKFLOW",
        help="take the specs from these workflow files' prod gates (promote.sh pre-flight); none declared = exit 0",
    )
    args = parser.parse_args(argv)
    if args.mode not in {
        "exact",
        "tree",
    }:  # checked here: the env default bypasses argparse choices
        sys.stderr.write(
            f"::error::require-green-run mode must be exact or tree, got {args.mode!r}\n"
        )
        return 1
    if args.evidence_from:
        try:
            args.evidence = evidence_from_workflows(
                [Path(f).read_text(encoding="utf-8") for f in args.evidence_from]
            )
        except EvidenceError as error:
            sys.stderr.write(f"::error::Cannot pre-check the prod gate: {error}\n")
            return 1
        if not args.evidence:
            sys.stdout.write(
                "No prod green-run gate declared in these workflows; nothing to check.\n"
            )
            return 0

    token = os.environ.get("GITHUB_TOKEN", "")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

    def fetch(path):
        return fetch_json(api_url, path, token)

    return (run_tree if args.mode == "tree" else run_exact)(fetch, args, token)


if __name__ == "__main__":
    sys.exit(main())
