"""irc-server — a real IRC server (RFC 2812) on raw sockets.

Public API::

    from irc_server import IRCServer, Channel, IRCMessage, parse, format_message

    with IRCServer() as server:
        print(f"irc://127.0.0.1:{server.port}")
"""

import logging

from .channel import Channel
from .message import (
    MAX_LINE,
    MAX_RECEIVED_LINE,
    IRCMessage,
    MessageError,
    format_message,
    is_valid_nick,
    parse,
    split_params,
)
from .server import CLIENT_TIMEOUT, SERVER_NAME, Client, IRCServer

logging.getLogger("irc_server").addHandler(logging.NullHandler())

__all__ = [
    "CLIENT_TIMEOUT",
    "Channel",
    "Client",
    "IRCMessage",
    "IRCServer",
    "MAX_LINE",
    "MAX_RECEIVED_LINE",
    "MessageError",
    "SERVER_NAME",
    "format_message",
    "is_valid_nick",
    "parse",
    "split_params",
]

__version__ = "1.0.0"
