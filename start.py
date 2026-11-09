"""Start the LLM Checker in the background and open it in the browser, unless it already runs.

Called by the plugin's SessionStart hook, so nobody needs a second terminal. The server
keeps running after Claude Code closes; the next session finds it and reuses it.
"""

import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

PORT = 1308  # also in server.py and hooks/hooks.json
HERE = Path(__file__).resolve().parent
DATA = Path.home() / ".llm-checker"


def running():
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


if not running():
    DATA.mkdir(exist_ok=True)
    log = open(DATA / "server.log", "a", encoding="utf-8")
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
              if sys.platform == "win32" else {"start_new_session": True})
    subprocess.Popen([sys.executable, str(HERE / "server.py")], cwd=os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()),
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, **detach)
    for _ in range(50):  # wait up to 5 s, so the first tool call finds the page
        if running():
            break
        time.sleep(0.1)
    webbrowser.open(f"http://127.0.0.1:{PORT}")
