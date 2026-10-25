#!/usr/bin/env python3

import ast
import hashlib
import ipaddress
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import termios
    import tty
    _RAW_OK = True
except ImportError:
    _RAW_OK = False

__version__ = "0.2.0"

BASE_URL = "http://127.0.0.1:8321/v1"
MODEL = "local"
WINDOW = 0
MAX_TOKENS = 0
COMPACT_TOKENS = 0
MARGIN = 0
REPLY_BYTES = 0
MAX_TOOL_OUTPUT = 8000
MAX_CALLS = 4
MAX_STEPS = 40
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SCAN_BYTES = 32 * 1024 * 1024
BASH_TIMEOUT = 300
SHELL = None
TOKEN_FIELD = "max_tokens"
STREAM_USAGE = True
HISTORY_ENABLED = True
HISTFILE = os.path.expanduser("~/.familiar_history")
LAST_USAGE = None
TOKEN_SCALE = 1.0
PLAN = False
SYSTEM_PROMPT = ""
DIM, BOLD, RESET = ("\033[2m", "\033[1m", "\033[0m") if _RAW_OK and sys.stdout.isatty() and os.environ.get("TERM") != "dumb" else ("", "", "")
STATE = {"pending_check": False, "nudged": False, "last_check": None, "recent": [], "draft": None, "pause": ""}
_READ_SEEN = {}
_FILE_VERSIONS = {}
MUTATORS = {"write_file", "edit_file", "multi_edit"}
READ_ONLY = {"read_file", "search"}


class ContextPause(Exception):
    pass


class ProtocolError(Exception):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def positive_int(value, name):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


def local_url(url):
    if any(ord(char) <= 32 for char in url):
        raise ValueError("FAMILIAR_URL cannot contain whitespace or control characters")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise ValueError("FAMILIAR_URL must be an http(s) loopback URL without credentials, query or fragment")
    try:
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        if parts.hostname.lower() == "localhost":
            addresses = socket.getaddrinfo("localhost", port or 80, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_loopback for a in addresses):
                raise ValueError
        elif not ipaddress.ip_address(parts.hostname).is_loopback:
            raise ValueError
    except (ValueError, OSError) as e:
        raise ValueError("Familiar sends model requests only to loopback; use 127.0.0.1 or [::1] and a valid port") from e
    return url.rstrip("/")


class LocalRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        local_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), LocalRedirect())


def resolve_ctx_budget():
    ceiling = os.environ.get("FAMILIAR_CTX_TOKENS")
    ceiling = positive_int(ceiling, "FAMILIAR_CTX_TOKENS") if ceiling is not None else None
    server = None
    url = BASE_URL.rsplit("/v1", 1)[0] + "/props"
    try:
        with HTTP.open(local_url(url), timeout=2) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError("/props response exceeds 64 KiB")
        props = json.loads(raw)
        if not isinstance(props, dict) or not isinstance(props.get("default_generation_settings", {}), dict):
            raise ValueError("unsupported /props object; configure a compatible local server")
        value = (props.get("default_generation_settings") or {}).get("n_ctx", props.get("n_ctx"))
        if value is not None:
            server = positive_int(value, "server n_ctx")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        pass
    if server is None and ceiling is None:
        raise ValueError("No server context limit discovered. Set FAMILIAR_CTX_TOKENS to the server's usable context window.")
    return min(ceiling or server, server or ceiling), server


def configure():
    global BASE_URL, MODEL, WINDOW, MAX_TOKENS, COMPACT_TOKENS, MARGIN, REPLY_BYTES
    global MAX_TOOL_OUTPUT, BASH_TIMEOUT, HISTFILE, HISTORY_ENABLED, SHELL, TOKEN_FIELD, STREAM_USAGE
    BASE_URL = local_url(os.environ.get("FAMILIAR_URL", BASE_URL))
    MODEL = os.environ.get("FAMILIAR_MODEL", "local").strip()
    if not MODEL or any(ord(c) < 32 for c in MODEL):
        raise ValueError("FAMILIAR_MODEL must be a nonempty model name without control characters")
    BASH_TIMEOUT = positive_int(os.environ.get("FAMILIAR_BASH_TIMEOUT", "300"), "FAMILIAR_BASH_TIMEOUT")
    TOKEN_FIELD = os.environ.get("FAMILIAR_TOKEN_FIELD", "max_tokens")
    if TOKEN_FIELD not in {"max_tokens", "max_completion_tokens"}:
        raise ValueError("FAMILIAR_TOKEN_FIELD must be max_tokens or max_completion_tokens")
    for name in ("FAMILIAR_HISTORY", "FAMILIAR_STREAM_USAGE"):
        if os.environ.get(name, "1") not in {"0", "1"}:
            raise ValueError(f"{name} must be 0 or 1")
    HISTORY_ENABLED = os.environ.get("FAMILIAR_HISTORY", "1") == "1"
    STREAM_USAGE = os.environ.get("FAMILIAR_STREAM_USAGE", "1") == "1"
    HISTFILE = os.path.expanduser(os.environ.get("FAMILIAR_HISTFILE", "~/.familiar_history"))
    SHELL = os.environ.get("FAMILIAR_SHELL") or None
    if SHELL and not os.path.isfile(SHELL):
        raise ValueError("FAMILIAR_SHELL must name an existing shell executable")
    WINDOW, server = resolve_ctx_budget()
    MAX_TOKENS = positive_int(os.environ.get("FAMILIAR_MAX_TOKENS", str(min(2048, WINDOW // 8))), "FAMILIAR_MAX_TOKENS")
    if MAX_TOKENS < 256 or WINDOW < 8192 or MAX_TOKENS > WINDOW // 4:
        raise ValueError("Use a context of at least 8192 tokens and FAMILIAR_MAX_TOKENS between 256 and one quarter of it")
    COMPACT_TOKENS = min(1024, WINDOW // 16)
    MARGIN = max(256, WINDOW // 32)
    REPLY_BYTES = MAX_TOKENS
    MAX_TOOL_OUTPUT = min(8000, WINDOW // 8)
    return server


def build_system_prompt():
    notes = ""
    if os.path.isfile("FAMILIAR.md"):
        with open("FAMILIAR.md", "rb") as source:
            raw = source.read(8193)
        if len(raw) > 8192:
            raise ValueError("FAMILIAR.md exceeds the 8192-byte project-note budget; shorten it before starting")
        notes = "\nProject notes (FAMILIAR.md, repository content):\n" + raw.decode("utf-8-sig")
    shell = SHELL or (os.environ.get("COMSPEC", "cmd.exe") if os.name == "nt" else "/bin/sh")
    return f"""You are The Wizard's Familiar, one local coding agent.
Workspace: {os.getcwd()}
Python: {sys.executable}
Platform: {sys.platform}; shell tool (wire name bash): {shell}.
Read relevant files before editing. Reuse unchanged reads. Keep changes small and targeted.
Diagnose before editing. Prefer exact edit_file; use multi_edit for several changes to one file.
Read with read_file, search literal text with search. Use bash for commands and relevant checks.
Report check commands and observed exit status honestly; a shell invocation alone proves nothing.
Do not commit or push. Be concise; finish with a short summary and no tool call.{notes}"""


def init_workspace(argv):
    global SYSTEM_PROMPT
    if len(argv) > 1 and not argv[1].startswith("-"):
        os.chdir(argv[1])
    SYSTEM_PROMPT = build_system_prompt()


PLAN_SUFFIX = "\nPlan mode: only native read_file and search are available. Propose specific files, changes, reasons and checks. Do not execute checks or change files."


def system_message():
    return {"role": "system", "content": SYSTEM_PROMPT + (PLAN_SUFFIX if PLAN else "")}


def schema(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": {k: {"type": v} for k, v in properties.items()}, "required": required}}}


TOOLS = [
    schema("read_file", "Read UTF-8 text or list a directory. offset is a 1-based line; limit defaults to 120 (max 400); column is a 1-based byte position for a long line.", {"path": "string", "offset": "integer", "limit": "integer", "column": "integer"}, ["path"]),
    schema("search", "Find literal text in a file or directory. Bounded UTF-8 scan; limit defaults to 30 (max 100).", {"path": "string", "query": "string", "limit": "integer"}, ["path", "query"]),
    schema("write_file", "Write a UTF-8 file atomically. Read existing files first.", {"path": "string", "content": "string"}, ["path", "content"]),
    schema("edit_file", "Replace unique exact old with new, or inclusive start_line/end_line with new. Do not combine forms.", {"path": "string", "old": "string", "new": "string", "start_line": "integer", "end_line": "integer"}, ["path", "new"]),
    schema("multi_edit", "Sequential unique exact edits to one file; all validate before writing. edits is a nonempty array of old/new objects.", {"path": "string", "edits": "array"}, ["path", "edits"]),
    schema("bash", "Execute a command in the platform shell; output and runtime are bounded. Unsandboxed; unavailable in Plan.", {"command": "string"}, ["command"]),
]
TOOLS[4]["function"]["parameters"]["properties"]["edits"]["items"] = {"type": "object", "properties": {"old": {"type": "string"}, "new": {"type": "string"}}, "required": ["old", "new"]}


def active_tools():
    return [t for t in TOOLS if not PLAN or t["function"]["name"] in READ_ONLY]


def validate_args(name, args):
    tool = next((t["function"] for t in TOOLS if t["function"]["name"] == name), None)
    if not tool or not isinstance(args, dict):
        raise ValueError("unknown tool or arguments are not an object")
    params = tool["parameters"]
    if any(k not in args for k in params["required"]) or any(k not in params["properties"] for k in args):
        raise ValueError(f"{name}: missing required or unknown arguments")
    types = {"string": str, "integer": int, "array": list}
    for key, value in args.items():
        if type(value) is not types[params["properties"][key]["type"]]:
            raise ValueError(f"{name}: invalid type for {key}")
    if name == "multi_edit" and (not args["edits"] or any(not isinstance(e, dict) or set(e) != {"old", "new"} or not isinstance(e["old"], str) or not e["old"] or not isinstance(e["new"], str) for e in args["edits"])):
        raise ValueError("edits must be nonempty old/new string objects with nonempty old")


def digest_file(path):
    value = hashlib.sha256()
    scanned = 0
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            scanned += len(chunk)
            if scanned > MAX_FILE_BYTES:
                raise ValueError("file grew beyond the bounded fingerprint size; read it again")
            value.update(chunk)
    return value.digest()


def file_signature(path):
    info = os.stat(path)
    return digest_file(path) if stat.S_ISREG(info.st_mode) and info.st_size <= MAX_FILE_BYTES else (info.st_mtime_ns, info.st_size)


def text_prefix(value, budget):
    return value.encode("utf-8")[:max(0, budget)].decode("utf-8", errors="ignore")


def tool_read_file(path, offset=1, limit=120, column=1, _budget=None):
    budget = _budget or MAX_TOOL_OUTPUT
    if type(offset) is not int or type(limit) is not int or type(column) is not int or offset < 1 or column < 1 or not 1 <= limit <= 400:
        return "ERROR: offset/column must be positive and limit must be 1..400"
    if os.path.isdir(path):
        body, count = [], 0
        with os.scandir(path) as entries:
            for entry in entries:
                item = entry.name + ("/" if entry.is_dir(follow_symlinks=False) else "")
                if count >= 400 or len(encoded(body + [item])) > budget - 256:
                    return f"[{path} is a directory; listing truncated after {count} entries; use a narrower path]\n" + "\n".join(body)
                body.append(item)
                count += 1
        return f"[{path} is a directory, {count} entries]\n" + "\n".join(sorted(body))
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        return "ERROR: read_file accepts regular files and directories only"
    rp, page = os.path.realpath(path), (offset, limit, column, budget)
    sig = file_signature(path)
    seen = _READ_SEEN.get(rp)
    if seen and seen["sig"] == sig and page in seen["pages"]:
        return f"[already read {path} from line {offset}, byte {column}; unchanged — reuse the earlier source]"
    body, used, line_no, scanned = [], 0, 1, 0
    continuation = ""
    with open(path, "rb") as source:
        while line_no < offset:
            chunk = source.readline(65536)
            if not chunk:
                return f"ERROR: offset {offset} exceeds the file's {line_no - 1} lines"
            scanned += len(chunk)
            if scanned > MAX_SCAN_BYTES:
                return f"ERROR: line seek exceeded {MAX_SCAN_BYTES} bytes; search a narrower file or earlier region"
            if chunk.endswith(b"\n"):
                line_no += 1
        if column > MAX_SCAN_BYTES:
            return "ERROR: column exceeds the bounded line seek"
        if column > 1:
            skipped = source.read(column - 1)
            if len(skipped) != column - 1 or b"\n" in skipped:
                return "ERROR: column is past the requested line"
        for _ in range(limit):
            room = budget - 320 - used
            if room < 16:
                continuation = f"offset={line_no}, column=1"
                break
            raw = source.readline(max(1, room // 6))
            if not raw:
                break
            if b"\0" in raw:
                return "ERROR: binary input is not supported"
            complete = raw.endswith(b"\n") or source.peek(1) == b""
            if not complete:
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                while raw and raw[-1] >= 128:
                    try:
                        raw.decode("utf-8-sig" if line_no == 1 and column == 1 else "utf-8")
                        break
                    except UnicodeDecodeError as error:
                        if error.reason != "unexpected end of data":
                            raise
                        raw = raw[:error.start]
                continuation = f"offset={line_no}, column={column + len(raw)}"
            text = raw.decode("utf-8-sig" if line_no == 1 and column == 1 else "utf-8").replace("\r\n", "\n")
            item = f"{line_no}\t{text}"
            body.append(item)
            used += len(encoded(item))
            if not complete:
                break
            line_no += 1
            column = 1
        else:
            if source.peek(1):
                continuation = f"offset={line_no}, column=1"
    if file_signature(path) != sig:
        return "ERROR: file changed during read; retry"
    if isinstance(sig, bytes):
        _FILE_VERSIONS[rp] = sig
    if not seen or seen["sig"] != sig:
        seen = {"sig": sig, "pages": set()}
        _READ_SEEN[rp] = seen
    seen["pages"].add(page)
    receipt = f"[{path}: lines from {offset}, byte {page[2]}]\n" + "".join(body)
    if continuation:
        receipt += f"\n[truncated; unread source starts at {continuation}; call read_file with those values]"
    return receipt if body else f"[{path}: empty file or end of file]"


def tool_search(path, query, limit=30, _budget=None):
    budget = _budget or MAX_TOOL_OUTPUT
    if not query or "\n" in query or len(query.encode("utf-8")) > 1024 or type(limit) is not int or not 1 <= limit <= 100:
        return "ERROR: use a nonempty literal query of at most 1024 bytes, no newline, and limit 1..100"
    pending, matches, skipped = [path], [], []
    scanned = files = entries = 0
    limited = False
    while pending and not limited:
        current = pending.pop()
        try:
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode):
                if len(skipped) < 8:
                    skipped.append(f"{current} (symlink)")
                continue
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(current) as children:
                    for entry in children:
                        entries += 1
                        if entries > 2000:
                            limited = True
                            break
                        if entry.name not in {".git", ".claude", "__pycache__", "node_modules", ".venv"}:
                            pending.append(entry.path)
                continue
            if not stat.S_ISREG(info.st_mode):
                if len(skipped) < 8:
                    skipped.append(f"{current} (non-regular)")
                continue
            files += 1
            if files > 200 or scanned + info.st_size > MAX_SCAN_BYTES or info.st_size > MAX_FILE_BYTES:
                limited = True
                break
            with open(current, "rb") as source:
                raw = source.read(min(MAX_FILE_BYTES + 1, MAX_SCAN_BYTES - scanned + 1))
            scanned += len(raw)
            if len(raw) > MAX_FILE_BYTES or scanned > MAX_SCAN_BYTES:
                limited = True
                break
            if b"\0" in raw:
                raise UnicodeError("binary")
            text = raw.decode("utf-8-sig")
            for number, line in enumerate(text.splitlines(), 1):
                if query in line:
                    index = line.index(query)
                    excerpt = text_prefix(line[max(0, index - 60):], 240)
                    item = f"{current}:{number}: {excerpt}" + (" [match text truncated]" if len(line.encode("utf-8")) > 240 else "")
                    if len(matches) >= limit or len(encoded(matches + [item])) > budget - 512:
                        limited = True
                        break
                    matches.append(item)
        except (OSError, UnicodeError) as error:
            if len(skipped) < 8:
                skipped.append(text_prefix(f"{current} ({type(error).__name__})", 120))
    suffix = f"\n[scan: {files} files, {scanned} bytes; excludes .git/.claude/cache/dependency directories; symlinks not followed]"
    if skipped:
        suffix += "\n[skipped paths, first 8: " + "; ".join(skipped) + "]"
    if limited:
        suffix += "\n[scan/match/output limit reached; more paths or matches may remain; narrow path/query]"
    return ("\n".join(matches) or "[no matches in scanned text]") + suffix


def python_edit_error(path, content):
    if str(path).endswith(".py"):
        try:
            ast.parse(content, filename=str(path))
        except (SyntaxError, ValueError) as error:
            return f"ERROR: invalid Python (nothing written): {error}"
    return None


def edit_target(path):
    if not os.path.lexists(path):
        return None, "", False, "\n", None
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("mutation requires a regular non-symlink file")
    if not info.st_mode & 0o222 or not os.access(path, os.W_OK):
        raise PermissionError("target is read-only")
    if info.st_size > MAX_FILE_BYTES:
        raise ValueError("file exceeds the 8 MiB edit limit")
    with open(path, "rb") as source:
        raw = source.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("file exceeds the 8 MiB edit limit")
    version = hashlib.sha256(raw).digest()
    expected = _FILE_VERSIONS.get(os.path.realpath(path))
    if expected is not None and expected != version:
        raise ValueError("file changed since the last read; read it again before editing")
    text = raw.decode("utf-8-sig")
    remainder = text.replace("\r\n", "")
    if "\r" in remainder or ("\r\n" in text and "\n" in remainder):
        raise ValueError("mixed or unsupported line endings; nothing written")
    return raw, text.replace("\r\n", "\n"), raw.startswith(b"\xef\xbb\xbf"), "\r\n" if "\r\n" in text else "\n", info


def atomic_write(path, content, target):
    before, _, bom, newline, info = target
    error = python_edit_error(path, content)
    if error:
        return error
    if "\r" in content.replace("\r\n", ""):
        raise ValueError("unsupported carriage return in replacement")
    data = (b"\xef\xbb\xbf" if bom else b"") + content.replace("\r\n", "\n").replace("\n", newline).encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("result exceeds the 8 MiB edit limit")
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".familiar-", dir=parent)
    committed, cleanup_error = False, None
    try:
        with os.fdopen(fd, "wb") as destination:
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
        if info is not None:
            os.chmod(temporary, stat.S_IMODE(info.st_mode))
            current = os.lstat(path)
            if not stat.S_ISREG(current.st_mode) or current.st_mode != info.st_mode or not current.st_mode & 0o222 or digest_file(path) != hashlib.sha256(before).digest():
                raise ValueError("file changed before replacement; read it again")
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        committed = True
        _FILE_VERSIONS[os.path.realpath(path)] = hashlib.sha256(data).digest()
    finally:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError as error:
            if not committed:
                raise
            cleanup_error = error
    return f"wrote {len(data)} bytes to {path}" + (f"; temporary cleanup failed: {temporary}: {cleanup_error}" if cleanup_error else "")


def tool_write_file(path, content, _budget=None):
    return atomic_write(path, content, edit_target(path))


def tool_edit_file(path, old=None, new="", start_line=None, end_line=None, _budget=None):
    target = edit_target(path)
    if target[0] is None:
        return "ERROR: file does not exist"
    text = target[1]
    if old is not None:
        if start_line is not None or end_line is not None or not old:
            return "ERROR: use nonempty old or a line range, never both"
        old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
        count = text.count(old)
        if count != 1:
            return f"ERROR: old string appears {count} times; require one exact unique match (nothing written)"
        content = text.replace(old, new, 1)
    else:
        lines = text.splitlines(keepends=True)
        if type(start_line) is not int or type(end_line) is not int or not 1 <= start_line <= end_line <= len(lines):
            return f"ERROR: require 1 <= start_line <= end_line <= {len(lines)} (nothing written)"
        replacement = new.replace("\r\n", "\n")
        if replacement and (end_line < len(lines) or lines[end_line - 1].endswith("\n")) and not replacement.endswith("\n"):
            replacement += "\n"
        content = "".join(lines[:start_line - 1]) + replacement + "".join(lines[end_line:])
    return atomic_write(path, content, target)


def tool_multi_edit(path, edits, _budget=None):
    validate_args("multi_edit", {"path": path, "edits": edits})
    target = edit_target(path)
    if target[0] is None:
        return "ERROR: file does not exist"
    text = target[1]
    for i, edit in enumerate(edits, 1):
        old = edit["old"].replace("\r\n", "\n")
        count = text.count(old)
        if count != 1:
            return f"ERROR: edit {i}: old string appears {count} times (nothing written)"
        text = text.replace(old, edit["new"].replace("\r\n", "\n"), 1)
    return atomic_write(path, text, target)


GIT_RO = {"status", "log", "diff", "show", "blame", "grep", "ls-files", "ls-remote",
          "rev-parse", "merge-base", "reflog", "describe", "shortlog", "cat-file",
          "for-each-ref", "show-ref", "branch"}


def git_guard(command):
    for match in re.finditer(r"\bgit\b((?:\s+(?:-C\s+\S+|--?[\w=./-]+))*)\s+([\w-]+)", command):
        sub = match.group(2)
        args = re.split(r"[;&|]", command[match.end():])[0]
        mutating_branch = sub == "branch" and re.search(r"(^|\s)-[dDmMcCf]\b|--(delete|move|copy|force|set-upstream|unset-upstream|edit-description)", args)
        if sub not in GIT_RO or mutating_branch:
            return f"ERROR: 'git {sub}' is blocked; Familiar only runs read-only git commands"
    return None


def bare_cat_paths(command):
    if not re.fullmatch(r"\s*cat\s+[^|&;<>`$()*?\[\]]+\s*", command):
        return None
    try:
        parts = shlex.split(command, posix=os.name != "nt")[1:]
    except ValueError:
        return None
    parts = [part.strip('"') if os.name == "nt" else part for part in parts]
    return parts if parts and not any(p.startswith("-") for p in parts) else None


class OutputBuffer:
    def __init__(self, limit):
        self.limit, self.total = limit, 0
        self.head, self.tail = b"", b""

    def add(self, data):
        self.total += len(data)
        half = self.limit // 2
        missing = max(0, half - len(self.head))
        self.head += data[:missing]
        self.tail = (self.tail + data[missing:])[-(self.limit - half):]

    def render(self, prefix, budget):
        head, tail = self.head, self.tail
        while True:
            omitted = self.total - len(head) - len(tail)
            marker = f"\n[output truncated: {omitted} bytes omitted]\n".encode() if omitted else b""
            result = prefix + (head + marker + tail).decode("utf-8", errors="replace")
            if len(encoded(result)) <= budget:
                return result
            if not head and not tail:
                return text_prefix(prefix, budget // 4) + "\n[output omitted]"
            head, tail = head[:len(head) * 3 // 4], tail[len(tail) - len(tail) * 3 // 4:]


def stop_process(process):
    if os.name == "nt":
        try:
            os.kill(process.pid, signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            pass
        taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
        try:
            result = subprocess.run([taskkill, "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            note = "native tree termination requested; detached descendants unverified" if result.returncode == 0 else "descendant cleanup uncertain (native tree termination failed)"
        except (OSError, subprocess.TimeoutExpired):
            note = "descendant cleanup uncertain (native tree termination unavailable)"
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                note = "process cleanup uncertain: native termination and parent kill failed"
    else:
        note = "process group terminated; detached descendants unverified"
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            except OSError:
                note = "process-group cleanup uncertain: signaling failed"
                break
            if sig == signal.SIGTERM:
                time.sleep(0.1)
    try:
        process.wait(timeout=2)
    except (subprocess.TimeoutExpired, OSError):
        note = "process cleanup uncertain: parent still running"
    return note


def tool_bash(command, _budget=None):
    budget = _budget or MAX_TOOL_OUTPUT
    paths = bare_cat_paths(command)
    if paths and all(os.path.isfile(path) for path in paths):
        if len(paths) > 4 or budget // len(paths) < 512:
            return "ERROR: read fewer files per call to retain bounded source pages"
        return "\n".join(tool_read_file(path, _budget=budget // len(paths) - 16) for path in paths)
    blocked = git_guard(command)
    if blocked:
        return blocked
    if os.name == "nt":
        argv = subprocess.list2cmdline([SHELL or os.environ.get("COMSPEC", "cmd.exe")]) + ' /d /s /c "' + command + '"'
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        argv = [SHELL or "/bin/sh", "-c", command]
        options = {"start_new_session": True}
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0, **options)
    output, done, stop_reader = OutputBuffer(max(32, budget - 512)), threading.Event(), threading.Event()

    def drain():
        try:
            if os.name == "nt":
                import ctypes
                import msvcrt
                from ctypes import wintypes
                peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
                peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
                peek.restype = wintypes.BOOL
                handle = msvcrt.get_osfhandle(process.stdout.fileno())
            while not stop_reader.is_set():
                size = 4096
                if os.name == "nt":
                    available = wintypes.DWORD()
                    if not peek(handle, None, 0, None, ctypes.byref(available), None):
                        break
                    if not available.value:
                        stop_reader.wait(0.02)
                        continue
                    size = min(size, available.value)
                data = process.stdout.read(size)
                if not data:
                    break
                output.add(data)
        except (OSError, ValueError):
            pass
        finally:
            done.set()

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    status, cleanup = "", ""
    try:
        try:
            process.wait(timeout=BASH_TIMEOUT)
            status = f"exit {process.returncode}"
        except subprocess.TimeoutExpired:
            status = f"ERROR: command timed out after {BASH_TIMEOUT}s"
            cleanup = stop_process(process)
        except KeyboardInterrupt:
            cleanup = stop_process(process)
            status = "CANCELED: command interrupted; side effects may have occurred"
        if not done.wait(0.3):
            cleanup = stop_process(process)
            if not done.wait(0.5):
                cleanup += "; output pipe cleanup uncertain"
    finally:
        if process.poll() is None:
            cleanup = stop_process(process)
        stop_reader.set()
        reader.join(timeout=0.5)
        process.stdout.close()
    return output.render(status + (f"\n[{cleanup}]" if cleanup else "") + "\n", budget)


TOOL_IMPL = {"read_file": tool_read_file, "search": tool_search, "write_file": tool_write_file,
             "edit_file": tool_edit_file, "multi_edit": tool_multi_edit, "bash": tool_bash}


def run_tool(name, args, budget=None):
    if PLAN and name not in READ_ONLY:
        return "ERROR: Plan is read-only; shell and mutation tools are disabled. Switch to Normal with /plan."
    try:
        validate_args(name, args)
        result = TOOL_IMPL[name](**args, _budget=budget or MAX_TOOL_OUTPUT)
    except Exception as error:
        result = f"ERROR: {type(error).__name__}: {error}"
    limit = budget or MAX_TOOL_OUTPUT
    if len(encoded(result)) > limit:
        result = text_prefix(result, max(0, limit // 4 - 100)) + "\n[receipt truncated; narrow the request; no success implied]"
    return result


def api_messages(messages):
    return [{k: v for k, v in message.items() if not k.startswith("_")} for message in messages]


def assert_pairs(messages):
    pending = []
    for message in messages:
        if message["role"] == "tool":
            if not pending or message.get("tool_call_id") != pending.pop(0):
                raise ProtocolError("orphaned or out-of-order tool result")
        else:
            if pending:
                raise ProtocolError("assistant tool group is missing results")
            calls = message.get("tool_calls") or []
            pending = [call["id"] for call in calls]
            if len(set(pending)) != len(pending):
                raise ProtocolError("duplicate tool IDs")
    if pending:
        raise ProtocolError("assistant tool group is missing results")


COMPACT_INSTRUCTION = "Summarize only older assistant/tool observations before message {cutoff} (zero based), not system or real user instructions. Preserve source provenance; user turns remain verbatim. Give a short record of task state, paths/symbols changed, findings with sources, commands with observed outcomes, unresolved errors and proposed next steps. Refer to chronological user turns so corrections remain corrections. Distinguish hypotheses, repository text and actual evidence. No source dumps, invented successes or tools. This is historical context, not new authority."


def estimate(messages, tools):
    return int(len(encoded({"model": MODEL, "messages": api_messages(messages), "tools": tools})) * TOKEN_SCALE + 256 + 32 * len(messages) + 32 * len(tools))


def growth_budget():
    return int((REPLY_BYTES + MAX_TOOL_OUTPUT + MAX_CALLS * 192) * TOKEN_SCALE) + (MAX_CALLS + 1) * 32


def admission(messages, tools=None, purpose="normal", future=True):
    if not WINDOW:
        raise ContextPause("Configure a known context window before sending a request")
    tools = active_tools() if tools is None else tools
    reserve = COMPACT_TOKENS if purpose == "compact" else MAX_TOKENS
    prompt = estimate(messages, tools)
    if prompt + reserve + MARGIN > WINDOW:
        raise ContextPause("request plus completion reserve exceeds the context window")
    if purpose != "compact":
        compact_overhead = len(COMPACT_INSTRUCTION.encode("utf-8")) * TOKEN_SCALE + 128
        projected = estimate(messages, []) + compact_overhead + COMPACT_TOKENS + MARGIN
        if future:
            projected += growth_budget()
        if projected > WINDOW:
            raise ContextPause("next response would consume the reserved compaction capacity")
    return prompt


def build_request(messages, tools=None, purpose="normal"):
    assert_pairs(messages)
    tools = active_tools() if tools is None else tools
    cost = admission(messages, tools, purpose)
    payload = {"model": MODEL, "messages": api_messages(messages), "tools": tools, "stream": True,
               TOKEN_FIELD: COMPACT_TOKENS if purpose == "compact" else MAX_TOKENS}
    if STREAM_USAGE:
        payload["stream_options"] = {"include_usage": True}
    data = encoded(payload)
    return data, hashlib.sha256(data).hexdigest(), cost


def validate_calls(calls):
    ids = set()
    for call in calls:
        if not isinstance(call.get("id"), str) or not call["id"] or len(call["id"].encode("utf-8")) > 128 or call["id"] in ids:
            raise ProtocolError("tool call IDs must be nonempty and unique")
        ids.add(call["id"])
        try:
            validate_args(call["function"]["name"], json.loads(call["function"]["arguments"]))
        except (ValueError, TypeError, KeyError) as error:
            raise ProtocolError(f"invalid tool call: {error}") from error


def stream_completion(messages, tools=None, purpose="normal"):
    global LAST_USAGE, TOKEN_SCALE
    data, fingerprint, estimated = build_request(messages, tools, purpose)
    request = urllib.request.Request(local_url(BASE_URL) + "/chat/completions", data=data, headers={"Content-Type": "application/json"})
    cap = COMPACT_TOKENS if purpose == "compact" else REPLY_BYTES
    content, slots, finish, marker, reasoning = "", {}, None, False, False
    received = displayed_reasoning = 0
    usage = None
    try:
        with HTTP.open(request, timeout=600) as response:
            while True:
                raw = response.readline(max(16384, cap * 4) + 1)
                if not raw:
                    break
                received += len(raw)
                if len(raw) > max(16384, cap * 4) or received > max(262144, cap * 1024):
                    raise ProtocolError("stream/frame byte limit exceeded; executable output discarded")
                line = raw.decode("utf-8").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    if line.startswith(("event:", "id:", "retry:")):
                        continue
                    raise ProtocolError("unexpected substantive stream frame")
                body = line[5:].strip()
                if body == "[DONE]":
                    marker = True
                    break
                try:
                    obj = json.loads(body)
                    if not isinstance(obj, dict) or obj.get("error"):
                        raise ValueError(f"server error: {text_prefix(str(obj), 200)}")
                    if obj.get("usage"):
                        usage = obj["usage"]
                    choices = obj.get("choices", [])
                    if not choices:
                        continue
                    if len(choices) != 1:
                        raise ValueError("one completion choice required")
                    choice = choices[0]
                    delta = choice.get("delta")
                    delta = {} if delta is None else delta
                    if not isinstance(delta, dict):
                        raise ValueError("delta must be an object")
                    reason = choice.get("finish_reason")
                    if finish and (delta.get("content") or delta.get("tool_calls")):
                        raise ValueError("output after finish marker")
                    if reason:
                        if reason not in {"stop", "tool_calls", "length", "max_tokens"}:
                            raise ValueError(f"unsupported finish reason: {reason}")
                        finish = reason
                    thought = delta.get("reasoning_content")
                    thought = "" if thought is None else thought
                    if not isinstance(thought, str):
                        raise ValueError("reasoning must be text")
                    if thought and displayed_reasoning < cap:
                        if not reasoning:
                            sys.stdout.write(DIM)
                            reasoning = True
                        excerpt = text_prefix(thought, cap - displayed_reasoning)
                        displayed_reasoning += len(excerpt.encode("utf-8"))
                        sys.stdout.write(excerpt)
                    text = delta.get("content")
                    text = "" if text is None else text
                    if not isinstance(text, str):
                        raise ValueError("content must be text")
                    if text:
                        if reasoning:
                            sys.stdout.write(RESET + "\n")
                            reasoning = False
                        content += text
                        if purpose != "compact":
                            sys.stdout.write(text_prefix(text, cap))
                    frame_indexes = set()
                    for call in delta.get("tool_calls") or []:
                        index = call["index"]
                        if type(index) is not int or not 0 <= index < MAX_CALLS or index in frame_indexes:
                            raise ValueError("invalid or duplicate tool-call index")
                        frame_indexes.add(index)
                        slot = slots.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if call.get("type", "function") != "function":
                            raise ValueError("unsupported tool-call type")
                        if call.get("id") is not None:
                            if not isinstance(call["id"], str) or slot["id"] and call["id"] and slot["id"] != call["id"]:
                                raise ValueError("tool-call ID changed during stream")
                            slot["id"] = call["id"] or slot["id"]
                        function = call.get("function")
                        function = {} if function is None else function
                        if not isinstance(function, dict):
                            raise ValueError("tool function must be an object")
                        for key in ("name", "arguments"):
                            part = function.get(key)
                            part = "" if part is None else part
                            if not isinstance(part, str):
                                raise ValueError("tool function fragments must be text")
                            slot[key] += part
                    if len(encoded({"content": content, "calls": slots})) > cap:
                        raise ValueError("completion byte limit exceeded")
                    sys.stdout.flush()
                except (ValueError, TypeError, KeyError, AttributeError) as error:
                    raise ProtocolError(f"invalid stream; no tools authorized: {error}") from error
        if not finish and not marker:
            raise ProtocolError("unexpected EOF without finish or [DONE]; incomplete calls discarded")
        message = {"role": "assistant", "content": content or None}
        if slots:
            if sorted(slots) != list(range(len(slots))):
                raise ProtocolError("noncontiguous tool-call indexes")
            message["tool_calls"] = [{"id": slot["id"], "type": "function", "function": {"name": slot["name"], "arguments": slot["arguments"]}} for _, slot in sorted(slots.items())]
        if len(encoded(message)) > cap:
            raise ProtocolError("serialized completion exceeds the accepted growth bound")
        capped = finish in {"length", "max_tokens"}
        if not capped:
            validate_calls(message.get("tool_calls", []))
        if usage and isinstance(usage, dict) and type(usage.get("prompt_tokens")) is int and usage["prompt_tokens"] >= 0:
            LAST_USAGE = {"fingerprint": fingerprint, "usage": usage, "estimated": estimated}
            if usage["prompt_tokens"] > estimated:
                TOKEN_SCALE *= usage["prompt_tokens"] / max(1, estimated) * 1.05
        return message, "capped" if capped else "complete"
    finally:
        if reasoning:
            sys.stdout.write(RESET)
        if content or reasoning:
            sys.stdout.write("\n")
        sys.stdout.flush()


def chat(messages):
    message, status = stream_completion(messages)
    if status == "complete":
        return message
    calls = message.get("tool_calls", [])
    if len(calls) > 1:
        return {"role": "assistant", "content": "[completion capped: multiple calls discarded; retry separately]"}
    if calls and (not calls[0]["id"] or calls[0]["function"]["name"] not in TOOL_IMPL):
        raise ProtocolError("capped call has incomplete identity; discarded without continuation")
    fragment = calls[0]["function"]["arguments"] if calls else message["content"] or ""
    for _ in range(2):
        instruction = "Continue only the unfinished JSON arguments, no repetition, fence or tools." if calls else "Continue the unfinished text without repetition or tools."
        more, status = stream_completion(messages + [{"role": "assistant", "content": fragment}, {"role": "user", "content": instruction, "_generated": True}], tools=[])
        if more.get("tool_calls"):
            break
        fragment += more["content"] or ""
        if len(fragment.encode("utf-8")) > REPLY_BYTES:
            break
        if status == "complete":
            if calls:
                calls[0]["function"]["arguments"] = fragment
                validate_calls(calls)
            else:
                message["content"] = fragment
            if len(encoded(message)) <= REPLY_BYTES:
                return message
            break
    return {"role": "assistant", "content": "[completion still capped: incomplete output discarded; retry a smaller request]"}


def receipt(call, content):
    return {"role": "tool", "tool_call_id": call["id"], "content": content}


def call_key(name, args):
    key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
    if name == "read_file" and isinstance(args.get("path"), str):
        try:
            key += (file_signature(args["path"]),)
        except OSError:
            pass
    return key


def pause(reason):
    STATE["pause"] = str(reason)
    print(f"Context paused: {reason}. No messages discarded. Use /compact, /new, or correct the context settings.")


def close_open_group(messages, start):
    index = len(messages) - 1
    while index >= start and messages[index]["role"] == "tool":
        index -= 1
    if index < start:
        return
    calls = messages[index].get("tool_calls") or []
    finished = len(messages) - index - 1
    for i, call in enumerate(calls[finished:]):
        messages.append(receipt(call, "CANCELED: no completed receipt; outcome uncertain, inspect before retrying" if i == 0 else "NOT RUN: previous call interrupted"))


def agent_turn(messages, user_input):
    start = len(messages)
    try:
        return _agent_turn(messages, user_input)
    finally:
        close_open_group(messages, start)


def _agent_turn(messages, user_input):
    assert_pairs(messages)
    if STATE["pause"].startswith("server context overflow"):
        STATE["draft"] = user_input
        pause(STATE["pause"])
        return
    proposed = messages + [{"role": "user", "content": user_input}]
    try:
        admission(proposed)
    except ContextPause as error:
        STATE["draft"] = user_input
        pause(error)
        print("Draft retained in memory, not sent or executed; /draft shows it.")
        return
    STATE["pause"], STATE["draft"] = "", None
    messages.append(proposed[-1])
    for _ in range(MAX_STEPS):
        try:
            message = chat(messages)
        except ContextPause as error:
            pause(error)
            return
        group_start = len(messages)
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            if STATE["pending_check"] and not STATE["nudged"] and not PLAN:
                nudge = {"role": "user", "content": "Files changed without a completed check. Run a relevant check, report its observed exit status, or explain why no check applies. Then summarize.", "_generated": True}
                try:
                    admission(messages + [nudge])
                except ContextPause as error:
                    pause(error)
                    return
                STATE["nudged"] = True
                messages.append(nudge)
                continue
            if STATE["pending_check"]:
                print("[latest command after edits: " + STATE["last_check"] + "; assess its relevance]" if STATE["last_check"] else "[files changed; no completed check recorded]")
            return
        current, result, started = 0, None, False
        remaining = MAX_TOOL_OUTPUT
        try:
            validate_calls(calls)
            for current, call in enumerate(calls):
                result, started = None, False
                rest = [receipt(c, "NOT RUN: context paused before dispatch") for c in calls[current + 1:]]
                budget = remaining - sum(len(encoded(r)) for r in rest) - 192
                if budget < 512:
                    raise ContextPause("tool-result group output budget exhausted")
                admission(messages + [receipt(call, "x" * budget)] + rest, future=False)
                name = call["function"]["name"]
                args = json.loads(call["function"]["arguments"])
                key = call_key(name, args)
                if key in STATE["recent"]:
                    result = "[skipped: identical to a recent call; reuse the earlier observation, change approach, verify or stop]"
                else:
                    print(f"{DIM}→ {name} {text_prefix(args.get('command') or args.get('path') or '', 160)}{RESET}")
                    started = True
                    STATE["recent"].append(key)
                    STATE["recent"] = STATE["recent"][-4:]
                    pending_before = STATE["pending_check"]
                    if name in MUTATORS and not PLAN:
                        STATE["pending_check"] = True
                    result = run_tool(name, args, budget=budget)
                    if name in MUTATORS and result.startswith("ERROR"):
                        STATE["pending_check"] = pending_before
                    if name in MUTATORS and not result.startswith("ERROR"):
                        STATE["nudged"] = False
                        STATE["last_check"] = None
                        STATE["recent"] = [k for k in STATE["recent"] if k[0] in MUTATORS]
                    elif name == "bash" and STATE["pending_check"]:
                        STATE["nudged"] = True
                        STATE["last_check"] = text_prefix(args["command"], 160) + " — " + result.splitlines()[0]
                messages.append(receipt(call, result))
                remaining -= len(encoded(messages[-1]))
                if result.startswith("CANCELED"):
                    current += 1
                    for unrun in calls[current:]:
                        messages.append(receipt(unrun, "NOT RUN: user canceled the preceding command"))
                    return
        except (KeyboardInterrupt, ContextPause, ProtocolError) as error:
            existing = len(messages) - group_start - 1
            for index, call in enumerate(calls[existing:], existing):
                note = result if index == current and result is not None else ("CANCELED: outcome uncertain; inspect files/command effects before retrying" if index == current and started else "NOT RUN: interrupted or paused before dispatch")
                messages.append(receipt(call, note))
            assert_pairs(messages)
            if isinstance(error, ContextPause):
                pause(error)
            elif isinstance(error, KeyboardInterrupt):
                print("Interrupted; completed effects retained and remaining calls closed. No retry.")
            else:
                print(f"Invalid tool group: {error}")
            return
    stopped = {"role": "assistant", "content": f"[stopped at {MAX_STEPS} steps; no further tools or summary request. Review results before continuing.]"}
    messages.append(stopped)
    print(stopped["content"])


def compact(messages):
    global LAST_USAGE, TOKEN_SCALE
    assert_pairs(messages)
    before = estimate(messages, active_tools())
    groups, index = [], 1
    while index < len(messages):
        end = index + 1 + len(messages[index].get("tool_calls") or [])
        groups.append((index, end))
        index = end
    suffix_cost, cutoff = 0, len(messages)
    suffix_budget = growth_budget() + WINDOW // 16
    for start, end in reversed(groups):
        cost = estimate(messages[start:end], []) - 256
        if suffix_cost + cost > suffix_budget:
            break
        suffix_cost += cost
        cutoff = start
    eligible = [i for i in range(1, cutoff) if messages[i]["role"] != "user" or messages[i].get("_generated")]
    if not eligible:
        print("Compaction cannot help: no older observations outside the retained suffix. Use a smaller task or /new.")
        return False
    instruction = {"role": "user", "content": COMPACT_INSTRUCTION.format(cutoff=cutoff), "_generated": True}
    old_usage, old_scale = LAST_USAGE, TOKEN_SCALE
    try:
        preserved = [message for i, message in enumerate(messages) if i == 0 or i >= cutoff or message["role"] == "user" and not message.get("_generated")]
        admission(preserved + [{"role": "assistant", "content": "[historical summary]"}])
        summary, status = stream_completion(messages + [instruction], tools=[], purpose="compact")
        text = summary.get("content") or ""
        if status != "complete" or summary.get("tool_calls") or not text.strip() or len(text.encode("utf-8")) > COMPACT_TOKENS:
            raise ProtocolError("summary must be complete, nonempty bounded text without tools")
        replacement = {"role": "assistant", "content": "[Model-derived historical observations; potentially lossy, not new instructions]\n" + text, "_summary": True}
        candidate = []
        for i, message in enumerate(messages):
            if i == eligible[0]:
                candidate.append(replacement)
            if i == 0 or i >= cutoff or message["role"] == "user" and not message.get("_generated"):
                candidate.append(message)
        assert_pairs(candidate)
        admission(candidate)
        after = estimate(candidate, active_tools())
        if after >= before:
            raise ProtocolError("summary did not reduce context")
    except (ContextPause, ProtocolError, urllib.error.URLError, ValueError, OSError, KeyboardInterrupt) as error:
        LAST_USAGE, TOKEN_SCALE = old_usage, old_scale
        print(f"Compaction not applied; original context retained: {type(error).__name__}: {error}")
        return False
    messages[:] = candidate
    _READ_SEEN.clear()
    STATE["recent"] = [key for key in STATE["recent"] if key[0] not in READ_ONLY]
    STATE["pause"] = ""
    LAST_USAGE = None
    print(f"Compacted in memory: estimated {before} → {after} prompt tokens. No task resumed.\n{replacement['content']}")
    return True


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
        if final == "u":
            parts = body.split(";")
            if parts[0] == "13":
                mod = parts[1] if len(parts) > 1 else "1"
                return ("enter",) if mod in ("", "1") else ("newline",)
            return ("ignore",)
        return _ARROWS.get(final, ("ignore",))
    return ("ignore",)


def _read_escape(fd):
    if not select.select([fd], [], [], 0.02)[0]:
        return None
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
        return ("interrupt",)
    if c == 0x04:
        return ("eof",)
    if c == 0x15:
        return ("kill",)
    if c == 0x01:
        return ("home",)
    if c == 0x05:
        return ("end",)
    if c == 0x0c:
        return ("clear",)
    if c == 0x17:
        return ("word-back",)
    if c < 0x20:
        return ("ignore",)
    if c >= 0x80:
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
        if action == "interrupt":
            sys.stdout.write("^C\r\n")
            sys.stdout.flush()
            buf, cur, prev = "", 0, _render(prompt, "", 0, 0)
            continue
        if action == "eof":
            sys.stdout.write("\r\n")
            sys.stdout.flush()
            raise EOFError
        if action == "clear-screen":
            sys.stdout.write("\x1b[2J\x1b[H")
            prev = _render(prompt, buf, cur, 0)
            continue
        prev = _render(prompt, buf, cur, prev)


def load_history():
    if not HISTORY_ENABLED:
        return []
    try:
        with open(HISTFILE, encoding="utf-8") as source:
            out = []
            for line in source:
                try:
                    value = json.loads(line)
                    if isinstance(value, str):
                        out.append(value)
                        if len(out) > 1000:
                            del out[0]
                except ValueError:
                    pass
            return out
    except (OSError, UnicodeError):
        return []


def save_history(line):
    if not HISTORY_ENABLED or line.startswith("/"):
        return
    try:
        with open(HISTFILE, "a", encoding="utf-8") as destination:
            destination.write(json.dumps(line) + "\n")
    except OSError:
        pass


def read_input(prompt, history):
    if not (_RAW_OK and sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"):
        sys.stdout.write(prompt)
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        return line.rstrip("\r\n")
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sys.stdout.write("\x1b[?2004h\x1b[>1u")
        sys.stdout.flush()
        return _edit(prompt, history)
    finally:
        try:
            sys.stdout.write("\x1b[<u\x1b[?2004l")
            sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def format_meter(messages):
    used = estimate(messages, active_tools())
    return f"ctx estimated {used / 1000:.1f}k / {WINDOW / 1000:g}k prompt tokens · {'Plan' if PLAN else 'Normal'}" + (f" · paused: {STATE['pause']}" if STATE["pause"] else "")


def cmd_tokens(messages):
    used = estimate(messages, active_tools())
    print(format_meter(messages))
    print(f"  completion reserve {MAX_TOKENS}; compaction reply reserve {COMPACT_TOKENS}; safety margin {MARGIN}")
    print(f"  bounded next-response/tool growth {growth_budget()}; ordinary free {max(0, WINDOW - used - MAX_TOKENS - MARGIN)}")
    print("  fallback: serialized UTF-8 bytes plus framing, calibrated upward; model tokenization may differ")
    if LAST_USAGE:
        usage = LAST_USAGE["usage"]
        print(f"  last request {LAST_USAGE['fingerprint'][:12]} measured: {usage.get('prompt_tokens', 0)} prompt + {usage.get('completion_tokens', 'unknown')} completion; not current context usage")
    if STATE["draft"] is not None:
        print("  an unsent draft is retained in memory (/draft)")
    try:
        admission(messages)
    except ContextPause as error:
        print(f"  pause reason: {error}")


HELP = """commands
  /new      clear in-memory conversation, keeps selected mode
  /plan     toggle Normal / Plan (Plan permits only native reads and search)
  /compact  summarize older observations in memory, then return to the prompt
  /tokens   estimated current budget and last-request measured usage
  /draft    show an unsent rejected draft
  /help     this help
  /quit     exit (also EOF / Ctrl-D on an empty line)
Unix terminal: Enter submits; Alt+Enter inserts a newline; Shift+Enter needs a
compatible terminal. Paste and up/down history supported. Ctrl-C abandons input.
Windows/non-TTY: plain one-line input; EOF exits, Ctrl-C cancels.
Input history stores private user prompts by default; FAMILIAR_HISTORY=0 disables
all history file reads/writes. Conversations and summaries are never saved."""


def new_conversation():
    global LAST_USAGE
    LAST_USAGE = None
    _READ_SEEN.clear()
    _FILE_VERSIONS.clear()
    STATE.update(pending_check=False, nudged=False, last_check=None, recent=[], draft=None, pause="")
    return [system_message()]


def repl():
    global PLAN, LAST_USAGE
    print(f"{BOLD}The Wizard's Familiar{RESET} — local coding agent")
    print(f"{DIM}server {BASE_URL} · cwd {os.getcwd()} · window {WINDOW} · /help{RESET}")
    if HISTORY_ENABLED:
        print("Input history is enabled: private prompt text is stored in " + HISTFILE + "; FAMILIAR_HISTORY=0 disables it.")
    messages = new_conversation()
    history = load_history()
    try:
        while True:
            try:
                print("\n" + format_meter(messages))
                user_input = read_input("plan › " if PLAN else "› ", history)
            except EOFError:
                print()
                return
            except KeyboardInterrupt:
                print("\nInput canceled.")
                continue
            if not user_input.strip():
                continue
            history.append(user_input)
            history[:] = history[-1000:]
            save_history(user_input)
            if user_input in ("/quit", "/exit"):
                return
            if user_input == "/new":
                messages = new_conversation()
                print("Conversation cleared in memory.")
                continue
            if user_input == "/plan":
                PLAN = not PLAN
                messages[0] = system_message()
                LAST_USAGE = None
                print("Plan: native read/search only." if PLAN else "Normal: execution and edits enabled.")
                continue
            if user_input == "/tokens":
                cmd_tokens(messages)
                continue
            if user_input == "/draft":
                print(STATE["draft"] if STATE["draft"] is not None else "No unsent draft.")
                continue
            if user_input == "/help":
                print(HELP)
                continue
            try:
                if user_input == "/compact":
                    compact(messages)
                elif user_input.startswith("/"):
                    print("Unknown command; /help lists commands.")
                else:
                    agent_turn(messages, user_input)
            except urllib.error.HTTPError as error:
                try:
                    body = error.read(512).decode("utf-8", errors="replace")
                finally:
                    error.close()
                if "context" in body.lower() and any(word in body.lower() for word in ("exceed", "length", "overflow", "limit")):
                    pause("server context overflow; /compact, /new, or lower the configured limit")
                else:
                    print(f"Server rejected request ({error.code}): {body}. No automatic retry. Check FAMILIAR_TOKEN_FIELD / FAMILIAR_STREAM_USAGE compatibility.")
            except (urllib.error.URLError, OSError, ProtocolError, ValueError) as error:
                print(f"Request stopped: {type(error).__name__}: {error}. Incomplete tools discarded; no automatic retry.")
            except KeyboardInterrupt:
                print("\nInterrupted. No automatic retry.")
    finally:
        messages.clear()
        history.clear()
        new_conversation()


def selftest():
    import contextlib
    import io
    from unittest.mock import patch
    real_connect = socket.socket.connect

    def local_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not ipaddress.ip_address(address[0]).is_loopback:
            raise OSError("selftest blocks external network access")
        return real_connect(sock, address)

    with patch.object(socket.socket, "connect", local_connect), contextlib.redirect_stdout(io.StringIO()):
        _selftest()
    print("selftest OK")


def _selftest():
    global BASE_URL, WINDOW, MAX_TOKENS, REPLY_BYTES, COMPACT_TOKENS, MARGIN, MAX_TOOL_OUTPUT
    global HISTFILE, HISTORY_ENABLED, PLAN, LAST_USAGE, TOKEN_SCALE, BASH_TIMEOUT, MAX_STEPS
    import copy
    import http.server
    import io
    from pathlib import Path
    from unittest.mock import patch

    WINDOW, MAX_TOKENS, REPLY_BYTES, COMPACT_TOKENS, MARGIN, MAX_TOOL_OUTPUT = 32768, 2048, 2048, 1024, 1024, 4096
    PLAN, TOKEN_SCALE = False, 1.0

    def frame(delta, finish=None):
        return b"data: " + encoded({"choices": [{"delta": delta, "finish_reason": finish}]}) + b"\n\n"

    def call(name, args, ident="test"):
        return {"role": "assistant", "content": None, "tool_calls": [{"id": ident, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}

    def streamed_call(name, args, ident="test", index=0):
        return {"tool_calls": [{"index": index, "id": ident, "type": "function", "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}]}

    done = {"role": "assistant", "content": "done"}
    with tempfile.TemporaryDirectory(prefix="familiar-test-", dir=os.getcwd()) as directory:
        root = Path(directory)
        probe = str(root / "prøbe.txt")

        class Mock(http.server.BaseHTTPRequestHandler):
            replies = []
            requests = []
            props = {"default_generation_settings": {"n_ctx": 32768}}

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                type(self).requests.append(payload)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                reply = type(self).replies.pop(0)
                self.wfile.write(reply)

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(encoded(type(self).props))

            def log_message(self, *args):
                pass

        class MinimalMock(Mock):
            props = {}
            replies = []
            requests = []

        servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 0), cls) for cls in (Mock, MinimalMock)]
        workers = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
        for worker in workers:
            worker.start()
        try:
            BASE_URL = f"http://127.0.0.1:{servers[0].server_port}/v1"
            with patch.dict(os.environ, {}, clear=True):
                assert resolve_ctx_budget() == (32768, 32768)
                os.environ["FAMILIAR_CTX_TOKENS"] = "65536"
                assert resolve_ctx_budget() == (32768, 32768)
                os.environ["FAMILIAR_CTX_TOKENS"] = "8192"
                assert resolve_ctx_budget() == (8192, 32768)
                os.environ["FAMILIAR_CTX_TOKENS"] = "bad"
                try:
                    resolve_ctx_budget()
                    assert False, "invalid context accepted"
                except ValueError:
                    pass
            Mock.replies = [frame(streamed_call("write_file", {"path": probe, "content": "hello"})) + b"data: [DONE]\n\n", frame({"content": "done"}, "stop"), frame({"content": "No relevant check applies to this text file."}, "stop")]
            transcript = new_conversation()
            agent_turn(transcript, "create the probe")
            assert Path(probe).read_bytes() == b"hello"
            assert_pairs(transcript)
            assert any(m.get("_generated") for m in transcript)
            assert not any("_generated" in m for r in Mock.requests for m in r["messages"])
            assert all(r["max_tokens"] == MAX_TOKENS for r in Mock.requests)

            BASE_URL = f"http://127.0.0.1:{servers[1].server_port}/v1"
            with patch.dict(os.environ, {}, clear=True):
                try:
                    resolve_ctx_budget()
                    assert False, "unknown context defaulted silently"
                except ValueError:
                    pass
                os.environ["FAMILIAR_CTX_TOKENS"] = "16384"
                assert resolve_ctx_budget() == (16384, None)
            MinimalMock.replies = [frame({"content": "minimal server"}) + b"data: [DONE]\n\n"]
            assert chat([{"role": "user", "content": "hello"}])["content"] == "minimal server"
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for worker in workers:
                worker.join(timeout=2)

        for bad_url in ("http://example.com/v1", "file:///tmp/server", "http://127.0.0.1:0/v1", "http://user@127.0.0.1/v1", "http://127.0.0.1/v1?key=x", "http://127.0.0.1:99999/v1"):
            try:
                local_url(bad_url)
                assert False, bad_url
            except ValueError:
                pass
        assert local_url("http://[::1]:8321/v1/") == "http://[::1]:8321/v1"
        try:
            LocalRedirect().redirect_request(urllib.request.Request(BASE_URL), None, 302, "", {}, "http://example.com/")
            assert False, "remote redirect accepted"
        except ValueError:
            pass
        assert not any(isinstance(handler, urllib.request.ProxyHandler) and handler.proxies for handler in HTTP.handlers)

        base = [{"role": "system", "content": "test"}, {"role": "user", "content": "write"}]
        full = streamed_call("write_file", {"path": probe, "content": "must not write"})
        failures = [frame(full), frame(full) + b"data: {bad json}\n\n", frame(full) + b'data: {"error":"failed"}\n\n', frame(streamed_call("write_file", "[]"), "stop"), frame(streamed_call("", {}), "stop"), frame({"tool_calls": full["tool_calls"] + streamed_call("read_file", {"path": probe}, index=1)["tool_calls"]}, "stop"), frame({"tool_calls": [dict(full["tool_calls"][0], index=-1)]}, "stop"), frame({"content": "x" * (REPLY_BYTES + 1)}, "stop"), frame({**full, "content": 0}, "stop")]
        for raw in failures:
            before = Path(probe).read_bytes()
            response = io.BytesIO(raw)
            with patch.object(HTTP, "open", return_value=response):
                try:
                    agent_turn(new_conversation(), "write something")
                    assert False, "failed stream accepted"
                except ProtocolError:
                    pass
            assert Path(probe).read_bytes() == before
            assert response.closed
        usage = {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 7}}
        with patch.object(HTTP, "open", return_value=io.BytesIO(b": ping\n\n" + frame({"content": "ok"}, "stop") + b"data: " + encoded(usage) + b"\n\ndata: [DONE]\n\n")):
            assert chat(base)["content"] == "ok"
        assert LAST_USAGE["usage"]["prompt_tokens"] == 120
        assert "estimated" in format_meter(base)
        partial = streamed_call("write_file", '{"path":"a.txt","content":"hel')
        with patch.object(HTTP, "open", side_effect=[io.BytesIO(frame(partial, "length")), io.BytesIO(frame({"content": 'lo"}'}, "stop"))]) as opener:
            result = chat(base)
            assert json.loads(result["tool_calls"][0]["function"]["arguments"])["content"] == "hello"
            assert json.loads(opener.call_args[0][0].data)["tools"] == []
        for raw in (frame({"content": "x"}, "length"), frame({"content": "bad JSON"}, "stop")):
            with patch.object(HTTP, "open", side_effect=[io.BytesIO(frame(partial, "length")), io.BytesIO(raw), io.BytesIO(raw)]) as opener:
                try:
                    assert not chat(base).get("tool_calls")
                except ProtocolError:
                    pass
                assert opener.call_count <= 3
        capped_multi = {"tool_calls": full["tool_calls"] + streamed_call("read_file", {"path": probe}, "two", 1)["tool_calls"]}
        with patch.object(HTTP, "open", return_value=io.BytesIO(frame(capped_multi, "length"))) as opener:
            assert not chat(base).get("tool_calls") and opener.call_count == 1

        for variant in (b"alpha\nbeta\n", b"alpha\nbeta", b"alpha\r\nbeta\r\n", b"\xef\xbb\xbfalpha\r\nbeta", "α alpha\nβ beta".encode()):
            _FILE_VERSIONS.clear()
            Path(probe).write_bytes(variant)
            tool_read_file(probe)
            assert not run_tool("edit_file", {"path": probe, "old": "beta", "new": "BETA"}).startswith("ERROR")
            assert Path(probe).read_bytes() == variant.replace(b"beta", b"BETA")
        _FILE_VERSIONS.clear()
        Path(probe).write_bytes(b"one\ntwo\nthree")
        for start, end in ((0, 1), (2, 1), (1, 4), (4, 4)):
            assert run_tool("edit_file", {"path": probe, "start_line": start, "end_line": end, "new": "bad"}).startswith("ERROR")
            assert Path(probe).read_bytes() == b"one\ntwo\nthree"
        assert not run_tool("edit_file", {"path": probe, "start_line": 3, "end_line": 3, "new": "THREE"}).startswith("ERROR")
        assert Path(probe).read_bytes() == b"one\ntwo\nTHREE"
        assert run_tool("edit_file", {"path": probe, "old": "one", "start_line": 1, "end_line": 1, "new": "x"}).startswith("ERROR")
        assert run_tool("edit_file", {"path": probe, "old": "", "new": "x"}).startswith("ERROR")
        assert run_tool("multi_edit", {"path": probe, "edits": []}).startswith("ERROR")
        assert run_tool("read_file", {"path": probe, "offset": True}).startswith("ERROR")
        assert run_tool("multi_edit", {"path": probe, "edits": [{"old": "one", "new": "ONE"}, {"old": "missing", "new": "X"}]}).startswith("ERROR")
        assert Path(probe).read_bytes() == b"one\ntwo\nTHREE"
        assert not run_tool("multi_edit", {"path": probe, "edits": [{"old": "one", "new": "ONE"}, {"old": "ONE", "new": "1"}]}).startswith("ERROR")
        assert Path(probe).read_bytes() == b"1\ntwo\nTHREE"
        before = Path(probe).read_bytes()
        for operation in ("replace", "fsync"):
            with patch.object(os, operation, side_effect=PermissionError("injected before commit")):
                assert run_tool("edit_file", {"path": probe, "old": "two", "new": "TWO"}).startswith("ERROR")
            assert Path(probe).read_bytes() == before
            assert not list(root.glob(".familiar-*"))
        tool_read_file(probe)
        Path(probe).write_bytes(before.replace(b"two", b"TWO"))
        assert "changed since" in run_tool("edit_file", {"path": probe, "old": "THREE", "new": "three"})
        tool_read_file(probe)
        assert not run_tool("edit_file", {"path": probe, "old": "THREE", "new": "three"}).startswith("ERROR")
        Path(probe).chmod(0o444)
        assert run_tool("write_file", {"path": probe, "content": "x"}).startswith("ERROR")
        Path(probe).chmod(0o644)
        if os.name != "nt":
            link = root / "link"
            link.symlink_to(probe)
            assert "non-symlink" in run_tool("write_file", {"path": str(link), "content": "x"})
            fifo = root / "fifo"
            os.mkfifo(fifo)
            assert "regular" in run_tool("read_file", {"path": str(fifo)})
            fifo.unlink()
        pyprobe = str(root / "test.py")
        run_tool("write_file", {"path": pyprobe, "content": "x = 1\n"})
        for name, args in (("edit_file", {"old": "1", "new": "("}), ("edit_file", {"start_line": 1, "end_line": 1, "new": "x = ("}), ("multi_edit", {"edits": [{"old": "1", "new": "("}]}), ("write_file", {"content": "x = ("})):
            assert "invalid Python" in run_tool(name, {"path": pyprobe, **args})
            assert Path(pyprobe).read_bytes() == b"x = 1\n"

        _READ_SEEN.clear()
        _FILE_VERSIONS.clear()
        Path(probe).write_bytes(b"a\na\nb\n")
        assert "offset=3" in tool_read_file(probe, limit=2)
        assert "already read" in tool_read_file(probe, limit=2)
        old_stat = os.stat(probe)
        Path(probe).write_bytes(b"a\na\nc\n")
        os.utime(probe, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        assert "already read" not in tool_read_file(probe, limit=2)
        long = root / "long.txt"
        long.write_bytes(b"a" * 100000 + b"needle\nlast\n")
        page = tool_read_file(str(long))
        assert "column=" in page and len(encoded(page)) <= MAX_TOOL_OUTPUT
        next_column = int(re.search(r"column=(\d+)", page)[1])
        assert f"byte {next_column}" in tool_read_file(str(long), column=next_column)
        binary = root / "binary"
        binary.write_bytes(b"a\0b")
        assert "binary" in tool_read_file(str(binary))
        assert "is a directory" in tool_read_file(directory)
        assert "long.txt:1:" in tool_search(str(long), "needle")
        assert "skipped" in tool_search(directory, "needle")
        assert "limit" in tool_search(probe, "a", limit=1)
        _READ_SEEN.clear()
        quote = subprocess.list2cmdline([probe]) if os.name == "nt" else shlex.quote(probe)
        assert "1\ta" in tool_bash("cat " + quote)
        assert "already read" in tool_bash("cat " + quote)
        assert bare_cat_paths("cat a | grep x") is None
        assert bare_cat_paths("cat -n a") is None
        assert bare_cat_paths("cat *.txt") is None

        for command in ("git commit -m hi", "git -C /tmp push", "git checkout main", "git branch -D old", "echo ok && git merge feature"):
            assert "blocked" in tool_bash(command)
        assert tool_bash("exit 3").startswith("exit 3")
        assert tool_bash("echo ok").startswith("exit 0")

        def python_command(code):
            return subprocess.list2cmdline([sys.executable, "-c", code]) if os.name == "nt" else shlex.join([sys.executable, "-c", code])

        spaced = root / "space name 雪.txt"
        spaced.write_bytes(b"path works")
        assert "path works" in tool_bash(python_command("from pathlib import Path; print(Path(" + repr(str(spaced)) + ").read_text())"))
        large = tool_bash(python_command("import sys; print('HEAD'); sys.stdout.write('x'*200000); sys.stderr.write('TAIL failure')"))
        assert "HEAD" in large and "TAIL failure" in large and "bytes omitted" in large
        assert len(large.encode()) < MAX_TOOL_OUTPUT
        saved_timeout, BASH_TIMEOUT = BASH_TIMEOUT, 1
        started = time.monotonic()
        timeout = tool_bash(python_command("import time; time.sleep(20)"))
        assert "timed out" in timeout and time.monotonic() - started < 8
        real_wait, interrupted = subprocess.Popen.wait, []

        def interrupt_once(process, *args, **kwargs):
            if not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt
            return real_wait(process, *args, **kwargs)

        with patch.object(subprocess.Popen, "wait", interrupt_once):
            canceled = tool_bash(python_command("import time; time.sleep(20)"))
        assert canceled.startswith("CANCELED")
        interrupted.clear()
        with patch.object(subprocess.Popen, "wait", interrupt_once), patch.object(os, "kill" if os.name == "nt" else "killpg", side_effect=PermissionError("injected cleanup failure")), patch.object(subprocess.Popen, "kill", side_effect=PermissionError("injected parent kill failure")), patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            canceled = tool_bash(python_command("import time; time.sleep(0.5)"))
        assert canceled.startswith("CANCELED") and "uncertain" in canceled
        BASH_TIMEOUT = saved_timeout
        child_script = root / "spawn.py"
        child_script.write_text("import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])\n", encoding="utf-8")
        started = time.monotonic()
        orphan = tool_bash(python_command("exec(open(" + repr(str(child_script)) + ").read())"))
        assert time.monotonic() - started < 8
        assert "cleanup" in orphan or "process group" in orphan or "tree termination" in orphan

        for script in ([call("read_file", {"path": "A"}), call("read_file", {"path": "B"}), call("read_file", {"path": "A"}), done], [call("read_file", {"path": "A", "offset": 1}), call("read_file", {"offset": 1, "path": "A"}), done]):
            transcript = new_conversation()
            with patch.dict(globals(), chat=lambda m, it=iter(script): next(it)):
                agent_turn(transcript, "read")
            assert any("skipped: identical" in m.get("content", "") for m in transcript if m["role"] == "tool")
        writes = [call("write_file", {"path": probe, "content": "same"}), call("write_file", {"path": probe + "2", "content": "same"}), call("write_file", {"path": probe, "content": "same"}), call("bash", {"command": "exit 1"}), done]
        transcript = new_conversation()
        with patch.dict(globals(), chat=lambda m, it=iter(writes): next(it)):
            agent_turn(transcript, "write")
        assert any("skipped: identical" in m.get("content", "") for m in transcript if m["role"] == "tool")
        assert STATE["pending_check"]

        group = call("write_file", {"path": probe, "content": "committed"}, "first")
        group["tool_calls"].append(call("bash", {"command": "exit 0"}, "second")["tool_calls"][0])
        group["tool_calls"].append(call("write_file", {"path": probe + "never", "content": "never"}, "third")["tool_calls"][0])
        transcript = new_conversation()
        real_run = run_tool

        def interrupted_run(name, args, budget=None):
            if name == "bash":
                raise KeyboardInterrupt
            return real_run(name, args, budget)

        with patch.dict(globals(), chat=lambda m: group, run_tool=interrupted_run):
            agent_turn(transcript, "do three things")
        assert_pairs(transcript)
        assert Path(probe).read_bytes() == b"committed" and not Path(probe + "never").exists()
        assert "uncertain" in transcript[-2]["content"] and "NOT RUN" in transcript[-1]["content"]
        transcript = new_conversation()
        with patch.dict(globals(), chat=lambda m: group, run_tool=lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt())):
            agent_turn(transcript, "cancel first")
        assert_pairs(transcript)
        assert all("NOT RUN" in m["content"] for m in transcript[-2:])
        transcript = new_conversation()
        with patch.dict(globals(), MAX_STEPS=1, chat=lambda m: call("read_file", {"path": probe})):
            agent_turn(transcript, "step bound")
        assert_pairs(transcript)
        assert "stopped at 1" in transcript[-1]["content"] and not transcript[-1].get("tool_calls")

        PLAN = True
        assert {t["function"]["name"] for t in active_tools()} == READ_ONLY
        for name, args in (("bash", {"command": "echo forbidden"}), ("write_file", {"path": probe, "content": "forbidden"}), ("edit_file", {"path": probe, "old": "committed", "new": "forbidden"}), ("multi_edit", {"path": probe, "edits": [{"old": "committed", "new": "forbidden"}]})):
            assert "read-only" in run_tool(name, args)
        assert "Plan mode" in new_conversation()[0]["content"] and PLAN
        PLAN = False
        assert "Plan mode" not in new_conversation()[0]["content"]

        transcript = new_conversation()
        before = copy.deepcopy(transcript)
        agent_turn(transcript, "later correction " * WINDOW)
        assert transcript == before and STATE["draft"].startswith("later correction")
        assert STATE["pause"]
        assert estimate([{"role": "user", "content": "雪" * 100}], []) > estimate([{"role": "user", "content": "a" * 100}], [])
        assert estimate(base, active_tools()) > estimate(base, [])
        transcript = new_conversation()
        transcript.extend([{"role": "user", "content": "original task"}, {"role": "assistant", "content": "observed " * 1500}, {"role": "user", "content": "later correction, verbatim\n snow 雪"}, {"role": "assistant", "content": "older findings " * 300}])
        last_group = call("read_file", {"path": probe}, "last")
        transcript.extend([last_group, receipt(last_group["tool_calls"][0], "latest source\n")])
        STATE["pending_check"], STATE["nudged"] = True, True
        users = [m["content"] for m in transcript if m["role"] == "user"]
        snapshot = copy.deepcopy(transcript)
        read_snapshot = copy.deepcopy(_READ_SEEN)
        for result in (({"role": "assistant", "content": ""}, "complete"), ({"role": "assistant", "content": "short"}, "capped"), (call("read_file", {"path": probe}), "complete")):
            with patch.dict(globals(), stream_completion=lambda *a, r=result, **kw: r):
                assert not compact(transcript)
            assert transcript == snapshot and _READ_SEEN == read_snapshot
        with patch.dict(globals(), stream_completion=lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt())):
            assert not compact(transcript)
        assert transcript == snapshot
        with patch.dict(globals(), stream_completion=lambda *a, **kw: ({"role": "assistant", "content": "Observed changes; no check passed. Later correction supersedes the task."}, "complete")):
            assert compact(transcript)
        assert [m["content"] for m in transcript if m["role"] == "user"] == users
        assert transcript[-2:] == snapshot[-2:] and not _READ_SEEN
        assert STATE["pending_check"] and STATE["nudged"] and LAST_USAGE is None
        assert_pairs(transcript)
        assert "already read" not in tool_read_file(probe)
        assert not compact(new_conversation())

        def fail_before_dispatch(calls):
            raise KeyboardInterrupt

        transcript = new_conversation()
        with patch.dict(globals(), chat=lambda m: group, validate_calls=fail_before_dispatch):
            agent_turn(transcript, "interrupt before dispatch")
        assert_pairs(transcript)
        assert all(m["content"].startswith("NOT RUN") for m in transcript[-3:])
        transcript = new_conversation()
        one_write = call("write_file", {"path": probe, "content": "once"})

        def interrupt_after_write(messages):
            if any(m["role"] == "tool" for m in messages):
                raise KeyboardInterrupt
            return one_write

        with patch.dict(globals(), chat=interrupt_after_write):
            try:
                agent_turn(transcript, "write once")
            except KeyboardInterrupt:
                pass
        assert_pairs(transcript)
        assert Path(probe).read_bytes() == b"once"

        real_admission = admission

        def pause_second(messages, *args, **kwargs):
            if any(m.get("tool_call_id") == "second" and (m.get("content") or "").startswith("xxxx") for m in messages):
                raise ContextPause("injected result saturation")
            return real_admission(messages, *args, **kwargs)

        transcript = new_conversation()
        with patch.dict(globals(), chat=lambda m: group, admission=pause_second):
            agent_turn(transcript, "one write then context saturation")
        assert_pairs(transcript)
        assert Path(probe).read_bytes() == b"committed" and not Path(probe + "never").exists()
        assert all(m["content"].startswith("NOT RUN") for m in transcript[-2:])
        assert STATE["pending_check"]

        def block_nudge(messages, *args, **kwargs):
            if messages[-1].get("_generated"):
                raise ContextPause("injected nudge saturation")
            return real_admission(messages, *args, **kwargs)

        transcript = new_conversation()
        with patch.dict(globals(), chat=lambda m, it=iter([one_write, done]): next(it), admission=block_nudge):
            agent_turn(transcript, "write, then pause before verification nudge")
        assert STATE["pending_check"] and not STATE["nudged"]
        assert not any(m.get("_generated") for m in transcript)

        transcript = new_conversation()
        transcript.append({"role": "user", "content": "One task followed by many observations; preserve this exactly."})
        for i in range(8):
            observation = call("read_file", {"path": probe}, f"obs_{i}")
            transcript.extend([observation, receipt(observation["tool_calls"][0], "older source observation " * 80)])
        PLAN = True
        transcript[0] = system_message()
        latest = copy.deepcopy(transcript[-2:])
        compact_request = []

        def summary_request(messages, tools=None, purpose="normal"):
            compact_request.append(copy.deepcopy(messages))
            data, _, _ = build_request(messages, tools, purpose)
            assert json.loads(data)["tools"] == []
            return {"role": "assistant", "content": "Earlier reads found source text. No edit or check was performed."}, "complete"

        with patch.dict(globals(), stream_completion=summary_request):
            assert compact(transcript)
        assert PLAN and transcript[0] == system_message() and transcript[-2:] == latest
        assert sum(m["role"] == "user" and not m.get("_generated") for m in compact_request[0]) == 1
        assert_pairs(transcript)
        transcript.insert(2, {"role": "assistant", "content": "additional old observation " * 300})
        with patch.dict(globals(), stream_completion=summary_request):
            assert compact(transcript)
        assert sum(m["role"] == "user" and not m.get("_generated") for m in transcript) == 1
        PLAN = False
        new_conversation()

        failed_state = [system_message(), {"role": "user", "content": "x" * WINDOW}, {"role": "assistant", "content": "old observation " * 2000}, done]
        old = copy.deepcopy(failed_state)
        with patch.object(HTTP, "open", side_effect=AssertionError("must refuse before HTTP")):
            assert not compact(failed_state)
        assert failed_state == old

        for invalid in (b"\xffbad", b"a\r\nb\nc"):
            _FILE_VERSIONS.clear()
            Path(probe).write_bytes(invalid)
            assert run_tool("edit_file", {"path": probe, "old": "a", "new": "A"}).startswith("ERROR")
            assert Path(probe).read_bytes() == invalid
        _FILE_VERSIONS.clear()
        Path(probe).write_bytes(b"original\n")
        real_fsync = os.fsync

        def change_before_commit(fd):
            Path(probe).write_bytes(b"external\n")
            return real_fsync(fd)

        with patch.object(os, "fsync", change_before_commit):
            assert "changed before" in run_tool("edit_file", {"path": probe, "old": "original", "new": "updated"})
        assert Path(probe).read_bytes() == b"external\n"
        fresh = str(root / "appeared.txt")
        real_link = os.link

        def appeared(source, destination):
            Path(destination).write_bytes(b"appeared meanwhile")
            return real_link(source, destination)

        with patch.object(os, "link", appeared):
            assert run_tool("write_file", {"path": fresh, "content": "must not overwrite"}).startswith("ERROR")
        assert Path(fresh).read_bytes() == b"appeared meanwhile"
        if os.name != "nt":
            Path(probe).chmod(0o755)
            assert not run_tool("edit_file", {"path": probe, "old": "external", "new": "updated"}).startswith("ERROR")
            assert stat.S_IMODE(os.stat(probe).st_mode) == 0o755

        previous_directory = os.getcwd()
        try:
            os.chdir(root)
            Path("FAMILIAR.md").write_bytes(b"x" * 8193)
            try:
                build_system_prompt()
                assert False, "oversized project notes accepted"
            except ValueError:
                pass
        finally:
            os.chdir(previous_directory)
        with patch.dict(globals()), patch.dict(os.environ, {}, clear=True), patch.object(HTTP, "open", return_value=io.BytesIO(b'{"n_ctx":8192}')):
            configure()
            assert WINDOW == 8192 and MAX_TOKENS == 1024 and COMPACT_TOKENS == 512
            assert admission([system_message()]) < 8192
        with patch.dict(globals(), TOKEN_FIELD="max_completion_tokens", STREAM_USAGE=False):
            data, _, _ = build_request(base)
            assert "max_completion_tokens" in json.loads(data) and "stream_options" not in json.loads(data)
        last_scale = TOKEN_SCALE
        measured = estimate(base, active_tools()) * 2
        raw = frame({"content": "calibrate"}, "stop") + b"data: " + encoded({"usage": {"prompt_tokens": measured}, "choices": []}) + b"\n\n"
        with patch.object(HTTP, "open", return_value=io.BytesIO(raw)):
            chat(base)
        assert TOKEN_SCALE > last_scale
        TOKEN_SCALE = last_scale

        with patch.dict(globals(), HISTORY_ENABLED=False), patch.dict(globals(), read_input=lambda *a, it=iter(["first", "unsent", "/tokens", "/quit"]): next(it)), patch.object(HTTP, "open", side_effect=urllib.error.HTTPError(BASE_URL, 400, "context", {}, io.BytesIO(b"context window length exceeded"))) as opener:
            repl()
            assert opener.call_count == 1

        HISTFILE = str(root / "history")
        HISTORY_ENABLED = False
        save_history("private")
        assert load_history() == [] and not Path(HISTFILE).exists()
        HISTORY_ENABLED = True
        save_history("first")
        save_history("two\nlines")
        save_history("/compact")
        assert load_history() == ["first", "two\nlines"]
        HISTORY_ENABLED = False
        before = Path(HISTFILE).read_bytes()
        save_history("not saved")
        assert load_history() == [] and Path(HISTFILE).read_bytes() == before
        HISTFILE = directory
        HISTORY_ENABLED = True
        assert load_history() == []
        save_history("unwritable")
        with patch.object(sys, "stdin", io.StringIO("hello\n")), patch.object(sys, "stdout", io.StringIO()) as output:
            assert read_input("prompt> ", []) == "hello"
            assert output.getvalue() == "prompt> "
            try:
                read_input("prompt> ", [])
                assert False
            except EOFError:
                pass
        assert _layout("abc", 3, 2) == (["ab", "c"], 1, 1)
        assert _interpret_escape(None, ("meta-enter",)) == ("newline",)
        assert _interpret_escape(None, ("csi", "13;2u")) == ("newline",)
        assert _interpret_escape(None, ("csi", "13u")) == ("enter",)
        assert _interpret_escape(None, ("csi", "3~")) == ("delete",)
        rfd, wfd = os.pipe()
        try:
            os.write(wfd, b"pasted\r\ntext\x1b[201~")
            assert _interpret_escape(rfd, ("csi", "200~")) == ("paste", "pasted\ntext")
        finally:
            os.close(rfd)
            os.close(wfd)
        history = {"items": ["one", "two"], "idx": 2, "stash": ""}
        assert _apply_key("", 0, ("up",), history)[0] == "two"
        assert _apply_key("abc", 3, ("newline",), history)[:2] == ("abc\n", 4)
        assert _apply_key("abc", 3, ("backspace",), history)[:2] == ("ab", 2)
        assert _apply_key("", 0, ("eof",), history)[2] == "eof"
        if _RAW_OK:
            with patch.dict(os.environ, {"TERM": "xterm"}), patch.object(sys.stdin, "isatty", return_value=True), patch.object(sys.stdout, "isatty", return_value=True), patch.object(sys.stdin, "fileno", return_value=0), patch.object(termios, "tcgetattr", return_value=["saved"]), patch.object(termios, "tcsetattr") as restore, patch.object(tty, "setraw"), patch.dict(globals(), _edit=lambda *a: (_ for _ in ()).throw(KeyboardInterrupt())):
                try:
                    read_input("x", [])
                except KeyboardInterrupt:
                    pass
                assert restore.call_args[0][2] == ["saved"]


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    argv = sys.argv if argv is None else argv
    if "--version" in argv:
        print(f"familiar {__version__}")
        return
    try:
        if "--selftest" in argv:
            selftest()
            return
        configure()
        init_workspace(argv)
        admission([system_message()])
        repl()
    except (ValueError, OSError, ContextPause) as error:
        print(f"Familiar: {error}", file=sys.stderr)
        if isinstance(error, ContextPause):
            print("Increase the actual server window/working ceiling, lower FAMILIAR_MAX_TOKENS, or shorten FAMILIAR.md.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
