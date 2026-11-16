"""Channels: membership tracking and topic storage."""

from __future__ import annotations

from typing import Dict, Iterator, List, Optional


class Channel:
    """One IRC channel.

    Membership is keyed by lower-cased nick so ``Alice`` and ``alice``
    are the same person, while the original spelling is kept for
    display.  The channel stores only data; the server performs all
    sends and broadcasts.
    """

    __slots__ = ("name", "topic", "topic_set_by", "_members")

    def __init__(self, name: str):
        self.name = name
        self.topic: Optional[str] = None
        self.topic_set_by: Optional[str] = None
        self._members: Dict[str, object] = {}

    def add(self, client) -> bool:
        """Add a client; ``False`` when already a member."""
        key = client.nick.lower()
        if key in self._members:
            return False
        self._members[key] = client
        return True

    def remove(self, nick: str) -> bool:
        """Remove by nick; ``False`` when not a member."""
        return self._members.pop(nick.lower(), None) is not None

    def contains(self, nick: str) -> bool:
        return nick.lower() in self._members

    def member(self, nick: str):
        """The client holding ``nick``, or ``None``."""
        return self._members.get(nick.lower())

    @property
    def members(self) -> List:
        """Members in join order."""
        return list(self._members.values())

    @property
    def nicks(self) -> List[str]:
        """Member nicks in join order (original spelling)."""
        return [client.nick for client in self._members.values()]

    def __len__(self) -> int:
        return len(self._members)

    def __iter__(self) -> Iterator:
        return iter(self._members.values())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Channel {self.name} members={self.nicks}>"
