#!/usr/bin/env python3
"""Block staged content that looks like real personal data."""
import re
import subprocess
import sys

BLOCKED_NAMES = re.compile(
    r"(\.db$|\.db-wal$|\.db-shm$|\.sqlite\d?$|\.vcf$|\.bak|\.ipa$"
    r"|^exports/|^data/|^imessage-import/|screenshot|\.mobilebackup)", re.I)

PATTERNS = {
    "a phone number": re.compile(r"\b(?:\+1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b"),
    "a personal email address": re.compile(
        r"[\w.+-]+@(?:gmail|icloud|me|comcast|yahoo|outlook|hotmail|aol)\.[a-z]+", re.I),
    "an Apple handle": re.compile(r"\b(?:iMessage;|tel:)\+?\d{7,}"),
    "a home directory path": re.compile(r"/(?:home|Users)/(?!USER\b)[a-z][\w.-]+"),
    "a tailnet address": re.compile(r"\b100\.(?:[6-9]\d|1\d\d|2[0-4]\d|25[0-5])\.\d{1,3}\.\d{1,3}\b"),
}
MAX_BYTES = 1_000_000

def staged():
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                         capture_output=True, text=True).stdout
    return [f for f in out.split("\n") if f]

def main() -> int:
    problems = []
    for path in staged():
        if BLOCKED_NAMES.search(path):
            problems.append(f"{path}: looks like message data or a backup, by its name")
            continue
        blob = subprocess.run(["git", "show", f":{path}"], capture_output=True).stdout
        if len(blob) > MAX_BYTES:
            problems.append(f"{path}: {len(blob)//1000} KB. Large files here are usually data.")
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for label, pat in PATTERNS.items():
            m = pat.search(text)
            if m:
                line = text[:m.start()].count("\n") + 1
                problems.append(f"{path}:{line}: contains {label} ({m.group(0)[:38]})")
    if not problems:
        return 0
    print("\nBlocked: this commit contains something that looks like real data.\n",
          file=sys.stderr)
    for p in problems:
        print(f"  {p}", file=sys.stderr)
    print("\nFix it, or use placeholders (USER, 555-0100, user@example.com).",
          file=sys.stderr)
    print("If it is genuinely fine: git commit --no-verify\n", file=sys.stderr)
    return 1

if __name__ == "__main__":
    sys.exit(main())
