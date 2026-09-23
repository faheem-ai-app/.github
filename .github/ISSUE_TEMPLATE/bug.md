---
name: "G2 · Bug"
about: Something is broken — symptom, evidence, expected vs actual.
title: "fix(<scope>): <symptom>"
labels: ["bug"]
---
<!-- G2 — Bug issue (approved 2026-09-23). Mini variant (obvious bug): one line per section; §0 + Not included never dropped. Title = L5 shape, `fix(<scope>): <symptom>` (standards.md).
     CLI: `sed '1,/^---$/d' bug.md > body.md`, fill it, `gh issue create --title … --body-file body.md`. -->

## 0) TL;DR

- 🐞 **Broken:** … (who is hurt, where, since when)
- 🤝 **Waiting on you:** fix · decision · info from … (or "triage only")

## 1) Evidence

- **Env / build:** dev | prod · commit or app version
- **Steps:** 1. … 2. …
- **Expected → actual:** … → …
- **Proof:** log line · request id · screenshot — secrets, tokens and real PII masked

## 2) Suspected cause

<!-- A guess labeled as a guess, or "unknown". -->

## 3) Not included

<!-- Non-optional. What this issue is NOT about (sibling bugs, nice-to-haves) — link them. -->

- …
