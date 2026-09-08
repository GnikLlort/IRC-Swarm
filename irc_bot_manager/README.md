# IRC Multi-Bot Manager

A polished, production-quality **Python 3 multi-IRC bot manager** for
*authorized* IRC administration, testing and development.

The manager drives many independent IRC clients at once.  A single command can
target **one bot**, a **group of bots**, or **all connected bots**
simultaneously and asynchronously.

```
say-all #test Hello from all bots!
```

makes every connected bot send that message to `#test`.

> ⚠️ **Authorized use only.** This tool exists to help you run bots you are
> authorised to run on networks you are authorised to use, and to test your own
> proxy/IRC infrastructure.  Do **not** use it for flooding, evading bans,
> abuse, or against any network/proxy you do not control.  It contains **no**
> remote-execution, flooding, DDoS, credential-theft, brute-force, exploitation,
> propagation, persistence or stealth features by design.

---

## 1. Requirements

* **Python 3.11+** (spec requires 3.12+; the code uses only stdlib that is
  present in 3.11 — `asyncio.StreamWriter.start_tls`, dataclasses, type hints)
* **PySocks** *(optional — the built-in SOCKS5/HTTP-CONNECT tunnelling is pure
  asyncio and does not require it)*
* **pytest** for the test-suite

There are no other runtime dependencies.

---

## 2. Installation

```bash
cd irc_bot_manager
python3 -m pip install -r requirements.txt
```

Run the test-suite (no public network required — it uses an in-process mock
IRC server):

```bash
cd irc_bot_manager
python3 -m pytest -q
```

---

## 3. Project layout

```
irc_bot_manager/
├── main.py            CLI entry point + event loop + graceful shutdown
├── manager.py         BotManager: proxy pool, assignment, *-all / group ops
├── bot.py             one Bot: lifecycle, reconnect/backoff, throttling, safe cmds
├── irc_client.py      async IRC client (TCP/TLS/proxy, parsing, events)
├── proxy.py           proxy.txt parsing + SOCKS5/HTTP-CONNECT tunnelling + health
├── commands.py        interactive CLI command parsing/dispatch
├── config.py          JSON config loading + validation
├── models.py          dataclasses & enums (Proxy, IrcMessage, statuses, …)
├── logger.py          rotating logs + secret redaction
├── utils.py           async EventEmitter + token-bucket rate limiter
│
├── config.json        example configuration
├── proxy.txt          example proxy pool (entries commented out)
├── requirements.txt
├── README.md
├── logs/              rotating per-bot + manager log files (git-ignored)
└── tests/             pytest suite + in-process mock IRC server
```

Architecture (single central `asyncio` event loop, no OS thread per bot):

```
Main
 └── BotManager
      ├── Bot001 ─ IrcClient ─ ProxyConnection (or direct)
      ├── Bot002 ─ IrcClient ─ ProxyConnection (or direct)
      ├── ...
      └── CLI
```

---

## 4. Configuration (`config.json`)

```jsonc
{
    "bot_count": 5,               // number of bots: bot001..bot005
    "nickname_prefix": "TestBot", // optional; default "Bot" -> Bot001...

    "irc": {
        "server": "irc.example.net",
        "port": 6697,
        "tls": true,
        "verify_tls": false,      // set false for self-signed test servers
        "username": "admin",
        "realname": "IRC Bot Manager",
        "password": null,         // server password (PASS), never logged
        "channels": ["#test"]     // auto-joined on connect
    },

    "proxy_mode": "rotate",       // "unique" | "rotate" | "direct"
    "proxy_file": "proxy.txt",
    "proxy_health_check": true,   // test TCP+handshake before assigning
    "proxy_timeout": 5.0,
    "connect_timeout": 10.0,

    "reassign_failed_proxy": true,          // assign another proxy on failure
    "fallback_direct_on_proxy_failure": false, // only if explicitly enabled
    "direct_when_no_proxy": false,          // when not enough proxies for everyone

    "rate_limit": { "messages_per_second": 2, "burst": 3 }, // per bot

    "reconnect": { "initial_delay": 2, "max_delay": 60, "factor": 2 },

    "groups": {
        "alpha": ["bot001", "bot002"],
        "beta":  ["bot003", "bot004", "bot005"]
    },

    "auto_connect": true,        // connect all bots at startup
    "log_level": "INFO",
    "log_dir": "logs"
}
```

* Every bot gets a unique id (`bot001`, `bot002`, …) padded to the width needed
  by `bot_count`.
* Nicknames are auto-generated from `nickname_prefix` (`TestBot001`, …).
* `reload-config` re-reads this file at runtime (the fleet is *not* resized
  live; operational settings — rate limit, backoff, proxy_mode, groups — are).

---

## 5. `proxy.txt`

One proxy per line.  Supported formats:

```
socks5://HOST:PORT
socks5://USER:PASSWORD@HOST:PORT
http://HOST:PORT
http://USER:PASSWORD@HOST:PORT
```

Blank lines and lines beginning with `#` are ignored, as is surrounding
whitespace.  Every entry is validated; invalid lines are logged without
crashing the program:

```
[WARNING] Invalid proxy on line 7
```

**Passwords are never logged** — the application parses them into private
fields and a log redaction filter scrubs any `scheme://user:pass@host` (or
registered secret) before a line is written.

The shipped `proxy.txt` uses **documentation/test addresses only** (RFC 5737
TEST-NET ranges) and is fully commented out so nothing is contacted by
default.  Replace it with proxies you are authorised to use.

---

## 6. Proxy modes & assignment

| Mode      | Behaviour                                                        |
|-----------|------------------------------------------------------------------|
| `unique`  | each bot gets its own proxy; extra bots stay offline (or direct  |
|           | if `direct_when_no_proxy`) when there aren't enough proxies       |
| `rotate`  | proxies are shared round-robin across all bots                   |
| `direct`  | bots connect with no proxy at all                                |

Automatic assignment maps `bot001 → proxy #01`, `bot002 → proxy #02`, etc.

When `proxy_health_check` is `true`, each proxy is tested (TCP connectivity +
real SOCKS5/HTTP-CONNECT handshake to the IRC server, bounded by
`proxy_timeout`) before assignment and marked `AVAILABLE` / `FAILED`.
`DISABLED` entries are also skipped.

---

## 7. Proxy health / reassignment

When a bot's proxy fails to connect:

1. the proxy is marked `FAILED` and its failure count recorded;
2. if `reassign_failed_proxy` is enabled, another `AVAILABLE` proxy is
   searched for (optionally re-checked), assigned, and the bot reconnects
   through it;
3. the CLI status table is updated.

The manager **never silently drops to a direct connection** — that only happens
if you explicitly set `fallback_direct_on_proxy_failure: true`.

```
bot007
   ↓
Proxy #07 FAILED
   ↓
Proxy #12 AVAILABLE
   ↓
bot007 → Proxy #12
   ↓
reconnect
```

---

## 8. Reconnection

Disconnects trigger automatic reconnects with **exponential backoff**
(`initial_delay`, `factor`, capped at `max_delay`).  The backoff resets after a
successful connection so there is never an infinite rapid reconnect loop.

```
2s  4s  8s  16s  30s  60s
```

---

## 9. Starting the manager

```bash
cd irc_bot_manager
python3 main.py [path/to/config.json]   # defaults to ./config.json
```

Startup banner:

```
=========================================
      IRC MULTI-BOT MANAGER
=========================================

Bots configured: 5   Connected: 0  Connecting: 0  Offline: 5
Type "help" for commands.
```

Press **CTRL+C** (or type `quit` / `exit`) for a graceful shutdown: the CLI
stops, connected bots are told to `QUIT`, sockets close, pending tasks are
cancelled, logs flush and the process exits cleanly with no orphaned tasks.

---

## 10. CLI commands

```
help                  list                       status [<bot>]
proxies               logs <bot>                 reload-proxies
reload-config         quit | exit                (graceful manager exit)

Lifecycle (one bot)     start <bot>  stop <bot>  restart <bot>  quit <bot>
Lifecycle (all bots)    start-all    stop-all    restart-all    quit-all

Messaging
  say <bot> <channel> <message>
  say-all <channel> <message>
  say-group <group> <channel> <message>

Channels
  join <bot> <chan>      join-all <chan>      join-group <grp> <chan>
  part <bot> <chan>      part-all <chan>      part-group <grp> <chan>
```

`say` / `say-group` / `say-all` keep the whole message (including spaces)
intact.

### `status`

```
ID       NICK             STATUS       PROXY
------------------------------------------------------------
bot001   TestBot001       CONNECTED    socks5 #01
bot002   TestBot002       CONNECTING   socks5 #02
bot003   TestBot003       OFFLINE      direct

Connected: 1  Connecting: 1  Offline: 1  Total: 3
```

`status <bot>` prints detailed per-bot state (nick, server, proxy, channels,
messages sent/received, uptime, reconnects, last error).

### `proxies`

```
PROXY STATUS

#01 SOCKS5  127.0.0.1:9050       ASSIGNED bot001
#02 SOCKS5  10.0.0.5:1080        ASSIGNED bot002
#03 HTTP    192.168.1.20:8080    AVAILABLE
#04 SOCKS5  10.0.0.20:1080       FAILED
```

Passwords never appear.

---

## 11. Global (`-all`) commands are concurrent

Every `*-all` command runs across the target bots with `asyncio.gather`, so
they are **not** executed sequentially and one failing bot does **not** stop
the others.  Individual outcomes are collected and reported:

```
[ALL] Sending message…

bot001 ✓
bot002 ✓
bot003 ✗ connection closed
bot004 ✓

Completed: 3/4
```

---

## 12. Bot behaviour & safe channel commands

Each bot is an independent cancellable `asyncio` task tracking its own id,
nickname, username/realname, server/port, TLS state, assigned proxy, channels,
connection state, messages sent/received, uptime, reconnection count and last
error.

While connected, a bot answers only a small set of **safe, authorised**
channel/private commands:

```
!ping    ->  pong
!uptime  ->  up 1h 2m 3s
!info    ->  <bot id/nick/server/status/proxy/counters/uptime>
```

The bots deliberately implement **no** remote-shell, arbitrary command
execution, malware, flooding/DDoS, credential theft, brute-forcing, exploit,
propagation, persistence or stealth/evasion capabilities.  Data received from
the IRC server is never executed and never trusted.

---

## 13. Logging

Rotating logs are written under `logs/`:

```
logs/manager.log      # everything (root handler)
logs/bot001.log       # only bot001's records
logs/bot002.log       # only bot002's records
...
```

Logged: connection attempts, proxy assignment, proxy failures, IRC
registration, JOIN/PART, messages, commands, disconnects, reconnection
attempts and exceptions.  **Never** logged: proxy passwords, IRC passwords or
any authentication secret (enforced both by never logging them and by a
redaction filter).

---

## 14. Rate limiting

Outbound chat is throttled **per bot** with an asyncio token bucket
(`messages_per_second` sustained rate, `burst` capacity).  Each bot owns its
own bucket, so throttling one bot never blocks another.

---

## 15. Reloading

* `reload-proxies` re-reads `proxy.txt`, validates entries, **preserves
  existing assignments** where proxies still exist, adds newly available
  proxies and marks removed/failed ones — without unnecessarily disconnecting
  healthy bots.
* `reload-config` re-reads `config.json` and applies new operational settings.

---

## 16. Security considerations

* Input from IRC servers is parsed defensively; malformed input never crashes
  a bot.
* Enforced maximum IRC line length; oversized / undelimited stream data is
  discarded to avoid unbounded buffering.
* Connection and proxy timeouts bound every socket operation.
* No `eval`, no `exec`, no shell execution anywhere.
* Proxy / IRC credentials are held privately and redacted from all logs.
* A single bot failure (including proxy failure) never crashes the manager.

---

## 17. Troubleshooting

| Symptom | Likely fix |
|---|---|
| `config not found: …` | run from the `irc_bot_manager` dir or pass a config path |
| `[WARNING] Invalid proxy on line N` | fix that line in `proxy.txt` |
| bots stay `CONNECTING` | check the IRC server/port; with TLS verify certs or set `verify_tls:false` for a self-signed test server |
| no proxies load | enable real entries in `proxy.txt` (the shipped one is commented out) |
| `proxy_mode` change doesn't reconnect | run `reload-proxies` / restart |
| tests can't import modules | run `python -m pytest` from inside `irc_bot_manager` (the suite adds the package dir to `sys.path`) |

---

## 18. Testing

`python -m pytest` from `irc_bot_manager`.  The suite uses an in-process mock
IRC server (see `tests/mock_server.py`) — **no public IRC network is required**.

Covered: configuration loading/invalidation, proxy parsing & invalid proxies,
SOCKS5/HTTP-CONNECT handshakes, health checks, proxy assignment
(unique/rotate/direct), IRC parsing/serialization, PING/PONG, JOIN, PRIVMSG,
bot lifecycle, manager lifecycle, `say-all` / `join-all` / `part-all`, group
commands, reconnection, proxy reassignment, rate limiting, and graceful
shutdown.
