#!/usr/bin/env python3
"""Run one contracted Claude Code task in an isolated Git worktree.

Claude uses its local configured model unless CLAUDE_EXECUTOR_MODEL is set.
The executor never commits, merges, or removes a worktree containing changes.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = {
    "task_id", "goal", "allowed_file_scope", "acceptance_criteria",
    "validation_commands", "exclusions",
}
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
TRANSIENT_MARKERS = ("http 429", "http 500", "http 502", "http 503", "http 504", "temporarily unavailable", "connection reset", "timed out")


def validate_contract(contract: Any) -> dict[str, Any]:
    if not isinstance(contract, dict) or REQUIRED_FIELDS - contract.keys():
        missing = sorted(REQUIRED_FIELDS - contract.keys()) if isinstance(contract, dict) else sorted(REQUIRED_FIELDS)
        raise ValueError(f"contract is missing required fields: {', '.join(missing)}")
    task_id = contract["task_id"]
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise ValueError("task_id must contain only letters, digits, dot, underscore, or hyphen")
    if not isinstance(contract["goal"], str) or not contract["goal"].strip():
        raise ValueError("goal must be a non-empty string")
    for field in ("allowed_file_scope", "acceptance_criteria", "validation_commands", "exclusions"):
        values = contract[field]
        if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError(f"{field} must be a list of non-empty strings")
    for item in contract["allowed_file_scope"]:
        path = Path(item)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe path in allowed_file_scope: {item}")
    for command in contract["validation_commands"]:
        if any(ch in command for ch in ";&|<>`\n\r") or "$" in command:
            raise ValueError(f"validation command contains shell syntax: {command}")
        try:
            tokens = shlex.split(command)
        except ValueError as exc:
            raise ValueError(f"invalid validation command: {command}") from exc
        if not tokens or any(token in {"&&", "||", ";", "|"} for token in tokens):
            raise ValueError(f"invalid validation command: {command}")
    return contract


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def changed_files(worktree: Path) -> list[str]:
    output = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=all"],
        text=True, capture_output=True, check=True,
    ).stdout
    return sorted(line[3:] for line in output.splitlines() if len(line) >= 4)


def permission_args(contract: dict[str, Any]) -> list[str]:
    tools = ["Read", "Edit", "Write", "Glob", "Grep"]
    tools.extend(f"Bash({command})" for command in contract["validation_commands"])
    return ["--allowedTools", *tools]


def _run_cli(command: list[str], worktree: Path, env: dict[str, str], retry_delay: float) -> tuple[subprocess.CompletedProcess[str], int]:
    for attempt in range(2):
        result = subprocess.run(command, cwd=worktree, env=env, text=True, capture_output=True, check=False)
        if result.returncode == 0 or attempt == 1:
            return result, attempt
        diagnostic = f"{result.stdout}\n{result.stderr}".lower()
        if not any(marker in diagnostic for marker in TRANSIENT_MARKERS):
            return result, attempt
        if retry_delay:
            time.sleep(retry_delay)
    raise AssertionError("unreachable")


def execute_task(
    repo: str | Path,
    contract_path: str | Path,
    worktree: str | Path,
    *,
    env: dict[str, str] | None = None,
    model: str | None = None,
    retry_delay: float = 1.0,
) -> dict[str, Any]:
    repository = Path(repo).resolve()
    worktree_path = Path(worktree).resolve()
    try:
        contract = validate_contract(json.loads(Path(contract_path).read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {"status": "invalid_contract", "changed_files": [], "validation_results": [], "unresolved_issues": [str(exc)]}

    if not (repository / ".git").exists():
        return _failure("invalid_repository", "repo must be a Git working tree")
    if worktree_path.exists():
        return _failure("worktree_exists", "refusing to reuse an existing worktree path")
    try:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        git(repository, "worktree", "add", "--detach", str(worktree_path), "HEAD")
    except (OSError, RuntimeError) as exc:
        return _failure("worktree_error", str(exc))

    run_env = dict(os.environ if env is None else env)
    binary = run_env.get("CLAUDE_EXECUTOR_BIN", "claude")
    selected_model = model if model is not None else run_env.get("CLAUDE_EXECUTOR_MODEL")
    prompt = (
        "Execute this single development task in the current worktree. Follow the allowed file scope, "
        "acceptance criteria, validation commands, and exclusions. Do not commit or merge. "
        "Run only listed validation commands. Return a concise account of changes, checks, and unresolved issues.\n\n"
        + json.dumps(contract, ensure_ascii=False)
    )
    command = [binary, "-p", "--output-format", "json"]
    if selected_model:
        command.extend(["--model", selected_model])
    command.extend(permission_args(contract))
    command.extend(["--", prompt])

    try:
        result, retries = _run_cli(command, worktree_path, run_env, retry_delay)
    except OSError as exc:
        return _finish(repository, worktree_path, _failure("cli_error", str(exc)), retain=False)
    output = result.stdout.strip()
    parsed: Any = None
    if output:
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            parsed = None
    diagnostics = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode != 0:
        status = "model_unavailable" if "model" in diagnostics.lower() and any(word in diagnostics.lower() for word in ("not found", "unknown", "unavailable")) else "cli_error"
        report = _failure(status, diagnostics or f"Claude exited with code {result.returncode}")
    elif parsed is None or not isinstance(parsed, dict):
        report = _failure("invalid_json", "Claude output was not a JSON object")
    elif parsed.get("is_error"):
        report = _failure("task_error", str(parsed.get("result") or parsed))
    else:
        files = changed_files(worktree_path)
        outside_scope = [
            path for path in files
            if not any(fnmatch.fnmatchcase(path, pattern) for pattern in contract["allowed_file_scope"])
        ]
        validation_results = run_validations(worktree_path, contract["validation_commands"], run_env)
        validation_failed = any(item["returncode"] != 0 for item in validation_results)
        report = {
            "status": (
                "scope_violation" if outside_scope else
                "validation_failed" if validation_failed else
                "completed_review_required"
            ),
            "claude_result": parsed.get("result", parsed),
            "changed_files": files,
            "validation_results": validation_results,
            "unresolved_issues": [
                *( [f"Changed files outside allowed scope: {', '.join(outside_scope)}"] if outside_scope else [] ),
                *( ["One or more validation commands failed."] if validation_failed else [] ),
                "Sol must independently verify acceptance criteria and validation results.",
            ],
            "retries": retries,
        }
    return _finish(repository, worktree_path, report, retain=bool(changed_files(worktree_path)))


def _failure(status: str, issue: str) -> dict[str, Any]:
    return {"status": status, "changed_files": [], "validation_results": [], "unresolved_issues": [issue]}


def run_validations(worktree: Path, commands: list[str], env: dict[str, str]) -> list[dict[str, Any]]:
    results = []
    for command in commands:
        try:
            completed = subprocess.run(
                shlex.split(command), cwd=worktree, env=env, text=True, capture_output=True, check=False
            )
            result = {
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        except OSError as exc:
            result = {"returncode": 127, "stdout": "", "stderr": str(exc)}
        results.append({"command": command, **result})
    return results


def _finish(repo: Path, worktree: Path, report: dict[str, Any], *, retain: bool) -> dict[str, Any]:
    if worktree.exists():
        files = changed_files(worktree)
        report["changed_files"] = files
        if files:
            report["worktree"] = str(worktree)
            report["worktree_retained"] = True
        elif not retain:
            try:
                git(repo, "worktree", "remove", "--force", str(worktree))
                report["worktree_retained"] = False
            except RuntimeError as exc:
                report.setdefault("unresolved_issues", []).append(f"Could not remove clean worktree: {exc}")
                report["worktree"] = str(worktree)
                report["worktree_retained"] = True
        else:
            report["worktree"] = str(worktree)
            report["worktree_retained"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="source Git repository")
    parser.add_argument("--contract", required=True, help="JSON task contract")
    parser.add_argument("--worktree", required=True, help="new task-specific worktree path")
    parser.add_argument("--model", help="override Claude Code's configured model")
    args = parser.parse_args(argv)
    report = execute_task(args.repo, args.contract, args.worktree, model=args.model)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed_review_required" else 1


if __name__ == "__main__":
    raise SystemExit(main())
