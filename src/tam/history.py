"""Earlier agent sessions: finding them (`search`) and opening them again (`reopen`).

Three sources, merged by agent and session id:

- closed.jsonl: agent panes that left tam's saved state while tmux kept running,
  written by `record_closed` from state.write. Any agent, with tam's window name.
- Claude Code's transcripts, ~/.claude/projects/<dir>/<id>.jsonl, also those
  started outside tam. Title: Claude's own (`custom-title`, else `ai-title`).
- Codex's transcripts, $CODEX_HOME/sessions/**/rollout-*.jsonl.

Neither transcript format is documented, so lines tam cannot read are skipped.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from tam import paths

CLOSED_KEEP = 500
# Bytes read from the end of a Claude transcript for its latest title.
TAIL_BYTES = 256 * 1024
HEAD_LINES = 200
SNIPPET = 100


@dataclass
class Session:
    agent: str
    session_id: str
    cwd: str
    title: str = ""
    time: float = 0.0  # last activity: transcript changed, or window closed
    closed_at: float = 0.0
    name: str = ""  # tam's agent name for the pane
    tmux_session: str = ""
    transcript: str = ""
    open_window: str = ""  # window id when the session is open in tam now
    running_pid: int = 0  # a Claude process outside tmux has it
    snippet: str = ""
    words: set[str] = field(default_factory=set, repr=False)

    @property
    def id(self) -> str:
        # Without a session id the agent resumes the latest session in cwd.
        return f"{self.agent}:{self.session_id}" if self.session_id else f"{self.agent}:@{self.cwd}"

    def as_dict(self) -> dict:
        out = {
            "id": self.id,
            "agent": self.agent,
            "dir": self.cwd,
            "title": self.title,
            "last_active": _iso(self.time),
        }
        if self.closed_at:
            out["closed_in_tam"] = _iso(self.closed_at)
        if self.tmux_session:
            out["tam_session"] = self.tmux_session
        if self.open_window:
            out["open_in_window"] = self.open_window
        if self.running_pid:
            out["running_outside_tam"] = self.running_pid
        if self.snippet:
            out["match"] = self.snippet
        return out

    def line(self) -> str:
        """The picker's line."""
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.time))
        where = self.cwd.replace(str(Path.home()), "~")
        state = " [open]" if self.open_window else " [running]" if self.running_pid else ""
        text = f"{when}  {self.agent:<7} {where}  {self.title}{state}"
        return f"{text}  · {self.snippet}" if self.snippet else text


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M", time.localtime(t)) if t else ""


def _clean(text: str) -> str:
    return " ".join(text.split())


# Closed in tam


def closed_file() -> Path:
    return paths.state_dir() / "closed.jsonl"


def _agent_panes(state: dict) -> dict[tuple, dict]:
    out = {}
    for s in state.get("sessions", []):
        for w in s.get("windows", []):
            for p in w.get("panes", []):
                a = p.get("agent")
                if not a:
                    continue
                sid = a.get("session_id") or ""
                key = (a["harness"], sid) if sid else (a["harness"], a.get("name"), p.get("cwd"))
                out[key] = {
                    "agent": a["harness"],
                    "session_id": sid,
                    "cwd": p.get("cwd", ""),
                    "name": a.get("name", ""),
                    "session": s.get("name", ""),
                    "window": w.get("name", ""),
                }
    return out


def record_closed(old: dict, new: dict) -> None:
    """Append the agent panes in old that new does not have to closed.jsonl.

    A pane whose agent changed session (Claude's /clear) counts as closed too:
    its earlier conversation is no longer open. A window moved to another session
    keeps its pane and is not recorded.
    """
    before, after = _agent_panes(old), _agent_panes(new)
    gone = [v for k, v in before.items() if k not in after]
    if not gone:
        return
    path = closed_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    lines = path.read_text().splitlines() if path.exists() else []
    lines += [json.dumps({**g, "closed_at": now}) for g in gone]
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text("\n".join(lines[-CLOSED_KEEP:]) + "\n")
    os.replace(tmp, path)


def _closed() -> list[Session]:
    path = closed_file()
    out = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            d = json.loads(line)
            out.append(
                Session(
                    agent=d["agent"],
                    session_id=d.get("session_id", ""),
                    cwd=d.get("cwd", ""),
                    title=d.get("window", ""),
                    time=d["closed_at"],
                    closed_at=d["closed_at"],
                    name=d.get("name", ""),
                    tmux_session=d.get("session", ""),
                )
            )
        except (ValueError, KeyError, TypeError):
            continue
    return out


# Transcripts


def _lines(path: Path, start: int = 0) -> Iterator[dict]:
    try:
        with path.open("rb") as f:
            if start:
                f.seek(start)
                f.readline()  # a partial line
            for raw in f:
                try:
                    yield json.loads(raw)
                except ValueError:
                    continue
    except OSError:
        return


def _claude_text(d: dict) -> str:
    """The text of a user or assistant message in a Claude transcript, else ""."""
    if d.get("type") not in ("user", "assistant"):
        return ""
    content = (d.get("message") or {}).get("content")
    if isinstance(content, str):
        return "" if content.startswith("<") else content
    if isinstance(content, list):
        return " ".join(
            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


def claude_transcripts() -> list[Path]:
    return list((paths.claude_dir() / "projects").glob("*/*.jsonl"))


def claude_session(path: Path) -> Session | None:
    cwd, first = "", ""
    for i, d in enumerate(_lines(path)):
        if i > HEAD_LINES or (cwd and first):
            break
        cwd = cwd or d.get("cwd", "")
        if not first and d.get("type") == "user":
            first = _clean(_claude_text(d))[:80]
    if not cwd:
        return None
    try:
        size, mtime = path.stat().st_size, path.stat().st_mtime
    except OSError:
        return None
    custom, ai = "", ""
    for d in _lines(path, max(0, size - TAIL_BYTES)):
        if d.get("type") == "custom-title" and d.get("customTitle"):
            custom = d["customTitle"]
        elif d.get("type") == "ai-title" and d.get("aiTitle"):
            ai = d["aiTitle"]
    return Session(
        agent="claude",
        session_id=path.stem,
        cwd=cwd,
        title=_clean(custom or ai or first),
        time=mtime,
        transcript=str(path),
    )


def codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def codex_transcripts() -> list[Path]:
    return list((codex_dir() / "sessions").glob("**/rollout-*.jsonl"))


def _codex_text(d: dict) -> str:
    p = d.get("payload") or {}
    if d.get("type") != "response_item" or p.get("type") != "message":
        return ""
    if p.get("role") not in ("user", "assistant"):
        return ""
    text = " ".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict))
    return "" if text.lstrip().startswith("<") else text


def codex_session(path: Path) -> Session | None:
    sid, cwd, first = "", "", ""
    for i, d in enumerate(_lines(path)):
        if i > HEAD_LINES or (sid and first):
            break
        p = d.get("payload") or {}
        if d.get("type") == "session_meta":
            sid, cwd = p.get("id", ""), p.get("cwd", "")
        elif not first and p.get("role") == "user":
            first = _clean(_codex_text(d))[:80]
    if not sid or not cwd:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return Session("codex", sid, cwd, title=first, time=mtime, transcript=str(path))


def _content_match(s: Session, words: list[str]) -> None:
    """Add to s.words the words its messages contain, and a snippet around the first."""
    path = Path(s.transcript)
    text_of = _claude_text if s.agent == "claude" else _codex_text
    missing = [w for w in words if w not in s.words]
    try:
        with path.open("rb") as f:
            for raw in f:
                low = raw.decode(errors="ignore").lower()
                if not any(w in low for w in missing):
                    continue
                try:
                    text = _clean(text_of(json.loads(raw)))
                except ValueError:
                    continue
                hit = [w for w in missing if w in text.lower()]
                if not hit:
                    continue
                if not s.snippet:
                    at = text.lower().index(hit[0])
                    start = max(0, at - SNIPPET // 3)
                    s.snippet = ("…" if start else "") + text[start : start + SNIPPET]
                s.words.update(hit)
                missing = [w for w in missing if w not in s.words]
                if not missing:
                    return
    except OSError:
        return


# Open now


def _ttys(pids: list[int]) -> dict[int, str]:
    import subprocess

    if not pids:
        return {}
    out = subprocess.run(
        ["ps", "-o", "pid=,tty=", "-p", ",".join(map(str, pids))], capture_output=True, text=True
    ).stdout
    return {int(p): t for p, t in (line.split() for line in out.splitlines() if line.strip())}


def _live() -> tuple[dict[tuple[str, str], str], dict[str, int]]:
    """Sessions running now: ((agent, id) -> window id in tam, Claude id -> pid outside tmux).

    A pane started by tam has the id as a pane option. A Claude started by hand
    in a tam pane is found from its session file (pid) and the pane's tty.
    """
    from tam.adopt import _alive, claude_registry
    from tam.tmux import query, server_running

    rows = []
    if server_running():
        try:
            rows = query(
                ["list-panes", "-a"], ["window_id", "pane_tty", "@tam_harness", "@tam_session_id"]
            )
        except Exception:
            rows = []
    opened = {
        (r["@tam_harness"], r["@tam_session_id"]): r["window_id"]
        for r in rows
        if r["@tam_harness"] and r["@tam_session_id"]
    }
    by_tty = {r["pane_tty"].removeprefix("/dev/"): r["window_id"] for r in rows}
    registry = {pid: v for pid, v in claude_registry().items() if _alive(pid)}
    ttys = _ttys(list(registry))
    running: dict[str, int] = {}
    for pid, v in registry.items():
        window = by_tty.get(ttys.get(pid, ""))
        if window:
            opened.setdefault(("claude", v["session_id"]), window)
        else:
            running[v["session_id"]] = pid
    return opened, running


# Search


def _all(agent: str = "") -> list[Session]:
    merged: dict[str, Session] = {}
    if agent in ("", "claude"):
        for path in claude_transcripts():
            s = claude_session(path)
            if s:
                merged[s.id] = s
    if agent in ("", "codex"):
        for path in codex_transcripts():
            s = codex_session(path)
            if s:
                merged[s.id] = s
    for c in _closed():
        if agent and c.agent != agent:
            continue
        s = merged.get(c.id)
        if s is None:
            merged[c.id] = c
        else:  # the transcript has the title; tam has where it was
            s.closed_at = max(s.closed_at, c.closed_at)
            s.name, s.tmux_session = c.name, c.tmux_session
            s.time = max(s.time, c.closed_at)
    opened, running = _live()
    for s in merged.values():
        s.open_window = opened.get((s.agent, s.session_id), "")
        if s.agent == "claude" and not s.open_window:
            s.running_pid = running.get(s.session_id, 0)
    return list(merged.values())


def search(
    query: str = "",
    dir: str = "",  # noqa: A002
    agent: str = "",
    content: bool = False,
    limit: int = 20,
) -> list[Session]:
    """Sessions, newest first, that have every word of query.

    The words are looked for in the title, directory, tam's names, and with
    content=True in the messages too. dir keeps sessions in or below it.
    """
    words = [w.lower() for w in query.split()]
    root = os.path.abspath(os.path.expanduser(dir)) if dir else ""
    found = []
    for s in sorted(_all(agent), key=lambda s: s.time, reverse=True):
        if root and not (s.cwd == root or s.cwd.startswith(root.rstrip("/") + "/")):
            continue
        meta = f"{s.title} {s.cwd} {s.name} {s.tmux_session} {s.agent}".lower()
        s.words = {w for w in words if w in meta}
        if content and s.transcript and len(s.words) < len(words):
            _content_match(s, words)
        if len(s.words) == len(words):
            found.append(s)
            if limit and len(found) >= limit:
                break
    return found


def find(session_id: str) -> Session | None:
    """The session with this id (`agent:session id`, or `agent:@dir`)."""
    agent, _, rest = session_id.partition(":")
    return next((s for s in _all(agent) if s.id == session_id or s.session_id == rest), None)


# Reopen


class ReopenError(Exception):
    pass


def reopen(
    session_id: str,
    *,
    split: str | None = None,
    target: str | None = None,
    select: bool = True,
) -> dict:
    """Open the session in a new tam window (or a split of target).

    The window goes to the tmux session the window was closed in, else the
    project's session, else target's. A session open in tam already is not
    opened twice: the result names its window.
    """
    from tam.harness import load
    from tam.new import new, session_name
    from tam.tmux import server_running, session_names

    s = find(session_id)
    if s is None:
        raise ReopenError(f"no session {session_id}; search_sessions lists them")
    if s.open_window:
        return {"open_in_window": s.open_window}
    if s.running_pid:
        raise ReopenError(
            f"Claude runs this session outside tam (pid {s.running_pid}); "
            "tam adopt moves it into tam"
        )
    if s.agent not in load():
        raise ReopenError(f"unknown agent {s.agent!r}")
    if not os.path.isdir(s.cwd):
        raise ReopenError(f"its directory {s.cwd} no longer exists")
    names = session_names() if server_running() else set()
    if not split:
        for candidate in (s.tmux_session, session_name(s.cwd)):
            if candidate in names:
                target = f"={candidate}:"
                break
    session = new(
        tam_exe=paths.tam_executable(),
        harness=s.agent,
        name=s.name or None,
        split=split,
        args=[],
        cwd=s.cwd,
        target=target,
        session_id=s.session_id or None,
        started=True,
        select=select,
    )
    return {"reopened": s.id, "session": session, "dir": s.cwd}


# The picker: tam reopen


def _current_pane() -> str | None:
    """The pane tam reopen was typed in, or the client's pane in a ⌥ / popup."""
    from tam.tmux import tmux

    if os.environ.get("TMUX_PANE"):
        return os.environ["TMUX_PANE"]
    if not os.environ.get("TMUX"):
        return None
    return tmux("display-message", "-p", "#{pane_id}", check=False).strip() or None


def pick(query: str = "", content: bool = False, agent: str = "", dir: str = "") -> int:  # noqa: A002
    """fzf over earlier sessions; Enter opens the chosen one in tam, or goes to it."""
    import subprocess

    from tam.tmux import tmux

    sessions = search(query, dir=dir, agent=agent, content=content, limit=0)
    if not sessions:
        print("tam: no earlier sessions match", file=sys.stderr)
        return 1
    lines = "\n".join(f"{s.id}\t{s.line()}" for s in sessions)
    proc = subprocess.run(
        [
            "fzf",
            "--delimiter",
            "\t",
            "--with-nth",
            "2..",
            "--no-sort",
            "--prompt",
            "reopen> ",
            "--header",
            "Enter: open in tam · [open]: go to its window",
        ],
        input=lines,
        stdout=subprocess.PIPE,
        text=True,
    )
    chosen = proc.stdout.strip().split("\t", 1)[0]
    if not chosen:
        return 0
    try:
        result = reopen(chosen, target=_current_pane())
    except ReopenError as e:
        print(f"tam: {e}", file=sys.stderr)
        return 1
    if os.environ.get("TMUX"):
        where = result.get("open_in_window") or f"={result['session']}:"
        tmux("switch-client", "-t", where, check=False)
    return 0
