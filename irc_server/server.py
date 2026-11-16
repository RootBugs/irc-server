"""Threaded IRC server: registration, channels, broadcast (RFC 2812).

Connection flow::

    accept ─► 220 server ready ─► NICK / USER (any order, any time)
                  │
                  └─ both present ─► 001 welcome + 002/003 + MOTD
                                     (375 / 372 / 376)

Registered clients then drive the channel commands: JOIN, PART,
PRIVMSG (channel or private), TOPIC, NAMES, WHO, PING/PONG, QUIT.

One handler thread per connection, a 1.0 s socket timeout so
``shutdown()`` never blocks, ephemeral port by default, and every
outbound line goes through a per-client send lock so concurrent
broadcasts cannot interleave half-lines on the wire.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Dict, List, Optional, Set

from .channel import Channel
from .message import (
    MAX_LINE,
    MAX_RECEIVED_LINE,
    IRCMessage,
    MessageError,
    is_valid_nick,
    parse,
)

logger = logging.getLogger("irc_server")

#: Idle socket timeout — lets blocked handlers notice shutdown().
CLIENT_TIMEOUT = 1.0
#: Server name used in numeric replies and the 220 banner.
SERVER_NAME = "irc.localhost"
#: MOTD text served after registration.
MOTD_LINES = [
    f"* Welcome to {SERVER_NAME}",
    "* This server is part of the OSS-100 collection.",
    "* No authentication, no persistence — messages are ephemeral.",
]


class Client:
    """One connected (or registering) client."""

    __slots__ = ("server", "sock", "nick", "username", "host", "registered",
                 "closed", "_send_lock", "_channel_names")

    def __init__(self, server: "IRCServer", sock: socket.socket, host: str):
        self.server = server
        self.sock = sock
        self.nick: Optional[str] = None
        self.username: Optional[str] = None
        self.host = host
        self.registered = False
        self.closed = False
        self._send_lock = threading.Lock()
        self._channel_names: Set[str] = set()

    # -- identity ----------------------------------------------------------
    @property
    def prefix(self) -> str:
        """``nick!user@host`` as seen by other clients."""
        return f"{self.nick}!{self.username or 'unknown'}@{self.host}"

    # -- sending -----------------------------------------------------------
    def send_line(self, line: str) -> None:
        """Send one line (CRLF appended) under the client's send lock."""
        data = (line + "\r\n").encode("utf-8", "replace")
        if len(data) > MAX_LINE:
            data = (line[: MAX_LINE - 3] + "\r\n").encode("utf-8", "replace")
        with self._send_lock:
            if self.closed:
                return
            try:
                self.sock.sendall(data)
            except OSError:
                self.closed = True

    def send_numeric(self, code: int, params: List[str], target: str = None) -> None:
        """Send ``:SERVER <code> <target> <params...>`` (code zero-padded).

        A multi-word last parameter is sent in trailing form (``:text``),
        the way real IRC servers format numeric replies.
        """
        who = target if target is not None else (self.nick or "*")
        parts = [str(p) for p in params]
        if parts and " " in parts[-1]:
            parts[-1] = ":" + parts[-1]
        body = f"{int(code):03d} {who} " + " ".join(parts)
        self.send_line(f":{SERVER_NAME} {body}")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class IRCServer:
    """A threading IRC server.

    Args:
        host: bind address (default ``127.0.0.1``).
        port: bind port; ``0`` selects an ephemeral port.

    Example::

        with IRCServer() as server:
            print(f"irc://127.0.0.1:{server.port}")
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self._sock: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._clients: List[Client] = []
        self._threads: List[threading.Thread] = []
        self._bound_port = 0
        self.channels: Dict[str, Channel] = {}
        self.connection_count = 0
        self.message_count = 0

    # -- lifecycle ---------------------------------------------------------
    @property
    def port(self) -> int:
        return self._bound_port

    @property
    def address(self):
        return (self.host, self._bound_port)

    @property
    def running(self) -> bool:
        return self._sock is not None and not self._stop.is_set()

    @property
    def clients(self) -> List[Client]:
        with self._lock:
            return list(self._clients)

    def start(self) -> "IRCServer":
        if self._sock is not None:
            raise RuntimeError("server already started")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, 0))
        sock.listen(32)
        self._sock = sock
        self._bound_port = sock.getsockname()[1]
        self._stop.clear()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="irc-accept", daemon=True
        )
        self._accept_thread.start()
        return self

    def shutdown(self, timeout: float = 5.0) -> None:
        if self._sock is None:
            return
        self._stop.set()
        sock, self._sock = self._sock, None
        try:
            sock.close()
        except OSError:
            pass
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout)
            self._accept_thread = None
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)
        with self._lock:
            self._threads.clear()

    def __enter__(self) -> "IRCServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.shutdown()

    # -- accept / serve ----------------------------------------------------
    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except OSError:
                break
            with self._lock:
                self.connection_count += 1
                index = self.connection_count
                client = Client(self, conn, addr[0])
                self._clients.append(client)
                thread = threading.Thread(
                    target=self._serve_client,
                    args=(client,),
                    name=f"irc-conn-{index}",
                    daemon=True,
                )
                self._threads.append(thread)
            thread.start()

    def _serve_client(self, client: Client) -> None:
        client.sock.settimeout(CLIENT_TIMEOUT)
        buffer = bytearray()
        try:
            client.send_line(f":{SERVER_NAME} NOTICE * :server ready")
            while not self._stop.is_set():
                try:
                    data = client.sock.recv(4096)
                except TimeoutError:
                    continue  # idle; re-check the stop flag
                except OSError:
                    break
                if not data:
                    break
                buffer.extend(data)
                if len(buffer) > MAX_RECEIVED_LINE * 4:
                    break  # absurd traffic, drop the client
                while True:
                    index = buffer.find(b"\n")
                    if index == -1:
                        break
                    raw = bytes(buffer[:index])
                    del buffer[: index + 1]
                    if raw.endswith(b"\r"):
                        raw = raw[:-1]
                    if len(raw) > MAX_RECEIVED_LINE:
                        continue  # ignore over-long line, stay in sync
                    self.message_count += 1
                    self._handle_line(client, raw.decode("utf-8", "replace"))
                    if client.closed or self._stop.is_set():
                        break
                if client.closed or self._stop.is_set():
                    break
        finally:
            self._drop_client(client)

    # -- dispatch ----------------------------------------------------------
    def _handle_line(self, client: Client, text: str) -> None:
        try:
            message = parse(text)
        except MessageError as exc:
            client.send_numeric(421, [text.split(" ", 1)[0] or text, str(exc)])
            return
        handler = getattr(self, f"_cmd_{message.command.lower()}", None)
        if handler is None:
            if not client.registered:
                client.send_numeric(451, ["You have not registered"])
            else:
                client.send_numeric(421, [message.command, "Unknown command"])
            return
        handler(client, message)

    def _require_registered(self, client: Client) -> bool:
        if client.registered:
            return True
        client.send_numeric(451, ["You have not registered"])
        return False

    def _require_params(self, client: Client, message: IRCMessage, count: int) -> bool:
        if len(message.params) >= count:
            return True
        client.send_numeric(461, [message.command, "Not enough parameters"])
        return False

    # -- registration ------------------------------------------------------
    def _cmd_nick(self, client: Client, message: IRCMessage) -> None:
        if not self._require_params(client, message, 1):
            return
        nick = message.params[0]
        if not is_valid_nick(nick):
            client.send_numeric(432, [nick, "Erroneous nickname"])
            return
        if self._nick_in_use(nick, exclude=client):
            client.send_numeric(433, [nick, "Nickname is already in use"])
            return
        old = client.nick
        client.nick = nick
        if client.registered and old:
            # a registered client renaming itself: tell everyone
            self._broadcast_raw(
                f":{old}!{client.username or 'unknown'}@{client.host} "
                f"NICK :{nick}",
                exclude=None,
            )
        if not client.registered:
            self._maybe_finish_registration(client)

    def _cmd_user(self, client: Client, message: IRCMessage) -> None:
        if not self._require_params(client, message, 1):
            return
        if client.registered:
            client.send_numeric(462, ["You may not reregister"])
            return
        client.username = message.params[0][:9] or "unknown"
        self._maybe_finish_registration(client)

    def _nick_in_use(self, nick: str, exclude: Optional[Client] = None) -> bool:
        key = nick.lower()
        for other in self.clients:
            if other is exclude or other.nick is None:
                continue
            if other.nick.lower() == key:
                return True
        return False

    def _maybe_finish_registration(self, client: Client) -> None:
        if client.registered or client.nick is None or client.username is None:
            return
        client.registered = True
        client.send_numeric(
            1, [f"Welcome to the {SERVER_NAME} IRC Network {client.prefix}"]
        )
        client.send_numeric(2, [f"Your host is {SERVER_NAME}, version 1.0.0"])
        client.send_numeric(
            3, ["This server was created Sat Nov 14 2026"]
        )
        client.send_numeric(375, [f"- {SERVER_NAME} Message of the day -"])
        for line in MOTD_LINES:
            client.send_numeric(372, ["- " + line])
        client.send_numeric(376, [f"End of /MOTD command for {client.nick}"])

    # -- channels ----------------------------------------------------------
    def _channel(self, name: str) -> Channel:
        key = name.lower()
        channel = self.channels.get(key)
        if channel is None:
            channel = Channel(name)
            self.channels[key] = channel
        return channel

    def _cmd_join(self, client: Client, message: IRCMessage) -> None:
        if not self._require_registered(client):
            return
        if not self._require_params(client, message, 1):
            return
        target = message.params[0]
        if not target.startswith("#") or len(target) < 2:
            client.send_numeric(403, [target, "No such channel"])
            return
        channel = self._channel(target)
        if not channel.add(client):
            return  # already a member: RFC silent
        client._channel_names.add(channel.name.lower())
        client.send_line(f":{client.prefix} JOIN :{channel.name}")
        if channel.topic is not None:
            client.send_numeric(332, [channel.name, channel.topic])
            if channel.topic_set_by:
                client.send_numeric(
                    333, [channel.name, channel.topic_set_by]
                )
        self._send_names(client, channel)
        self._broadcast(
            f":{client.prefix} JOIN :{channel.name}",
            channel,
            exclude=client,
        )

    def _cmd_part(self, client: Client, message: IRCMessage) -> None:
        if not self._require_registered(client):
            return
        if not self._require_params(client, message, 1):
            return
        target = message.params[0]
        channel = self.channels.get(target.lower())
        if channel is None or not channel.contains(client.nick):
            client.send_numeric(442, [target, "You're not on that channel"])
            return
        reason = message.params[1] if len(message.params) > 1 else client.nick
        # 1) the leaver sees their own PART, 2) membership shrinks, 3) the
        # remaining members are told — so by the time anyone else receives
        # the line, the removal has already happened (no race for readers)
        client.send_line(f":{client.prefix} PART {channel.name} :{reason}")
        channel.remove(client.nick)
        client._channel_names.discard(channel.name.lower())
        self._broadcast(
            f":{client.prefix} PART {channel.name} :{reason}",
            channel,
            exclude=None,
        )

    def _cmd_topic(self, client: Client, message: IRCMessage) -> None:
        if not self._require_registered(client):
            return
        if not self._require_params(client, message, 1):
            return
        name = message.params[0]
        channel = self.channels.get(name.lower())
        if channel is None or not channel.contains(client.nick):
            client.send_numeric(442, [name, "You're not on that channel"])
            return
        if len(message.params) == 1:  # query
            if channel.topic is None:
                client.send_numeric(331, [channel.name, "No topic is set"])
            else:
                client.send_numeric(332, [channel.name, channel.topic])
                if channel.topic_set_by:
                    client.send_numeric(333, [channel.name, channel.topic_set_by])
            return
        channel.topic = message.params[1]
        channel.topic_set_by = client.prefix
        self._broadcast(f":{client.prefix} TOPIC {channel.name} :{channel.topic}",
                        channel, exclude=None)

    def _cmd_privmsg(self, client: Client, message: IRCMessage) -> None:
        if not self._require_registered(client):
            return
        if not self._require_params(client, message, 1):
            return
        target = message.params[0]
        if len(message.params) < 2:
            client.send_numeric(412, ["No text to send"])
            return
        text = message.params[1]
        line = f":{client.prefix} PRIVMSG {target} :{text}"
        if target.startswith("#") or target.startswith("&"):
            channel = self.channels.get(target.lower())
            if channel is None or not channel.contains(client.nick):
                client.send_numeric(
                    403 if channel is None else 442,
                    [target, "No such channel" if channel is None
                     else "You're not on that channel"],
                )
                return
            self._broadcast(line, channel, exclude=client)
            return
        recipient = self._find_client(target)
        if recipient is None:
            client.send_numeric(401, [target, "No such nick/channel"])
            return
        recipient.send_line(line)

    def _cmd_names(self, client: Client, message: IRCMessage) -> None:
        if not self._require_registered(client):
            return
        if not self._require_params(client, message, 1):
            return
        channel = self.channels.get(message.params[0].lower())
        if channel is None:
            client.send_numeric(366, [message.params[0], "End of /NAMES list"])
            return
        self._send_names(client, channel)

    def _cmd_who(self, client: Client, message: IRCMessage) -> None:
        if not self._require_registered(client):
            return
        if not self._require_params(client, message, 1):
            return
        channel = self.channels.get(message.params[0].lower())
        if channel is not None:
            for member in channel.members:
                client.send_line(
                    f":{SERVER_NAME} 352 {client.nick} {channel.name} "
                    f"{member.username} {member.host} {SERVER_NAME} "
                    f"{member.nick} H :0 {member.nick}"
                )
        client.send_numeric(315, [message.params[0], "End of /WHO list"])

    def _cmd_ping(self, client: Client, message: IRCMessage) -> None:
        token = message.params[0] if message.params else SERVER_NAME
        client.send_line(f":{SERVER_NAME} PONG {SERVER_NAME} :{token}")

    def _cmd_pong(self, client: Client, message: IRCMessage) -> None:
        pass  # liveness acknowledged; nothing to do

    def _cmd_quit(self, client: Client, message: IRCMessage) -> None:
        reason = message.params[0] if message.params else client.nick or "Quit"
        line = f":{client.prefix} QUIT :{reason}"
        # tell channel-mates first, while we still know the memberships
        for name in list(client._channel_names):
            channel = self.channels.get(name)
            if channel is not None:
                self._broadcast(line, channel, exclude=client)
                channel.remove(client.nick)
        client._channel_names.clear()  # prevents a duplicate QUIT on drop
        client.close()

    # -- helpers -----------------------------------------------------------
    def _find_client(self, nick: str) -> Optional[Client]:
        key = nick.lower()
        for other in self.clients:
            if other.nick and other.nick.lower() == key and other.registered:
                return other
        return None

    def _send_names(self, client: Client, channel: Channel) -> None:
        nicks = " ".join(channel.nicks)
        client.send_line(
            f":{SERVER_NAME} 353 {client.nick} = {channel.name} :{nicks}"
        )
        client.send_line(
            f":{SERVER_NAME} 366 {client.nick} {channel.name} :End of /NAMES list"
        )

    def _broadcast(self, line: str, channel: Channel, exclude: Optional[Client]) -> None:
        for member in channel.members:
            if member is exclude or member.closed:
                continue
            member.send_line(line)

    def _broadcast_raw(self, line: str, exclude: Optional[Client]) -> None:
        for other in self.clients:
            if other is exclude or other.closed or not other.registered:
                continue
            other.send_line(line)

    def _drop_client(self, client: Client) -> None:
        # a vanishing socket behaves like QUIT: channel-mates are told
        if client.registered and client.nick:
            line = f":{client.prefix} QUIT :Connection closed"
            for name in list(client._channel_names):
                channel = self.channels.get(name)
                if channel is not None:
                    channel.remove(client.nick)
                    self._broadcast(line, channel, exclude=client)
        client.close()
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
