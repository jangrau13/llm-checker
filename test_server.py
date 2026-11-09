"""Round trip: a hook call blocks until the page decides. Run from the plugin folder: python3 test_server.py"""

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import server

opened = []
server.open_page = lambda: opened.append(True)  # no real browser tabs during the test
TMP = Path(__file__).resolve().parent / "tmp"  # git-ignored scratch folder
TMP.mkdir(exist_ok=True)

srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"


def post(path, body):
    with urllib.request.urlopen(base + path, json.dumps(body).encode()) as r:
        return json.loads(r.read())


def hook_then_decide(hook, approve):
    out = {}
    t = threading.Thread(target=lambda: out.update(post("/hook", hook)))
    t.start()
    time.sleep(0.2)
    assert t.is_alive(), "hook must wait for a decision"
    item = json.loads(urllib.request.urlopen(base + "/state").read())["pending"][0]
    post("/decide", {"id": item["id"], "approve": approve})
    t.join(2)
    return item, out


ls = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "s", "tool_input": {"command": "ls"}}
ls_explained = {**ls, "tool_input": {"command": "ls", "description": "Lists the files in this folder."}}
reason = lambda out: out["hookSpecificOutput"]["permissionDecisionReason"]

# Pre-step: the first call goes straight back to Claude Code with the request to explain it.
assert reason(post("/hook", ls)) == server.summary_request()
# The retry waits on the page with the explanation, even when Claude changed the command;
# approving lets the normal permission flow run.
changed = {**ls, "tool_input": {"command": "ls -la", "description": "Lists the files in this folder."}}
item, out = hook_then_decide(changed, True)
assert item["summary"] == "Lists the files in this folder." and item["content"]["command"] == "ls -la" and out == {}
assert opened, "a waiting card opens the page when none is open"

# The same call twice (hooks in a settings file and in the plugin): a single card, the same answer for both.
twice = {**ls_explained, "tool_use_id": "toolu_1"}
server.explained.add("s")
outs = []
threads = [threading.Thread(target=lambda: outs.append(post("/hook", twice))) for _ in range(2)]
for t in threads:
    t.start()
time.sleep(0.3)
waiting = json.loads(urllib.request.urlopen(base + "/state").read())["pending"]
assert len(waiting) == 1, waiting
post("/decide", {"id": waiting[0]["id"], "approve": True})
for t in threads:
    t.join(2)
assert outs == [{}, {}], outs
assert post("/hook", {"hook_event_name": "PreToolUse", "tool_name": "ToolSearch", "tool_input": {}}) == {}
# The next identical call asks for an explanation again, in the style saved on the page.
server.STYLE_FILE = TMP / "explain-style-test.txt"
post("/style", {"style": "One sentence, in German."})
assert reason(post("/hook", ls)).endswith("One sentence, in German.")
server.STYLE_FILE.unlink()
item, out = hook_then_decide(ls_explained, False)
assert reason(out) != server.summary_request()

# Read, Edit and Write calls skip the page; only the content of a Read result waits.
assert post("/hook", {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {}}) == {}
assert post("/hook", {"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_response": {}}) == {}
assert post("/hook", {"hook_event_name": "PreToolUse", "tool_name": "mcp__docs__search", "tool_input": {}}) == {}
assert b"LLM Checker" in urllib.request.urlopen(base + "/").read()

# Access sidebar and trusted sites, on a throwaway project in tmp/.
import shutil

import access

proj = TMP / "access-test"
shutil.rmtree(proj, ignore_errors=True)
for d in (".git", ".claude", "sources", "results"):
    (proj / d).mkdir(parents=True)
(proj / "private.txt").write_text("not for Claude")
(proj / ".claude/settings.json").write_text(json.dumps({"permissions": {
    "allow": ["Edit(/results/**)"], "deny": ["Edit(/sources/**)", "Read(private.txt)"]}}))
tree = {c["name"]: c for c in access.access(proj)["tree"]["children"]}
assert tree["results"]["edit"] == "allow" and tree["sources"]["edit"] == "deny", tree
assert tree["sources"]["read"] == "allow" and tree["private.txt"]["read"] == "deny", tree

fetch = {"hook_event_name": "PreToolUse", "tool_name": "WebFetch", "cwd": str(proj / "results"),
         "tool_input": {"url": "https://example.org/page", "prompt": "summarise"}}
t = threading.Thread(target=post, args=("/hook", fetch))
t.start()
time.sleep(0.2)
item = json.loads(urllib.request.urlopen(base + "/state").read())["pending"][0]
assert item["site"] == "example.org"
post("/decide", {"id": item["id"], "approve": True, "trust": True})  # super like
t.join(2)
assert "WebFetch(domain:example.org)" in (proj / ".claude/settings.local.json").read_text()
assert post("/hook", fetch) == {}, "a trusted site no longer waits"
shutil.rmtree(proj)

# Windows: paths are matched in /c/... form, and a chunked hook request body is read whole.
import http.client
from pathlib import PureWindowsPath

assert access.posix(PureWindowsPath(r"C:\Users\anna\project")) == "/c/Users/anna/project"
conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
conn.request("POST", "/hook", body=iter([b'{"hook_event_name": "PreToolUse", ', b'"tool_name": "Read"}']),
             encode_chunked=True, headers={"Content-Type": "application/json"})
assert json.loads(conn.getresponse().read()) == {}

bash_out = {"stdout": "secret", "stderr": "", "interrupted": False, "isImage": False}
_, out = hook_then_decide({"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_response": bash_out}, False)
assert out["hookSpecificOutput"]["updatedToolOutput"] == {**bash_out, "stdout": server.REJECTED, "stderr": server.REJECTED}
assert post("/hook", {"hook_event_name": "UserPromptSubmit", "prompt": "hi"}) == {}

# A failed command waits too; blocking it stops Claude, since error text cannot be replaced.
failure = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_input": {"command": "cat key"},
           "error": "Exit code 1\nAPI_KEY=abc"}
item, out = hook_then_decide(failure, False)
assert item["content"] == {"error": "Exit code 1\nAPI_KEY=abc"} and item["note"] == server.FAILURE_NOTE
assert out == {"continue": False, "stopReason": server.FAILURE_STOP}
assert hook_then_decide(failure, True)[1] == {}
print("ok")
