---
name: astra-orchestrator
description: Orchestrate complex Codex coding work with GPT-6.1 Sol as planner, integrator, and reviewer, using the local Claude Code CLI for bounded implementation tasks in isolated Git worktrees. Use for multi-file features, debugging across components, and repo-wide changes.
---

# Orchestrator — GPT-6.1 Sol + Local Claude Code

The user's explicit instructions take precedence over this skill.

## Topology

- root: `gpt-6.1-sol` with reasoning effort selected by the user's Codex configuration; owns planning, task decomposition, integration, and evaluation
- local executor: Claude Code CLI, invoked through `claude_executor.py` for one bounded implementation task
- Codex subagents remain available for independent exploration and review; they do not start Claude Code

## Execution protocol

For each delegated implementation task, Sol creates a task contract with a unique task ID, goal, allowed file scope, acceptance criteria, allowed validation commands, and explicit exclusions. Keep each contract narrow enough to review as one unit. Save the contract as JSON in the target repository, for example `task.json`:

```json
{
  "task_id": "invoice-export",
  "goal": "Add CSV export for invoices",
  "allowed_file_scope": ["src/invoices/**", "tests/invoices/**"],
  "acceptance_criteria": ["Exports include invoice number, date, and total"],
  "validation_commands": ["python -m pytest tests/invoices"],
  "exclusions": ["Do not change the database schema", "Do not commit"]
}
```

Run the executor from the target Git repository, using a new worktree path for each task:

```sh
python3 .agents/skills/astra-orchestrator/claude_executor.py \
  --repo . \
  --contract task.json \
  --worktree ../my-project-claude-tasks/invoice-export
```

The executor creates a task-specific Git worktree and runs `claude -p --output-format json` there. Claude uses the model configured in the local Claude Code settings by default. Set `CLAUDE_EXECUTOR_MODEL` only when you need to select another Provider-supported model, such as `deepseek-v4-flash`; set `CLAUDE_EXECUTOR_BIN` to override the CLI executable path. The executor does not read, modify, or replace user-level Claude configuration or credentials.

Claude's tools are limited to file operations and the validation commands listed in the contract. Do not use permission-bypass options. The executor never commits, merges, or removes a worktree containing changes. Inspect its structured result and worktree diff, run or verify the acceptance criteria, then integrate approved changes yourself.

Non-zero CLI exits, unavailable models, malformed output, failed validation, and unmet acceptance criteria return to Sol for diagnosis and replanning. Retry at most once for an explicitly transient CLI failure; never repeat a task because its implementation or acceptance check failed.

## Verification

Review changed files against the contract's allowed scope and exclusions. Verify every acceptance criterion and validation result independently before integration. Resolve remaining issues or report them; do not claim completion based only on Claude's summary.
