# LLM Checker

A Claude Code plugin that shows, in a browser page, what Claude Code is about to do and what it is about to send to Claude, and waits until you send or block it.

## Motivation

Claude Code gives Claude access to your computer: it runs commands, reads files and fetches web pages for you. Without a computer science degree, it is often hard to tell what a command does or which of your data ends up with Claude. LLM Checker tries to close that gap in two ways:

1. It translates every Bash command into plain English before it runs, following instructions you write yourself.
2. It shows what is about to be sent to Claude (file contents, command output, web pages), as far as Claude Code lets a plugin see it, and lets you block it.

- Bash commands wait as a card. Claude Code first explains each command in plain words, following instructions you can edit on the page. When a command runs a script (for example `python analyse.py` or `./run.sh`), Claude Code is asked to read the script and explain it too, and the card shows the script's code.
- Tool results (file contents, command output, web pages, MCP answers) wait before they go to Claude. PDFs, images, spreadsheets, HTML, Markdown and code are shown as such.
- Swipe right or press the right arrow to send. Swipe left or press the left arrow to block. For a website, swipe up or press the up arrow to send it and trust the site in this project.
- The left side shows which folders Claude Code can read and edit, worked out from your settings files.

Prompts you type, and Read, Edit and Write calls, do not wait: you see those in Claude Code itself. The content of a Read result does wait.

## Requirements

- Claude Code 2.1.295 or later. Older versions let tool calls through when the page is not running.
- Python 3 on the PATH as `python3`, `python` or `py`.
- On Windows: Git for Windows, so the start-up hook can run. Without it, start the page yourself with `py server.py` in the plugin folder.

## Install

For everyone who opens a project, without any command: add this to the project's `.claude/settings.json`. Claude Code then offers the plugin once the folder is trusted.

```json
{
  "extraKnownMarketplaces": {
    "llm-checker": { "source": { "source": "github", "repo": "jangrau13/llm-checker" } }
  },
  "enabledPlugins": { "llm-checker@llm-checker": true }
}
```

For yourself, in a terminal (this also covers the VS Code extension, which has no `/plugin` command):

```
claude plugin marketplace add jangrau13/llm-checker
claude plugin install llm-checker@llm-checker
```

In the Claude Code terminal app you can type the same as `/plugin marketplace add jangrau13/llm-checker` and `/plugin install llm-checker@llm-checker`. To run the checker in a single project only, run the install command inside that project's folder and add `--scope project`. Then start a new session: the page opens at http://127.0.0.1:1308.

## Limits

- The page sees only what passes through hooks. The system prompt, CLAUDE.md files, pasted images, `@file` mentions, editor selections and context compaction reach Claude without stopping on the page.
- Blocking a tool result replaces its text before it goes to Claude, but the tool has already run.
- Claude Code cannot replace the text of a tool error. Blocking an error stops Claude before it reads it, but the error stays in the conversation: rewind to before that step, or it goes to Claude with your next message.

Your explanation instructions and the server log are stored in `~/.llm-checker/`.

## License

MIT, see [LICENSE](LICENSE). The software comes without any warranty; use it at your own risk.

## Test

```
python3 test_server.py
```
