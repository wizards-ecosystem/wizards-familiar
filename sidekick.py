#!/usr/bin/env python3
# Sidekick — minimal local coding agent. One trusted user, one local model server.
# Talks to any OpenAI-compatible /chat/completions endpoint (llama-server, LM Studio).
# stdlib only. Run: ./sidekick.py   Self-check: ./sidekick.py --selftest
import contextlib
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

BASE_URL = os.environ.get("SIDEKICK_URL", "http://localhost:8321/v1")
MODEL = os.environ.get("SIDEKICK_MODEL", "local")
CTX_CHARS = int(os.environ.get("SIDEKICK_CTX_TOKENS", "28000")) * 3  # ~3 chars/token, code-heavy
MAX_TOOL_OUTPUT = 8000
MAX_STEPS = 40
HISTFILE = os.path.expanduser(os.environ.get("SIDEKICK_HISTFILE", "~/.sidekick_history"))

LAST_USAGE = None  # real token counts from the server's last stream, if it reports them

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"

# `sidekick [dir]` — work on that repo; default is wherever you launched from
if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
    os.chdir(sys.argv[1])

PROJECT_NOTES = ""
if os.path.exists("SIDEKICK.md"):
    with open("SIDEKICK.md", errors="replace") as f:
        PROJECT_NOTES = "\n\nProject notes (from SIDEKICK.md):\n" + f.read()

SYSTEM_PROMPT = f"""You are Sidekick, a coding agent running fully locally on the user's Mac.
Working directory: {os.getcwd()}
Platform: macOS (zsh available via the bash tool).

Use the tools to inspect and change code. Rules:
- Read a file before editing it. Keep edits minimal and targeted.
- Prefer edit_file for small changes; write_file only for new files or full rewrites.
- Use bash for everything else: ls, grep, find, git, running code and tests.
- Verify your work (run the code or a quick check) before declaring done.
- Be concise. When the task is complete, reply with a short summary, no tool call.{PROJECT_NOTES}"""

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
        "description": "Replace an exact, unique substring in a file. Fails if absent or ambiguous.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
        }, "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {
        "name": "bash",
        "description": "Run a shell command in the working directory. 120s timeout.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
        }, "required": ["command"]}}},
]


def tool_read_file(path, offset=1, **_):
    with open(path, errors="replace") as f:
        lines = f.readlines()
    start = max(int(offset) - 1, 0)
    chunk = lines[start:start + 400]
    body = "".join(f"{start + i + 1}\t{l}" for i, l in enumerate(chunk))
    if start + 400 < len(lines):
        body += f"\n[truncated: file has {len(lines)} lines, use offset to read more]"
    return body or "[empty file]"


def tool_write_file(path, content, **_):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return f"wrote {len(content)} chars to {path}"


def tool_edit_file(path, old, new, **_):
    with open(path, errors="replace") as f:
        text = f.read()
    n = text.count(old)
    if n == 0:
        return "ERROR: old string not found in file"
    if n > 1:
        return f"ERROR: old string appears {n} times, add context to make it unique"
    with open(path, "w") as f:
        f.write(text.replace(old, new, 1))
    return f"edited {path}"


GIT_RO = {"status", "log", "diff", "show", "blame", "grep", "ls-files", "ls-remote",
          "rev-parse", "merge-base", "reflog", "describe", "shortlog", "cat-file",
          "for-each-ref", "show-ref", "branch"}


def git_guard(command):
    # ponytail: read-only git allowlist; non-git writes stay open (trusted single user)
    for m in re.finditer(r"\bgit\b((?:\s+(?:-C\s+\S+|--?[\w=./-]+))*)\s+([\w-]+)", command):
        sub = m.group(2)
        args = re.split(r"[;&|]", command[m.end():])[0]  # this invocation's args only
        mutating_branch = sub == "branch" and re.search(
            r"(^|\s)-[dDmMcCf]\b|--(delete|move|copy|force|set-upstream|unset-upstream|edit-description)", args)
        if sub not in GIT_RO or mutating_branch:
            return (f"ERROR: 'git {sub}' is blocked — Sidekick may only run read-only "
                    "git commands (status, log, diff, show, branch, blame, ...)")
    return None


def tool_bash(command, **_):
    # ponytail: git is read-only (guard above); everything else unsandboxed by design
    blocked = git_guard(command)
    if blocked:
        return blocked
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out after 120s"
    out = (r.stdout + r.stderr).strip()
    return f"exit {r.returncode}\n{out}" if out else f"exit {r.returncode} (no output)"


TOOL_IMPL = {"read_file": tool_read_file, "write_file": tool_write_file,
             "edit_file": tool_edit_file, "bash": tool_bash}


def run_tool(name, args):
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
    payload = {"model": MODEL, "messages": messages, "tools": TOOLS, "stream": True,
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


def context_chars(messages):
    return sum(len(json.dumps(m)) for m in messages)


def est_tokens(messages):
    return context_chars(messages) // 3  # ~3 chars/token, matches CTX_CHARS


def trim(messages):
    # ponytail: crude char-count trim; summarize-on-trim if quality suffers.
    # Keeps [0] system and [1] the original task (dropping it makes the model drift),
    # and drops tool results with their tool_calls message (orphans are a server 400).
    dropped = 0
    while context_chars(messages) > CTX_CHARS and len(messages) > 4:
        del messages[2]
        dropped += 1
        while len(messages) > 2 and messages[2]["role"] == "tool":
            del messages[2]
            dropped += 1
    if dropped:
        print(f"{DIM}⋯ context full — dropped {dropped} old message(s) to stay under "
              f"{CTX_CHARS // 3 // 1000}k tokens{RESET}")
    return dropped


def agent_turn(messages, user_input):
    messages.append({"role": "user", "content": user_input})
    for _ in range(MAX_STEPS):
        trim(messages)
        msg = chat(messages)
        messages.append(msg)
        if not msg.get("tool_calls"):
            return
        for tc in msg["tool_calls"]:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError as e:
                args, result = {}, f"ERROR: malformed tool arguments: {e}"
            else:
                preview = args.get("command") or args.get("path") or ""
                print(f"{DIM}→ {name} {preview}{RESET}")
                result = run_tool(name, args)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
    messages.append({"role": "user", "content":
                     "Step limit reached. Summarize progress and stop."})
    messages.append(chat(messages))


def run_task(task):
    """Headless one-shot: run the agent loop once on a fresh context, return the
    final answer text. Used when a task is piped in (`sidekick <dir> < task.txt`) for
    scripting or non-interactive runs.
    ponytail: the deliberate non-interactive entry point.
    The streaming loop trace (reasoning, tool previews) is sent to stderr so stdout
    stays clean; the caller reads only the returned final answer, not the transcript.
    Context is fresh per call, so each run is stateless — no cross-task drift.
    trim() already protects messages[0] (system) and messages[1] (the task)."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    with contextlib.redirect_stdout(sys.stderr):
        agent_turn(messages, task)
    return (messages[-1].get("content") or "").strip()


# ── input editor ──────────────────────────────────────────────────────────────
# Raw-mode multiline editor: Enter submits, Shift+Enter / Alt+Enter insert a newline,
# and bracketed paste keeps multi-line pastes intact instead of submitting on the first
# newline. Falls back to plain input() when stdin isn't a TTY (pipes, --selftest).
# ponytail: full redraw per keystroke — O(buffer) per key; fine for prompt-sized text,
# revisit only if pasting huge blobs gets janky.
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
    # ponytail: JSON-per-line so multi-line prompts round-trip; keep last 1000 in memory
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
    print(f"{DIM}budget {budget // 1000}k tokens (SIDEKICK_CTX_TOKENS); server hard cap "
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
    global LAST_USAGE
    print(f"{BOLD}Sidekick{RESET} — local coding agent")
    print(f"{DIM}server {BASE_URL} · cwd {os.getcwd()} · /help for keys · ctrl-d quits{RESET}")
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    history = load_history()
    while True:
        try:
            print(f"\n{DIM}{format_meter(messages)}{RESET}")
            user_input = read_input("› ", history).strip()
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
            print(f"{DIM}context cleared{RESET}")
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
                  "likely context overflow — /new resets, or lower SIDEKICK_CTX_TOKENS")
        except urllib.error.URLError as e:
            print(f"\ncannot reach {BASE_URL} ({e.reason}) — start the model: ./serve.sh")
        except KeyboardInterrupt:
            print(f"\n{DIM}interrupted{RESET}")
            messages.append({"role": "user", "content": "[interrupted by user]"})


def selftest():
    """Mock the server, verify the full loop: tool call → execution → final answer."""
    global BASE_URL, CTX_CHARS, LAST_USAGE, HISTFILE
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
                args = json.dumps({"path": probe, "content": "hello from sidekick"})
                chunks = [{"delta": {"tool_calls": [{"index": 0, "id": "call_1",
                          "function": {"name": "write_file", "arguments": args}}]}}]
            for c in chunks:
                self.wfile.write(f"data: {json.dumps({'choices': [c]})}\n\n".encode())
            usage = {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 7}}
            self.wfile.write(f"data: {json.dumps(usage)}\n\n".encode())  # usage-only frame
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    BASE_URL = f"http://127.0.0.1:{srv.server_port}/v1"

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    agent_turn(messages, "create the probe file")
    with open(probe) as f:
        assert f.read() == "hello from sidekick", "tool execution failed"
    assert messages[-1]["content"] == "done: wrote probe", "final answer missing"
    assert any(m["role"] == "tool" for m in messages), "tool result not in transcript"

    # headless run_task: returns the final answer, leaks nothing to stdout (trace→stderr)
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        answer = run_task("create the probe file")
    assert answer == "done: wrote probe", f"run_task should return the final answer, got {answer!r}"
    assert buf.getvalue() == "", "run_task must keep stdout clean (trace goes to stderr)"

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
    assert tool_bash("exit 3").startswith("exit 3")
    assert "blocked" in tool_bash("git commit -m hi")
    assert "blocked" in tool_bash("cd /tmp && git merge feat/x")
    assert "blocked" in tool_bash("git checkout main")
    assert "blocked" in tool_bash("git branch -D old")
    assert "blocked" in tool_bash("git -C /tmp push origin main")
    assert "blocked" not in tool_bash("git log -1")
    assert "blocked" not in tool_bash("git -C /tmp status && git branch --show-current")
    assert "truncated" not in tool_read_file(probe)

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


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif not sys.stdin.isatty():          # task piped in → one-shot headless run
        task = sys.stdin.read().strip()   # whole stdin is ONE task, not line-by-line
        if task:
            print(run_task(task))         # stdout = final answer only
    else:
        repl()
