"""What Claude Code may read and edit in a project, worked out from the settings files.

Covers the Read and Edit permission rules, the default access to the project folder,
additionalDirectories, WebFetch site rules and the Bash sandbox settings. Hooks such as
an "ask outside project" script can add checks this file cannot see.
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

HOME = Path.home()
MANAGED = Path(r"C:\Program Files\ClaudeCode\managed-settings.json") if sys.platform == "win32" else \
    Path("/Library/Application Support/ClaudeCode/managed-settings.json")
SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv"}
READ_TOOLS = {"Read"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
RULE = re.compile(r"^([\w-]+)(?:\((.*)\))?$")


def project_root(cwd=None):
    """The git root above cwd, else cwd: where Claude Code looks for .claude/settings*.json."""
    p = Path(cwd or Path.cwd()).resolve()
    return next((d for d in (p, *p.parents) if (d / ".git").exists()), p)


def posix(p):
    """Path as Claude Code matches it: forward slashes, and C:\\x becomes /c/x on Windows."""
    s = p.as_posix() if hasattr(p, "as_posix") else Path(p).as_posix()
    return "/" + s[0].lower() + s[2:] if len(s) > 1 and s[1] == ":" else s


def settings(root):
    files = [("managed", MANAGED),
             ("user", HOME / ".claude/settings.json"),
             ("project", root / ".claude/settings.json"),
             ("local", root / ".claude/settings.local.json")]
    out = []
    for source, f in files:
        try:
            out.append((source, json.loads(f.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            pass
    return out


def glob_regex(g):
    out, i = "", 0
    while i < len(g):
        if g.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif g.startswith("**", i):
            out, i = out + ".*", i + 2
        elif g[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif g[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(g[i]), i + 1
    return out


def path_rule(pattern, kind, root):
    """(literal base folder, regex) for a Read/Edit pattern, following the gitignore-style anchors."""
    if pattern.startswith("//"):
        base, rest = Path("/"), pattern[2:]
    elif pattern.startswith("~/"):
        base, rest = HOME, pattern[2:]
    elif pattern.startswith("/"):
        # ponytail: "/" anchors at the settings source; for user settings this treats it as the project too
        base, rest = root, pattern[1:]
    else:
        base, rest = root, pattern.removeprefix("./")
        segs = rest.rstrip("/").split("/")
        if len(segs) == 1 or (kind != "allow" and len(segs) == 2 and segs[1] == "**"):
            rest = "**/" + rest  # bare names, and deny/ask "dir/**", match at any depth
    rest = rest.rstrip("/")
    literal = []
    for seg in rest.split("/"):
        if any(c in seg for c in "*?["):
            break
        literal.append(seg)
    rx = re.compile(re.escape(posix(base).rstrip("/")) + "/" + glob_regex(rest) + "$")
    return base.joinpath(*literal), rx


def rules(root):
    out = []
    for source, s in settings(root):
        for kind in ("deny", "ask", "allow"):
            for text in (s.get("permissions") or {}).get(kind, []):
                m = RULE.match(text.strip())
                if not m:
                    continue
                r = {"kind": kind, "tool": m[1], "arg": m[2], "text": text, "source": source}
                if m[2] and m[1] in READ_TOOLS | EDIT_TOOLS:
                    r["base"], r["rx"] = path_rule(m[2], kind, root)
                out.append(r)
    return out


def decide(path, tools, rs, workdirs, mode):
    """(decision, why) for an action on path: deny beats ask beats allow, then the defaults."""
    chain = [posix(p) for p in (path, *path.parents)]
    for kind in ("deny", "ask", "allow"):
        for r in rs:
            if r["kind"] == kind and r["tool"] in tools and (
                    not r["arg"] or any(r["rx"].match(p) for p in chain)):
                return kind, f'{r["text"]} ({r["source"]} settings)'
    inside = any(path == w or w in path.parents for w in workdirs)
    if mode == "bypassPermissions" or (tools is READ_TOOLS and inside):
        return "allow", "default: inside the project" if inside else "default: bypassPermissions mode"
    if inside and mode == "acceptEdits":
        return "allow", "default: acceptEdits mode"
    return "ask", "default: Claude Code asks first"


def access(root, limit=3000):
    root = Path(root)
    rs = rules(root)
    merged = {}
    for _, s in settings(root):
        for k, v in (s.get("permissions") or {}).items():
            merged.setdefault(k, []).extend(v if isinstance(v, list) else [v])
    mode = (merged.get("defaultMode") or ["default"])[-1]
    extra = [Path(d).expanduser() if Path(d).expanduser().is_absolute() else (root / d).resolve()
             for d in merged.get("additionalDirectories", [])]
    workdirs = [root, *extra]

    def status(p):
        read, read_why = decide(p, READ_TOOLS, rs, workdirs, mode)
        edit, edit_why = decide(p, EDIT_TOOLS, rs, workdirs, mode)
        return {"read": read, "readWhy": read_why, "edit": edit, "editWhy": edit_why}

    count = 0

    def node(d, depth):
        nonlocal count
        item = {"name": d.name, "path": str(d.relative_to(root)), "dir": True,
                **status(d / "a-new-file"), "children": []}
        try:
            entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            entries = []
        for e in entries:
            count += 1
            if count > limit or depth > 8:
                item["truncated"] = True
                break
            if e.is_dir() and not e.is_symlink():
                if e.name not in SKIP:
                    item["children"].append(node(e, depth + 1))
                continue
            s = status(e)
            if (s["read"], s["edit"]) != (item["read"], item["edit"]):  # only files that differ from their folder
                item["children"].append({"name": e.name, "path": str(e.relative_to(root)), "dir": False, **s})
        return item

    outside = [{"path": str(r["base"]), "rule": r["text"], "kind": r["kind"], "source": r["source"],
                "action": "read" if r["tool"] in READ_TOOLS else "edit"}
               for r in rs if "base" in r and not (r["base"] == root or root in r["base"].parents)]
    outside += [{"path": str(d), "rule": "additionalDirectories", "kind": "allow", "source": "settings",
                 "action": "read"} for d in extra]

    sites = [{"site": (r["arg"] or "any site").removeprefix("domain:"), "kind": r["kind"], "source": r["source"],
              "tool": r["tool"]} for r in rs if r["tool"] in ("WebFetch", "WebSearch")]

    sandbox = {}
    for source, s in settings(root):
        sb = s.get("sandbox") or {}
        if "enabled" in sb:
            sandbox["enabled"] = sb["enabled"]
        for k, v in (sb.get("filesystem") or {}).items():
            sandbox.setdefault(k, []).extend(v)
    if sys.platform == "win32" and sandbox.get("enabled"):
        sandbox["enabled"] = False  # the sandbox exists only on macOS, Linux and WSL2; native Windows ignores it
        sandbox["windows"] = True

    tree = node(root, 0)
    tree["name"] = str(root).replace(str(HOME), "~", 1)
    return {"root": str(root), "mode": mode, "tree": tree, "outside": outside, "sites": sites, "sandbox": sandbox}


def host(url):
    return (urlparse(url or "").hostname or "").lower()


def trusted(root, url):
    """True when the project's own settings allow WebFetch for the url's site."""
    h = host(url)
    return bool(h) and any(r["tool"] == "WebFetch" and r["kind"] == "allow" and r["source"] in ("project", "local")
                           and r["arg"] == "domain:" + h for r in rules(Path(root)))


def trust(root, site):
    """Save an allow rule for the site in the project's .claude/settings.local.json."""
    f = Path(root) / ".claude/settings.local.json"
    data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    if "WebFetch(domain:" + site + ")" not in allow:
        allow.append("WebFetch(domain:" + site + ")")
        f.parent.mkdir(exist_ok=True)
        f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
