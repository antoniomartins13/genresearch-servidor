"""Keeps the real server process alive for a final metrics snapshot.

Protocol: stdout is server JSON, then child waits for a newline on stdin.
"""
import sys
from genresearch.server import main


def run():
    status = main(sys.argv[1:])
    sys.stdout.flush()
    sys.stdin.readline()
    return status


if __name__ == '__main__':
    sys.exit(run())
