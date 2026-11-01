---
name: tmux-subagents
description: Subagents to delegate work to - using full, interactive Claude Code sessions running in tmux windows. Only use when instructed by the user or by prompts. (Perks over native Claude Code systems: subagents can ask the parent questions mid-task, can be inspected or taken over at any time by the user, can spawn their own subagents to any depth, and the parent can use "user" slash commands like /context or /usage).
---

# tmux-subagents

A helper is a normal interactive `claude` running in its own tmux session. You give it a task
file, it works in a fresh context, and it writes a short report back. You only see the report,
not everything it read or tried. Helpers can be run one after another or side by side, and they
can start helpers of their own, so each level only holds the reports of the level below.

Script: `scripts/agents.py` in this skill's folder (only needs python3, tmux and claude). Run it with `-h` for all commands.

## The loop

1. **Write the task** to a file: what to do, what to read, what not to touch, and what the report
   should contain. The helper knows nothing of your conversation, so give it everything it
   needs or point it at docs.
2. `agents.py start NAME TASKFILE` (options: `--cwd DIR`; anything after `--` goes to `claude` as is).
   Without extras, the helper starts like a plain `claude` with the user's settings.
   To pick a model or effort for one helper, for instance: `agents.py start NAME TASKFILE -- --model M --effort E`.
3. `agents.py wait NAME` **with run_in_background**, then end your turn. You're woken when it exits.
   For parallel helpers, start each one and run one background `wait` per helper.
4. Act on what `wait` prints:
   - **FINISHED** + report: check the result as much as the task calls for. Then `agents.py stop NAME`,
     or keep it open for a follow-up (`send` + `wait`).
   - **QUESTION**: answer it if you can (`agents.py send NAME "..."`, then wait again). If only
     the user can answer, ask the user. If you're a helper yourself, write your own `question.md` and end your turn.
   - **STOPPED / DIED / STILL RUNNING**: read the screen it printed (`peek` for more). If it's
     waiting on a background task of its own, just `wait` again (it counts the next event only).
     Else nudge it (`send NAME "go on"`), or start a fresh helper on what's left.
   - **NOT STARTED**: it's stuck before its first turn, usually a trust-folder or login dialog.
     Answer it once via `tmux attach -t NAME` (or ask the user to), then wait again.
   - **WAITING**: it wants input on its screen (a prompt or dialog). Read the screen, then deal with it
     or ask the user to (`tmux attach -t NAME`), or stop it. If you're a helper yourself, ask through
     your own `question.md`: only the top session can reach the user.

`send` sets the old report aside, so the next `wait` waits for a new one. Use it for follow-ups
that build on what the helper already knows. For multi-line text: `agents.py send NAME - <<'EOF' ... EOF`.
A slash command (`agents.py send NAME /context`) is typed as is and starts no turn: read its result
with `peek` instead of waiting.

## Good to know

- **Parallel helpers and shared files**: two helpers editing the same files will clash. Give
  them separate files, or separate worktrees/folders (`--cwd`).
- **Names** (letters, digits, `-`, `_`): prefix a child's name with its parent's (`fix`, `fix-tests`, `fix-docs`), so `cost fix`
  covers the whole tree. `list` shows all helpers and their state.
- **Reports come through files, not the screen.** The helper's last act is writing `report.md`
  (or `question.md`) in `$SUB_DIR`. It never uses AskUserQuestion, since nobody would answer.
- `agents.py cost NAME` gives the tokens a helper (and its children) used, from the session logs.
- The user can watch any helper live: `tmux attach -t NAME` (detach: Ctrl-b d).

## How it works (for debugging)

State lives in `~/.cache/tmux-subagents/NAME/` (`TMUX_SUBAGENTS_DIR` overrides it): `task.md`,
`report.md`, `question.md`, plus marker files. `start` passes a Stop hook and a Notification hook
via `claude --settings`. They write `stopped` / `waiting` marks into `$SUB_DIR`, so `wait` can tell
"ended its turn without reporting" apart from "still working". A helper waiting on its own
helpers holds a `busy-CHILD` mark per child, so its parent ignores its turn ends meanwhile.
Session log: `~/.claude/projects/<cwd with non-alphanumerics as ->/<session-id>.jsonl`.

If helpers can't be started, do the work in your own session and say so.
