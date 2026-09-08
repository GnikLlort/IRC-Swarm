"""IRC Multi-Bot Manager — entry point.

Run with::

    python main.py [path/to/config.json]

then type ``help`` for available commands.  CTRL+C performs a graceful
shutdown (QUIT connected bots, cancel tasks, flush logs).
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading

import config as config_mod
from logger import setup as setup_logging
from manager import BotManager
from commands import run_command

BANNER = """
=========================================
      IRC MULTI-BOT MANAGER
=========================================
"""


def _package_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _resolve_path(base: str, path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.join(base, path)


def _print(text: str = "") -> None:
    print(text)
    sys.stdout.flush()


def _state_printer():
    _shown = {}

    def cb(bot_id: str, state: dict) -> None:
        key = (bot_id, state["status"])
        if _shown.get(bot_id) == key:
            return
        _shown[bot_id] = key
        if state["status"] in ("CONNECTED", "OFFLINE", "STOPPED", "RECONNECTING"):
            print(f"  » {bot_id} {state['status']}", flush=True)

    return cb


async def _cli_loop(manager: BotManager) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    except (NotImplementedError, RuntimeError):
        pass

    lines = asyncio.Queue()
    quit_flag = asyncio.Event()
    _stdin_reader = threading.Thread(
        target=_read_stdin, args=(loop, lines, quit_flag, stop),
        daemon=True, name="cli-stdin")
    _stdin_reader.start()

    _print(BANNER)
    _print(manager.summary())
    _print('Type "help" for commands.')

    try:
        while not stop.is_set():
            getter = asyncio.create_task(lines.get())
            stopper = asyncio.create_task(stop.wait())
            try:
                done, pending = await asyncio.wait(
                    {getter, stopper}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in pending:
                    t.cancel()

            if getter in done:
                try:
                    line = getter.result()
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    line = None
                if line is None:
                    break
                if line.strip():
                    try:
                        should_exit, output = await run_command(manager, line)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        should_exit, output = False, f"error: {exc}"
                    if output:
                        _print(output)
                    if should_exit:
                        break
            else:
                # stop requested (SIGINT)
                break
    finally:
        quit_flag.set()
        _print("\nShutting down…")
        await _shutdown(manager)


def _read_stdin(loop, queue, quit_flag, stop) -> None:
    """Blocking reader running on a daemon thread, feeding the loop queue."""
    try:
        while True:
            if stop.is_set() or quit_flag.is_set():
                return
            try:
                line = input("irc> ")
            except EOFError:
                loop.call_soon_threadsafe(queue.put_nowait, None)
                return
            except Exception:
                return
            loop.call_soon_threadsafe(queue.put_nowait, line)
    finally:
        pass


async def _shutdown(manager: BotManager, reason: str = "Manager shutting down") -> None:
    try:
        await asyncio.wait_for(manager.shutdown(reason=reason), timeout=5)
    except asyncio.TimeoutError:
        # Force-cancel any stragglers so no orphan tasks remain.
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()
    _print("Goodbye.")


async def _amain(config_path: str) -> int:
    cfg = config_mod.load_config_file(config_path)
    package = _package_dir()

    # resolve relative file locations against the package dir so that
    # "python main.py" works regardless of the caller's cwd.
    cfg.proxy_file = _resolve_path(package, cfg.proxy_file)
    cfg.log_dir = _resolve_path(package, cfg.log_dir)

    setup_logging(cfg.log_dir, cfg.log_level)

    manager = BotManager(cfg)
    manager.state_callback = _state_printer()
    await manager.startup()
    await _cli_loop(manager)
    return 0


def main(argv: list = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    config_path = argv[0] if argv else os.path.join(_package_dir(), "config.json")
    if not os.path.isfile(config_path):
        print(f"config not found: {config_path}")
        return 1
    try:
        return asyncio.run(_amain(config_path))
    except KeyboardInterrupt:
        return 0
    except config_mod.ConfigError as exc:
        print(f"[config error] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
