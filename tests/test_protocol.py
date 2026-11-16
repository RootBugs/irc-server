"""Message syntax and registration-protocol tests (unit + live socket)."""

import unittest

from irc_server import (
    IRCMessage,
    MessageError,
    format_message,
    is_valid_nick,
    parse,
    split_params,
)
from tests.helpers import connect


class ParseTests(unittest.TestCase):
    def test_parse_full_message(self):
        message = parse(":alice!alice@host PRIVMSG #chat :hello there")
        self.assertEqual(message.prefix, "alice!alice@host")
        self.assertEqual(message.command, "PRIVMSG")
        self.assertEqual(message.params, ["#chat", "hello there"])

    def test_parse_without_prefix_and_trailing(self):
        message = parse("NICK alice")
        self.assertIsNone(message.prefix)
        self.assertEqual(message.command, "NICK")
        self.assertEqual(message.params, ["alice"])

        message = parse("353 bob = #chat :alice bob")
        self.assertEqual(message.params, ["bob", "=", "#chat", "alice bob"])

        # empty trailing parameter after ":"
        message = parse("PRIVMSG bob :")
        self.assertEqual(message.params, ["bob", ""])

    def test_command_is_uppercased(self):
        self.assertEqual(parse("join #chat").command, "JOIN")

    def test_malformed_lines_rejected(self):
        for bad in ("", ":prefixonly", "  "):
            with self.assertRaises(MessageError, msg=repr(bad)):
                parse(bad)
        # a lone command with no params, however, is legal
        self.assertEqual(parse("PRIVMSG").params, [])

    def test_format_roundtrip(self):
        line = format_message(
            "PRIVMSG", ["#chat", "hello world"], prefix="alice!a@h"
        )
        self.assertEqual(line, ":alice!a@h PRIVMSG #chat :hello world")
        message = parse(line)
        self.assertEqual(message.prefix, "alice!a@h")
        self.assertEqual(message.command, "PRIVMSG")
        self.assertEqual(message.params, ["#chat", "hello world"])

        # a middle parameter containing a space cannot be encoded
        with self.assertRaises(ValueError):
            format_message("PRIVMSG", ["a b", "c"])

    def test_split_params(self):
        message = parse("JOIN #chat secret")
        self.assertEqual(split_params(message, 2), ["#chat", "secret"])
        self.assertIsNone(split_params(message, 3))

    def test_nick_validation(self):
        for good in ("alice", "A1", "bob_2", "x[zebra]", "nick-with-dash", "`tick"):
            self.assertTrue(is_valid_nick(good), msg=good)
        for bad in ("", "9lives", "-lead", "has space", "a" * 31, "#", "#chan"):
            # '#' starts channels, not nicknames
            self.assertFalse(is_valid_nick(bad), msg=repr(bad))


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        from irc_server import IRCServer

        self.server = IRCServer().start()
        self.addCleanup(self.server.shutdown)

    def test_banner_then_motd_on_connect(self):
        client = connect(self.server)
        self.addCleanup(client.close)
        self.assertIn("NOTICE", client.banner)
        self.assertIn("server ready", client.banner)

        client.command("NICK", "alice")
        client.command("USER", "alice", "0", "*", ":Alice on OSS")
        welcome = client.read_numeric(1)
        self.assertIn("Welcome", welcome)
        self.assertIn("alice!alice@", welcome)

        motd_start = client.read_numeric(375)
        self.assertIn("Message of the day", motd_start)
        body = client.read_numeric(372)
        self.assertTrue(body.rstrip().endswith("ephemeral.") or "-" in body)
        end = client.read_numeric(376)
        self.assertIn("End of /MOTD", end)

    def test_commands_before_registration_get_451(self):
        client = connect(self.server)
        self.addCleanup(client.close)
        client.command("NICK", "alice")  # NICK alone: not registered yet
        client.command("PRIVMSG", "#chat", ":too early")
        line = client.read_numeric(451)
        self.assertIn("not registered", line)
        # an unknown command is gated the same way
        client.command("FROBNICATE")
        self.assertIn("451", client.read_numeric(451))

        # finish registration → PRIVMSG now reaches the protocol layer
        client.command("USER", "alice", "0", "*", ":Alice")
        client.read_numeric(376)
        client.command("PRIVMSG", "#chat", ":nobody home")
        self.assertIn("403", client.read_numeric(403))

    def test_user_alone_is_not_enough(self):
        client = connect(self.server)
        self.addCleanup(client.close)
        client.command("USER", "solo", "0", "*", ":Solo")
        client.command("PING", "check")
        # PONG proves the connection is alive without any 001 having been sent
        pong = client.read_until("PONG")
        self.assertIn(":check", pong)
        # still unregistered: JOIN is refused
        client.command("JOIN", "#chat")
        self.assertIn("451", client.read_numeric(451))

    def test_nick_collision_rejected(self):
        first = connect(self.server)
        second = connect(self.server)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.register("alice")

        second.command("NICK", "ALICE")  # case-insensitive collision
        collision = second.read_numeric(433)
        self.assertIn("ALICE", collision)
        self.assertIn("already in use", collision)
        # the second client never registered, so JOIN stays refused
        second.command("JOIN", "#chat")
        line = second.read_numeric(451)
        self.assertIn("451", line)
        self.assertIn("not registered", line)

        # ...and once it picks a free nick it can register normally
        second.command("NICK", "bob")
        second.command("USER", "bob", "0", "*", ":Bob")
        second.read_numeric(1)
        second.read_numeric(376)
        second.command("JOIN", "#chat")
        second.read_until("JOIN")
        self.assertEqual(self.server.channels["#chat"].nicks, ["bob"])

    def test_erroneous_nick_rejected(self):
        client = connect(self.server)
        self.addCleanup(client.close)
        client.command("NICK", "9lives")
        line = client.read_numeric(432)
        self.assertIn("Erroneous", line)

    def test_not_enough_parameters_gets_461(self):
        client = connect(self.server)
        self.addCleanup(client.close)
        client.register("alice")
        client.command("JOIN")
        self.assertIn("461", client.read_numeric(461))
        client.command("PRIVMSG")
        self.assertIn("461", client.read_numeric(461))

    def test_unknown_command_after_registration_gets_421(self):
        client = connect(self.server)
        self.addCleanup(client.close)
        client.register("alice")
        client.command("FROBNICATE")
        line = client.read_numeric(421)
        self.assertIn("Unknown command", line)

    def test_ping_pong_roundtrip(self):
        client = connect(self.server)
        self.addCleanup(client.close)
        client.command("PING", "token123")
        pong = client.read_until("PONG")
        head, separator, tail = pong.partition(" :")
        self.assertIn("PONG", head)
        self.assertEqual(separator, " :")
        self.assertEqual(tail, "token123")

    def test_registered_nick_change_is_broadcast(self):
        alice = connect(self.server)
        bob = connect(self.server)
        self.addCleanup(alice.close)
        self.addCleanup(bob.close)
        alice.register("alice", user="alice")
        bob.register("bob", user="bob")

        alice.command("NICK", "alicia")
        # both clients see the change, with a full nick!user@host prefix
        for client in (alice, bob):
            line = client.read_until("NICK")
            self.assertTrue(line.startswith(":alice!alice@"), msg=line)
            self.assertTrue(line.endswith("NICK :alicia"), msg=line)

    def test_quit_closes_the_connection(self):
        client = connect(self.server)
        self.addCleanup(client.close)
        client.register("alice")
        # the quitter gets no echo (IRC convention): the socket just closes
        client.command("QUIT", ":bye")
        client.expect_eof()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
