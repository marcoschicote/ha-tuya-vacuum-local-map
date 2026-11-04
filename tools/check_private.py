"""Look for private data in tracked and staged files and in commit messages.

This repo is public, so run this before committing. It finds what looks private
in general, plus the terms in .private-terms (git-ignored, one term per line, for
the owner's own device ID, room names and so on). It can't read images.
"""

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TERMS_FILE = ROOT / ".private-terms"
SKIPPED_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}

PATTERNS = {
    "private IP address": re.compile(
        r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"
    ),
    "Tuya device ID": re.compile(r"\bbf[0-9a-z]{18,24}\b"),
    "local key value": re.compile(
        r"""(?i)local_?key["']?\s*[:=]\s*["'][^"'\s.]{16}["']"""
    ),
    "email address": re.compile(
        r"\b[\w.+-]+@(?!users\.noreply|anthropic\.com|github\.com)[\w-]+\.\w+"
    ),
}


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def load_terms() -> list[str]:
    if not TERMS_FILE.exists():
        return []
    lines = TERMS_FILE.read_text().splitlines()
    return [term.strip() for term in lines if term.strip()]


def problems_in(label: str, text: str, terms: list[str]) -> list[str]:
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        where = f"{label}:{lineno}"
        found += [f"{where}: {kind}" for kind, p in PATTERNS.items() if p.search(line)]
        # Terms are private themselves, so report the line and not the term.
        if any(term.casefold() in line.casefold() for term in terms):
            found.append(f"{where}: a term from .private-terms")
    return found


def main() -> int:
    terms = load_terms()
    problems = []
    for name in git("ls-files", "--cached").splitlines():
        path = ROOT / name
        if path.suffix.lower() in SKIPPED_SUFFIXES or not path.is_file():
            continue
        try:
            problems += problems_in(name, path.read_text(), terms)
        except UnicodeDecodeError:
            problems.append(f"{name}: binary file, check it by hand")
    problems += problems_in("commit messages", git("log", "--format=%B"), terms)
    # A machine's default git email can reveal its hostname and network name.
    emails = set(git("log", "--format=%ae%n%ce").split())
    emails.add(git("config", "user.email").strip())
    problems += [
        f"git email isn't a GitHub noreply address: {email}"
        for email in sorted(emails)
        if not email.endswith(("users.noreply.github.com", "noreply@github.com"))
    ]

    print("\n".join(problems) if problems else "No private data found.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
