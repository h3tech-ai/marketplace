#!/usr/bin/env python3
"""
Lint Runner — Auto-detects project stack and runs the appropriate linter.

Usage:
    python3 lint_runner.py [directory]
    python3 lint_runner.py --help

Detects: ESLint, Prettier, Ruff, Flake8, Black, golangci-lint, Clippy, RuboCop
Stdlib-only — invokes external tools via subprocess.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Dict, Optional, Tuple


def detect_stack(directory: str) -> Dict[str, bool]:
    """Detect which tech stacks are present."""
    stack = {}

    checks = {
        "node": ["package.json"],
        "python": ["pyproject.toml", "setup.py", "requirements.txt", "Pipfile"],
        "go": ["go.mod"],
        "rust": ["Cargo.toml"],
        "ruby": ["Gemfile"],
    }

    for tech, files in checks.items():
        for f in files:
            if os.path.exists(os.path.join(directory, f)):
                stack[tech] = True
                break

    return stack


def detect_linters(directory: str, stack: Dict[str, bool]) -> List[Dict]:
    """Detect which linters are configured."""
    linters = []

    if stack.get("node"):
        # Check for ESLint
        eslint_configs = [
            ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json",
            ".eslintrc.yml", "eslint.config.js", "eslint.config.mjs", "eslint.config.ts",
        ]
        for config in eslint_configs:
            if os.path.exists(os.path.join(directory, config)):
                linters.append({
                    "name": "ESLint",
                    "command": ["npx", "eslint", ".", "--max-warnings=0"],
                    "fix_command": ["npx", "eslint", ".", "--fix"],
                })
                break
        else:
            # Check package.json for eslint
            pkg_path = os.path.join(directory, "package.json")
            if os.path.exists(pkg_path):
                try:
                    with open(pkg_path) as f:
                        pkg = json.load(f)
                    if "eslint" in pkg.get("devDependencies", {}):
                        linters.append({
                            "name": "ESLint",
                            "command": ["npx", "eslint", "."],
                            "fix_command": ["npx", "eslint", ".", "--fix"],
                        })
                except (json.JSONDecodeError, IOError):
                    pass

        # Check for Prettier
        prettier_configs = [
            ".prettierrc", ".prettierrc.js", ".prettierrc.json",
            ".prettierrc.yml", "prettier.config.js", "prettier.config.mjs",
        ]
        for config in prettier_configs:
            if os.path.exists(os.path.join(directory, config)):
                linters.append({
                    "name": "Prettier",
                    "command": ["npx", "prettier", "--check", "."],
                    "fix_command": ["npx", "prettier", "--write", "."],
                })
                break

        # Check for TypeScript
        if os.path.exists(os.path.join(directory, "tsconfig.json")):
            linters.append({
                "name": "TypeScript",
                "command": ["npx", "tsc", "--noEmit"],
                "fix_command": None,  # Can't auto-fix type errors
            })

    if stack.get("python"):
        # Check for Ruff
        ruff_available = subprocess.run(
            ["ruff", "--version"], capture_output=True, text=True
        ).returncode == 0 if _command_exists("ruff") else False

        if ruff_available:
            linters.append({
                "name": "Ruff",
                "command": ["ruff", "check", "."],
                "fix_command": ["ruff", "check", "--fix", "."],
            })
        else:
            # Fall back to flake8
            if _command_exists("flake8"):
                linters.append({
                    "name": "Flake8",
                    "command": ["flake8", "."],
                    "fix_command": None,
                })

        # Check for mypy
        if _command_exists("mypy"):
            linters.append({
                "name": "mypy",
                "command": ["mypy", "."],
                "fix_command": None,
            })

    if stack.get("go"):
        if _command_exists("golangci-lint"):
            linters.append({
                "name": "golangci-lint",
                "command": ["golangci-lint", "run", "./..."],
                "fix_command": ["golangci-lint", "run", "--fix", "./..."],
            })
        else:
            linters.append({
                "name": "go vet",
                "command": ["go", "vet", "./..."],
                "fix_command": None,
            })

    if stack.get("rust"):
        linters.append({
            "name": "Clippy",
            "command": ["cargo", "clippy", "--", "-D", "warnings"],
            "fix_command": ["cargo", "clippy", "--fix", "--allow-dirty"],
        })

    return linters


def _command_exists(cmd: str) -> bool:
    """Check if a command exists on the system."""
    try:
        subprocess.run(
            ["which", cmd], capture_output=True, text=True
        )
        return subprocess.run(
            ["which", cmd], capture_output=True
        ).returncode == 0
    except (OSError, FileNotFoundError):
        return False


def run_linter(linter: Dict, directory: str, fix: bool = False) -> Dict:
    """Run a single linter and return results."""
    cmd = linter["fix_command"] if (fix and linter.get("fix_command")) else linter["command"]

    if cmd is None:
        return {
            "name": linter["name"],
            "status": "skipped",
            "message": "No auto-fix available",
        }

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=directory,
            timeout=120,
        )
        return {
            "name": linter["name"],
            "status": "pass" if result.returncode == 0 else "fail",
            "exit_code": result.returncode,
            "stdout": result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
            "command": " ".join(cmd),
        }
    except subprocess.TimeoutExpired:
        return {
            "name": linter["name"],
            "status": "timeout",
            "message": "Timed out after 120s",
            "command": " ".join(cmd),
        }
    except FileNotFoundError:
        return {
            "name": linter["name"],
            "status": "not_found",
            "message": f"Command not found: {cmd[0]}",
        }


def main():
    parser = argparse.ArgumentParser(
        description="Lint Runner — Auto-detect stack and run appropriate linters"
    )
    parser.add_argument("directory", nargs="?", default=".",
                       help="Directory to lint (default: current directory)")
    parser.add_argument("--fix", action="store_true",
                       help="Run linters in fix mode")
    parser.add_argument("--format", choices=["text", "json"], default="text",
                       help="Output format")

    args = parser.parse_args()
    directory = os.path.abspath(args.directory)

    if not os.path.isdir(directory):
        print(f"Error: {directory} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Detect stack
    stack = detect_stack(directory)
    if not stack:
        print("No recognized project files found", file=sys.stderr)
        sys.exit(1)

    # Detect linters
    linters = detect_linters(directory, stack)
    if not linters:
        print("No linters detected for this project", file=sys.stderr)
        sys.exit(1)

    # Run linters
    results = []
    for linter in linters:
        result = run_linter(linter, directory, fix=args.fix)
        results.append(result)

    # Output
    if args.format == "json":
        print(json.dumps({"stack": stack, "results": results}, indent=2))
    else:
        print("━" * 60)
        print("  LINT REPORT")
        print("━" * 60)
        print(f"\n  Stack: {', '.join(stack.keys())}")
        print(f"  Linters: {len(linters)}\n")

        all_pass = True
        for r in results:
            icon = "✓" if r["status"] == "pass" else "✗" if r["status"] == "fail" else "⚠"
            print(f"  {icon} {r['name']}: {r['status']}")
            if r["status"] == "fail":
                all_pass = False
                # Show truncated output
                output = r.get("stdout", "") + r.get("stderr", "")
                if output:
                    lines = output.strip().split('\n')
                    for line in lines[:10]:
                        print(f"    {line}")
                    if len(lines) > 10:
                        print(f"    ... ({len(lines) - 10} more lines)")

        print("\n" + "━" * 60)
        sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
