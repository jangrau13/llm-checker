"""LLM Checker: an approval page for Claude Code hooks, on macOS, Linux and Windows.

Tool calls and tool results wait here until they are sent or blocked in the browser
at http://127.0.0.1:1308. A Bash call is first sent back to Claude Code with a request
to explain the command; the retry then shows that explanation next to the command.

The plugin's SessionStart hook runs start.py, which starts this server in the background
and opens the page. The tool hooks are HTTP hooks, so they need no shell or curl.
"""

import itertools
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import access

PORT = 1308  # also in start.py and hooks/hooks.json
REJECTED = "[withheld: rejected in the approval page]"

# How Claude Code should explain a Bash command. Editable on the page; saved in STYLE_FILE.
DEFAULT_STYLE = (
    "Up to three plain sentences for a science student who knows the project's folders and files "
    "but not computer science terms. Name the files and folders by their paths, say what happens "
    "to them and what the user gets back, and say whether anything is deleted, overwritten or sent "
    "over the internet (and to which website). Avoid jargon such as pipe, stdout, flag, regex, "
    "process or shell; if a tool name such as git, grep or curl is unavoidable, say in a few words "
    "what it is for."
)
DATA = Path.home() / ".llm-checker"  # survives plugin updates, which replace the plugin folder
STYLE_FILE = DATA / "explain-style.txt"


def style():
    return STYLE_FILE.read_text(encoding="utf-8").strip() if STYLE_FILE.exists() else DEFAULT_STYLE


def summary_request():
    """Sent to Claude Code as the reason for holding back a Bash call it has not explained yet."""
    return ("Not run yet: the user wants an explanation first. Call Bash again with exactly the same "
            "command and write the explanation in `description`, following these instructions from "
            "the user:\n\n" + style())

# Steps that skip the page: prompts the user typed, and file operations VS Code already shows.
# The content of a Read result still waits on the page, because that is what leaves the machine.
PASS_THROUGH = {("UserPromptSubmit", None)} | {("PreToolUse", t) for t in ("Read", "Edit", "Write")} | {
    ("PostToolUse", t) for t in ("Edit", "Write")}

lock = threading.Lock()
ids = itertools.count(1)
pending = {}  # id -> {"item": dict, "event": Event, "approve": bool}
history = []  # decided items, newest first
explained = set()  # (session, command) of Bash calls Claude Code was asked to explain
last_root = [access.project_root()]  # project of the latest hook call; the sidebar shows its access


def view(hook, ask_summary):
    """The part of the hook input the page shows."""
    event = hook.get("hook_event_name")
    if ask_summary:
        return "Asked Claude Code to explain this Bash command first (automatic)", {
            "command": hook["tool_input"].get("command"), "sent to Claude": summary_request()}
    if event == "UserPromptSubmit":
        return "Your prompt -> Claude", hook.get("prompt")
    if event == "PreToolUse":
        return f"Claude asks to run {hook.get('tool_name')}", hook.get("tool_input")
    if event == "PostToolUse":
        return f"Result of {hook.get('tool_name')} -> Claude", hook.get("tool_response")
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


def answer(hook, approve, ask_summary):
    """Hook JSON output. Approve = empty output, so the normal permission flow still applies."""
    if approve and not ask_summary:
        return {}
    event = hook.get("hook_event_name")
    if event == "UserPromptSubmit":
        return {"decision": "block", "reason": "Rejected in the approval page."}
    if event == "PreToolUse":
        reason = summary_request() if ask_summary else "The user rejected this tool call in the approval page."
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
    return {"decision": "block", "reason": "Rejected in the approval page."}


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
            with lock:
                self.send(200, {"pending": [p["item"] for p in pending.values()], "history": history[:50]})
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
            event, tool = hook.get("hook_event_name"), hook.get("tool_name") or ""
            # MCP calls: only what comes back and goes to Claude waits, not the call itself.
            if (event, tool or None) in PASS_THROUGH or (event == "PreToolUse" and tool.startswith("mcp__")):
                self.send(200, {})
                return
            root = access.project_root(hook.get("cwd"))
            last_root[0] = root
            url = (hook.get("tool_input") or {}).get("url")
            if hook.get("tool_name") == "WebFetch" and access.trusted(root, url):
                with lock:  # a site trusted for this project: logged, no click needed
                    history.insert(0, {"id": next(ids), "event": hook.get("hook_event_name"), "tool": "WebFetch",
                                       "title": f"{access.host(url)} is trusted in this project (automatic)",
                                       "content": hook.get("tool_input"), "approved": True})
                self.send(200, {})
                return
            bash = hook.get("hook_event_name") == "PreToolUse" and hook.get("tool_name") == "Bash"
            key = (hook.get("session_id"), (hook.get("tool_input") or {}).get("command"))
            with lock:
                ask_summary = bash and key not in explained
                explained.discard(key)  # the retry is gated once; a later identical call asks again
            title, content = view(hook, ask_summary)
            item = {"id": next(ids), "event": hook.get("hook_event_name"), "title": title,
                    "content": content, "session": hook.get("session_id"),
                    "tool": hook.get("tool_name"), "input": hook.get("tool_input")}
            if ask_summary:  # automatic: logged in the history, no click needed
                with lock:
                    explained.add(key)
                    history.insert(0, {**item, "approved": True})
                self.send(200, answer(hook, True, True))
                return
            if bash:
                item["summary"] = hook["tool_input"].get("description")
            if hook.get("tool_name") == "WebFetch" and access.host(url):
                item["site"] = access.host(url)
            entry = {"item": item, "event": threading.Event(), "approve": False, "root": root}
            with lock:
                pending[item["id"]] = entry
            entry["event"].wait()  # blocks the hook, and with it Claude, until a decision
            self.send(200, answer(hook, entry["approve"], False))
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
