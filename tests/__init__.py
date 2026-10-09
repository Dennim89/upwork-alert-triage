"""Test package. Importing it hard-blocks sockets and subprocesses for the whole run, so no test can reach
the mailbox, Telegram or the Claude CLI even by mistake. Run from the project root: python -m unittest discover
"""
import logging
import socket
import subprocess


def _blocked(*_a, **_k):
    raise RuntimeError("network/subprocess access is disabled in tests")


class _NoPopen:
    def __init__(self, *_a, **_k):
        _blocked()


socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
subprocess.Popen = _NoPopen
logging.getLogger("bot").setLevel(logging.CRITICAL)        # expected warnings stay out of the test output
