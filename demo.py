"""Runnable example: three clients chat on a live server.

Run with:  python demo.py
"""

import socket


def read_line(sock_file) -> str:
    raw = sock_file.readline()
    if not raw:
        return ""  # server closed the connection
    return raw.decode("utf-8", "replace").rstrip("\r\n")


def main() -> None:
    from irc_server import IRCServer

    with IRCServer() as server:
        print(f"irc://127.0.0.1:{server.port}")

        def make_client(nick: str):
            sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
            reader = sock.makefile("rb")
            banner = read_line(reader)
            print(f"[{nick}] <- {banner}")
            sock.sendall(f"NICK {nick}\r\nUSER {nick} 0 * :{nick} demo\r\n".encode())
            while True:  # consume 001..376
                line = read_line(reader)
                if line.split(" ")[1:2] == ["376"]:
                    break
            print(f"[{nick}] registered (welcome + MOTD received)")
            return sock, reader

        alice_sock, alice = make_client("alice")
        bob_sock, bob = make_client("bob")

        alice_sock.sendall(b"JOIN #chat\r\n")
        while True:
            line = read_line(alice)
            print(f"[alice] <- {line}")
            if line.endswith("End of /NAMES list"):
                break

        bob_sock.sendall(b"JOIN #chat\r\n")
        while True:
            line = read_line(bob)
            print(f"[bob]   <- {line}")
            if line.endswith("End of /NAMES list"):
                break
        print(f"[alice] <- {read_line(alice)}")  # bob's JOIN broadcast

        alice_sock.sendall(b"TOPIC #chat :welcome to oss-100\r\n")
        print(f"[alice] <- {read_line(alice)}")   # own TOPIC echo
        print(f"[bob]   <- {read_line(bob)}")

        alice_sock.sendall(b"PRIVMSG #chat :hello bob!\r\n")
        print(f"[bob]   <- {read_line(bob)}")

        bob_sock.sendall(b"PRIVMSG alice :hi alice, private\r\n")
        print(f"[alice] <- {read_line(alice)}")

        bob_sock.sendall(b"QUIT :bye\r\n")
        print(f"[alice] <- {read_line(alice)}")   # QUIT broadcast

        alice_sock.sendall(b"PRIVMSG #chat :anyone left?\r\n")
        alice_sock.sendall(b"PING done\r\n")
        print(f"[alice] <- {read_line(alice)}")   # PONG: nobody else heard it

        print(f"server: {server.connection_count} connections, "
              f"{server.message_count} messages, "
              f"channel members: {server.channels['#chat'].nicks}")
        alice_sock.close()
        bob_sock.close()


if __name__ == "__main__":
    main()
