"""Minimal IRC test client: raw socket, independent line framing.

The reader is deliberately hand-rolled (no ``makefile`` tricks): it
buffers bytes and returns one complete line at a time, so tests see
exactly what went over the wire and can assert on numeric codes,
prefixes and trailing parameters without importing parsing helpers
from the server.
"""

from __future__ import annotations

import socket
from typing import List, Optional, Tuple

TIMEOUT = 5.0


class IRCClient:
    """One IRC conversation over a raw socket."""

    def __init__(self, host: str, port: int):
        self.sock = socket.create_connection((host, port), timeout=TIMEOUT)
        self._buf = bytearray()
        self.banner = self.read_line()  # the 220-equivalent banner

    # -- reading -----------------------------------------------------------
    def _recv_some(self) -> None:
        data = self.sock.recv(65536)
        if not data:
            raise ConnectionError("server closed the connection")
        self._buf.extend(data)

    def read_line(self) -> str:
        """Read one complete line (CR/LF stripped)."""
        while True:
            index = self._buf.find(b"\n")
            if index != -1:
                line = bytes(self._buf[:index])
                del self._buf[: index + 1]
                if line.endswith(b"\r"):
                    line = line[:-1]
                return line.decode("utf-8", "replace")
            self._recv_some()

    def read_numeric(self, code: int, limit: int = 30) -> str:
        """Read lines until one whose command is ``code``; return it.

        Numeric commands are compared as integers so ``001`` and ``1``
        are the same match.
        """
        for _ in range(limit):
            line = self.read_line()
            command = _command_of(line)
            if command and command.isdigit() and int(command) == code:
                return line
        raise AssertionError(f"numeric {code} never arrived")

    def read_until(self, needle: str, limit: int = 50) -> str:
        """Read lines until one contains ``needle``; return it."""
        for _ in range(limit):
            line = self.read_line()
            if needle in line:
                return line
        raise AssertionError(f"{needle!r} never arrived")

    def drain(self) -> List[str]:
        """Return everything already buffered, without blocking."""
        lines = []
        while b"\n" in self._buf:
            index = self._buf.find(b"\n")
            line = bytes(self._buf[:index])
            del self._buf[: index + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            lines.append(line.decode("utf-8", "replace"))
        return lines

    def expect_eof(self, timeout: float = 5.0) -> None:
        """Assert the server closes the connection from here on."""
        self.sock.settimeout(timeout)
        try:
            got = self.sock.recv(4096)
        except (socket.timeout, TimeoutError):
            raise AssertionError("expected EOF, connection still open") from None
        finally:
            self.sock.settimeout(TIMEOUT)
        if got != b"":
            raise AssertionError(f"expected EOF, got {got!r}")

    # -- writing -----------------------------------------------------------
    def send_raw(self, line: str) -> None:
        self.sock.sendall(line.encode("utf-8") + b"\r\n")

    def command(self, *parts: str) -> None:
        """Send one command: ``command(\"PRIVMSG\", \"#c\", \":hi\")``."""
        self.send_raw(" ".join(parts))

    def register(self, nick: str, user: str = "user") -> None:
        """NICK + USER, then consume the welcome + MOTD block."""
        self.command("NICK", nick)
        self.command("USER", user, "0", "*", f":{nick} real")
        self.read_numeric(1)   # 001 welcome
        self.read_numeric(376)  # end of MOTD

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "IRCClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _command_of(line: str) -> Optional[str]:
    """The command (numeric or verb) of a server line, skipping prefix."""
    text = line[1:] if line.startswith(":") else line
    parts = text.split(" ")
    if not parts:
        return None
    # server numerics look like ":server 001 target ..." → index 1
    if parts[0] == "irc.localhost" or parts[0].startswith("irc."):
        return parts[1] if len(parts) > 1 else None
    return parts[0]


def parse_trailing(line: str) -> Tuple[str, str]:
    """Split ``a b :c d`` into (\"a b\", \"c d\")."""
    if " :" in line:
        head, _, tail = line.partition(" :")
        return head, tail
    if line.startswith(":"):
        _, _, tail = line.partition(" ")
        return "", tail
    return line, ""


def connect(server) -> IRCClient:
    return IRCClient("127.0.0.1", server.port)
