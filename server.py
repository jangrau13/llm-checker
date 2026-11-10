"""LLM Checker: an approval page for Claude Code hooks, on macOS, Linux and Windows.

Tool calls and tool results wait here until they are sent or blocked in the browser
at http://127.0.0.1:1308. A Bash call is first sent back to Claude Code with a request
to explain the command; the retry then shows that explanation next to the command.

The plugin's SessionStart hook runs start.py, which starts this server in the background
and opens the page. The tool hooks are HTTP hooks, so they need no shell or curl.
"""

import itertools
import json
import re
import secrets
import shlex
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import access

PORT = 1308  # also in start.py and hooks/hooks.json
REJECTED = "[withheld by LLM Checker]"

# How Claude Code should explain a Bash command. Editable on the page; saved in STYLE_FILE.
# Language rules after the STE80 style of github.com/Dodaios/ste80-humanizer (MIT):
# Simplified Technical English (ASD-STE100), applied about 80% strictly.
DEFAULT_STYLE = (
    "Explain the command to a science student who knows the project but does not know computer science terms. "
    "Use Simplified Technical English:\n"
    "- Keep each sentence to one idea and 20 words. Use no more than four sentences.\n"
    "- Use active voice, simple tenses and plain words. Avoid phrasal verbs.\n"
    "- Use the same term for the same thing. Put conditions and warnings first.\n"
    "- Replace jargon such as pipe, stdout, flag, regex, process and shell with plain language. Briefly explain "
    "named tools such as git, grep and curl.\n"
    "Cover this information in order:\n"
    "- Warn about output that may contain passwords, keys, personal data, unpublished results or internal data.\n"
    "- Name each file and folder the command reads. State which content will go to Claude.\n"
    "- Name websites that receive data and files that the command deletes or overwrites."
)
DATA = Path.home() / ".llm-checker"  # survives plugin updates, which replace the plugin folder
STYLE_FILE = DATA / "explain-style.txt"
TOKEN_FILE = DATA / "server.token"  # lets start.py stop this server when a newer version starts
TOKEN = secrets.token_hex(16)
VERSION = json.loads((Path(__file__).parent / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))["version"]
WATCH_FILE = DATA / "watchlist.txt"  # the user's private terms; never part of any answer to Claude

# Built-in detectors for data that should rarely reach Claude. Tuned to catch, not to be exact:
# a false alarm costs a click, a miss costs the data.
DETECTORS = [
    ("API key or token", r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{30,}|\bxox[abpr]-[A-Za-z0-9-]{10,}"
                         r"|\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{35}|\b[a-z]{2,8}_(?:live|test)_[A-Za-z0-9_]{12,}"),
    ("password or secret in settings", r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d|pwd)\b['\"]?\s*[:=]\s*['\"]?[^\s'\",;&)]{6,}"),
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("web token (JWT)", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    # Personal data (PII), international. Names are not detectable by pattern: put the names that
    # matter on the watch list.
    ("email address", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ("phone number", r"(?<![\w.])\+\d{1,3}[ .-]?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,4}\b"  # international, +country code
                     r"|\(\d{3}\)\s?\d{3}[-.]\d{4}\b"                                   # North America (555) 123-4567
                     r"|(?<![\w.])0\d{1,4}[ /-]\d{2,4}(?:[ -]\d{2,4}){1,3}\b"),         # national trunk 0 formats
    ("payment card number", r"\b(?:\d[ -]?){12,18}\d\b", "luhn"),
    ("IBAN", r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b"),
    ("national ID number",
     r"\b\d{3}-\d{2}-\d{4}\b"                                     # US social security number
     r"|\b[A-CEGHJ-PR-TW-Z]{2} ?\d{2} ?\d{2} ?\d{2} ?[A-D]\b"     # UK national insurance number
     r"|\b756\.\d{4}\.\d{4}\.\d{2}\b"                             # Swiss AHV number
     r"|\b[12] ?\d{2} ?\d{2} ?\d{2} ?\d{3} ?\d{3} ?\d{2}\b"       # French social security number (NIR)
     r"|\b[A-Z]{6}\d{2}[A-EHLMPRST]\d{2}[A-Z]\d{3}[A-Z]\b"        # Italian codice fiscale
     r"|\b[XYZ]?\d{7,8}-?[A-HJ-NP-TV-Z]\b"                        # Spanish DNI / NIE
     r"|\b\d{6}[-+]\d{4}\b"                                       # Swedish / Danish / Finnish personal number
     r"|\b\d{4} \d{4} \d{4}\b"                                    # Indian Aadhaar
     r"|\b\d{3}\.\d{3}\.\d{3}-\d{2}\b"),                          # Brazilian CPF
    ("IP address", r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"),
    ("coordinates", r"-?\d{1,2}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}"),
]
# ponytail: patterns only; a proper PII engine (for example Microsoft Presidio) would also find names,
# postal addresses and dates of birth, at the cost of a large dependency.


def luhn(text):
    """The checksum every payment card number passes; filters out long numbers in data tables."""
    digits = [int(c) for c in text if c.isdigit()][::-1]
    return sum(d if i % 2 == 0 else (d * 2 - 9 if d > 4 else d * 2) for i, d in enumerate(digits)) % 10 == 0


CHECKS = {"luhn": luhn}


def watchlist_text():
    return WATCH_FILE.read_text(encoding="utf-8") if WATCH_FILE.exists() else ""


class Pattern:
    """A regex plus an optional check that a match must pass (for example the card checksum)."""

    def __init__(self, rx, check=None):
        self.rx, self.check = re.compile(rx) if isinstance(rx, str) else rx, check

    def finditer(self, text):
        return (m for m in self.rx.finditer(text) if not self.check or self.check(m.group(0)))

    def sub(self, repl, text):
        return self.rx.sub(lambda m: repl if not self.check or self.check(m.group(0)) else m.group(0), text)


def patterns():
    """(label, pattern) for the detectors and the watch list. Lines starting with re: are regexes."""
    out = [(d[0], Pattern(d[1], CHECKS.get(d[2]) if len(d) > 2 else None)) for d in DETECTORS]
    for line in watchlist_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rx = re.compile(line[3:].strip()) if line.startswith("re:") else re.compile(re.escape(line), re.I)
        except re.error:
            continue  # a broken regex on the list must not stop the checker
        out.append(("watch list", Pattern(rx)))
    return out


def strings(value, key=None):
    """Every string inside a tool result, except type tags and file data such as images."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from strings(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)
    elif isinstance(value, str) and key not in ("type", "base64", "data"):
        yield value


def findings(value):
    """What the detectors and the watch list find, with counts, for the card."""
    found = {}
    for label, rx in patterns():
        for text in strings(value):
            for m in rx.finditer(text):
                entry = found.setdefault((label, m.group(0)), {"label": label, "match": m.group(0), "count": 0})
                entry["count"] += 1
    return list(found.values())


def scrub(value, key=None):
    """The same tool result with every finding replaced, keeping the shape Claude Code expects."""
    if isinstance(value, dict):
        return {k: scrub(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str) and key not in ("type", "base64", "data"):
        for label, rx in patterns():
            value = rx.sub(f"[removed: {label}]", value)
    return value


PRIVATE = (".llm-checker",)  # tool calls that mention the checker's own data folder are refused

FAILURE_STOP = "You blocked a tool error. Rewind past this step before sending another message."
FAILURE_NOTE = "Claude has not read this error. Block it, then rewind past this step before sending another message."


def style():
    return STYLE_FILE.read_text(encoding="utf-8").strip() if STYLE_FILE.exists() else DEFAULT_STYLE


def protected(root):
    """The Read rules from the settings that block or question reading: what the user does not want read."""
    seen = []
    for r in access.rules(Path(root)):
        if r["tool"] == "Read" and r["kind"] in ("deny", "ask") and r["arg"] and r["arg"] not in seen:
            seen.append(r["arg"])
    return seen


def summary_request(scripts=(), root=None):
    """Sent to Claude Code as the reason for holding back a Bash call it has not explained yet."""
    root = root or access.project_root()
    read_first = ("This command runs these script files: " + ", ".join(scripts) + ". Read each of them "
                  "before explaining the command. ") if scripts else ""
    blocked = protected(root)
    boundary = (f"\n\nThe project folder is {root}. Do not read outside this folder. The Read rules also protect "
                "these paths: " + (", ".join(blocked) or "none") + ". Relative paths start at the project folder; "
                "paths beginning with ~/ or // use their stated location. Begin with a warning if the command or "
                "one of its scripts reads a protected path.")
    return ("Explain this command before running it. " + read_first + "Call Bash again with the explanation in "
            "`description`. Follow the instructions below in place of the usual description guidance." + boundary +
            "\n\nExplanation instructions:\n" + style())


SCRIPT_EXT = (".py", ".sh", ".bash", ".zsh", ".r", ".js", ".mjs", ".ts", ".pl", ".rb", ".ps1", ".jl")


def scripts_in(command, cwd):
    """Script files a Bash command runs: words ending in a script extension or starting with ./ that exist."""
    try:
        words = shlex.split(command or "")
    except ValueError:  # unbalanced quotes: fall back to plain splitting
        words = (command or "").split()
    found = []
    for w in words:
        w = w.strip(";&|()<>")
        if w and not w.startswith("-") and (w.lower().endswith(SCRIPT_EXT) or w.startswith("./")):
            # ponytail: paths are taken relative to the session folder, so "cd sub && python x.py" is missed,
            # and so is "python -m module"; Claude is still asked to explain the command itself
            p = (Path(cwd or ".") / Path(w).expanduser()).resolve()
            if p.is_file() and str(p) not in found:
                found.append(str(p))
    return found


def read_script(path, limit=100_000):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return text if len(text) <= limit else text[:limit] + f"\n... ({len(text) - limit} more characters not shown)"

# Steps that skip the page: prompts the user typed, and file operations VS Code already shows.
# The content of a Read result still waits on the page, because that is what leaves the machine.
# ToolSearch only loads Claude Code's own tool descriptions, no user data.
# Web calls: Claude Code's own permission prompt already asks before a search or fetch runs.
WEB = ("WebFetch", "WebSearch")
PASS_THROUGH = {("UserPromptSubmit", None)} | {
    ("PreToolUse", t) for t in ("Read", "Edit", "Write", "ToolSearch", *WEB)} | {
    ("PostToolUse", t) for t in ("Edit", "Write", "ToolSearch")}

paused = [False]  # "Turn off" on the page; back on after a server restart, the safe default
# "Cautiousness high" on the page. Off by default: Bash commands run without a card or an explanation,
# web results go to Claude unseen, and only Bash output and the other tool results wait on the page.
bash_before = [False]
inflight = {}  # (event, tool_use_id) -> answer slot; ponytail: never pruned, a few KB per session
last_poll = [0.0]  # when a page last asked for /state
last_open = [0.0]


def open_page():
    webbrowser.open(f"http://127.0.0.1:{PORT}")


def ensure_page_open():
    """Open the page when a card waits and no page has polled for a few seconds."""
    now = time.time()
    if now - last_poll[0] > 5 and now - last_open[0] > 30:
        last_open[0] = now
        open_page()

lock = threading.Lock()
ids = itertools.count(1)
pending = {}  # id -> {"item": dict, "event": Event, "approve": bool}
history = []  # decided items, newest first
explained = set()  # (session, agent) whose next Bash call carries the explanation Claude Code was asked for
last_root = [access.project_root()]  # project of the latest hook call; the sidebar shows its access


def view(hook, request=None):
    """The part of the hook input the page shows."""
    event = hook.get("hook_event_name")
    if request:
        return "Requested a command explanation", {
            "command": hook["tool_input"].get("command"), "request sent to Claude": request}
    if event == "UserPromptSubmit":
        return "Prompt sent to Claude", hook.get("prompt")
    if event == "PreToolUse":
        return f"Claude requested {hook.get('tool_name')}", hook.get("tool_input")
    if event == "PostToolUse":
        return f"{hook.get('tool_name')} result waiting to be sent", hook.get("tool_response")
    if event == "PostToolUseFailure":
        return f"{hook.get('tool_name')} error waiting to be sent", {"error": hook.get("error")}
    return event, hook


def redact(value, key=None):
    """Replace every string with REJECTED but keep the shape the tool output schema expects."""
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str) and key != "type":
        return REJECTED
    return value


def answer(hook, approve, request=None, clean=False):
    """Hook JSON output. Approve = empty output, so the normal permission flow still applies.
    request: the explanation request that holds back an unexplained Bash call.
    clean: send the tool result with the detector and watch list findings removed."""
    event = hook.get("hook_event_name")
    if approve and clean and event == "PostToolUse":
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                       "updatedToolOutput": scrub(hook.get("tool_response"))}}
    if approve and not request:
        return {}
    if event == "UserPromptSubmit":
        return {"decision": "block", "reason": "The user blocked this item in LLM Checker."}
    if event == "PreToolUse":
        reason = request or "The user blocked this tool call in LLM Checker."
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}
    if event == "PostToolUse":
        # ponytail: if the redacted value does not match the tool's output schema,
        # Claude Code ignores it and sends the original; check new tools before relying on it.
        return {"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": redact(hook.get("tool_response")),
        }}
    if event == "PostToolUseFailure":
        # Claude Code offers no way to replace error text, so blocking stops Claude before its next request.
        return {"continue": False, "stopReason": FAILURE_STOP}
    return {"decision": "block", "reason": "The user blocked this item in LLM Checker."}


def handle_hook(hook):
    """The hook answer for a single call; waits on the page when the call needs a decision."""
    event, tool = hook.get("hook_event_name"), hook.get("tool_name") or ""
    # The checker's own folder holds the watch list: Claude must never read it, whatever the tool.
    # ponytail: plain text match; a command that builds the path (for example with wildcards) gets past it
    if event == "PreToolUse" and any(p in s for s in strings(hook.get("tool_input")) for p in PRIVATE):
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": "This request would expose private LLM Checker data."}}
    # MCP calls: only what comes back and goes to Claude waits, not the call itself.
    if (event, tool or None) in PASS_THROUGH or (event == "PreToolUse" and tool.startswith("mcp__")):
        return {}
    root = access.project_root(hook.get("cwd"))
    last_root[0] = root
    if paused[0]:  # turned off on the page: logged, everything passes
        with lock:
            history.insert(0, {"id": next(ids), "event": event, "tool": tool, "content": view(hook)[1],
                               "title": f"{view(hook)[0]} (checker off)", "approved": True,
                               "automatic": True})
        return {}
    if not bash_before[0] and (event == "PreToolUse" and tool == "Bash" or tool in WEB):
        return {}  # the command runs (its output still waits on the page); web results go straight to Claude
    url = (hook.get("tool_input") or {}).get("url")
    if tool == "WebFetch" and access.trusted(root, url):
        with lock:  # a site trusted for this project: logged, no click needed
            history.insert(0, {"id": next(ids), "event": event, "tool": "WebFetch",
                               "title": f"Sent automatically because {access.host(url)} is trusted for this project",
                               "content": hook.get("tool_input"), "approved": True, "automatic": True})
        return {}
    bash = event == "PreToolUse" and tool == "Bash"
    # Keyed by session, not by command: Claude often adjusts the command when it retries.
    # The card shows the command that will really run, so the change cannot slip past.
    key = (hook.get("session_id"), hook.get("agent_id"))  # a subagent's Bash calls are explained on their own
    with lock:
        ask_summary = bash and key not in explained
        if bash:
            explained.discard(key)  # the retry is gated once; the session's next Bash call asks again
    scripts = scripts_in((hook.get("tool_input") or {}).get("command"), hook.get("cwd")) if bash else []
    request = summary_request(scripts, root) if ask_summary else None
    title, content = view(hook, request)
    item = {"id": next(ids), "event": event, "title": title, "content": content,
            "session": hook.get("session_id"), "tool": tool, "input": hook.get("tool_input")}
    if hook.get("agent_id"):  # the step comes from a subagent, not from the main conversation
        item["agent"] = hook.get("agent_type") or "Subagent"
    if ask_summary:  # automatic: logged in the history, no click needed
        with lock:
            explained.add(key)
            history.insert(0, {**item, "approved": True, "automatic": True})
        return answer(hook, True, request)
    if bash:
        item["summary"] = hook["tool_input"].get("description")
        # Shown on the card from your disk, so you can check the code yourself; not sent to Claude by this.
        item["scripts"] = [{"path": p, "content": read_script(p)} for p in scripts]
    if event == "PostToolUseFailure":
        item["note"] = FAILURE_NOTE
    if event in ("PostToolUse", "PostToolUseFailure"):
        item["findings"] = findings(content)  # what is about to reach Claude, checked locally
        item["cleanable"] = event == "PostToolUse" and bool(item["findings"])
    # File content is already visible in the editor: without "Cautiousness high" it waits only with findings.
    if event == "PostToolUse" and tool in ("Read", "NotebookRead") and not item["findings"] and not bash_before[0]:
        with lock:
            history.insert(0, {**item, "title": f"{tool} result sent: no findings", "approved": True, "automatic": True})
        return {}
    if tool == "WebFetch" and access.host(url):
        item["site"] = access.host(url)
    entry = {"item": item, "event": threading.Event(), "approve": False, "clean": False, "root": root}
    with lock:
        pending[item["id"]] = entry
    ensure_page_open()
    entry["event"].wait()  # blocks the hook, and with it Claude, until a decision
    return answer(hook, entry["approve"], clean=entry["clean"])


class Handler(BaseHTTPRequestHandler):
    def send(self, status, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            data = b""
            while size := int(self.rfile.readline().split(b";")[0], 16):
                data += self.rfile.read(size)
                self.rfile.readline()
            self.rfile.readline()
        else:
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        return json.loads(data or b"{}")

    def do_GET(self):
        if self.path == "/state":
            last_poll[0] = time.time()
            with lock:
                auto = sum(1 for h in history if h.get("automatic"))
                sent = sum(1 for h in history if h.get("approved")) - auto
                self.send(200, {"pending": [p["item"] for p in pending.values()], "history": history[:50],
                                "paused": paused[0], "bash_before": bash_before[0],
                                "totals": {"sent": sent, "blocked": len(history) - sent - auto, "auto": auto}})
        elif self.path == "/version":
            self.send(200, {"version": VERSION})
        elif self.path == "/access":
            self.send(200, access.access(last_root[0]))
        elif self.path == "/style":
            self.send(200, {"style": style(), "default": DEFAULT_STYLE})
        elif self.path == "/watchlist":
            self.send(200, {"watchlist": watchlist_text(), "detectors": [label for label, _ in DETECTORS]})
        elif self.path == "/":
            self.send(200, PAGE.read_text(encoding="utf-8"), "text/html; charset=utf-8")
        else:
            self.send(404, {})

    def do_POST(self):
        if self.path == "/hook":
            hook = self.read_json()
            # The same call can arrive twice, when the hooks are both in a settings file and in the
            # plugin. All copies of a call wait for, and get, the answer to the first copy.
            k = (hook.get("hook_event_name"), hook.get("tool_use_id"))
            if not k[1]:
                self.send(200, handle_hook(hook))
                return
            with lock:
                slot = inflight.get(k)
                first = slot is None
                if first:
                    slot = inflight[k] = {"done": threading.Event(), "answer": {}}
            if first:
                slot["answer"] = handle_hook(hook)
                slot["done"].set()
            else:
                slot["done"].wait()
            self.send(200, slot["answer"])
        elif self.path == "/decide":
            body = self.read_json()
            with lock:
                entry = pending.pop(body.get("id"), None)
                if entry and body.get("trust") and entry["item"].get("site"):
                    access.trust(entry["root"], entry["item"]["site"])  # super like: saved for this project
                    entry["item"]["title"] += "; site trusted for this project"
                if entry:
                    entry["approve"] = bool(body.get("approve"))
                    entry["clean"] = bool(body.get("clean")) and entry["item"].get("cleanable", False)
                    if entry["clean"]:
                        entry["item"]["title"] += "; findings removed before sending"
                    entry["item"]["approved"] = entry["approve"]
                    history.insert(0, entry["item"])
                    entry["event"].set()
            self.send(200 if entry else 404, {})
        elif self.path == "/watchlist":
            text = str(self.read_json().get("watchlist", ""))
            WATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
            WATCH_FILE.write_text(text, encoding="utf-8")
            self.send(200, {"watchlist": watchlist_text()})
        elif self.path == "/bash-before":
            bash_before[0] = bool(self.read_json().get("on"))
            self.send(200, {"bash_before": bash_before[0]})
        elif self.path == "/pause":
            paused[0] = bool(self.read_json().get("paused"))  # cards already waiting keep waiting
            self.send(200, {"paused": paused[0]})
        elif self.path == "/quit":
            # A newer version takes over the port: start.py proves it may stop this server with the token.
            ok = secrets.compare_digest(str(self.read_json().get("token", "")), TOKEN)
            self.send(200 if ok else 403, {})
            if ok:
                threading.Thread(target=self.server.shutdown).start()
        elif self.path == "/style":
            text = str(self.read_json().get("style", "")).strip()
            STYLE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STYLE_FILE.write_text((text or DEFAULT_STYLE) + "\n", encoding="utf-8")
            self.send(200, {"style": style()})
        else:
            self.send(404, {})

    def log_message(self, *args):
        pass


PAGE = Path(__file__).with_name("page.html")  # read on every request, so page edits need no restart

if __name__ == "__main__":
    print(f"LLM Checker {VERSION}: http://127.0.0.1:{PORT}")
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(TOKEN, encoding="utf-8")  # for start.py; Claude is denied this folder
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.serve_forever()
    srv.server_close()
