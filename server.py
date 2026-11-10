"""LLM Checker: an approval page for Claude Code hooks, on macOS, Linux and Windows.

Tool calls and tool results wait here until they are sent or blocked in the browser
at http://127.0.0.1:1308. A Bash call is first sent back to Claude Code with a request
to explain the command; the retry then shows that explanation next to the command.

The plugin's SessionStart hook runs start.py, which starts this server in the background
and opens the page. The tool hooks are HTTP hooks, so they need no shell or curl.
"""

import itertools
import json
import shlex
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import access

PORT = 1308  # also in start.py and hooks/hooks.json
REJECTED = "[withheld: rejected in the approval page]"

# How Claude Code should explain a Bash command. Editable on the page; saved in STYLE_FILE.
# Language rules after the STE80 style of github.com/Dodaios/ste80-humanizer (MIT):
# Simplified Technical English (ASD-STE100), applied about 80% strictly.
DEFAULT_STYLE = (
    "Write for a science student who knows the project's folders and files but not computer "
    "science terms. Use Simplified Technical English, about 80% strict:\n"
    "- One idea in each sentence. At most 20 words in a sentence. At most four sentences.\n"
    "- Active voice and simple tenses. Plain words with one meaning. No phrasal verbs.\n"
    "- Use one term for one thing. Put a condition first: \"If X, Y\". Put a warning first.\n"
    "- No jargon such as pipe, stdout, flag, regex, process or shell. If you must name a tool "
    "such as git, grep or curl, say in a few words what it does.\n"
    "Give this information, in this order:\n"
    "1. A warning, if the output that goes to Claude can contain passwords, keys, personal data, "
    "unpublished results or other internal data.\n"
    "2. The files and folders that the command reads, by path, and which part of their content goes to Claude.\n"
    "3. If the command sends data over the internet, the website. If it deletes or overwrites files, which files."
)
DATA = Path.home() / ".llm-checker"  # survives plugin updates, which replace the plugin folder
STYLE_FILE = DATA / "explain-style.txt"

FAILURE_STOP = ("Stopped in LLM Checker: you blocked the error output of a tool. It is still in the "
                "conversation and goes to Claude with your next message, unless you rewind to before this step.")
FAILURE_NOTE = ("Blocking an error stops Claude before it reads it. The error stays in the conversation: "
                "rewind to before this step, or it goes to Claude with your next message.")


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
                  "first, so your explanation covers what they do. ") if scripts else ""
    blocked = protected(root)
    boundary = (f"\n\nThe project folder is {root}. The user does not want files outside it read, nor these "
                "files and folders (patterns from the settings' Read rules, relative to the project folder "
                "unless they start with ~/ or //): " + (", ".join(blocked) or "none") + ". If the command, or a "
                "script it runs, reads any of them, start the explanation with a clear warning that names them.")
    return ("Not run yet: the user wants an explanation first. " + read_first + "Then call Bash again for "
            "this step with the explanation in `description`. Write it exactly as the user's instructions "
            "below say: they replace the usual short description and take precedence over any other "
            "guidance on how to describe a command." + boundary + "\n\nThe user's instructions:\n" + style())


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
PASS_THROUGH = {("UserPromptSubmit", None)} | {("PreToolUse", t) for t in ("Read", "Edit", "Write", "ToolSearch")} | {
    ("PostToolUse", t) for t in ("Edit", "Write", "ToolSearch")}

paused = [False]  # "Turn off" on the page; back on after a server restart, the safe default
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
        return "Asked Claude Code to explain this Bash command first (automatic)", {
            "command": hook["tool_input"].get("command"), "sent to Claude": request}
    if event == "UserPromptSubmit":
        return "Your prompt -> Claude", hook.get("prompt")
    if event == "PreToolUse":
        return f"Claude asks to run {hook.get('tool_name')}", hook.get("tool_input")
    if event == "PostToolUse":
        return f"Result of {hook.get('tool_name')} -> Claude", hook.get("tool_response")
    if event == "PostToolUseFailure":
        return f"Error of {hook.get('tool_name')} -> Claude", {"error": hook.get("error")}
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


def answer(hook, approve, request=None):
    """Hook JSON output. Approve = empty output, so the normal permission flow still applies.
    request: the explanation request that holds back an unexplained Bash call."""
    if approve and not request:
        return {}
    event = hook.get("hook_event_name")
    if event == "UserPromptSubmit":
        return {"decision": "block", "reason": "Rejected in the approval page."}
    if event == "PreToolUse":
        reason = request or "The user rejected this tool call in the approval page."
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
    return {"decision": "block", "reason": "Rejected in the approval page."}


def handle_hook(hook):
    """The hook answer for a single call; waits on the page when the call needs a decision."""
    event, tool = hook.get("hook_event_name"), hook.get("tool_name") or ""
    # MCP calls: only what comes back and goes to Claude waits, not the call itself.
    if (event, tool or None) in PASS_THROUGH or (event == "PreToolUse" and tool.startswith("mcp__")):
        return {}
    root = access.project_root(hook.get("cwd"))
    last_root[0] = root
    if paused[0]:  # turned off on the page: logged, everything passes
        with lock:
            history.insert(0, {"id": next(ids), "event": event, "tool": tool, "content": view(hook)[1],
                               "title": f"{view(hook)[0]} (checker off, automatic)", "approved": True})
        return {}
    url = (hook.get("tool_input") or {}).get("url")
    if tool == "WebFetch" and access.trusted(root, url):
        with lock:  # a site trusted for this project: logged, no click needed
            history.insert(0, {"id": next(ids), "event": event, "tool": "WebFetch",
                               "title": f"{access.host(url)} is trusted in this project (automatic)",
                               "content": hook.get("tool_input"), "approved": True})
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
        item["agent"] = hook.get("agent_type") or "subagent"
    if ask_summary:  # automatic: logged in the history, no click needed
        with lock:
            explained.add(key)
            history.insert(0, {**item, "approved": True})
        return answer(hook, True, request)
    if bash:
        item["summary"] = hook["tool_input"].get("description")
        # Shown on the card from your disk, so you can check the code yourself; not sent to Claude by this.
        item["scripts"] = [{"path": p, "content": read_script(p)} for p in scripts]
    if event == "PostToolUseFailure":
        item["note"] = FAILURE_NOTE
    if tool == "WebFetch" and access.host(url):
        item["site"] = access.host(url)
    entry = {"item": item, "event": threading.Event(), "approve": False, "root": root}
    with lock:
        pending[item["id"]] = entry
    ensure_page_open()
    entry["event"].wait()  # blocks the hook, and with it Claude, until a decision
    return answer(hook, entry["approve"])


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
                self.send(200, {"pending": [p["item"] for p in pending.values()], "history": history[:50],
                                "paused": paused[0]})
        elif self.path == "/access":
            self.send(200, access.access(last_root[0]))
        elif self.path == "/style":
            self.send(200, {"style": style(), "default": DEFAULT_STYLE})
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
                    entry["item"]["title"] += " (site now trusted)"
                if entry:
                    entry["approve"] = bool(body.get("approve"))
                    entry["item"]["approved"] = entry["approve"]
                    history.insert(0, entry["item"])
                    entry["event"].set()
            self.send(200 if entry else 404, {})
        elif self.path == "/pause":
            paused[0] = bool(self.read_json().get("paused"))  # cards already waiting keep waiting
            self.send(200, {"paused": paused[0]})
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
    print(f"Approval page: http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
