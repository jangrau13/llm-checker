# LLM Checker

A browser review page for Claude Code commands and tool results. LLM Checker holds each item until you send or block it.

## Operation

Claude Code can run commands, read files and fetch web pages on your computer. LLM Checker explains commands before they run and displays outgoing tool results supported by the plugin API.

- When command review is on, each Bash command waits on a card. Claude Code explains the command using editable instructions. If the command runs a script such as `python analyse.py` or `./run.sh`, the card also shows the script and the explanation covers its behavior. The default instructions use Simplified Technical English based on [ste80-humanizer](https://github.com/Dodaios/ste80-humanizer).
- Tool results wait before Claude receives them. The page renders file contents, command output, web pages, MCP answers, PDFs, images, spreadsheets, HTML, Markdown and code.
- Local checks scan each result for sensitive data and private watch-list terms. Findings appear on the card and are removed when you send the result. Choose **Send original** to keep them. The private watch list remains in `~/.llm-checker`.
- Swipe right or press the Right Arrow key to send. Swipe left or press the Left Arrow key to block. Swipe up or press the Up Arrow key to send a website result and trust its site for the project.
- The access panel shows the read and edit rules calculated from your Claude Code settings.

Prompts and Read, Edit and Write calls pass through because Claude Code already displays them. Content returned by Read waits for review.

## Requirements

- Claude Code 2.1.295 or later. Older versions let tool calls through when the page is not running.
- Python 3 on the PATH as `python3`, `python` or `py`.
- Git for Windows when running on Windows. If it is unavailable, start the page with `py server.py` from the plugin folder.

## Install

Add the following settings to `.claude/settings.json` to offer the plugin to everyone who opens the project. Claude Code offers the plugin after they trust the folder.

```json
{
  "extraKnownMarketplaces": {
    "llm-checker": { "source": { "source": "github", "repo": "jangrau13/llm-checker" } }
  },
  "enabledPlugins": { "llm-checker@llm-checker": true }
}
```

Run these terminal commands for a personal installation or the VS Code extension:

```
claude plugin marketplace add jangrau13/llm-checker
claude plugin install llm-checker@llm-checker
```

The Claude Code terminal app also accepts `/plugin marketplace add jangrau13/llm-checker` and `/plugin install llm-checker@llm-checker`. Add `--scope project` to the install command to limit it to the current project. Start a new session and open http://127.0.0.1:1308.

## Limits

- The page sees data exposed through hooks. System prompts, CLAUDE.md files, pasted images, `@file` mentions, editor selections and compacted context bypass the page.
- Blocking a tool result replaces its text after the tool has run.
- Claude Code does not allow plugins to replace tool errors. Block an error to stop Claude, then rewind past that step before sending another message.

Your explanation instructions and the server log are stored in `~/.llm-checker/`.

## License

[MIT](LICENSE). The software has no warranty.

## Test

```
python3 test_server.py
```
