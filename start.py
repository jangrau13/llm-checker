"""Start the LLM Checker in the background, unless it already runs.

Called by the plugin's SessionStart hook, so nobody needs a second terminal. The server
keeps running after Claude Code closes; the next session finds it and reuses it. When the
running server is an older version, for example after a plugin update, it is stopped and
replaced. The server opens the page itself as soon as a card waits and no page is open.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT = 1308  # also in server.py and hooks/hooks.json
HERE = Path(__file__).resolve().parent
DATA = Path.home() / ".llm-checker"


VERSION = json.loads((HERE / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))["version"]


def running():
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def parse(version):
    return tuple(int(p) for p in version.split(".") if p.isdigit())


def running_version():
    """The version of the server on the port; (0,) for versions before 0.3.1, which cannot say."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/version", timeout=2) as r:
            return parse(json.loads(r.read())["version"])
    except Exception:
        return (0,)


def wait(up):
    for _ in range(50):  # up to 5 s
        if running() == up:
            return
        time.sleep(0.1)


if running() and running_version() < parse(VERSION):
    # Only replace older versions, so two projects with different versions do not take turns.
    token_file = DATA / "server.token"
    token = token_file.read_text(encoding="utf-8").strip() if token_file.exists() else ""
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/quit",
                                                      json.dumps({"token": token}).encode()), timeout=2)
    except Exception:
        pass  # versions before 0.3.1 cannot be stopped this way: stop them by hand once
    wait(False)

if not running():
    DATA.mkdir(exist_ok=True)
    log = open(DATA / "server.log", "a", encoding="utf-8")
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
              if sys.platform == "win32" else {"start_new_session": True})
    subprocess.Popen([sys.executable, str(HERE / "server.py")], cwd=os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()),
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, **detach)
    wait(True)  # so the first tool call finds the server
