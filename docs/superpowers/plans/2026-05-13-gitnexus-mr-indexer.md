# GitNexus MR Indexer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an optional GitLab webhook pre-review step that clones each MR source commit into an isolated workspace, runs GitNexus analysis, and lets the same PR-Agent review use that MR-head index.

**Architecture:** Add a focused `GitNexusMRIndexer` service under `pr_agent/servers/` and call it from the existing GitLab webhook before auto commands run. The indexer stores data under `workspace_root/project_id/mr_iid/source_sha/`, supports bounded parallelism, reuses existing indexes for the same SHA, and deletes old workspaces by TTL.

**Tech Stack:** Python 3.12, FastAPI background tasks, subprocess git/npx calls, Dynaconf settings, pytest unit tests.

---

### Task 1: Indexer Unit

**Files:**
- Create: `pr_agent/servers/gitnexus_mr_indexer.py`
- Test: `tests/unittest/test_gitnexus_mr_indexer.py`

- [ ] Write tests for deterministic workspace paths, existing index reuse, TTL cleanup, and per-MR latest SHA checks.
- [ ] Run `PYTHONPATH=. python -m pytest tests/unittest/test_gitnexus_mr_indexer.py -q` and confirm the new tests fail because the module does not exist.
- [ ] Implement the indexer with injected command runner and clock so tests do not run real `git` or `npx`.
- [ ] Re-run the same test file and confirm it passes.

### Task 2: GitLab Webhook Integration

**Files:**
- Modify: `pr_agent/servers/gitlab_webhook.py`
- Test: `tests/unittest/test_gitnexus_mr_indexer.py`

- [ ] Add tests that GitLab MR open/update payloads prepare a GitNexus index when enabled and skip cleanly when disabled or metadata is incomplete.
- [ ] Run the test and confirm it fails before webhook integration.
- [ ] Call the indexer before `_perform_commands_gitlab`, then set `gitnexus` runtime settings for the current request to use `mode = "pr_head_context"` with the prepared workspace.
- [ ] Re-run targeted webhook/indexer tests.

### Task 3: PR Reviewer Mode

**Files:**
- Modify: `pr_agent/tools/pr_reviewer.py`
- Test: `tests/unittest/test_mcp_config.py`

- [ ] Add tests for `mode = "pr_head_context"` prompt/header behavior and MCP calls.
- [ ] Run `PYTHONPATH=. python -m pytest tests/unittest/test_mcp_config.py -q` and confirm the new tests fail.
- [ ] Implement `pr_head_context` as MR-head indexed context, similar to `base_context` but without stale snapshot warnings.
- [ ] Re-run `tests/unittest/test_mcp_config.py`.

### Task 4: Configuration and Docs

**Files:**
- Modify: `pr_agent/settings/configuration.toml`
- Modify: `docs/docs/usage-guide/internal_fork_zh.md`
- Modify: `docs/docs/usage-guide/additional_configurations.md`

- [ ] Add `[gitnexus_indexer]` defaults with the feature disabled.
- [ ] Document the GitLab MR workflow, workspace layout, concurrency limit, timeout, TTL cleanup, and fallback behavior.
- [ ] Run `git diff --check`.

### Task 5: Verification

**Files:**
- All touched files

- [ ] Run targeted tests:
  `PYTHONPATH=. python -m pytest tests/unittest/test_gitnexus_mr_indexer.py tests/unittest/test_mcp_config.py tests/unittest/test_gitlab_webhook_port.py -q`
- [ ] Run existing GitNexus/GitLab tests:
  `PYTHONPATH=. python -m pytest tests/unittest/test_mcp_config.py tests/unittest/test_gitlab_provider.py tests/unittest/test_gitlab_webhook_port.py -q`
- [ ] Run full unit tests if targeted tests are clean:
  `PYTHONPATH=. python -m pytest tests/unittest -q`
- [ ] Commit and push `codex/gitnexus-mr-indexer`.
