"""pi-profiles is a public repo: no personal identifiers (the team's real hostnames, logins or home
paths) anywhere in the tree. Use generic ones: hq-mac, op-mac, corp-laptop-01, pi@isr-pi.local, /home/pi.

The patterns are built from pieces so this file never matches them itself. Real values belong in a
station's own config (station.env, /etc/*.env), never in a default."""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FORBIDDEN = re.compile("|".join([
    "oh" + "ml",                    # the corporate hostname prefix
    "ja" + "haynes",                # the user's name as a hostname / username
    "jacobs-mac" + "book",          # a personal Mac's default hostname
    r"\bj" + r"hh@",                # the Pi login
    "/home/j" + "hh",
]), re.I)


def tracked_files():
    try:
        out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, timeout=20, check=True).stdout
        return [REPO / f for f in out.decode().split("\0") if f]
    except (OSError, subprocess.SubprocessError):
        skip = {".git", ".venv", "node_modules", "__pycache__"}
        return [p for p in REPO.rglob("*") if p.is_file() and not skip & set(p.parts)]


def test_no_personal_identifiers_in_the_tree():
    hits = []
    for p in tracked_files():
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:          # binary (fonts, vendored minified JS is text and still checked)
            continue
        for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            if FORBIDDEN.search(line):
                hits.append(f"{p.relative_to(REPO)}:{n}: {line.strip()[:120]}")
    assert not hits, "personal identifiers in a public repo (use hq-mac, op-mac, pi@isr-pi.local, /home/pi):\n" + "\n".join(hits)


def test_the_check_catches_them():
    for s in ("OH" + "ML-Someone.local", "ja" + "haynes", "jacobs-mac" + "book-pro", "j" + "hh@isr-pi.local", "/home/j" + "hh/x"):
        assert FORBIDDEN.search(s), s
    for s in ("hq-mac", "op-mac", "pi@isr-pi.local", "/home/pi", "corp-laptop-01", "Jacob-Haynes/pi-profiles"):
        assert not FORBIDDEN.search(s), s
