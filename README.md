# irc-server

A real IRC-style chat server speaking RFC 2812 over raw `socket` +
threads: registration (`NICK`/`USER`), MOTD, channels with membership
tracking, `PRIVMSG` broadcast and private messaging, `TOPIC`, `NAMES`,
`WHO`, `PART`, `QUIT`, and full server prefixing
(`:nick!user@host ...`).

## Why it's special

The interesting part of IRC isn't the syntax — it's the *fan-out*.
Every broadcast is a tiny exercise in ordering and exclusion: senders
don't receive their own messages, non-members never see channel
traffic, a leaver sees their own `PART` before their membership
disappears, and a quitter's channel-mates are told exactly once. This
server makes each of those ordering guarantees explicit (see the
comment in `_cmd_part`: send → remove → broadcast), and the test suite
proves isolation with a **canary pattern** instead of sleeps: after
alice's channel message, a client outside the channel triggers its own
line — if the message had leaked, it would arrive *first* and the
"next line" assertion would fail.

## Architecture

```
irc_server/
├── message.py   line parsing/formatting: prefix, command, params,
│                trailing parameters, 512-byte line cap, nick rules
├── channel.py   membership keyed by lower-cased nick (original
│                spelling kept for display), topic storage
└── server.py    IRCServer + Client: registration gate → 001/002/003
                 + MOTD (375/372/376); dispatch for JOIN/PART/PRIVMSG/
                 TOPIC/NAMES/WHO/PING/PONG/QUIT; numerics 451/461/421/
                 401/403/433/432/442; per-client send lock; thread
                 per connection; graceful shutdown
```

Protocol highlights:

- **Registration gate.** `NICK` and `USER` may arrive in any order;
  only when both are present does the server send `001`–`003` plus the
  MOTD block. Channel commands before that → `451 You have not
  registered`.
- **Nick collision.** Case-insensitive uniqueness → `433`; syntax
  violations (`9lives`, `#chan`) → `432`. A registered client can
  rename itself and everyone (including itself) sees
  `:old!user@host NICK :new`.
- **Broadcast rules.** `PRIVMSG` to a channel reaches every member
  *except* the sender; `TOPIC`/`PART` reach everyone (including the
  originator); a private message reaches exactly one socket; `NAMES`
  (`353`+`366`) and `WHO` (`352`+`315`) replies go only to the
  requester.
- **Clean departures.** `QUIT` broadcasts to channel-mates, removes
  memberships, then closes the socket — so the drop path can never
  emit a duplicate `QUIT`. A vanishing connection (no `QUIT`) is
  treated the same way for everyone else.
- **Concurrency.** Each `Client` serializes its own writes with a send
  lock, so concurrent broadcasts can't interleave half-lines.

## Runnable example

`demo.py` runs three phases — register two clients, join and topic a
channel, then exchange channel and private messages:

```python
import socket
from irc_server import IRCServer

with IRCServer() as server:
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    reader = sock.makefile("rb")
    sock.sendall(b"NICK alice\r\nUSER alice 0 * :Alice\r\n")
    # <- :irc.localhost 001 alice :Welcome to the irc.localhost IRC Network ...
    # ... 002, 003, 375, 372, 376 (MOTD)
    sock.sendall(b"JOIN #chat\r\n")
    # <- :alice!alice@127.0.0.1 JOIN :#chat
    # <- :irc.localhost 353 alice = #chat :alice
    # <- :irc.localhost 366 alice #chat :End of /NAMES list
    sock.sendall(b"PRIVMSG #chat :hello bob!\r\n")
    # bob's socket receives:
    # <- :alice!alice@127.0.0.1 PRIVMSG #chat :hello bob!
```

Actual output of `python demo.py` (port varies per run):

```
irc://127.0.0.1:49885
[alice] <- :irc.localhost NOTICE * :server ready
[alice] registered (welcome + MOTD received)
[bob] <- :irc.localhost NOTICE * :server ready
[bob] registered (welcome + MOTD received)
[alice] <- :alice!alice@127.0.0.1 JOIN :#chat
[alice] <- :irc.localhost 353 alice = #chat :alice
[alice] <- :irc.localhost 366 alice #chat :End of /NAMES list
[bob]   <- :bob!bob@127.0.0.1 JOIN :#chat
[bob]   <- :irc.localhost 353 bob = #chat :alice bob
[bob]   <- :irc.localhost 366 bob #chat :End of /NAMES list
[alice] <- :bob!bob@127.0.0.1 JOIN :#chat
[alice] <- :alice!alice@127.0.0.1 TOPIC #chat :welcome to oss-100
[bob]   <- :alice!alice@127.0.0.1 TOPIC #chat :welcome to oss-100
[bob]   <- :alice!alice@127.0.0.1 PRIVMSG #chat :hello bob!
[alice] <- :bob!bob@127.0.0.1 PRIVMSG alice :hi alice, private
[alice] <- :bob!bob@127.0.0.1 QUIT :bye
[alice] <- :irc.localhost PONG irc.localhost :done
server: 2 connections, 12 messages, channel members: ['alice']
```

Note the last exchange: alice's `PRIVMSG #chat` after bob quit reaches
nobody, so her very next line is the `PONG` — the canary in action.

## Tests

```
python -m unittest discover -s tests -v
```

28 tests across two modules, all against a live server on an ephemeral
port (threads, clean shutdown, event-driven — no sleeps):

- **protocol** — parse/format roundtrips, trailing parameters, nick
  validation, MOTD on connect, `451` gating before registration,
  `433` case-insensitive nick collision, `432` erroneous nick, `461`
  missing parameters, `421` unknown command, `PING`/`PONG`, registered
  nick-change broadcast, `QUIT` closes the connection.
- **channels** — only members receive channel messages (4-client
  canary), private message reaches exactly one client (double canary),
  `NAMES` lists members in join order, `QUIT` removes from channels,
  `TOPIC` set and served to joiners (`332`), off-channel topic → `442`,
  `PART` observable by both sides, `WHO` lists members (`352`+`315`),
  non-member `PRIVMSG` → `442`, unknown channel → `403`, unknown nick
  → `401`.

## License

MIT — see [LICENSE](LICENSE).
