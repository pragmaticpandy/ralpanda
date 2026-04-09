# ralpanda

An autonomous agent loop for Claude Code. Decomposes plans into a DAG of tasks, executes them sequentially with automatic retries, runs review checks, and manages splits — all from a curses TUI. Zero dependencies beyond Python 3, git, and the Claude CLI.

## Install

Install globally:
```bash
cd ~/.claude/skills && git clone https://github.com/pragmaticpandy/ralpanda.git
```

## Use

`/ralpanda queue up tasks for this plan foo.md`

## Run tests

Tests MUST be run before considering a change complete.

```bash
cd scripts && python3 -m unittest discover -s ralpanda -p 'test_*.py' -v
```
