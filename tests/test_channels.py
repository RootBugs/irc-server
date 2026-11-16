"""Channel tests: membership, broadcast isolation, topic, part, quit.

Broadcast isolation is proven with a *canary* pattern instead of
sleeps: after alice's channel message, a client outside the channel
causes an unrelated line (its own PRIVMSG target's reply or a PONG);
if the channel message had leaked to it, it would arrive **before**
the canary and the "next line" assertion would fail.
"""

import unittest

from irc_server import IRCServer
from tests.helpers import connect


class ChannelTestBase(unittest.TestCase):
    def setUp(self):
        self.server = IRCServer().start()
        self.addCleanup(self.server.shutdown)
        self._clients = []

    def join(self, nick: str, channel: str):
        """Register a client and complete its JOIN handshake."""
        client = connect(self.server)
        self.addCleanup(client.close)
        self._clients.append(client)
        client.register(nick, user=nick)
        client.command("JOIN", channel)
        client.read_until("JOIN")   # own join line
        client.read_numeric(353)
        client.read_numeric(366)
        return client

    def channel(self, name: str):
        return self.server.channels[name.lower()]


class MembershipTests(ChannelTestBase):
    def test_only_members_receive_channel_messages(self):
        alice = self.join("alice", "#chat")
        bob = self.join("bob", "#chat")
        carol = self.join("carol", "#other")
        dave = self.join("dave", "#other")
        # consume the JOIN broadcasts the earlier members have waiting:
        # if these ever leaked a #chat message, the assertions below fail
        self.assertIn("JOIN", alice.read_line())    # bob joined #chat
        self.assertIn("JOIN", carol.read_line())    # dave joined #other

        alice.command("PRIVMSG", "#chat", ":hello-bob")
        bob_line = bob.read_line()
        self.assertIn("PRIVMSG #chat", bob_line)
        self.assertIn(":hello-bob", bob_line)
        self.assertTrue(bob_line.startswith(":alice!alice@"), msg=bob_line)

        # canary: dave (NOT in #chat) triggers traffic in #other; his very
        # next line must be that canary — the #chat message never leaked
        carol.command("PRIVMSG", "#other", ":canary-dave")
        dave_line = dave.read_line()
        self.assertIn("canary-dave", dave_line)
        self.assertNotIn("hello-bob", dave_line)
        self.assertTrue(dave_line.startswith(":carol!carol@"), msg=dave_line)

        # the sender does not receive her own message either:
        # alice's very next line is her PONG, nothing in between
        alice.command("PING", "canary-alice")
        pong = alice.read_line()
        self.assertIn("PONG", pong)
        self.assertIn("canary-alice", pong)

        # and carol (also outside #chat) saw only her own canary traffic
        carol.command("PING", "canary-carol")
        self.assertIn("PONG", carol.read_line())

    def test_private_message_reaches_exactly_one_client(self):
        alice = connect(self.server)
        bob = connect(self.server)
        carol = connect(self.server)
        for client, nick in ((alice, "alice"), (bob, "bob"), (carol, "carol")):
            self.addCleanup(client.close)
            client.register(nick, user=nick)

        alice.command("PRIVMSG", "bob", ":secret-word")

        line = bob.read_line()
        self.assertIn("PRIVMSG bob :secret-word", line)
        self.assertTrue(line.startswith(":alice!alice@"), msg=line)

        # canaries: neither carol nor alice receives the private message
        carol.command("PING", "canary-carol")
        pong = carol.read_line()
        self.assertIn("PONG", pong)
        self.assertIn("canary-carol", pong)
        alice.command("PING", "canary-alice")
        pong = alice.read_line()
        self.assertIn("PONG", pong)
        self.assertIn("canary-alice", pong)

        # an unknown recipient is a 401
        alice.command("PRIVMSG", "ghost", ":anyone there?")
        self.assertIn("401", alice.read_numeric(401))
    def test_names_lists_members_after_join(self):
        alice = self.join("alice", "#chat")
        bob = self.join("bob", "#chat")
        carol = self.join("carol", "#other")

        alice.command("NAMES", "#chat")
        listing = alice.read_numeric(353)
        self.assertIn("#chat :alice bob", listing)  # join order
        end = alice.read_numeric(366)
        self.assertIn("End of /NAMES", end)

        # the listing is addressed to the requester
        self.assertIn(" 353 alice ", listing)

        # an unknown channel still terminates cleanly with 366
        bob.command("NAMES", "#ghost")
        ghost_end = bob.read_numeric(366)
        self.assertIn("#ghost", ghost_end)

        # carol, in another channel, receives no stray listing
        carol.command("PING", "canary-carol")
        pong = carol.read_line()
        self.assertIn("PONG", pong)
        self.assertIn("canary-carol", pong)

    def test_quit_removes_client_from_channels(self):
        alice = self.join("alice", "#chat")
        bob = self.join("bob", "#chat")

        bob.command("QUIT", ":gone")
        # the quitter's socket closes without an echo...
        bob.expect_eof()

        # ...and alice is told about it
        alice_line = alice.read_until("QUIT")
        self.assertIn(":bob!bob@", alice_line)
        self.assertEqual(self.channel("#chat").nicks, ["alice"])

        alice.command("NAMES", "#chat")
        listing = alice.read_numeric(353)
        self.assertIn("#chat :alice", listing)
        self.assertNotIn(":alice bob", listing)

    def test_part_removes_membership_for_both_sides(self):
        alice = self.join("alice", "#chat")
        bob = self.join("bob", "#chat")
        self.assertIn("JOIN", alice.read_line())  # bob's JOIN broadcast

        alice.command("PART", "#chat")
        own = alice.read_line()
        other = bob.read_line()
        for line in (own, other):
            self.assertIn("PART #chat", line)
            self.assertTrue(line.startswith(":alice!alice@"), msg=line)
        self.assertEqual(self.channel("#chat").nicks, ["bob"])

        # a later message from bob no longer reaches alice
        bob.command("PRIVMSG", "#chat", ":after-part")
        bob.command("PING", "canary-bob")
        self.assertIn("PONG", bob.read_line())
        alice.command("PING", "canary-alice")
        pong = alice.read_line()
        self.assertIn("PONG", pong)
        self.assertIn("canary-alice", pong)

    def test_join_a_channel_you_already_left_is_rejoinable(self):
        alice = self.join("alice", "#chat")
        alice.command("PART", "#chat")
        alice.read_line()  # own PART echo

        alice.command("JOIN", "#chat")
        alice.read_until("JOIN")
        alice.read_numeric(353)
        alice.read_numeric(366)
        self.assertEqual(self.channel("#chat").nicks, ["alice"])


class TopicTests(ChannelTestBase):
    def test_topic_is_served_to_later_joiners(self):
        alice = self.join("alice", "#chat")
        alice.command("TOPIC", "#chat", ":welcome to oss-100")
        # the setter sees her own TOPIC broadcast (exclude=None)
        setter_line = alice.read_line()
        self.assertIn("TOPIC #chat :welcome to oss-100", setter_line)
        self.assertTrue(setter_line.startswith(":alice!alice@"), msg=setter_line)

        bob = connect(self.server)
        self.addCleanup(bob.close)
        bob.register("bob", user="bob")
        bob.command("JOIN", "#chat")
        bob.read_until("JOIN")
        topic = bob.read_numeric(332)
        self.assertIn("#chat :welcome to oss-100", topic)
        bob.read_numeric(353)
        bob.read_numeric(366)

        # a query gets the same topic back
        bob.command("TOPIC", "#chat")
        self.assertIn("welcome to oss-100", bob.read_numeric(332))

    def test_topic_query_without_topic_gets_331(self):
        alice = self.join("alice", "#empty")
        alice.command("TOPIC", "#empty")
        line = alice.read_numeric(331)
        self.assertIn("No topic is set", line)

    def test_setting_topic_off_channel_is_442(self):
        alice = self.join("alice", "#chat")
        outsider = connect(self.server)
        self.addCleanup(outsider.close)
        outsider.register("mallory", user="mallory")
        outsider.command("TOPIC", "#chat", ":let me in")
        self.assertIn("442", outsider.read_numeric(442))


class RoutingTests(ChannelTestBase):
    def test_who_lists_channel_members(self):
        alice = self.join("alice", "#chat")
        bob = self.join("bob", "#chat")

        alice.command("WHO", "#chat")
        first = alice.read_numeric(352)
        second = alice.read_numeric(352)
        end = alice.read_numeric(315)
        listing = first + " " + second
        self.assertIn(" alice ", listing)
        self.assertIn(" bob ", listing)
        self.assertIn("#chat", listing)
        self.assertIn("End of /WHO", end)

    def test_privmsg_to_channel_by_non_member_is_refused(self):
        alice = self.join("alice", "#chat")
        outsider = connect(self.server)
        self.addCleanup(outsider.close)
        outsider.register("mallory", user="mallory")

        outsider.command("PRIVMSG", "#chat", ":let me in")
        self.assertIn("442", outsider.read_numeric(442))
        outsider.command("PRIVMSG", "#nosuch", ":anyone?")
        self.assertIn("403", outsider.read_numeric(403))

        # the refusal did not leak into the channel
        alice.command("PING", "canary-alice")
        pong = alice.read_line()
        self.assertIn("PONG", pong)
        self.assertIn("canary-alice", pong)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
