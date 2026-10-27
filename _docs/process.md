- Tasks are GitHub issues, one at a time
- Read the acceptance criteria before starting and before closing
- Commit regularly

Roles

- PM - grooms a task before anyone implements it, follows _docs/team/pm.md
- Engineer - implements one groomed task, follows _docs/team/software-engineer.md
- QA - checks the result against the acceptance criteria, follows _docs/team/qa-engineer.md

Orchestrator

The main session is the orchestrator. Use Sonnet model with Low or Medium effort for it, as it is just a controller that process goes smoothly. It launches the PM, the engineer and QA as subagents. It does not groom, implement or test itself.

The orchestrator, and only the orchestrator, reads `AGENTS.md` and this file
and follows them. Role agents (PM, engineer, QA) must not read either: they may
run on a different system than the Windows WSL setup `AGENTS.md` describes. So
the orchestrator turns what those files say into the launch prompt. Every
launch prompt:

- tells the agent to read its role file first and act as it says
  (`_docs/team/pm.md`, `_docs/team/software-engineer.md` or
  `_docs/team/qa-engineer.md`), and names the issue number
- does not paraphrase or override the role file. It adds only what the agent
  cannot know: the issue, the branch or PR, and the environment facts it needs
  from `AGENTS.md` (how to run the tests here, what must never be touched, for
  example the production compose project and its volumes)
- tells the engineer to branch from the current `origin/master` (fetch first),
  never from an old or unmerged branch. An earlier partial branch is reference
  material at most, and the prompt must say so explicitly when one exists
- does not tell any role agent to read `AGENTS.md` or `process.md`

Testing scope: the engineer and QA run only the tests that cover the change
(the tests the issue added or touched and those of the modules it edits), not
the whole suite. Running all of it is CI's job, on the PR. CI itself runs only
the jobs a change needs: `scripts/ci_changes.sh` maps the changed paths to the
`python`, `frontend` and `images` jobs (e2e follows `frontend`), docs no test
reads start nothing, and pushes to master and manual runs always run all of
it. When a change adds a new kind of file, check that script's rules. The
launch prompt for the engineer and for QA states this scope.

What a role agent reads from `_docs/architecture.md` (the spec):

- PM: the whole file, to understand what the project is about before grooming
- engineer: the paragraphs relevant to the issue, and follows them
- QA: only the paragraphs relevant to the criteria being checked

While a subagent is running, check on it about every 5 minutes rather than
waiting for its completion notification. Decide which of three states it is
in: working, finished but silent, or stuck. Notifications can arrive late, out
of order, or not at all (an agent killed by a rate limit may never send one),
so waiting on them is not monitoring.

Check from outside the agent. Never read its task output file to do this: that
is the full subagent transcript and it floods the orchestrator's context.

- `ls -lt --time-style=+%H:%M:%S <the files it owns>` against the current time.
  The newest mtime versus now is the single best liveness signal
- `pgrep -af 'pytest|alembic|npm|vitest'` - is real work actually running
- `git log --oneline --since='N minutes ago'` and `git status --short`
- `gh issue view <n> --json comments --jq '.comments[-1]'` - an agent posts a
  parking note when it stops on a blocker, which is the fastest way to learn why

Recent mtimes or a live pytest means working: say so briefly and leave it
alone. A clean tree, a fresh commit and no processes means finished: go read
its issue comments and move on without waiting for the notification. Old
mtimes with no processes and no commit means stuck or dead: nudge it with a
message, or treat it as dead and check the tree for damage before resuming it.

Beware `pgrep -f`: it matches the polling shell's own command line, so
`until ! pgrep -f X; do sleep 5; done` waits on itself forever. Kill by PID.

Reporting issues

Whenever the owner asks about issues (status, backlog, what is left, what to work
on next), answer in this shape, in this order, from live data
(`gh issue list --state all --limit 500 --json number,title,state,labels,milestone`;
run `git fetch` first and say so if master moved):

1. Totals table: total, closed, open, and of the open ones how many need an
   owner decision (`needs-decision`) and how many are groomed and ready
2. Area table: one row per label (deployment, services, frontend, api,
   adapters, enhancement, phase-0, foundation) with open, closed and total
   columns. Labels overlap, so say the rows do not sum to the total
3. Batches of the open issues, each a bold heading with its issue count and
   a one-line reason it is grouped, then the issue numbers with a few words
   each. Group by what unblocks the work, not by label:
   - ready to work, no decision needed (the groomed ones)
   - small or nearly done (flag an issue whose work is already on master and
     only needs verifying and closing)
   - feature sets that need ordering (name which comes first)
   - owner decisions
   - needs a real deploy host or production data
   - platform or image decisions
   - Phase 0
   Drop a group that is empty and add one when a new theme appears
4. A suggested order, and the one question the owner must answer to start

Keep it to tables and short lists. Do not pad it with per-issue detail.

Lifecycle

1. Pick the next open issue from the backlog
2. PM grooms it. Use Sonnet 5 model with medium effort for it
3. Engineer implements it. Use Sonnet 5 model with low effort for it
4. QA verifies it. Use Sonnet 5 model with low effort for it. It checks the
   changed functionality only; the PR's CI result covers the rest
5. On FAIL, back to step 3 with the QA comment as input
6. On PASS, close the issue
7. Repeat until the backlog is empty

Rules

- Do not skip step 2
- The engineer does not close the issue
- QA does not fix the code, only outputs PASS or FAIL
- The orchestrator closes the issue only after QA outputs PASS
