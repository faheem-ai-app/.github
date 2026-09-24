#!/usr/bin/env python3
# Generated from faheem-ai-app/f-infra@bad28821 — edit there, not here
"""docs-check.py — stdlib-only docs-drift checker (f-infra, D1-D5).

Classifies every tracked ``*.md`` file (outside ``.claude/``) via a YAML-lite
frontmatter subset (``type:`` / ``covers:``), then:

  1. flags dead repo-relative path refs in ``living`` docs (FAIL)
  2. (range/pre-push only) flags code changes that match a living doc's
     ``covers:`` glob when the doc itself wasn't touched and no commit in the
     range carries a ``Docs: n/a`` trailer (FAIL)
  3. warns on a living doc with no ``covers:`` (WARN)
  4. warns on ``file.ext:NN`` line citations in living docs (WARN, never FAIL)
  5. warns on docs with no ``type:`` ("unclassified"); any other ``type:``
     value is a frozen record and is never checked further.

Markdown matched by the repo-root ``.docs-check-ignore`` (gitignore-style) is
a fixture: never checked, never warned. A ``covers:`` glob that matches no
tracked file is a WARN (dangling-covers).

Internal errors never block: with ``--warn-only`` they print
``WARN: internal-error`` and exit 0. See scripts/docs/README.md for the full
contract.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_CHARS = set("*<>{}$")
CODE_EXTENSIONS = {
    "py",
    "go",
    "cs",
    "ts",
    "tsx",
    "js",
    "jsx",
    "rb",
    "java",
    "cpp",
    "cc",
    "c",
    "h",
    "hpp",
    "sh",
    "sql",
    "json",
    "yml",
    "yaml",
    "md",
    "rs",
    "php",
    "kt",
    "swift",
    "cshtml",
    "proto",
    "graphql",
    "tf",
    "bicep",
}
LINE_CITE_RE = re.compile(r"\b([\w./-]+)\.([A-Za-z0-9]+):(\d+)\b")
FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
MDLINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
DOCS_NA_RE = re.compile(r"^Docs:\s*n/a\b", re.IGNORECASE | re.MULTILINE)

# A path suffix that points *into* a file: `x.py:42`, `x.py:42-50`, `x.py::Sym`.
LINE_SUFFIX_RE = re.compile(r":[^/]*$")
QUERY_RE = re.compile(r"\?[\w.-]+=")

# Sibling fleet repos (f-infra, f-brain, f-csc-at, ...). A ref that names one
# of them is cross-repo and cannot be verified from this checkout.
REPO_NAME_PATTERN = r"f-[a-z0-9]+(?:-[a-z0-9]+)*"
REPO_SEGMENT_RE = re.compile(r"^" + REPO_NAME_PATTERN + r"$")
PRECEDED_BY_REPO_RE = re.compile(
    r"(?:^|[^\w/.-])(?:faheem-ai-app/)?(" + REPO_NAME_PATTERN + r")(?:['’]s)?"  # noqa: RUF001 — matches curly apostrophes in real doc prose
    r"(?:\s+(?:repo|repository)(?:['’]s)?)?[\s:(*_]*$"  # noqa: RUF001 — matches curly apostrophes in real doc prose
)
FOLLOWED_BY_REPO_RE = re.compile(
    r"^[*_]*\s*(?:\(\s*[*_]*(?:faheem-ai-app/)?(" + REPO_NAME_PATTERN + r")[*_]*\s*\)"
    r"|(?:in|on|from|under|inside|of|at)\s+(?:the\s+)?[*_]*(?:faheem-ai-app/)?("
    + REPO_NAME_PATTERN
    + r"))(?![\w/.-])"
)
LIST_SEPARATOR_RE = re.compile(r"^\s*(?:[,;+&/]\s*)?(?:(?:and|or)\s*)?$")

# A note exempts a dead ref only when it is attached to that ref: the
# keyword sits right before the span ("Removed: `x`"), right after it
# ("`x` was removed", "`x` (new script)"), in the same parenthetical clause,
# or in a "Deleted under D4: `a`, `b`" lead-in label. The target of a
# rename/move ("renamed to `x`", "→ `x`", "use `x`") is always checked.
PURGE_WORDS = r"removed|deleted|retired|purged|renamed|relocated|no longer exists?"
NOTE_WORDS = (
    PURGE_WORDS + r"|to follow|new script|not yet (?:created|committed|built|added|written|landed)"
    r"|to be (?:created|added|written|built)|will be (?:created|added|written)"
)
_EMPH = r"(?:\*\*|\*|_|~~)*"
_AUX = r"(?:(?:was|were|is|are|has been|have been|had been|got|since|later|now)\s+)?"
_FILLER = r"(?:(?!(?:not|never|no)\b)[a-z][\w'’-]*\s+){0,2}?"  # noqa: RUF001 — matches curly apostrophes in real doc prose
NOTE_BEFORE_RE = re.compile(r"\b(?i:" + PURGE_WORDS + r")" + _EMPH + r":?\s*" + _EMPH + r"$")
NOTE_AFTER_RE = re.compile(
    r"^" + _EMPH + r"\s*(?:\(\s*" + _AUX + r"(?i:" + NOTE_WORDS + r")\b"
    r"|(?:[—–:,-]\s*)?" + _AUX + _FILLER + r"(?i:" + NOTE_WORDS + r")\b)"  # noqa: RUF001 — matches em/en dashes in real doc prose
)
NOTE_ANYWHERE_RE = re.compile(r"\b(?i:" + NOTE_WORDS + r")\b")
REDIRECT_BEFORE_RE = re.compile(r"(?:→|->|=>|\b(?i:to|into|by|use))\s*" + _EMPH + r"\s*$")
LEAD_IN_LABEL_RE = re.compile(r"\s*(?:[-*+]\s+|\d+[.)]\s+)?([^:]{1,80}?):" + _EMPH + r"\s")
PAREN_CLAUSE_SPLIT_RE = re.compile(r";\s|\s[—–]\s")  # noqa: RUF001 — matches em/en dashes in real doc prose
# A doc that declares Azure Blob containers ("`tools/`, `configs/` are
# `publicAccess: blob`") cites blob paths under them, not repo paths.
BLOB_DECL_RE = re.compile(
    r"(?i)publicaccess|blob\s+containers?|storage\s+containers?|\.blob\.core\.windows\.net"
)
CONTAINER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}/?$")
SENTENCE_END_RE = re.compile(r"[.!?](?=\s)")
CLAUSE_SPLIT_RE = re.compile(r";\s")
IGNORE_NEXT_LINE_RE = re.compile(r"<!--\s*docs-check:\s*ignore-next-line\s*-->")
BLOCK_START_RE = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|#{1,6}\s|\||<!--)")
STANDALONE_LINE_RE = re.compile(r"^\s*(?:#{1,6}\s|\||<!--)")
BLOCKQUOTE_RE = re.compile(r"^\s*(?:>\s?)+")

# Agent-harness folders are globally git-ignored across the fleet (and absent
# in CI checkouts), so refs into them can never be verified.
HARNESS_PREFIXES = (".claude/", ".codex/", ".agents/", ".opencode/")

# Repo-root file of gitignore-style globs; matched markdown is a fixture
# (runtime-served content, test data) — never checked, never warned.
IGNORE_FILE = ".docs-check-ignore"


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


@dataclass
class Finding:
    severity: str  # "FAIL" | "WARN"
    rule: str
    path: str
    line: int | None
    detail: str


def format_text(f: Finding) -> str:
    loc = f.path or "-"
    if f.path and f.line:
        loc = f"{f.path}:{f.line}"
    return f"{f.severity}: {f.rule} {loc} — {f.detail}"


def _gh_escape_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _gh_escape_property(value: str) -> str:
    return _gh_escape_data(value).replace(":", "%3A").replace(",", "%2C")


def format_github(f: Finding) -> str:
    level = "error" if f.severity == "FAIL" else "warning"
    props = []
    if f.path:
        props.append(f"file={_gh_escape_property(f.path)}")
        if f.line:
            props.append(f"line={f.line}")
    loc = (" " + ",".join(props)) if props else ""
    return f"::{level}{loc}::{_gh_escape_data(f.rule + ': ' + f.detail)}"


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------


def run_git(repo: Path, args: Sequence[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def try_git(repo: Path, args: Sequence[str]) -> str | None:
    try:
        return run_git(repo, args)
    except RuntimeError:
        return None


def get_tracked_files(repo: Path, ref: str | None) -> list[str]:
    if ref is None:
        out = run_git(repo, ["ls-files"])
    else:
        out = run_git(repo, ["ls-tree", "-r", "--name-only", ref])
    return [line for line in out.splitlines() if line]


def read_file(repo: Path, path: str, ref: str | None) -> str:
    if ref is None:
        try:
            return (repo / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""  # tracked but deleted in the working tree
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def is_zero_sha(sha: str | None) -> bool:
    return bool(sha) and set(sha) <= {"0"}


def resolve_new_branch_base(repo: Path, local_sha: str) -> str | None:
    for candidate in ("origin/HEAD", "origin/dev", "origin/main"):
        out = try_git(repo, ["merge-base", local_sha, candidate])
        if out:
            return out.strip()
    return None


def resolve_range_base(repo: Path, base: str | None, head: str) -> str | None:
    """The merge-base of base and head, so a PR range never includes code that
    landed on the base branch after the PR branched off. A missing/zero base
    (new branch) falls back to the default branch's merge-base."""
    if not base or is_zero_sha(base):
        return resolve_new_branch_base(repo, head)
    out = try_git(repo, ["merge-base", base, head])
    return out.strip() if out and out.strip() else None


def diff_name_only(repo: Path, base: str, head: str) -> list[str] | None:
    out = try_git(repo, ["diff", "--name-only", f"{base}..{head}"])
    if out is None:
        return None
    return [line for line in out.splitlines() if line]


def log_messages(repo: Path, base: str, head: str) -> list[str]:
    out = try_git(repo, ["log", f"{base}..{head}", "--format=%B%x1e"])
    if not out:
        return []
    return [m for m in out.split("\x1e") if m.strip()]


def current_repo_name(repo: Path) -> str | None:
    url = try_git(repo, ["config", "--get", "remote.origin.url"])
    if url and url.strip():
        name = re.split(r"[/:]", url.strip().rstrip("/"))[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if name:
            return name
    env_repo = os.environ.get("GITHUB_REPOSITORY", "")
    if env_repo:
        return env_repo.split("/")[-1]
    return None


# --------------------------------------------------------------------------
# Frontmatter parsing (stdlib subset — no PyYAML)
# --------------------------------------------------------------------------


_MIN_QUOTED_LEN = 2  # opening + closing quote char


def strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= _MIN_QUOTED_LEN and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def strip_yaml_comment(value: str) -> str:
    """Drops a trailing ` # comment` that sits outside quotes."""
    quote = None
    for i, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"') and (i == 0 or value[i - 1] in " \t[,"):
            quote = ch
        elif ch == "#" and (i == 0 or value[i - 1] in " \t"):
            return value[:i].rstrip()
    return value


def split_flow_items(inner: str) -> list[str]:
    """Splits a flow-list body on commas that sit outside quotes."""
    items: list[str] = []
    buf: list[str] = []
    quote = None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"') and not "".join(buf).strip():
            quote = ch
            buf.append(ch)
            continue
        if ch == ",":
            items.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    items.append("".join(buf))
    return [strip_quotes(x.strip()) for x in items if x.strip()]


def parse_inline_list(value: str) -> list[str]:
    inner = value.strip()
    if inner.startswith("["):
        inner = inner[1:]
    if inner.endswith("]"):
        inner = inner[:-1]
    return split_flow_items(inner)


KEY_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$")
LIST_ITEM_RE = re.compile(r"^-(?:\s+(.*))?$")


def parse_frontmatter(  # noqa: PLR0912, PLR0915 — stdlib YAML-subset parser; branches map 1:1 to the frontmatter grammar, splitting it would hide that
    text: str,
) -> tuple[dict[str, object], str, int]:
    """Returns (data, body, offset) where offset = # of lines consumed by
    the frontmatter block (0 if there is none)."""
    text = text.lstrip("﻿")  # a UTF-8 BOM must not hide the frontmatter
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text, 0
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, text, 0

    data: dict[str, object] = {}
    block_list_key: str | None = None  # key whose value is a `- item` list
    flow_key: str | None = None  # key whose `[...]` spans several lines
    flow_buf = ""
    for raw_line in lines[1:end_idx]:
        if flow_key is not None:
            flow_buf += " " + strip_yaml_comment(raw_line.strip())
            if "]" in flow_buf:
                data[flow_key] = parse_inline_list(flow_buf[: flow_buf.rindex("]") + 1])
                flow_key = None
            continue
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        item = LIST_ITEM_RE.match(stripped)
        if item and block_list_key is not None:
            # YAML allows the `- item` list at column 0 or indented.
            value = strip_quotes(strip_yaml_comment((item.group(1) or "").strip()))
            if value:
                existing = data.get(block_list_key)
                if isinstance(existing, list):
                    existing.append(value)
            continue
        if raw_line[0] in (" ", "\t"):
            continue  # nested mapping content — not part of the contract
        m = KEY_RE.match(raw_line)
        block_list_key = None
        if not m:
            continue
        key = m.group(1)
        value = strip_yaml_comment(m.group(2).strip())
        if value == "":
            data[key] = []
            block_list_key = key
        elif value.startswith("["):
            if value.endswith("]"):
                data[key] = parse_inline_list(value)
            else:
                flow_key, flow_buf = key, value
        else:
            data[key] = strip_quotes(value)
    if flow_key is not None:  # unterminated flow list: best effort
        data[flow_key] = parse_inline_list(flow_buf)

    body = "\n".join(lines[end_idx + 1 :])
    return data, body, end_idx + 1


def as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def type_value(frontmatter: dict[str, object]) -> str:
    value = frontmatter.get("type")
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else ""
    return str(value or "").strip()


def classify(frontmatter: dict[str, object]) -> str:
    value = type_value(frontmatter).lower()
    if not value:
        return "unclassified"
    if value == "living":
        return "living"
    return "record"


# --------------------------------------------------------------------------
# Glob matching — "**" crosses directories, "*" does not
# --------------------------------------------------------------------------

_glob_cache: dict[str, re.Pattern[str]] = {}


def glob_body(pattern: str) -> str:
    """Regex body (no anchors) for a repo-relative glob."""
    parts = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                i += 2
                if i < n and pattern[i] == "/":
                    parts.append("(?:.*/)?")  # "**/" = zero or more whole dirs
                    i += 1
                else:
                    parts.append(".*")
                continue
            parts.append("[^/]*")
            i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(c))
            i += 1
    return "".join(parts)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    cached = _glob_cache.get(pattern)
    if cached is not None:
        return cached
    regex = re.compile("^" + glob_body(pattern) + "$")
    _glob_cache[pattern] = regex
    return regex


def glob_match_any(path: str, patterns: Iterable[str]) -> bool:
    return any(glob_to_regex(p).match(path) for p in patterns)


IgnorePattern = tuple[bool, "re.Pattern[str]"]  # (negated, regex)


def compile_ignore_patterns(text: str) -> list[IgnorePattern]:
    """gitignore-style: `#` comments, `!` negation (last match wins), a
    trailing `/` = directory, a pattern with an inner `/` is anchored at the
    repo root, otherwise it matches at any depth."""
    patterns: list[IgnorePattern] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        if line.startswith("\\"):
            line = line[1:]  # escaped leading `#` or `!`
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            continue
        anchored = "/" in line
        line = line.lstrip("/")
        prefix = "^" if anchored else "^(?:.*/)?"
        suffix = "/.*$" if dir_only else "(?:/.*)?$"
        patterns.append((negated, re.compile(prefix + glob_body(line) + suffix)))
    return patterns


def matches_ignore(path: str, patterns: Sequence[IgnorePattern]) -> bool:
    ignored = False
    for negated, regex in patterns:
        if regex.match(path):
            ignored = not negated
    return ignored


# --------------------------------------------------------------------------
# Repo index — the tree a check runs against
# --------------------------------------------------------------------------


def top_level_entries(tracked_files: Sequence[str]) -> set[str]:
    return {f.split("/")[0] for f in tracked_files}


def build_dir_set(tracked_files: Sequence[str]) -> set[str]:
    dirs = set()
    for f in tracked_files:
        parts = f.split("/")[:-1]
        cur = ""
        for p in parts:
            cur = f"{cur}/{p}" if cur else p
            dirs.add(cur)
    return dirs


class RepoIndex:
    def __init__(
        self,
        files: Sequence[str],
        repo: Path | None = None,
        self_name: str | None = None,
        fixture_patterns: Sequence[IgnorePattern] = (),
    ):
        self.repo = repo
        self.fixture_patterns = list(fixture_patterns)
        self._glob_hits: dict[str, bool] = {}
        self.files = list(files)
        self.file_set = set(self.files)
        self.dirs = build_dir_set(self.files)
        self.top_level = top_level_entries(self.files)
        self.self_name = self_name
        self._gitignored: dict[str, bool] = {}

    def is_fixture(self, path: str) -> bool:
        return matches_ignore(path, self.fixture_patterns)

    def glob_matches_something(self, pattern: str) -> bool:
        hit = self._glob_hits.get(pattern)
        if hit is None:
            regex = glob_to_regex(pattern)
            hit = any(regex.match(f) for f in self.files)
            self._glob_hits[pattern] = hit
        return hit

    def exists(self, path: str) -> bool:
        return path in self.file_set or path.rstrip("/") in self.dirs

    def is_gitignored(self, path: str) -> bool:
        """True when git would ignore the path (outputs, local-only files)."""
        if self.repo is None:
            return False
        cached = self._gitignored.get(path)
        if cached is None:
            result = subprocess.run(
                ["git", "-C", str(self.repo), "check-ignore", "-q", "--no-index", "--", path],
                capture_output=True,
                check=False,
            )
            cached = result.returncode == 0
            self._gitignored[path] = cached
        return cached


def build_index(repo: Path, ref: str | None) -> RepoIndex:
    return RepoIndex(
        get_tracked_files(repo, ref),
        repo=repo,
        self_name=current_repo_name(repo),
        fixture_patterns=compile_ignore_patterns(read_file(repo, IGNORE_FILE, ref)),
    )


# --------------------------------------------------------------------------
# Rule 1: dead repo-relative path refs
# --------------------------------------------------------------------------


@dataclass
class Candidate:
    line_no: int  # 1-based, relative to the body
    raw: str
    kind: str  # "code" | "backtick" | "mdlink"
    noted: bool = False  # an attached note says it is gone on purpose / not built yet
    cross_repo: bool = False
    suppressed: bool = False
    blob_path: bool = False  # under a blob container the doc declares


def strip_token_punct(tok: str) -> str:
    return tok.strip("`'\",;()[]")


def expand_braces(token: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", token)
    if not m:
        return [token]
    inner = m.group(1)
    if "," not in inner:
        return [token]
    prefix, suffix = token[: m.start()], token[m.end() :]
    results: list[str] = []
    for part in (p.strip() for p in inner.split(",")):
        candidate = prefix + part + suffix
        if "{" in candidate and "}" in candidate:
            results.extend(expand_braces(candidate))
        else:
            results.append(candidate)
    return results


def suppressed_line_numbers(body: str) -> set[int]:
    """Lines right after a `<!-- docs-check: ignore-next-line -->` marker."""
    return {
        idx + 1
        for idx, line in enumerate(body.splitlines(), start=1)
        if IGNORE_NEXT_LINE_RE.search(line)
    }


def mask_code_spans(text: str) -> str:
    """Same-length copy with code-span contents blanked, so their words and
    punctuation never count as prose."""
    return BACKTICK_RE.sub(lambda m: "`" + "x" * len(m.group(1)) + "`", text)


def _segment_start(masked: str, start: int) -> int:
    """Start of the sentence clause (split on `. ` and `; `) holding `start`."""
    seg = 0
    for m in SENTENCE_END_RE.finditer(masked, 0, start):
        seg = m.end()
    for m in CLAUSE_SPLIT_RE.finditer(masked, seg, start):
        seg = m.end()
    return seg


def _paren_clause_has_note(masked: str, start: int, end: int) -> bool:
    depth = 0
    open_idx = None
    for i in range(start - 1, -1, -1):
        ch = masked[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            if depth == 0:
                open_idx = i
                break
            depth -= 1
    if open_idx is None:
        return False
    depth = 0
    close_idx = None
    for i in range(end, len(masked)):
        ch = masked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                close_idx = i
                break
            depth -= 1
    if close_idx is None:
        return False
    inner = masked[open_idx + 1 : close_idx]
    rel_start, rel_end = start - open_idx - 1, end - open_idx - 1
    clause_start = 0
    for m in PAREN_CLAUSE_SPLIT_RE.finditer(inner, 0, rel_start):
        clause_start = m.end()
    m = PAREN_CLAUSE_SPLIT_RE.search(inner, rel_end)
    clause_end = m.start() if m else len(inner)
    return bool(NOTE_ANYWHERE_RE.search(inner[clause_start:clause_end]))


_LEAD_IN_LABEL_MAX_WORDS = 8


def _lead_in_has_note(masked: str, start: int) -> bool:
    seg = _segment_start(masked, start)
    lm = LEAD_IN_LABEL_RE.match(masked, seg, start)
    if not lm or len(lm.group(1).split()) > _LEAD_IN_LABEL_MAX_WORDS:
        return False
    return bool(NOTE_ANYWHERE_RE.search(lm.group(1)))


def note_before(masked: str, start: int) -> bool:
    return bool(NOTE_BEFORE_RE.search(masked, max(0, start - 120), start))


def note_after(masked: str, end: int) -> bool:
    return bool(NOTE_AFTER_RE.match(masked[end : end + 160]))


def redirected(masked: str, start: int) -> bool:
    """`x` is the new name/place ("renamed to `x`", "→ `x`", "use `x`")."""
    return bool(REDIRECT_BEFORE_RE.search(masked, max(0, start - 40), start))


def span_notes(masked: str, spans: list[tuple[int, int]]) -> list[bool]:
    """Per span: does an attached note say it is gone on purpose / not built
    yet? A list of spans ("`a`, `b` and `c` were removed") shares the note
    attached to its first or last member."""
    own = [
        note_after(masked, e)
        or _paren_clause_has_note(masked, s, e)
        or _lead_in_has_note(masked, s)
        or note_before(masked, s)
        for s, e in spans
    ]
    result = list(own)
    i = 0
    while i < len(spans):
        j = i
        while j + 1 < len(spans) and LIST_SEPARATOR_RE.match(masked[spans[j][1] : spans[j + 1][0]]):
            j += 1
        if j > i:
            group_note = (
                note_before(masked, spans[i][0])
                or _lead_in_has_note(masked, spans[i][0])
                or note_after(masked, spans[j][1])
            )
            for k in range(i, j + 1):
                result[k] = result[k] or group_note
        i = j + 1
    return [
        noted and not redirected(masked, s) for noted, (s, _e) in zip(result, spans, strict=True)
    ]


def declared_blob_containers(joined: str) -> set[str]:
    masked = mask_code_spans(joined)
    containers: set[str] = set()
    starts = [0] + [m.end() for m in SENTENCE_END_RE.finditer(masked)]
    ends = [*starts[1:], len(joined)]
    for a, b in zip(starts, ends, strict=True):
        sentence = joined[a:b]
        if not BLOB_DECL_RE.search(sentence):
            continue
        for m in BACKTICK_RE.finditer(sentence):
            name = m.group(1).strip()
            if CONTAINER_NAME_RE.match(name):
                containers.add(name.rstrip("/"))
    return containers


def names_other_repo(name: str | None, self_name: str | None) -> bool:
    return bool(name) and name != self_name


def first_segment_is_other_repo(token: str, self_name: str | None) -> bool:
    first = token[2:] if token.startswith("./") else token
    first = first.split("/")[0]
    return bool(REPO_SEGMENT_RE.match(first)) and first != self_name


def _block_candidates(
    block: list[tuple[int, str]], self_name: str | None, suppressed: set[int]
) -> tuple[list[Candidate], set[str]]:
    """Candidates from one prose block (a paragraph, list item, table row or
    heading), plus the blob containers it declares. The block's lines are
    joined so sentences can span soft wraps."""
    offsets: list[int] = []
    pos = 0
    for _idx, content in block:
        offsets.append(pos)
        pos += len(content) + 1
    joined = " ".join(content for _idx, content in block)
    masked = mask_code_spans(joined)

    def line_of(abs_pos: int) -> int:
        line_no = block[0][0]
        for i, off in enumerate(offsets):
            if off <= abs_pos:
                line_no = block[i][0]
        return line_no

    out: list[Candidate] = []
    spans = list(BACKTICK_RE.finditer(joined))
    notes = span_notes(masked, [(m.start(), m.end()) for m in spans])
    prev_cross_end: int | None = None
    for m, noted in zip(spans, notes, strict=True):
        line_no = line_of(m.start())
        pm = PRECEDED_BY_REPO_RE.search(joined[: m.start()])
        fm = FOLLOWED_BY_REPO_RE.match(joined[m.end() :])
        named = (pm.group(1) if pm else None) or (fm and (fm.group(1) or fm.group(2)))
        cross = names_other_repo(named, self_name)
        if (
            not cross
            and prev_cross_end is not None
            and LIST_SEPARATOR_RE.match(joined[prev_cross_end : m.start()])
        ):
            cross = True  # "f-brain `a`, `b`" — the list inherits the repo
        span_cross = cross
        # A backtick span may hold a whole shell command
        # ("./iaac/deploy/x.sh dev") — tokenize so trailing args aren't glued on.
        for tok in m.group(1).split():
            tok_clean = strip_token_punct(tok)
            if "/" not in tok_clean:
                continue
            if first_segment_is_other_repo(tok_clean, self_name):
                span_cross = True
            out.append(
                Candidate(line_no, tok_clean, "backtick", noted, cross, line_no in suppressed)
            )
        prev_cross_end = m.end() if span_cross else None
    for m in MDLINK_RE.finditer(joined):
        target = m.group(1).strip()
        if not target:
            continue
        line_no = line_of(m.start())
        noted = span_notes(masked, [(m.start(), m.end())])[0]
        out.append(
            Candidate(line_no, target.split()[0], "mdlink", noted, False, line_no in suppressed)
        )
    return out, declared_blob_containers(joined)


def find_path_candidates(body: str, self_name: str | None = None) -> list[Candidate]:
    lines = body.splitlines()
    suppressed = suppressed_line_numbers(body)
    candidates: list[Candidate] = []
    containers: set[str] = set()
    block: list[tuple[int, str]] = []

    def flush() -> None:
        if block:
            found, declared = _block_candidates(block, self_name, suppressed)
            candidates.extend(found)
            containers.update(declared)
            block.clear()

    in_fence = False
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if FENCE_RE.match(stripped):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            code_line = line
            cm = re.search(r"(^|\s)#", code_line)
            if cm:
                code_line = code_line[: cm.start()]
            for tok in code_line.split():
                tok_clean = strip_token_punct(tok)
                if "/" in tok_clean:
                    candidates.append(
                        Candidate(idx, tok_clean, "code", suppressed=idx in suppressed)
                    )
            continue
        content = BLOCKQUOTE_RE.sub("", line)
        if not content.strip():
            flush()
            continue
        if BLOCK_START_RE.match(content):
            flush()
        block.append((idx, content))
        if STANDALONE_LINE_RE.match(content):
            flush()
    flush()
    for cand in candidates:
        token = cand.raw[2:] if cand.raw.startswith("./") else cand.raw
        cand.blob_path = token.split("/")[0] in containers
    return candidates


def normalize_ref(raw: str, kind: str) -> str | None:
    """Reduces a ref to the path part: drops `#anchor`, `?query` and a
    `:line` / `::Symbol` suffix. None = not a path worth checking."""
    token = raw.strip()
    if kind == "mdlink" and token.startswith("<") and token.endswith(">"):
        token = token[1:-1]
    if not token or token.startswith("#"):
        return None
    if "://" in token or token.startswith(("mailto:", "tel:")):
        return None
    token = token.split("#", 1)[0]
    if "?" in token:
        if kind != "mdlink" and not QUERY_RE.search(token):
            return None  # a shell/glob wildcard, not a query string
        token = token.split("?", 1)[0]
    token = LINE_SUFFIX_RE.sub("", token)
    return token or None


def resolve_repo_path(candidate: str, kind: str, doc_path: str, index: RepoIndex) -> str | None:
    """Maps a ref to a repo-relative path, or None when it is not a path in
    this repo (unknown top-level, another fleet repo, outside the root)."""
    if kind == "mdlink":
        if candidate.startswith("/"):
            repo_path = candidate.lstrip("/")
        else:
            doc_dir = posixpath.dirname(doc_path)
            joined = posixpath.join(doc_dir, candidate) if doc_dir else candidate
            repo_path = posixpath.normpath(joined)
        if repo_path == ".." or repo_path.startswith("../"):
            return None  # points at a sibling checkout
        first = repo_path.split("/")[0]
        if REPO_SEGMENT_RE.match(first) and first not in index.top_level:
            return None
        return repo_path
    repo_path = candidate[2:] if candidate.startswith("./") else candidate
    first, _, rest = repo_path.partition("/")
    if first in index.top_level:
        return repo_path
    if index.self_name and first == index.self_name and rest:
        return resolve_repo_path(rest, kind, doc_path, index)  # `f-infra/docs/x.md` in f-infra
    return None


def is_exempt(repo_path: str, cand: Candidate, index: RepoIndex) -> bool:
    """A dead-looking ref that is dead on purpose or unverifiable here."""
    if repo_path.startswith(HARNESS_PREFIXES) or repo_path + "/" in HARNESS_PREFIXES:
        return True
    if cand.noted or cand.blob_path:
        return True
    return index.is_gitignored(repo_path)


def check_path_candidate(cand: Candidate, doc_path: str, index: RepoIndex) -> list[str]:
    """Returns the dead repo-relative sub-paths for this candidate (usually 0
    or 1; brace expansion can yield more than one)."""
    if cand.suppressed or cand.cross_repo:
        return []
    token = normalize_ref(cand.raw, cand.kind)
    if token is None:
        return []
    if any(ch in token for ch in ("$", "<", ">", "*")):
        return []

    dead: list[str] = []
    for candidate in expand_braces(token):
        if not candidate or any(ch in candidate for ch in FORBIDDEN_CHARS):
            continue
        repo_path = resolve_repo_path(candidate, cand.kind, doc_path, index)
        if repo_path is None:
            continue
        repo_path = repo_path[2:] if repo_path.startswith("./") else repo_path
        if index.exists(repo_path):
            continue
        if is_exempt(repo_path, cand, index):
            continue
        dead.append(repo_path)
    return dead


def check_dead_refs(doc_path: str, body: str, offset: int, index: RepoIndex) -> list[Finding]:
    findings = []
    for cand in find_path_candidates(body, index.self_name):
        for dead_path in check_path_candidate(cand, doc_path, index):
            findings.append(
                Finding(
                    "FAIL",
                    "dead-ref",
                    doc_path,
                    offset + cand.line_no,
                    f"{dead_path!r} (from {cand.raw!r}) not found",
                )
            )
    return findings


# --------------------------------------------------------------------------
# Rule 4: file:line citations
# --------------------------------------------------------------------------


def check_line_citations(doc_path: str, body: str, offset: int) -> list[Finding]:
    findings = []
    suppressed = suppressed_line_numbers(body)
    for idx, line in enumerate(body.splitlines(), start=1):
        if idx in suppressed:
            continue
        for m in LINE_CITE_RE.finditer(line):
            ext = m.group(2).lower()
            if ext not in CODE_EXTENSIONS:
                continue
            findings.append(
                Finding(
                    "WARN",
                    "line-citation",
                    doc_path,
                    offset + idx,
                    f"cites {m.group(0)!r} — cite symbols, not line numbers",
                )
            )
    return findings


# --------------------------------------------------------------------------
# covers: glob hygiene
# --------------------------------------------------------------------------


def check_covers_globs(doc_path: str, covers: Sequence[str], index: RepoIndex) -> list[Finding]:
    findings = []
    for pattern in covers:
        if "{" in pattern or "[" in pattern:
            findings.append(
                Finding(
                    "WARN",
                    "unsupported-glob",
                    doc_path,
                    1,
                    f"covers: {pattern!r} — '{{...}}' and '[...]' are matched literally; "
                    "list each glob separately",
                )
            )
            continue
        if index.glob_matches_something(pattern):
            continue
        hint = ""
        if first_segment_is_other_repo(pattern, index.self_name):
            hint = "; covers: lists this repo's paths only, never another repo's"
        findings.append(
            Finding(
                "WARN",
                "dangling-covers",
                doc_path,
                1,
                f"covers: {pattern!r} matches no tracked file, so rule 2 never fires for it{hint}",
            )
        )
    return findings


# --------------------------------------------------------------------------
# Per-doc processing
# --------------------------------------------------------------------------


def process_doc(
    doc_path: str,
    content: str,
    index: RepoIndex,
    treat_unclassified_as_living: bool,
) -> tuple[list[Finding], str, list[str]]:
    frontmatter, body, offset = parse_frontmatter(content)
    classification = classify(frontmatter)
    covers = as_list(frontmatter.get("covers"))
    findings: list[Finding] = []

    if classification == "unclassified":
        findings.append(Finding("WARN", "unclassified", doc_path, 1, "no 'type:' in frontmatter"))
        if not treat_unclassified_as_living:
            return findings, classification, covers
    elif classification == "record":
        declared = type_value(frontmatter)
        if "living" in declared.lower():
            findings.append(
                Finding(
                    "WARN",
                    "suspect-type",
                    doc_path,
                    1,
                    f"type {declared!r} is treated as a record; write exactly 'type: living' to have it checked",
                )
            )
        return findings, classification, covers

    if not covers:
        findings.append(
            Finding("WARN", "missing-covers", doc_path, 1, "living doc has no 'covers:' globs")
        )
    findings += check_covers_globs(doc_path, covers, index)

    findings += check_dead_refs(doc_path, body, offset, index)
    findings += check_line_citations(doc_path, body, offset)
    return findings, classification, covers


def is_living_doc_path(path: str) -> bool:
    return path.endswith(".md") and not path.startswith(".claude/")


# --------------------------------------------------------------------------
# Mode: all
# --------------------------------------------------------------------------


def run_all_checks(repo: Path, treat_unclassified_as_living: bool) -> list[Finding]:
    index = build_index(repo, ref=None)
    findings: list[Finding] = []
    for doc in index.files:
        if not is_living_doc_path(doc) or index.is_fixture(doc):
            continue
        content = read_file(repo, doc, ref=None)
        doc_findings, _classification, _covers = process_doc(
            doc, content, index, treat_unclassified_as_living
        )
        findings += doc_findings
    return findings


# --------------------------------------------------------------------------
# Mode: range / pre-push
# --------------------------------------------------------------------------


def run_range_checks(
    repo: Path, base: str | None, head: str, treat_unclassified_as_living: bool
) -> list[Finding]:
    index = build_index(repo, ref=head)
    findings: list[Finding] = []
    living_covers: dict[str, list[str]] = {}
    for doc in index.files:
        if not is_living_doc_path(doc) or index.is_fixture(doc):
            continue
        content = read_file(repo, doc, ref=head)
        doc_findings, classification, covers = process_doc(
            doc, content, index, treat_unclassified_as_living
        )
        findings += doc_findings
        if classification == "living" or (
            classification == "unclassified" and treat_unclassified_as_living
        ):
            living_covers[doc] = covers

    effective_base = resolve_range_base(repo, base, head)
    changed_all = diff_name_only(repo, effective_base, head) if effective_base else None
    if changed_all is None:
        findings.append(
            Finding(
                "WARN",
                "range-unresolved",
                "",
                None,
                f"no usable base for {head[:12]} (base {str(base or 'none')[:12]} is not in local "
                "history) — covers-drift skipped; fetch and push again",
            )
        )
        return findings

    changed_all_set = set(changed_all)
    changed_non_md = [f for f in changed_all if not f.endswith(".md")]
    messages = log_messages(repo, effective_base, head)
    has_na_trailer = any(DOCS_NA_RE.search(msg) for msg in messages)

    for doc, covers in living_covers.items():
        if not covers:
            continue
        matched = [f for f in changed_non_md if glob_match_any(f, covers)]
        if matched and doc not in changed_all_set and not has_na_trailer:
            findings.append(
                Finding(
                    "FAIL",
                    "covers-drift",
                    doc,
                    None,
                    f"covers changed files {matched} with no doc update and no 'Docs: n/a' trailer",
                )
            )
    return findings


_PRE_PUSH_LINE_FIELDS = 4  # <local ref> <local sha> <remote ref> <remote sha>


def run_pre_push_checks(
    repo: Path, stdin_lines: Iterable[str], treat_unclassified_as_living: bool
) -> list[Finding]:
    findings: list[Finding] = []
    for raw_line in stdin_lines:
        line = raw_line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != _PRE_PUSH_LINE_FIELDS:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = parts
        if is_zero_sha(local_sha):
            continue  # delete — nothing to check
        base = None if is_zero_sha(remote_sha) else remote_sha
        findings += run_range_checks(repo, base, local_sha, treat_unclassified_as_living)
    return findings


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Faheem docs-drift checker")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--mode", choices=["all", "range", "pre-push"], default="all")
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--warn-only", action="store_true")
    parser.add_argument("--format", choices=["text", "github"], default="text")
    parser.add_argument("--treat-unclassified-as-living", action="store_true")
    return parser


EXIT_CLEAN = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 3


class UsageError(Exception):
    pass


def dedupe(findings: Iterable[Finding]) -> list[Finding]:
    """Pushing several refs re-checks the same tree; print each finding once."""
    seen = set()
    unique: list[Finding] = []
    for f in findings:
        key = (f.severity, f.rule, f.path, f.line, f.detail)
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)
    return unique


def collect_findings(args: argparse.Namespace) -> list[Finding]:
    repo = Path(args.repo).resolve()
    if args.mode == "range":
        if not args.base or not args.head:
            raise UsageError("--mode range requires --base and --head")
        return run_range_checks(repo, args.base, args.head, args.treat_unclassified_as_living)
    if args.mode == "pre-push":
        stdin_lines = sys.stdin.readlines()
        return run_pre_push_checks(repo, stdin_lines, args.treat_unclassified_as_living)
    return run_all_checks(repo, args.treat_unclassified_as_living)


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    warn_only = "--warn-only" in raw_args
    try:
        args = build_parser().parse_args(raw_args)
    except SystemExit as exc:
        # argparse already printed its usage error; warn-only never blocks.
        if exc.code in (0, None):
            return EXIT_CLEAN
        return EXIT_CLEAN if warn_only else EXIT_USAGE

    formatter = format_github if args.format == "github" else format_text
    try:
        findings = collect_findings(args)
    except UsageError as exc:
        print(f"docs-check: {exc}", file=sys.stderr)
        return EXIT_CLEAN if warn_only else EXIT_USAGE
    except Exception as exc:
        finding = Finding(
            "WARN", "internal-error", "docs-check.py", None, f"{type(exc).__name__}: {exc}"
        )
        print(formatter(finding))
        return EXIT_CLEAN if warn_only else EXIT_INTERNAL

    findings = dedupe(findings)
    findings.sort(key=lambda f: (f.path, f.line or 0, f.rule, f.detail))
    for f in findings:
        print(formatter(f))

    if warn_only:
        return EXIT_CLEAN
    return EXIT_FAIL if any(f.severity == "FAIL" for f in findings) else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
