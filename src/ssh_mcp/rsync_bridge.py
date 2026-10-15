import json
import os
import socket
import sys
import threading


def main() -> None:
    transport = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    transport.connect(sys.argv[1])
    transport.sendall(json.dumps(sys.argv[3:]).encode() + b"\n")

    def send_input() -> None:
        try:
            while chunk := os.read(sys.stdin.fileno(), 262144):
                transport.sendall(chunk)
            transport.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    threading.Thread(target=send_input, daemon=True).start()
    try:
        while chunk := transport.recv(262144):
            pending = memoryview(chunk)
            while pending:
                pending = pending[os.write(sys.stdout.fileno(), pending) :]
    except BrokenPipeError:
        pass
    finally:
        transport.close()


if __name__ == "__main__":
    main()
