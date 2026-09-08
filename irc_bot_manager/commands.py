"""Interactive CLI command parsing and execution.

Each command produces ``(should_exit: bool, output: str)``.  Output formatting
lives here so the actual event-loop front-end in :mod:`main` stays tiny.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

import config as config_mod
from config import AppConfig
from manager import BotManager
from models import CommandError, ConfigError

EXIT = ("exit",)
HELP_NEEDED = object()


HELP = f"""
IRC MULTI-BOT MANAGER — COMMANDS
================================
help                         show this help
list                         list all configured bots
status                       status summary for every bot
status <bot>                 detailed status for one bot
proxies                      proxy status table

Lifecycle (single)
  start <bot>                start one bot
  stop  <bot>                stop one bot (no reconnect)
  restart <bot>              restart one bot
  quit  <bot>                send QUIT to one bot

Lifecycle (all)
  start-all                  start every bot
  stop-all                   stop every bot
  restart-all                restart every bot
  quit-all                   QUIT every connected bot

Messaging
  say <bot> <chan> <msg>     send msg to a channel
  say-all <chan> <msg>       every connected bot sends msg
  say-group <grp> <chan> <m> all bots in group send msg

Channels
  join <bot> <chan> / join-all <chan> / join-group <grp> <chan>
  part <bot> <chan> / part-all <chan> / part-group <grp> <chan>

Utility
  logs <bot>                 tail this bot's log file
  reload-proxies             re-read proxy.txt
  reload-config              re-read config.json
  exit | quit                shutdown the manager gracefully
""".strip("\n")


def _words(line: str) -> list:
    return line.strip().split()


async def _run(manager: BotManager, line: str) -> Tuple[bool, str]:
    """Execute one user command line. Returns (should_exit, output)."""
    line = line.strip()
    if not line:
        return False, ""
    words = _words(line)
    verb = words[0].lower()

    try:
        if verb in ("help", "?"):
            return False, HELP
        if verb == "list":
            return False, "\n".join(f"{bid:8} {b.nickname:16} {b.status.value}"
                                     for bid, b in manager.bots.items())
        if verb == "status":
            if len(words) > 1:
                return False, manager.detail(words[1])
            return False, manager.status_table()
        if verb == "proxies":
            return False, manager.proxy_table()
        if verb == "exit":
            return True, "shutting down…"
        if verb == "quit":
            if len(words) == 1:
                return True, "shutting down…"
            res = await manager.quit_bot(words[1])
            return False, _fmt_results([res])
        if verb == "logs":
            return False, _tail_log(words[1] if len(words) > 1 else "")

        # ---- single-bot lifecycle ----
        if verb in ("start", "stop", "restart") and len(words) >= 2:
            bid = words[1]
            if verb == "start":
                res = await manager.start_bot(bid)
            elif verb == "stop":
                res = await manager.stop_bot(bid)
            else:
                res = await manager.restart_bot(bid)
            return False, _fmt_results([res])

        # ---- fleet lifecycle ----
        if verb in ("start-all", "stop-all", "restart-all", "quit-all",
                    "start-group", "stop-group", "restart-group", "quit-group"):
            group = None
            action = verb.split("-")[0]
            if verb.endswith("-group"):
                if len(words) < 2:
                    raise CommandError(f"usage: {verb} <group>")
                group = words[1]
            if action == "start":
                res = await manager.start_all(group)
            elif action == "stop":
                res = await manager.stop_all(group)
            elif action == "restart":
                res = await manager.restart_all(group)
            else:
                res = await manager.quit_all(group)
            return False, _fmt_results(res)

        # ---- messaging (message is the remainder, may contain spaces) ----
        if verb in ("say", "say-all", "say-group"):
            if verb == "say":
                parts = line.split(maxsplit=3)
                if len(parts) < 4:
                    raise CommandError("usage: say <bot> <channel> <message>")
                _, target, chan, msg = parts
            elif verb == "say-all":
                parts = line.split(maxsplit=2)
                if len(parts) < 3:
                    raise CommandError("usage: say-all <channel> <message>")
                _, chan, msg = parts
                target = None
            else:
                parts = line.split(maxsplit=3)
                if len(parts) < 4:
                    raise CommandError("usage: say-group <group> <channel> <message>")
                _, target, chan, msg = parts
            results = await manager.say(target, chan, msg)
            return False, manager._summarize(results)

        # ---- join / part ----
        if verb in ("join", "join-all", "join-group", "part", "part-all", "part-group"):
            base = verb
            target = None
            if verb == "join-all":
                if len(words) < 2:
                    raise CommandError("usage: join-all <channel>")
                chan = words[1]
                base = "join"
            elif verb == "part-all":
                if len(words) < 2:
                    raise CommandError("usage: part-all <channel>")
                chan = words[1]
                base = "part"
            elif verb == "join-group":
                if len(words) < 3:
                    raise CommandError("usage: join-group <group> <channel>")
                target, chan = words[1], words[2]
                base = "join"
            elif verb == "part-group":
                if len(words) < 3:
                    raise CommandError("usage: part-group <group> <channel>")
                target, chan = words[1], words[2]
                base = "part"
            else:
                if len(words) < 3:
                    raise CommandError(f"usage: {verb} <bot> <channel>")
                target, chan = words[1], words[2]
            if base == "join":
                results = await manager.join(target, chan)
            else:
                results = await manager.part(target, chan)
            return False, _fmt_results(results)

        if verb == "reload-proxies":
            out = await manager.reload_proxies()
            return False, out
        if verb == "reload-config":
            path = os.path.join(os.getcwd(), "config.json")
            if not os.path.isfile(path):
                return False, f"no config.json found in {os.getcwd()}"
            cfg = config_mod.load_config_file(path)
            out = await manager.reload_config(cfg)
            return False, out
        if verb in ("status-all",):
            return False, manager.status_table()

        raise CommandError(f"unknown command {words[0]!r}; type 'help'")
    except ConfigError as exc:
        return False, f"[config error] {exc}"
    except CommandError as exc:
        return False, f"{exc}"
    except Exception as exc:  # noqa: BLE001 - never crash the CLI
        return False, f"error: {exc}"


def _fmt_results(results) -> str:
    if not results:
        return "no bots matched"
    lines = []
    for r in results:
        mark = "✓" if r.ok else "✗"
        detail = "" if r.ok else f" {r.message}"
        lines.append(f"{r.bot_id} {mark}{detail}")
    ok = sum(1 for r in results if r.ok)
    return "\n".join(lines) + f"\n\nCompleted: {ok}/{len(results)}"


def _tail_log(bot_id: str, lines: int = 40) -> str:
    if not bot_id:
        return "usage: logs <bot>"
    from logger import facility
    path = os.path.join(facility().log_dir, f"{bot_id}.log")
    if not os.path.isfile(path):
        # fall back to manager log which receives propagated records
        return f"no dedicated log yet for {bot_id}; see logs/manager.log"
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        content = fh.read().splitlines()
    return "\n".join(content[-lines:]) if content else "(empty log)"


async def run_command(manager: BotManager, line: str) -> Tuple[bool, str]:
    return await _run(manager, line)
