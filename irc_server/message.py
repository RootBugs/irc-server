"""IRC message parsing and formatting (RFC 2812 wire syntax).

A line looks like::

    [:prefix] COMMAND [param] [param] [:trailing param with spaces]

The prefix starts with ``:``; parameters are space-separated; the first
parameter starting with ``:`` consumes the rest of the line (the
"trailing" parameter).  Lines are capped at 512 bytes including the
terminating CRLF, per RFC 2812 §2.3 — outgoing lines are checked, and
the server refuses to read absurd ones.
"""

from __future__ import annotations

#: Maximum line length including CRLF (RFC 2812 §2.3).
MAX_LINE = 512
#: Maximum bytes accepted from a client before the connection is dropped.
MAX_RECEIVED_LINE = 1024


class MessageError(Exception):
    """A line could not be parsed as an IRC message."""


class IRCMessage:
    """One parsed IRC message."""

    __slots__ = ("prefix", "command", "params")

    def __init__(self, prefix, command, params):
        self.prefix = prefix
        self.command = command
        self.params = params

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<IRCMessage {self.prefix!r} {self.command} {self.params}>"

    def __eq__(self, other) -> bool:
        if not isinstance(other, IRCMessage):
            return NotImplemented
        return (
            self.prefix == other.prefix
            and self.command == other.command
            and self.params == other.params
        )


def parse(line: str) -> IRCMessage:
    """Parse one IRC line (CR/LF already stripped by the caller)."""
    if not line:
        raise MessageError("empty line")
    prefix = None
    if line.startswith(":"):
        prefix, _, rest = line.partition(" ")
        prefix = prefix[1:]
        line = rest
        if not line:
            raise MessageError("prefix without a command")
    command, _, rest = line.partition(" ")
    if not command:
        raise MessageError("missing command")
    params = []
    while rest:
        if rest.startswith(" "):
            rest = rest[1:]
            continue
        if rest.startswith(":"):
            params.append(rest[1:])
            break
        token, _, rest = rest.partition(" ")
        params.append(token)
    return IRCMessage(prefix, command.upper(), params)


def format_message(command: str, params=None, prefix: str = None) -> str:
    """Serialize a message to a single line (no CRLF).

    The last parameter is written in trailing form when it contains a
    space or starts with ``:``; a non-final parameter with a space
    cannot be encoded and raises :class:`ValueError`.
    """
    parts = []
    if prefix:
        parts.append(":" + prefix)
    parts.append(command)
    params = list(params or [])
    for index, value in enumerate(params):
        is_last = index == len(params) - 1
        if " " in value or value.startswith(":") or (value == "" and is_last):
            if not is_last:
                raise ValueError(f"middle parameter cannot be encoded: {value!r}")
            parts.append(":" + value)
        else:
            parts.append(value)
    return " ".join(parts)


def is_valid_nick(nick: str) -> bool:
    """RFC 2812 §2.3.1 nickname rules (30 chars, letter/special first)."""
    if not nick or len(nick) > 30:
        return False
    first = nick[0]
    if not (first.isalpha() or first in "[]\\`_^{|}"):
        return False
    return all(ch.isalnum() or ch in "-[]\\`_^{|}" for ch in nick)


def split_params(message: IRCMessage, count: int):
    """Return the first ``count`` params; ``None`` when there are fewer."""
    if len(message.params) < count:
        return None
    return message.params[:count]
