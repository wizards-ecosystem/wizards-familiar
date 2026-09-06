#!/usr/bin/env python3
# The Wizard's Familiar — minimal local coding agent (stdlib only). See README.
import json
import os
import re
import select
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

try:
    import termios
    import tty
    _RAW_OK = True
except ImportError:  # non-unix; read_input() falls back to plain input()
    _RAW_OK = False

__version__ = "0.1.0"

BASE_URL = os.environ.get("FAMILIAR_URL", "http://localhost:8321/v1")
MODEL = os.environ.get("FAMILIAR_MODEL", "local")
CTX_CHARS = 55000 * 3  # ~3 chars/token; resolve_ctx_budget() sizes this to the server window at startup
MAX_TOOL_OUTPUT = 8000
MAX_STEPS = 40
BASH_TIMEOUT = int(os.environ.get("FAMILIAR_BASH_TIMEOUT", "300"))  # seconds; raise for slow builds/tests
HISTFILE = os.path.expanduser(os.environ.get("FAMILIAR_HISTFILE", "~/.familiar_history"))

LAST_USAGE = None  # real token counts from the server's last stream, if it reports them
PLAN = False  # /plan: read-only mode — edit tools disabled, model proposes changes instead

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"

SYSTEM_PROMPT = ""  # built by init_workspace(), which needs the working directory first


def build_system_prompt():
    """Assemble the prompt for the current working directory, appending the repo's
    FAMILIAR.md notes when it has them."""
    notes = ""
    if os.path.exists("FAMILIAR.md"):
        with open("FAMILIAR.md", errors="replace") as f:
            notes = "\n\nProject notes (from FAMILIAR.md):\n" + f.read()
    return f"""You are the Wizard's Familiar, a coding agent running fully locally on the user's Mac.
Working directory: {os.getcwd()}
Platform: macOS (zsh available via the bash tool).

Use the tools to inspect and change code. Rules:
- Read a file before editing it. Keep edits minimal and targeted.
- You already have what you've read — don't re-read a file that hasn't changed; scroll up.
- Diagnose the root cause before editing. Don't stack speculative fixes hoping one sticks.
- Prefer edit_file for small changes; use multi_edit to change several spots in one file at once;
  write_file only for new files or full rewrites.
- Use bash for everything else: grep, find, git, running code and tests — but read files with
  read_file, never `cat`, so unchanged re-reads are deduped and long files paginate.
- Verify your work (run the code or a quick check) before declaring done.
- Be concise. When the task is complete, reply with a short summary, no tool call.{notes}"""


def init_workspace(argv):
    # `familiar [dir]` — work on that repo; default is wherever you launched from
    global SYSTEM_PROMPT
    if len(argv) > 1 and not argv[1].startswith("-"):
        os.chdir(argv[1])
    SYSTEM_PROMPT = build_system_prompt()

PLAN_SUFFIX = ("\n\n[PLAN MODE] You are read-only. The write/edit tools are disabled — do not try "
               "to change files. Explore with read_file and bash (read-only git only), then deliver "
               "a concrete implementation plan: which files change, what changes in each, and why.")

TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file. Returns up to 400 lines from offset.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "1-based start line, default 1"},
        }, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a file with the given content.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        }, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace text in a file. Default: give 'old' (an exact, unique substring) "
                       "and 'new'. If an exact match is hard (whitespace/ambiguity), instead give "
                       "'start_line' and 'end_line' (1-based, inclusive) to replace that line range "
                       "with 'new' (empty 'new' deletes the lines).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "old": {"type": "string", "description": "exact unique substring to replace (omit if using line range)"},
            "new": {"type": "string"},
            "start_line": {"type": "integer", "description": "1-based first line to replace (line-range mode)"},
            "end_line": {"type": "integer", "description": "1-based last line to replace, inclusive"},
        }, "required": ["path", "new"]}}},
    {"type": "function", "function": {
        "name": "multi_edit",
        "description": "Apply several exact-match edits to one file in a single call, in order. "
                       "Atomic: if any 'old' is missing or non-unique at its turn, nothing is written. "
                       "Use for multi-spot changes to avoid repeated round-trips.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "edits": {"type": "array", "items": {"type": "object", "properties": {
                "old": {"type": "string"}, "new": {"type": "string"},
            }, "required": ["old", "new"]}},
        }, "required": ["path", "edits"]}}},
    {"type": "function", "function": {
        "name": "bash",
        "description": f"Run a shell command in the working directory. {BASH_TIMEOUT}s timeout.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
        }, "required": ["command"]}}},
]


# re-read guard — stub an unchanged re-read so a weak model can't spin on it;
# cleared on trim and /new so an evicted read can be fetched again.
_READ_SEEN = {}  # realpath -> {"sig": (mtime_ns, size), "offsets": set()}


def tool_read_file(path, offset=1, **_):
    if os.path.isdir(path):  # a weak model often reads a dir by mistake — list it, don't error
        entries = sorted(os.listdir(path))
        listing = "\n".join(e + ("/" if os.path.isdir(os.path.join(path, e)) else "") for e in entries)
        return f"[{path} is a directory, {len(entries)} entries]\n{listing}" if entries else f"[{path} is an empty directory]"
    off = int(offset)
    rp = os.path.realpath(path)
    try:
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    seen = _READ_SEEN.get(rp)
    if seen and seen["sig"] == sig and off in seen["offsets"]:
        return (f"[already read {path} (from line {off}) earlier in this conversation and it "
                "hasn't changed since — reuse that read instead of reading it again]")
    with open(path, errors="replace") as f:
        lines = f.readlines()
    start = max(off - 1, 0)
    chunk = lines[start:start + 400]
    body = "".join(f"{start + i + 1}\t{l}" for i, l in enumerate(chunk))
    if start + 400 < len(lines):
        body += f"\n[truncated: file has {len(lines)} lines, use offset to read more]"
    if seen and seen["sig"] == sig:
        seen["offsets"].add(off)
    else:
        _READ_SEEN[rp] = {"sig": sig, "offsets": {off}}
    return body or "[empty file]"


def tool_write_file(path, content, **_):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return f"wrote {len(content)} chars to {path}"


def tool_edit_file(path, old=None, new="", start_line=None, end_line=None, **_):
    with open(path, errors="replace") as f:
        text = f.read()
    if start_line is not None:  # line-range replace — a fallback when exact match is awkward
        lines = text.splitlines(keepends=True)
        s = int(start_line) - 1
        e = int(end_line) if end_line is not None else int(start_line)
        if s < 0 or s >= len(lines):
            return f"ERROR: start_line {start_line} out of range (file has {len(lines)} lines)"
        repl = [] if new == "" else [new if new.endswith("\n") else new + "\n"]
        lines[s:e] = repl
        with open(path, "w") as f:
            f.write("".join(lines))
        return f"replaced lines {start_line}-{e} in {path}"
    if not old:
        return "ERROR: provide 'old' (exact match) or 'start_line'/'end_line'"
    n = text.count(old)
    if n == 0:
        return "ERROR: old string not found in file"
    if n > 1:
        return f"ERROR: old string appears {n} times, add context to make it unique"
    with open(path, "w") as f:
        f.write(text.replace(old, new, 1))
    return f"edited {path}"


def tool_multi_edit(path, edits, **_):
    # atomic: validate+apply sequentially in memory, write only if all succeed
    with open(path, errors="replace") as f:
        text = f.read()
    for i, e in enumerate(edits, 1):
        old = e["old"]
        n = text.count(old)
        if n == 0:
            return f"ERROR: edit {i}: old string not found (nothing written)"
        if n > 1:
            return f"ERROR: edit {i}: old string appears {n} times, add context (nothing written)"
        text = text.replace(old, e["new"], 1)
    with open(path, "w") as f:
        f.write(text)
    return f"applied {len(edits)} edits to {path}"


GIT_RO = {"status", "log", "diff", "show", "blame", "grep", "ls-files", "ls-remote",
          "rev-parse", "merge-base", "reflog", "describe", "shortlog", "cat-file",
          "for-each-ref", "show-ref", "branch"}


def git_guard(command):
    # read-only git allowlist; non-git writes stay open (trusted single user)
    for m in re.finditer(r"\bgit\b((?:\s+(?:-C\s+\S+|--?[\w=./-]+))*)\s+([\w-]+)", command):
        sub = m.group(2)
        args = re.split(r"[;&|]", command[m.end():])[0]  # this invocation's args only
        mutating_branch = sub == "branch" and re.search(
            r"(^|\s)-[dDmMcCf]\b|--(delete|move|copy|force|set-upstream|unset-upstream|edit-description)", args)
        if sub not in GIT_RO or mutating_branch:
            return (f"ERROR: 'git {sub}' is blocked — Familiar may only run read-only "
                    "git commands (status, log, diff, show, branch, blame, ...)")
    return None


# route a bare `cat file` through read_file so the re-read guard dedupes it — a weak
# model reaches for cat and re-dumps the same file into context. Pipes/redirects/flags/globs/vars
# fall through to real bash; cat is legitimate inside a pipeline.
_BARE_CAT = re.compile(r"^\s*cat\s+(?P<args>[^|&;<>`$()]+?)\s*$")


def bare_cat_paths(command):
    m = _BARE_CAT.match(command)
    if not m:
        return None
    parts = m.group("args").split()
    if not parts or any(p.startswith("-") for p in parts):
        return None
    return parts


def tool_bash(command, **_):
    # git is read-only (guard above); everything else unsandboxed by design
    paths = bare_cat_paths(command)
    if paths and all(os.path.isfile(p) for p in paths):
        return "\n".join(tool_read_file(p) for p in paths)
    blocked = git_guard(command)
    if blocked:
        return blocked
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=BASH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {BASH_TIMEOUT}s (raise FAMILIAR_BASH_TIMEOUT)"
    out = (r.stdout + r.stderr).strip()
    return f"exit {r.returncode}\n{out}" if out else f"exit {r.returncode} (no output)"


TOOL_IMPL = {"read_file": tool_read_file, "write_file": tool_write_file,
             "edit_file": tool_edit_file, "multi_edit": tool_multi_edit, "bash": tool_bash}


def run_tool(name, args):
    if PLAN and name in MUTATORS:
        return ("ERROR: plan mode is read-only — the edit tools are off. Propose the change in "
                "your plan instead of making it (/plan to exit plan mode).")
    try:
        result = TOOL_IMPL[name](**args)
    except Exception as e:  # bad path, bad args — feed the error back to the model
        result = f"ERROR: {type(e).__name__}: {e}"
    if len(result) > MAX_TOOL_OUTPUT:
        result = result[:MAX_TOOL_OUTPUT] + "\n[output truncated]"
    return result


def chat(messages):
    """Stream one completion. Prints content live, returns the assistant message."""
    global LAST_USAGE
    payload = {"model": MODEL, "messages": messages, "tools": active_tools(), "stream": True,
               "stream_options": {"include_usage": True}}  # ask for real token counts
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=600)

    content, reasoning, tool_calls = "", False, {}
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            obj = json.loads(line[6:])
        except json.JSONDecodeError:
            continue  # skip a garbled frame rather than crash the REPL
        if obj.get("usage"):
            LAST_USAGE = obj["usage"]
        if obj.get("error"):
            sys.stdout.write(f"\n[server error: {obj['error']}]\n")
            break
        choices = obj.get("choices") or []
        if not choices:  # usage-only / keep-alive frame has no choices
            continue
        delta = choices[0].get("delta", {})
        r = delta.get("reasoning_content")
        if r:  # think-blocks: show dim so slow local gen isn't a silent wait
            if not reasoning:
                sys.stdout.write(DIM)
                reasoning = True
            sys.stdout.write(r)
            sys.stdout.flush()
        c = delta.get("content")
        if c:
            if reasoning:
                sys.stdout.write(RESET + "\n")
                reasoning = False
            content += c
            sys.stdout.write(c)
            sys.stdout.flush()
        for tc in delta.get("tool_calls") or []:
            slot = tool_calls.setdefault(tc["index"], {"id": "", "name": "", "arguments": ""})
            slot["id"] = tc.get("id") or slot["id"]
            fn = tc.get("function", {})
            slot["name"] += fn.get("name") or ""
            slot["arguments"] += fn.get("arguments") or ""
    if reasoning:
        sys.stdout.write(RESET)
    if content:
        sys.stdout.write("\n")

    msg = {"role": "assistant", "content": content or None}
    if tool_calls:
        msg["tool_calls"] = [
            {"id": t["id"] or f"call_{i}", "type": "function",
             "function": {"name": t["name"], "arguments": t["arguments"]}}
            for i, t in sorted(tool_calls.items())]
    return msg


def resolve_ctx_budget():
    """Size the conversation budget to the server's real context window (~80%, leaving
    headroom so a long reasoning turn isn't truncated). Explicit FAMILIAR_CTX_TOKENS wins;
    safe fallback if the server can't be probed. Returns (budget_tokens, server_ctx|None)."""
    override = os.environ.get("FAMILIAR_CTX_TOKENS")
    if override:
        return int(override), None
    try:
        with urllib.request.urlopen(BASE_URL.rsplit("/v1", 1)[0] + "/props", timeout=2) as r:
            props = json.load(r)
        n = (props.get("default_generation_settings") or {}).get("n_ctx") or props.get("n_ctx")
        if n:
            return int(int(n) * 0.8), int(n)
    except Exception:  # server down or /props unavailable — fall back
        pass
    return 55000, None


def context_chars(messages):
    return sum(len(json.dumps(m)) for m in messages)


def est_tokens(messages):
    return context_chars(messages) // 3  # ~3 chars/token, matches CTX_CHARS


def trim(messages):
    # crude char-count trim, summarize-on-trim if quality suffers. Keeps [0] system
    # and [1] the task; drops tool results with their tool_calls message (orphans 400 the server).
    dropped = 0
    while context_chars(messages) > CTX_CHARS and len(messages) > 4:
        del messages[2]
        dropped += 1
        while len(messages) > 2 and messages[2]["role"] == "tool":
            del messages[2]
            dropped += 1
    if dropped:
        _READ_SEEN.clear()  # a dropped read is no longer in context — allow re-reading it
        print(f"{DIM}⋯ context full — dropped {dropped} old message(s) to stay under "
              f"{CTX_CHARS // 3 // 1000}k tokens{RESET}")
    return dropped


MUTATORS = {"write_file", "edit_file", "multi_edit"}


def active_tools():
    return [t for t in TOOLS if t["function"]["name"] not in MUTATORS] if PLAN else TOOLS


def agent_turn(messages, user_input):
    messages.append({"role": "user", "content": user_input})
    # loop-breakers for a weak model — `edited`/`verified` gate the verify nudge
    # (once), `prev_key` skips a tool call identical to the last one.
    edited = verified = nudged = False
    prev_key = None
    for _ in range(MAX_STEPS):
        trim(messages)
        msg = chat(messages)
        messages.append(msg)
        if not msg.get("tool_calls"):
            if edited and not verified and not nudged:  # don't stop on unverified edits
                nudged = True
                messages.append({"role": "user", "content":
                    "You changed files but ran nothing to check them. Run the build or a test "
                    "(bash), or state why no check applies — then give your final summary."})
                continue
            return
        for tc in msg["tool_calls"]:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError as e:
                result = f"ERROR: malformed tool arguments: {e}"
            else:
                key = (name, tc["function"]["arguments"] or "")
                if key == prev_key:  # identical back-to-back call — a loop, don't rerun it
                    result = ("[skipped: identical to your previous call. Repeating it changes "
                              "nothing — make a different change, verify, or stop and summarize.]")
                else:
                    preview = args.get("command") or args.get("path") or ""
                    print(f"{DIM}→ {name} {preview}{RESET}")
                    result = run_tool(name, args)
                    if name in MUTATORS and not result.startswith("ERROR"):
                        edited, verified = True, False
                    elif name == "bash" and edited:
                        verified = True
                prev_key = key
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
    messages.append({"role": "user", "content":
                     "Step limit reached. Summarize progress and stop."})
    messages.append(chat(messages))


# ── input editor (raw-mode multiline; see README) ───────────────────────────────
# full redraw per keystroke — O(buffer)/key, fine for prompt-sized text.
_ARROWS = {"A": ("up",), "B": ("down",), "C": ("right",), "D": ("left",),
           "H": ("home",), "F": ("end",)}


def _layout(text, cursor_pos, width):
    rows, line, col = [], "", 0
    cur_row = cur_col = 0
    placed = False
    for i, ch in enumerate(text):
        if i == cursor_pos:
            cur_row, cur_col, placed = len(rows), col, True
        if ch == "\n":
            rows.append(line)
            line, col = "", 0
        else:
            line += ch
            col += 1
            if col == width:
                rows.append(line)
                line, col = "", 0
    rows.append(line)
    if not placed:
        cur_row, cur_col = len(rows) - 1, col
    return rows, cur_row, cur_col


def _render(prompt, buf, cur, prev_rows):
    width = max(shutil.get_terminal_size(fallback=(80, 24)).columns, 1)
    rows, cur_row, cur_col = _layout(prompt + buf, len(prompt) + cur, width)
    out = [f"\x1b[{prev_rows}A" if prev_rows else "", "\r\x1b[J", "\r\n".join(rows)]
    up = (len(rows) - 1) - cur_row
    if up > 0:
        out.append(f"\x1b[{up}A")
    out.append("\r")
    if cur_col > 0:
        out.append(f"\x1b[{cur_col}C")
    sys.stdout.write("".join(out))
    sys.stdout.flush()
    return cur_row


def _read_paste(fd):
    end = b"\x1b[201~"
    buf = b""
    while not buf.endswith(end):
        d = os.read(fd, 1)
        if not d:
            break
        buf += d
    body = buf[:-len(end)] if buf.endswith(end) else buf
    return body.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


def _interpret_escape(fd, seq):
    if seq is None:
        return ("ignore",)
    kind = seq[0]
    if kind == "meta-enter":
        return ("newline",)
    if kind == "ss3":
        return _ARROWS.get(seq[1], ("ignore",))
    if kind == "csi":
        s = seq[1]
        final, body = s[-1], s[:-1]
        if final == "~":
            num = body.split(";")[0]
            if num == "200":
                return ("paste", _read_paste(fd))
            if num == "3":
                return ("delete",)
            return ("ignore",)
        if final == "u":                       # kitty keyboard protocol
            parts = body.split(";")
            if parts[0] == "13":               # Enter keycode
                mod = parts[1] if len(parts) > 1 else "1"
                return ("enter",) if mod in ("", "1") else ("newline",)
            return ("ignore",)
        return _ARROWS.get(final, ("ignore",))
    return ("ignore",)


def _read_escape(fd):
    if not select.select([fd], [], [], 0.02)[0]:
        return None  # lone ESC
    c = os.read(fd, 1)
    if c == b"[":
        params = b""
        while True:
            d = os.read(fd, 1)
            params += d
            if not d or 0x40 <= d[0] <= 0x7e:
                break
        return ("csi", params.decode("latin1"))
    if c == b"O":
        return ("ss3", os.read(fd, 1).decode("latin1"))
    if c in (b"\r", b"\n"):
        return ("meta-enter",)
    return ("meta",)


def _read_key(fd):
    b = os.read(fd, 1)
    if not b:
        return ("eof-hard",)
    c = b[0]
    if c == 0x1b:
        return _interpret_escape(fd, _read_escape(fd))
    if c in (0x0d, 0x0a):
        return ("enter",)
    if c in (0x7f, 0x08):
        return ("backspace",)
    if c == 0x03:
        return ("interrupt",)   # Ctrl-C
    if c == 0x04:
        return ("eof",)         # Ctrl-D
    if c == 0x15:
        return ("kill",)        # Ctrl-U
    if c == 0x01:
        return ("home",)        # Ctrl-A
    if c == 0x05:
        return ("end",)         # Ctrl-E
    if c == 0x0c:
        return ("clear",)       # Ctrl-L
    if c == 0x17:
        return ("word-back",)   # Ctrl-W
    if c < 0x20:
        return ("ignore",)
    if c >= 0x80:               # UTF-8 multibyte
        n = 3 if c >= 0xf0 else 2 if c >= 0xe0 else 1
        try:
            return ("char", (bytes([c]) + os.read(fd, n)).decode("utf-8"))
        except UnicodeDecodeError:
            return ("ignore",)
    return ("char", chr(c))


def _apply_key(buf, cur, key, hist):
    t = key[0]
    if t == "enter":
        return buf, cur, "submit"
    if t == "newline":
        return buf[:cur] + "\n" + buf[cur:], cur + 1, "continue"
    if t in ("char", "paste"):
        s = key[1]
        return buf[:cur] + s + buf[cur:], cur + len(s), "continue"
    if t == "backspace":
        return (buf[:cur - 1] + buf[cur:], cur - 1, "continue") if cur else (buf, cur, "continue")
    if t == "delete":
        return (buf[:cur] + buf[cur + 1:], cur, "continue") if cur < len(buf) else (buf, cur, "continue")
    if t == "left":
        return buf, max(0, cur - 1), "continue"
    if t == "right":
        return buf, min(len(buf), cur + 1), "continue"
    if t == "home":
        return buf, 0, "continue"
    if t == "end":
        return buf, len(buf), "continue"
    if t == "kill":
        return "", 0, "continue"
    if t == "word-back":
        j = cur
        while j > 0 and buf[j - 1].isspace():
            j -= 1
        while j > 0 and not buf[j - 1].isspace():
            j -= 1
        return buf[:j] + buf[cur:], j, "continue"
    if t == "clear":
        return buf, cur, "clear-screen"
    if t == "up":
        items = hist["items"]
        if items and hist["idx"] > 0:
            if hist["idx"] == len(items):
                hist["stash"] = buf
            hist["idx"] -= 1
            return items[hist["idx"]], len(items[hist["idx"]]), "continue"
        return buf, cur, "continue"
    if t == "down":
        items = hist["items"]
        if hist["idx"] < len(items):
            hist["idx"] += 1
            nb = items[hist["idx"]] if hist["idx"] < len(items) else hist["stash"]
            return nb, len(nb), "continue"
        return buf, cur, "continue"
    if t == "interrupt":
        return buf, cur, "interrupt"
    if t in ("eof", "eof-hard"):
        return buf, cur, ("eof" if buf == "" else "continue")
    return buf, cur, "continue"


def _edit(prompt, history):
    fd = sys.stdin.fileno()
    buf, cur = "", 0
    hist = {"items": history, "idx": len(history), "stash": ""}
    prev = _render(prompt, buf, cur, 0)
    while True:
        buf, cur, action = _apply_key(buf, cur, _read_key(fd), hist)
        if action == "submit":
            _render(prompt, buf, len(buf), prev)
            sys.stdout.write("\r\n")
            sys.stdout.flush()
            return buf
        if action == "interrupt":                  # Ctrl-C abandons the line
            sys.stdout.write("^C\r\n")
            sys.stdout.flush()
            buf, cur, prev = "", 0, _render(prompt, "", 0, 0)
            continue
        if action == "eof":                        # Ctrl-D on empty line quits
            sys.stdout.write("\r\n")
            sys.stdout.flush()
            raise EOFError
        if action == "clear-screen":
            sys.stdout.write("\x1b[2J\x1b[H")
            prev = _render(prompt, buf, cur, 0)
            continue
        prev = _render(prompt, buf, cur, prev)


def load_history():
    # JSON-per-line so multi-line prompts round-trip; keep last 1000 in memory
    try:
        with open(HISTFILE, errors="replace") as f:
            out = []
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
            return out[-1000:]
    except FileNotFoundError:
        return []


def save_history(line):
    if line.startswith("/"):  # don't clutter recall with slash commands
        return
    try:
        with open(HISTFILE, "a") as f:
            f.write(json.dumps(line) + "\n")
    except OSError:
        pass


def read_input(prompt, history):
    if not (_RAW_OK and sys.stdin.isatty()):
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        return line.rstrip("\n")
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sys.stdout.write("\x1b[?2004h\x1b[>1u")  # bracketed paste + kitty keyboard
        sys.stdout.flush()
        return _edit(prompt, history)
    finally:
        sys.stdout.write("\x1b[<u\x1b[?2004l")   # pop kitty flags, disable paste mode
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def format_meter(messages):
    budget = CTX_CHARS // 3
    if LAST_USAGE:  # ground truth from the server's last turn beats the char estimate
        used = LAST_USAGE.get("prompt_tokens", 0) + LAST_USAGE.get("completion_tokens", 0)
        mark = ""
    else:
        used, mark = est_tokens(messages), "~"
    pct = used / budget if budget else 0
    note = (" · dropping oldest turns to fit" if pct >= 0.9
            else " · nearing limit" if pct >= 0.75 else "")
    return f"ctx {mark}{used / 1000:.1f}k / {budget // 1000}k tokens ({pct:.0%}){note}"


def cmd_tokens(messages):
    budget = CTX_CHARS // 3
    used = est_tokens(messages)
    pct = used / budget if budget else 0
    by_role = {}
    for m in messages:
        by_role[m["role"]] = by_role.get(m["role"], 0) + len(json.dumps(m)) // 3
    print(f"{DIM}budget {budget // 1000}k tokens (FAMILIAR_CTX_TOKENS); server hard cap "
          f"set by -c in serve.sh{RESET}")
    print(f"  estimate  {used:>6} tokens (~{pct:.0%} of budget) across {len(messages)} messages")
    if LAST_USAGE:
        print(f"  measured  {LAST_USAGE.get('prompt_tokens', 0):>6} prompt + "
              f"{LAST_USAGE.get('completion_tokens', 0)} completion (server, last turn)")
    print(f"  free      {max(budget - used, 0):>6} tokens before oldest turns start dropping")
    for role, n in by_role.items():
        print(f"    {role:<10} {n:>6}")


HELP = f"""{BOLD}commands{RESET}
  /new      clear the conversation (keeps the system prompt)
  /plan     toggle read-only plan mode (explore & propose, no edits)
  /tokens   detailed context-usage breakdown
  /help     this help
  /quit     exit (also ctrl-d on an empty line)
{BOLD}keys{RESET}
  Enter                     submit
  Shift+Enter / Alt+Enter   newline (multi-line prompt)
  paste                     multi-line paste stays intact — no early submit
  ↑ / ↓                     recall previous prompts
  Ctrl+U clear line · Ctrl+W delete word · Ctrl+C abandon line"""


def repl():
    global LAST_USAGE, CTX_CHARS, PLAN
    budget, server_ctx = resolve_ctx_budget()
    CTX_CHARS = budget * 3
    ctx_note = (f"ctx {budget // 1000}k / {server_ctx // 1000}k window" if server_ctx
                else f"ctx {budget // 1000}k")
    print(f"{BOLD}Familiar{RESET} — local coding agent")
    print(f"{DIM}server {BASE_URL} · cwd {os.getcwd()} · {ctx_note} · /help · ctrl-d quits{RESET}")
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    history = load_history()
    while True:
        try:
            print(f"\n{DIM}{format_meter(messages)}{RESET}")
            user_input = read_input("plan › " if PLAN else "› ", history).strip()
        except EOFError:
            print()
            return
        if not user_input:
            continue
        history.append(user_input)
        save_history(user_input)
        if user_input in ("/quit", "/exit"):
            return
        if user_input == "/new":
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            LAST_USAGE = None  # else the meter keeps showing the pre-clear token count
            _READ_SEEN.clear()
            print(f"{DIM}context cleared{RESET}")
            continue
        if user_input == "/plan":
            PLAN = not PLAN
            messages[0]["content"] = SYSTEM_PROMPT + (PLAN_SUFFIX if PLAN else "")
            print(f"{DIM}plan mode {'on — read-only, edits disabled' if PLAN else 'off'}{RESET}")
            continue
        if user_input == "/tokens":
            cmd_tokens(messages)
            continue
        if user_input == "/help":
            print(HELP)
            continue
        try:
            agent_turn(messages, user_input)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            print(f"\nserver rejected the request ({e.code}): {body}\n"
                  "likely context overflow — /new resets, or lower FAMILIAR_CTX_TOKENS")
        except urllib.error.URLError as e:
            print(f"\ncannot reach {BASE_URL} ({e.reason}) — start the model: ./serve.sh")
        except KeyboardInterrupt:
            print(f"\n{DIM}interrupted{RESET}")
            messages.append({"role": "user", "content": "[interrupted by user]"})


def selftest():
    """Mock the server, verify the full loop: tool call → execution → final answer."""
    global BASE_URL, CTX_CHARS, LAST_USAGE, HISTFILE, PLAN
    import http.server
    import tempfile
    import threading

    probe = os.path.join(tempfile.mkdtemp(), "probe.txt")

    class Mock(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if any(m["role"] == "tool" for m in body["messages"]):
                chunks = [{"delta": {"content": "done: "}}, {"delta": {"content": "wrote probe"}}]
            else:
                args = json.dumps({"path": probe, "content": "hello from familiar"})
                chunks = [{"delta": {"tool_calls": [{"index": 0, "id": "call_1",
                          "function": {"name": "write_file", "arguments": args}}]}}]
            for c in chunks:
                self.wfile.write(f"data: {json.dumps({'choices': [c]})}\n\n".encode())
            usage = {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 7}}
            self.wfile.write(f"data: {json.dumps(usage)}\n\n".encode())  # usage-only frame
            self.wfile.write(b"data: [DONE]\n\n")

        def do_GET(self):  # /props for resolve_ctx_budget()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"default_generation_settings": {"n_ctx": 10000}}).encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    BASE_URL = f"http://127.0.0.1:{srv.server_port}/v1"

    # context budget resolution: probe (80% of server n_ctx), env override, fallback
    os.environ.pop("FAMILIAR_CTX_TOKENS", None)
    assert resolve_ctx_budget() == (8000, 10000), "budget should be 80% of probed n_ctx"
    os.environ["FAMILIAR_CTX_TOKENS"] = "12345"
    assert resolve_ctx_budget() == (12345, None), "env override should win"
    os.environ.pop("FAMILIAR_CTX_TOKENS", None)
    _saved_url, BASE_URL = BASE_URL, "http://127.0.0.1:1/v1"  # unreachable
    assert resolve_ctx_budget() == (55000, None), "fallback when server unreachable"
    BASE_URL = _saved_url

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    agent_turn(messages, "create the probe file")
    with open(probe) as f:
        assert f.read() == "hello from familiar", "tool execution failed"
    assert messages[-1]["content"] == "done: wrote probe", "final answer missing"
    assert any(m["role"] == "tool" for m in messages), "tool result not in transcript"
    assert any("ran nothing to check" in (m.get("content") or "")
               for m in messages if m["role"] == "user"), "verify-after-edit nudge should fire"

    # consecutive-duplicate breaker: an identical back-to-back call is skipped, ending the loop
    loopf = os.path.join(tempfile.mkdtemp(), "loop.txt")
    tool_write_file(loopf, "x\n")
    _READ_SEEN.clear()

    class LoopMock(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            tools = [m for m in body["messages"] if m["role"] == "tool"]
            if tools and "skipped: identical" in tools[-1]["content"]:
                chunks = [{"delta": {"content": "stopped"}}]  # loop broken → answer
            else:  # keep issuing the same read — a stuck model
                args = json.dumps({"path": loopf})
                chunks = [{"delta": {"tool_calls": [{"index": 0, "id": "call_r",
                          "function": {"name": "read_file", "arguments": args}}]}}]
            for c in chunks:
                self.wfile.write(f"data: {json.dumps({'choices': [c]})}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *a):
            pass

    srv2 = http.server.HTTPServer(("127.0.0.1", 0), LoopMock)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    BASE_URL = f"http://127.0.0.1:{srv2.server_port}/v1"
    m2 = [{"role": "system", "content": "s"}]
    agent_turn(m2, "read it")
    assert m2[-1]["content"] == "stopped", "loop should end once the dup call is skipped"
    assert any("skipped: identical" in (m.get("content") or "") for m in m2), "dup not skipped"
    BASE_URL = _saved_url

    # usage-only frame captured (also exercises the empty-choices guard in chat())
    assert LAST_USAGE and LAST_USAGE["prompt_tokens"] == 100, "server usage not captured"
    assert not format_meter(messages).startswith("ctx ~"), "meter should use measured tokens"

    saved, CTX_CHARS = CTX_CHARS, 50
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "the task"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
            {"role": "tool", "tool_call_id": "t1", "content": "x" * 500},
            {"role": "user", "content": "follow-up"}, {"role": "assistant", "content": "a"}]
    trim(msgs)
    CTX_CHARS = saved
    assert msgs[1]["content"] == "the task", "trim dropped the original task"
    for i, m in enumerate(msgs):
        if m["role"] == "tool":
            assert msgs[i - 1].get("tool_calls"), "trim orphaned a tool result"

    assert "not found" in tool_edit_file(probe, "nope", "x")
    tool_write_file(probe, "aa")
    assert "2 times" in tool_edit_file(probe, "a", "b")

    # line-range edit (fallback mode) and multi_edit (atomic)
    tool_write_file(probe, "one\ntwo\nthree\n")
    tool_edit_file(probe, new="TWO", start_line=2, end_line=2)
    with open(probe) as f:
        assert f.read() == "one\nTWO\nthree\n", "line-range replace"
    tool_edit_file(probe, new="", start_line=1, end_line=1)  # delete line 1
    with open(probe) as f:
        assert f.read() == "TWO\nthree\n", "line-range delete"
    tool_write_file(probe, "aXbYc")
    assert "3 edits" in tool_multi_edit(probe, [{"old": "X", "new": "1"}, {"old": "Y", "new": "2"}, {"old": "c", "new": "3"}])
    with open(probe) as f:
        assert f.read() == "a1b2" "3", "multi_edit applied in order"
    tool_write_file(probe, "aXbXc")  # ambiguous 2nd edit → atomic abort, no write
    assert "nothing written" in tool_multi_edit(probe, [{"old": "a", "new": "Z"}, {"old": "X", "new": "1"}])
    with open(probe) as f:
        assert f.read() == "aXbXc", "multi_edit atomic: no partial write"

    assert tool_bash("exit 3").startswith("exit 3")
    assert "blocked" in tool_bash("git commit -m hi")
    assert "blocked" in tool_bash("cd /tmp && git merge feat/x")
    assert "blocked" in tool_bash("git checkout main")
    assert "blocked" in tool_bash("git branch -D old")
    assert "blocked" in tool_bash("git -C /tmp push origin main")
    assert "blocked" not in tool_bash("git log -1")
    assert "blocked" not in tool_bash("git -C /tmp status && git branch --show-current")
    assert "truncated" not in tool_read_file(probe)

    # plan mode: mutators vanish from the tool list and are hard-blocked at execution
    PLAN = True
    assert all(t["function"]["name"] not in MUTATORS for t in active_tools()), "plan hides mutators"
    assert "read-only" in run_tool("write_file", {"path": probe, "content": "x"}), "plan blocks writes"
    assert "blocked" not in run_tool("bash", {"command": "echo ok"}), "plan keeps bash for exploring"
    PLAN = False
    assert any(t["function"]["name"] == "write_file" for t in active_tools()), "normal mode restores mutators"

    # reading a directory lists it instead of erroring (common weak-model mistake)
    d = os.path.dirname(probe)
    out = tool_read_file(d)
    assert "is a directory" in out and os.path.basename(probe) in out, "dir read should list entries"

    # re-read guard: unchanged re-read is deduped; a real on-disk change re-enables the read
    assert "already read" in tool_read_file(probe), "unchanged re-read should be deduped"
    tool_write_file(probe, "changed on disk\n")
    assert "already read" not in tool_read_file(probe), "read after change should return content"

    # bare `cat file` routes through read_file (line numbers + dedup); pipes/flags/globs don't
    tool_write_file(probe, "L1\nL2\n")
    _READ_SEEN.clear()
    assert "1\tL1" in tool_bash(f"cat {probe}"), "bare cat should return read_file output"
    assert "already read" in tool_bash(f"cat {probe}"), "second cat should dedupe like read_file"
    assert bare_cat_paths("cat a.txt | grep x") is None, "cat in a pipe stays raw bash"
    assert bare_cat_paths("cat -n a.txt") is None, "cat with flags stays raw bash"
    assert tool_bash("cat /no/such/file/xyz").startswith("exit"), "missing file falls through to bash"

    # input editor: layout, key handling, escape parsing (no TTY needed)
    rows, cr, cc = _layout("> abc", 5, 80)
    assert rows == ["> abc"] and (cr, cc) == (0, 5), "layout basic"
    rows, cr, cc = _layout("abc", 3, 2)
    assert rows == ["ab", "c"] and (cr, cc) == (1, 1), "layout wrap"
    assert _interpret_escape(None, ("meta-enter",)) == ("newline",), "alt+enter"
    assert _interpret_escape(None, ("csi", "13;2u")) == ("newline",), "shift+enter"
    assert _interpret_escape(None, ("csi", "13u")) == ("enter",), "kitty plain enter"
    assert _interpret_escape(None, ("csi", "C")) == ("right",)
    assert _interpret_escape(None, ("csi", "3~")) == ("delete",)
    assert _interpret_escape(None, ("ss3", "A")) == ("up",)
    rfd, wfd = os.pipe()
    os.write(wfd, b"pasted\r\ntext\x1b[201~")
    assert _interpret_escape(rfd, ("csi", "200~")) == ("paste", "pasted\ntext"), "bracketed paste"
    os.close(rfd)
    os.close(wfd)
    h = {"items": ["one", "two"], "idx": 2, "stash": ""}
    b, c, a = _apply_key("", 0, ("up",), h)
    assert b == "two" and a == "continue", "history recall"
    b, c, a = _apply_key(b, c, ("up",), h)
    assert b == "one"
    b, c, a = _apply_key(b, c, ("char", "X"), h)
    assert (b, c) == ("oneX", 4), "insert char"
    b, c, a = _apply_key(b, c, ("newline",), h)
    assert (b, c) == ("oneX\n", 5), "insert newline"
    b, c, a = _apply_key("hello", 5, ("backspace",), h)
    assert (b, c) == ("hell", 4), "backspace"
    assert _apply_key("hi", 2, ("enter",), h)[2] == "submit", "enter submits"
    assert _apply_key("", 0, ("eof",), h)[2] == "eof", "ctrl-d on empty"
    assert _apply_key("x", 1, ("eof",), h)[2] == "continue", "ctrl-d ignored with text"

    assert est_tokens([{"role": "user", "content": "x" * 300}]) > 90, "token estimate"
    LAST_USAGE = {"prompt_tokens": 12000, "completion_tokens": 345}
    assert "12.3k" in format_meter([]), "meter should render measured tokens"
    LAST_USAGE = None
    assert format_meter([{"role": "user", "content": "x" * 300}]).startswith("ctx ~"), \
        "meter should mark char estimate with ~"

    # persistent prompt history: JSON-per-line round-trips multi-line entries
    HISTFILE = os.path.join(tempfile.mkdtemp(), "hist")
    save_history("first prompt")
    save_history("two\nlines")
    save_history("/skip me")  # slash commands aren't persisted
    assert load_history() == ["first prompt", "two\nlines"], "history round-trip"
    print("selftest OK")


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if "--version" in argv:
        print(f"familiar {__version__}")
        return
    init_workspace(argv)
    if "--selftest" in argv:
        selftest()
    else:
        repl()


if __name__ == "__main__":
    main()
