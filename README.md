# IRC-Swarm

A production-quality **Python 3 multi-IRC bot manager** for *authorized* IRC
administration, testing and development.  It manages many independent IRC bots
at once (each optionally behind its own SOCKS5/HTTP-CONNECT proxy) and lets a
single command target **one bot, a group, or all bots** concurrently.

```
say-all #test Hello from all bots!
```

See **[`irc_bot_manager/README.md`](irc_bot_manager/README.md)** for full
documentation (installation, configuration, `proxy.txt`, CLI commands,
testing, security).

### Quick start

```bash
cd irc_bot_manager
python3 -m pip install -r requirements.txt
python3 -m pytest -q      # full test-suite, no public network needed
python3 main.py           # interactive manager
```

> Authorized-use only — no flooding, evasion, exploitation or automation of
> abuse is included or supported.
