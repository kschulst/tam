"""`tam adopt`: move a Claude session from outside tam into a tam pane."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from tam import paths
from tam.new import new
from tam.tmux import query, server_running

RECENT_LIMIT = 30


@dataclass
class Candidate:
    session_id: str
    cwd: str
    title: str
    pid: int | None = None
    tty: str = ""
    mtime: float = 0.0
    status: str = ""  # Claude's own (idle, busy, ...), when it reports one

    def line(self) -> str:
        where = self.cwd.replace(str(Path.home()), "~")
        if self.pid:
            status = f"{self.status or 'running':<8} {self.tty:<8}"
        else:
            status = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.mtime))
        return f"{status}  {where}  {self.title}"


def project_dir(cwd: str) -> Path:
    return paths.claude_dir() / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def transcript_info(path: Path) -> tuple[str, str]:
    """(cwd, first user message) from a transcript."""
    cwd, title = "", ""
    try:
        with path.open() as f:
            for i, line in enumerate(f):
                if i > 200 or (cwd and title):
                    break
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                cwd = cwd or d.get("cwd", "")
                if d.get("type") == "user" and not title:
                    content = d.get("message", {}).get("content")
                    if isinstance(content, list):
                        content = next(
                            (c.get("text", "") for c in content if c.get("type") == "text"), ""
                        )
                    if isinstance(content, str) and not content.startswith("<"):
                        title = " ".join(content.split())[:80]
    except OSError:
        pass
    return cwd, title


def _running_claude() -> list[tuple[int, str]]:
    out = subprocess.run(["ps", "-axo", "pid=,tty=,comm="], capture_output=True, text=True).stdout
    procs = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and os.path.basename(parts[2]) == "claude":
            procs.append((int(parts[0]), parts[1]))
    return procs


def _tmux_ttys() -> set[str]:
    if not server_running():
        return set()
    return {r["pane_tty"].removeprefix("/dev/") for r in query(["list-panes", "-a"], ["pane_tty"])}


def _proc_cwd(pid: int) -> str:
    out = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True
    ).stdout
    return next((line[1:] for line in out.splitlines() if line.startswith("n")), "")


def _newest_transcript(cwd: str) -> Path | None:
    files = sorted(project_dir(cwd).glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def claude_registry() -> dict[int, dict]:
    """pid -> {session_id, cwd, name, status} from Claude Code's own session files.

    Recent Claude Code versions write ~/.claude/sessions/<pid>.json for each running
    interactive session. The format is not documented, so anything missing or
    unreadable is skipped, and callers fall back to the seen files and lsof.
    """
    out: dict[int, dict] = {}
    for f in (paths.claude_dir() / "sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text())
            pid, sid, cwd = int(d["pid"]), str(d["sessionId"]), str(d["cwd"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if sid and cwd:
            name, status = d.get("name"), d.get("status")
            out[pid] = {
                "session_id": sid,
                "cwd": cwd,
                "name": name if isinstance(name, str) else "",
                "status": status if isinstance(status, str) else "",
            }
    return out


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _gc_seen() -> None:
    seen = paths.seen_dir()
    if not seen.exists():
        return
    for f in seen.iterdir():
        if f.name.isdigit() and not _alive(int(f.name)):
            f.unlink(missing_ok=True)


def candidates() -> list[Candidate]:
    _gc_seen()
    in_tmux = _tmux_ttys()
    result: list[Candidate] = []
    used: set[str] = set()

    registry = claude_registry()
    for pid, tty in _running_claude():
        known = registry.get(pid, {})
        sid, cwd = _session_of(pid, known, fallback_cwd="")
        used.add(sid)
        if tty in in_tmux or not sid:
            continue
        _, title = transcript_info(project_dir(cwd) / f"{sid}.jsonl")
        title = known.get("name") or title
        result.append(Candidate(sid, cwd, title, pid=pid, tty=tty, status=known.get("status", "")))

    recent = sorted(
        (paths.claude_dir() / "projects").glob("*/*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for t in recent:
        if len(result) >= RECENT_LIMIT:
            break
        if t.stem in used:
            continue
        cwd, title = transcript_info(t)
        if not cwd or not title:
            continue
        result.append(Candidate(t.stem, cwd, title, mtime=t.stat().st_mtime))
    return result


def _pick(items: list[Candidate]) -> Candidate | None:
    lines = "\n".join(f"{i}\t{c.line()}" for i, c in enumerate(items))
    proc = subprocess.run(
        ["fzf", "--delimiter", "\t", "--with-nth", "2..", "--prompt", "adopt> ", "--no-sort"],
        input=lines,
        stdout=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return items[int(proc.stdout.split("\t", 1)[0])]


def _stop(pid: int) -> bool:
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not _alive(pid):
            return True
        time.sleep(0.2)
    return False


def adopt(tam_exe: str) -> int:
    from tam.hook import find_ancestor

    claude_pid = find_ancestor("claude")
    if claude_pid:
        return adopt_self(tam_exe, claude_pid)

    items = candidates()
    if not items:
        print("tam: no Claude sessions to adopt")
        return 0
    c = _pick(items)
    if c is None:
        return 0
    if c.pid:
        answer = input(f"Stop claude (pid {c.pid}, {c.tty}) and continue it here? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            return 0
        if not _stop(c.pid):
            print(f"tam: pid {c.pid} did not exit", file=sys.stderr)
            return 1
    cwd = c.cwd if os.path.isdir(c.cwd) else str(Path.home())
    new(
        tam_exe=tam_exe,
        harness="claude",
        name=None,
        split=None,
        args=[],
        cwd=cwd,
        session_id=c.session_id,
    )
    return 0


def _session_of(
    pid: int, known: dict | None = None, fallback_cwd: str | None = None
) -> tuple[str, str]:
    """(session id, cwd) of a running Claude process.

    In order: Claude Code's own session file (known, from claude_registry), tam's
    seen file from the SessionStart hook, and a guess from the newest transcript
    in the process's directory.
    """
    known = claude_registry().get(pid, {}) if known is None else known
    if known:
        return known["session_id"], known["cwd"]
    seen_file = paths.seen_dir() / str(pid)
    if seen_file.exists():
        info = json.loads(seen_file.read_text())
        return info["session_id"], info["cwd"]
    cwd = _proc_cwd(pid) or (os.getcwd() if fallback_cwd is None else fallback_cwd)
    t = _newest_transcript(cwd) if cwd else None
    return (t.stem if t else ""), cwd


def adopt_self(tam_exe: str, claude_pid: int) -> int:
    """`! tam adopt` inside a Claude session: move that session into tam.

    A detached helper stops Claude after this command has returned its output,
    then opens the tam window. It must not be a child of Claude, which is stopped.
    """
    from tam.new import session_name
    from tam.tmux import pane_option

    pane = os.environ.get("TMUX_PANE")
    if pane and pane_option(pane, "@tam_harness"):
        print("tam: this session is already in tam")
        return 0
    session_id, cwd = _session_of(claude_pid)
    # Claude Code passes its session id to commands it runs, including `!` commands.
    # This also covers sessions started before tam's hook was installed.
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID") or session_id
    cwd = _proc_cwd(claude_pid) or cwd
    if not session_id:
        print("tam: could not find this session's id", file=sys.stderr)
        return 1
    log = paths.state_dir() / "adopt.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as out:
        subprocess.Popen(
            [tam_exe, "adopt", "--finish", str(claude_pid), session_id, cwd],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,
        )
    print(
        f"tam: moving this session to tmux session '{session_name(cwd)}'. "
        "Claude stops in a moment; then run `tam` to continue."
    )
    return 0


def finish(tam_exe: str, claude_pid: int, session_id: str, cwd: str) -> int:
    """Second half of adopt_self, run detached from Claude."""
    from tam import state
    from tam.tmux import tmux

    time.sleep(1.0)  # let Claude show the command output first
    if _alive(claude_pid) and not _stop(claude_pid):
        print(f"tam adopt: pid {claude_pid} did not exit", file=sys.stderr)
        return 1
    if not os.path.isdir(cwd):
        cwd = str(Path.home())
    os.environ.pop("TMUX", None)
    os.environ.pop("TMUX_PANE", None)
    from tam.tmux import query, server_running

    was_running = server_running()
    state.ensure_tmux(tam_exe, cwd)
    # Without a server and saved state, ensure_tmux creates an empty shell session
    # as a placeholder. It is removed once the agent window exists.
    placeholder = None
    if not was_running:
        windows = query(["list-windows", "-a"], ["window_id", "session_windows"])
        if len(windows) == 1:
            placeholder = windows[0]["window_id"]
    session = new(
        tam_exe=tam_exe,
        harness="claude",
        name=None,
        split=None,
        args=[],
        cwd=cwd,
        session_id=session_id,
        attach=False,
    )
    if placeholder:
        tmux("kill-window", "-t", placeholder, check=False)
    tmux("set-option", "-g", state.ATTACH_TO, session)
    return 0
