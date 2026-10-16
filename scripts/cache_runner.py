"""Run Davi C4 CLI and hold process for final resource sampling."""
import sys
from tools.run_c4 import main

if __name__ == '__main__':
    code = main(sys.argv[1:])
    sys.stdout.flush()
    sys.stdin.readline()
    sys.exit(code)
