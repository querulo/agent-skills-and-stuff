#!/usr/bin/env python3
"""Helper Claude Code sessions in tmux, driven by a parent Claude session.

Each helper is a normal interactive `claude` in its own tmux session NAME. It talks back
through files in $TMUX_SUBAGENTS_DIR/NAME/ (default ~/.cache/tmux-subagents/NAME/):
  task.md      its instructions (copied there by start)
  report.md    written by the helper when finished (its report to the parent)
  question.md  written by the helper when it needs an answer; then it ends its turn
  stopped      touched by a Stop hook at each of its turn ends
  waiting      touched by a Notification hook (it wants input on its screen)
  busy-CHILD   the helper is itself waiting on helper CHILD: its turn ends don't count
               while CHILD still works
The hooks come in through `claude --settings`, so no project config is needed.
Nesting works: each helper has its own folder, a parent only reads its child's.

  agents.py start NAME TASKFILE [--cwd DIR] [-- CLAUDE_ARGS...]
                                  extra args go to `claude` as they are
  agents.py wait NAME [MINUTES]   block until FINISHED / QUESTION / STOPPED / WAITING / DIED /
                                  timeout (default 480 min). Run it in the background and end
                                  your turn: you're woken when it exits.
  agents.py send NAME TEXT...|-   type TEXT (or stdin, kept multi-line) into NAME. A slash command
                                  (/context, /usage) is typed bare: read its result with peek (answer, nudge, follow-up task); then wait again
  agents.py peek NAME [LINES]     last LINES of its screen (default 40)
  agents.py stop NAME             close the tmux session (its files stay)
  agents.py list                  helpers and their state
  agents.py cost NAME...          tokens spent, from the session logs (NAME covers NAME-* too)
Watch one live: `tmux attach -t NAME` (detach: Ctrl-b d).
"""
import argparse, json, os, re, shlex, shutil, subprocess, sys, time, uuid

ROOT = os.path.expanduser(os.environ.get("TMUX_SUBAGENTS_DIR", "~/.cache/tmux-subagents"))
CLAUDE_DIR = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
SCRIPT = os.path.abspath(__file__)
ME = os.environ.get("SUB_DIR")  # set when the caller is itself a helper
START_GRACE = 90  # seconds before a helper with no session log counts as stuck at startup
QUIET = 20  # seconds a "stopped" must hold before we believe it (it may be writing report.md)


def tmux(*a, check=False):
    return subprocess.run(["tmux", *a], capture_output=True, text=True, check=check)


def exists(name):
    return tmux("has-session", "-t", f"={name}").returncode == 0


def alive(name):  # claude still runs (the pane stays after it exits, to show its last screen)
    r = tmux("display-message", "-p", "-t", f"={name}:", "#{pane_dead}")
    return r.returncode == 0 and r.stdout.strip() == "0"


def path(name, f=""):
    return os.path.join(ROOT, name, f) if f else os.path.join(ROOT, name)


def mtime(f):
    return os.path.getmtime(f) if os.path.exists(f) else 0


def touch(f):
    with open(f, "w") as fh:
        fh.write(str(time.time()))


def set_busy(child):
    """The caller (if a helper) now waits on child: its own turn ends mean nothing to its parent."""
    if ME:
        touch(os.path.join(ME, f"busy-{child}"))


def clear_busy(child):
    f = os.path.join(ME or "", f"busy-{child}")
    if ME and os.path.exists(f):
        touch(os.path.join(ME, "woke"))  # older stopped/waiting marks no longer count; mark before
        os.remove(f)  # removing busy, or the parent may see a false STOPPED


def log_file(name):
    f = path(name, "session-id")
    if not os.path.exists(f):
        return ""
    cwd = open(path(name, "cwd")).read().strip()
    proj = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    return os.path.join(CLAUDE_DIR, "projects", proj, open(f).read().strip() + ".jsonl")


def start(a):
    name = a.name
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        sys.exit("agents.py: use only letters, digits, - and _ in NAME (tmux mangles . and :)")
    if exists(name):
        sys.exit(f"agents.py: tmux session {name!r} already exists (agents.py stop {name}, or pick another name)")
    d = path(name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    shutil.copy(a.taskfile, path(name, "task.md"))
    cwd = os.path.abspath(a.cwd or os.getcwd())
    open(path(name, "cwd"), "w").write(cwd)
    sid = str(uuid.uuid4())
    open(path(name, "session-id"), "w").write(sid)
    hook = lambda f: {"hooks": [{"type": "command", "timeout": 5,
                                 "command": f'[ -n "$SUB_DIR" ] && date +%s > "$SUB_DIR/{f}" || true'}]}
    settings = {"hooks": {"Stop": [hook("stopped")], "Notification": [hook("waiting")]}}
    touch(path(name, "sent"))
    prompt = (f"You are a helper session ({name}) started by another Claude session. Read {path(name, 'task.md')} "
              "and do what it says. When finished, write your report to report.md in that folder as your very last "
              "act; to ask your parent something, write question.md there and end your turn. Nobody watches your "
              "screen: never use AskUserQuestion, and don't end a turn otherwise (run long commands in the "
              "foreground), except while waiting on a helper of your own in the background. "
              f"To start helpers of your own, use {SCRIPT} (-h for usage).")
    cmd = ["env", f"SUB_DIR={d}", "claude", "--session-id", sid, "--settings", json.dumps(settings), "--add-dir", ROOT,
           *a.claude_args, prompt]
    tmux("new-session", "-d", "-s", name, "-x", "200", "-y", "50", "-c", cwd, 
         'tmux set-option -w -t "$TMUX_PANE" remain-on-exit on; exec ' + shlex.join(cmd), check=True)
    set_busy(name)
    print(f"started {name} (watch: tmux attach -t {name}; folder {d})")


def state(name):
    """(STATE, file to print or None); None while it's still working."""
    since = max(mtime(path(name, "sent")), mtime(path(name, "woke")))
    if os.path.exists(path(name, "report.md")):
        return "FINISHED", "report.md"
    if os.path.exists(path(name, "question.md")):
        return "QUESTION", "question.md"
    if not alive(name):
        return "DIED (claude exited or was closed)", None
    if not os.path.exists(log_file(name)) and time.time() - mtime(path(name, "session-id")) > START_GRACE:
        return f"NOT STARTED (no session log after {START_GRACE} s): likely a trust or login dialog, peek / tmux attach", None
    # waiting on a child that still works: turn ends are deliberate. A mark whose child is done
    # doesn't count (the helper's own wait would have woken it; if that wait was killed, it's stuck)
    busy = any(f.startswith("busy-") and os.path.isdir(path(f[5:])) and state(f[5:])[0] is None
               for f in os.listdir(path(name)))
    if not busy and mtime(path(name, "waiting")) > since and mtime(path(name, "stopped")) <= since:
        return "WAITING for input on its screen: peek, then deal with it (tmux attach) or stop it", None
    if not busy and mtime(path(name, "stopped")) > since:
        return "STOPPED without a report or question: peek, then send it what to do", None
    return None, None


def wait(name, minutes=480):
    if not os.path.isdir(path(name)):
        sys.exit(f"agents.py: no helper {name!r}")
    set_busy(name)  # also on a re-wait: our turn ends while waiting mustn't look like a stop
    end, first = time.time() + minutes * 60, None
    while True:
        s, f = state(name)
        if s and (s.startswith(("STOPPED", "WAITING"))):
            first = first or time.time()
            # must still hold: it may be writing report.md, or its own wait is about to wake it
            hold = QUIET + (60 if any(x.startswith("busy-") for x in os.listdir(path(name))) else 0)
            if time.time() - first < hold:
                time.sleep(5)
                continue
        elif s is None:
            first = None
            if time.time() > end:
                s = f"STILL RUNNING after {minutes} min: peek, wait again or stop it"
            else:
                time.sleep(10)
                continue
        break
    if s.startswith(("STOPPED", "WAITING")):
        touch(path(name, "woke"))  # seen: a later wait waits for the next event (e.g. a background task)
    clear_busy(name)
    print(f"== {name}: {s}")
    if f:
        print(open(path(name, f)).read().strip())
    else:
        peek(name, 30)


def send(name, text):
    if not alive(name):
        sys.exit(f"agents.py: {name} is not running (start a fresh helper)")
    if text.strip().startswith("/"):  # a slash command (/usage, /context): typed bare, no turn, no wait
        tmux("send-keys", "-t", f"={name}:", "-l", text.strip(), check=True)
        time.sleep(0.5)
        tmux("send-keys", "-t", f"={name}:", "Enter", check=True)
        print(f"sent to {name}: read the result with peek (Escape closes a panel: tmux send-keys -t {name} Escape)")
        return
    for f in ("report.md", "question.md"):  # set the old one aside, so wait looks for a new one
        if os.path.exists(path(name, f)):
            os.replace(path(name, f), path(name, f"{f[:-3]}-{int(time.time())}.md"))
    touch(path(name, "sent"))
    text += f"\n\n(When done, write report.md or question.md in {path(name)} as before.)"
    subprocess.run(["tmux", "load-buffer", "-b", f"agents-{name}", "-"], input=text, text=True, check=True)
    tmux("paste-buffer", "-p", "-d", "-b", f"agents-{name}", "-t", f"={name}:", check=True)  # -p: keeps newlines
    time.sleep(0.7)  # let the paste land before Enter
    tmux("send-keys", "-t", f"={name}:", "Enter", check=True)
    set_busy(name)
    print(f"sent to {name}")


def peek(name, lines=40):
    if not exists(name):
        print(f"({name} is closed)")
        return
    out = tmux("capture-pane", "-p", "-J", "-S", f"-{lines * 2}", "-t", f"={name}:").stdout  # incl. scrollback
    out = re.sub(r"\n\s*\n(\s*\n)+", "\n\n", out).strip().splitlines()  # collapse blank runs
    print("\n".join(out[-lines:]))


def list_():
    for n in sorted(os.listdir(ROOT)) if os.path.isdir(ROOT) else []:
        s, _ = state(n)
        s = "closed" if not exists(n) else (s or "WORKING").split(" ")[0]
        print(f"{n:16} {s}")


def cost(names):
    tot = [0, 0, 0, 0]
    for n in sorted(h for h in (os.listdir(ROOT) if os.path.isdir(ROOT) else []) for x in names if h == x or h.startswith(x + "-")):
        f, seen, t = log_file(n), set(), [0, 0, 0, 0]
        if not os.path.exists(f):
            continue
        for line in open(f):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            m = d.get("message") or {}
            u = m.get("usage")
            if d.get("type") == "assistant" and u and m.get("id") not in seen:
                seen.add(m.get("id"))
                for i, k in enumerate(("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")):
                    t[i] += u.get(k, 0)
        tot = [x + y for x, y in zip(tot, t)]
        print(f"{n:16} in {t[0]/1e3:7.0f}k  cache-write {t[1]/1e3:7.0f}k  cache-read {t[2]/1e3:8.0f}k  out {t[3]/1e3:6.0f}k")
    print(f"{'total':16} in {tot[0]/1e3:7.0f}k  cache-write {tot[1]/1e3:7.0f}k  cache-read {tot[2]/1e3:8.0f}k  out {tot[3]/1e3:6.0f}k")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or (a[0] not in ("list", "cost") and len(a) < 2) or "-h" in a or "--help" in a or a[0] not in ("start", "wait", "send", "peek", "stop", "list", "cost"):
        sys.exit(__doc__)
    if a[0] == "start":
        p = argparse.ArgumentParser(prog="agents.py start")
        p.add_argument("name"); p.add_argument("taskfile")
        p.add_argument("--cwd")
        own, extra = (a[1:a.index("--")], a[a.index("--") + 1:]) if "--" in a else (a[1:], [])
        args = p.parse_args(own)
        args.claude_args = extra
        start(args)
    elif a[0] == "wait":
        wait(a[1], *map(int, a[2:3]))
    elif a[0] == "send":
        send(a[1], sys.stdin.read() if a[2:] == ["-"] else " ".join(a[2:]))
    elif a[0] == "peek":
        peek(a[1], *map(int, a[2:3]))
    elif a[0] == "stop":
        tmux("kill-session", "-t", f"={a[1]}")
        clear_busy(a[1])
    elif a[0] == "list":
        list_()
    elif a[0] == "cost":
        cost(a[1:])
