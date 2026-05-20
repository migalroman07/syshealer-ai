#!/usr/bin/env python3
import argparse as ap
import sys

from src.collector import collect_logs
from src.tui import main_menu


def main():
    parser = ap.ArgumentParser(description="SysHealer-AI")
    parser.add_argument(
        "--scan", action="store_true", help="Force system scan in background."
    )
    args = parser.parse_args()

    if args.scan:
        print("[*] Force scanning journalctl for the last 24 hours...")
        collect_logs("24 hours ago")
        print("[+] Scan complete. Run 'syshealer' to review new incidents.")
        sys.exit(0)

    try:
        main_menu()
    except KeyboardInterrupt:
        print("\nExiting... Daemon will continue working.")
        sys.exit(0)


if __name__ == "__main__":
    main()
