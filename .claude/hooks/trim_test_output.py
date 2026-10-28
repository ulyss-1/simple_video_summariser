#!/usr/bin/env python3
"""PreToolUse(Bash) hook: shrink test-runner output to the failures.

Hook mode (no args): reads the hook JSON on stdin. If the Bash command runs a
test suite, rewrites it so the real output goes to a temp file and only the
failure lines come back, capped at MAX_LINES with a note of what was dropped.
The original exit code is preserved.

Filter mode (`--filter FILE RC`): prints the trimmed view of FILE. The
rewritten command calls this.
"""
import json
import re
import shlex
import sys

MAX_LINES = 100
TEST_CMD = re.compile(
    r"\b(pytest|vitest|playwright test|npm (run )?test(:e2e)?)\b"
)
FAIL_LINE = re.compile(
    r"(FAILED|ERROR|FAIL\b|\u2717|\u00d7|\u2718|AssertionError|Traceback|^E\s|"
    r"\d+ (failed|error))",
    re.IGNORECASE,
)


def failures(text: str) -> list[str]:
    lines = text.splitlines()
    # pytest: keep the FAILURES/ERRORS sections and the
    keep: list[str] = []
    on = False
    for line in lines:
        if re.match(r"^=+ (FAILURES|ERRORS) =+$", line) or re.match(
            r"^=+ short test summary info =+$", line
        ):
            on = True
        elif re.match(r"^=+ .*(passed|failed|error).* =+$", line):
            keep.append(line)
            on = False
            continue
        if on:
            keep.append(line)
    if keep:
        return keep
    # Other runners: failure-looking lines plus the closong summary.
    keep = [line for line in lines if FAIL_LINE.search(line)]
    return keep + lines[-3:] if keep else lines[-3:]


def trim(text: str, passed: bool) -> str:
    out = text.splitlines()[-3:] if passed else failures(text)
    dropped = max(0, len(out) - MAX_LINES)
    if dropped:
        out = out[-MAX_LINES:]
        out.insert(0, f"[trim_test_output: {dropped} earlier lines dropped]")
    return "\n".join(out)


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "--filter":
        with open(sys.argv[2], errors="replace") as f:
            print(trim(f.read(), sys.argv[3] == "0"))
        return
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Bash":
        return
    tool_input = data.get("tool_input", {})
    cmd = tool_input.get("command", "")
    if (
        not TEST_CMD.search(cmd)
        or "|" in cmd
        or "trim_test_output" in cmd
        or tool_input.get("run_in_background")
    ):
        return
    me = shlex.quote(__file__)
    wrapped = (
        f'__t=$(mktemp); {{ {cmd}\n}} >"$__t" 2>&1; __rc=$?; '
        f'python3 {me} --filter "$__t" "$__rc"; rm -f "$__t"; exit $__rc'
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "test output trimming",
                    "updatedInput": {**tool_input, "command": wrapped},
                }
            }
        )
    )


if __name__ == "__main__":
    main()
