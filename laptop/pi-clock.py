#!/usr/bin/env python3
"""Set the Pi's clock from this Mac when they differ by more than 2 s (the Pi has no time source offline).

    laptop/pi-clock.py <user>@isr-pi.local [--min-skew 2] [--source NAME]

The Mac's time is sent only after the ssh session is up (the Pi prints "ready" first), so connection
and login time don't add error: what's left is about half a round trip. On the Pi it runs
sudo -n isr-set-clock (or, on a Pi without it yet, sudo -n date + fake-hwclock save), which won't touch
an NTP-synced clock. Used by hq-tunnel.sh on every connect, push-to-pi.sh and setup-mac.sh --from-pi.
Exit 0 when the clock is fine or was corrected; 1 when it couldn't check or set it.
"""
import os
import re
import shlex
import socket
import subprocess
import sys
import time

REMOTE = r'''echo ready; IFS= read -r t
if [ -x /opt/pi-profiles/bin/isr-set-clock ]; then
  sudo -n /opt/pi-profiles/bin/isr-set-clock --source {src} --min-skew {min} "$t" 2>&1
else
  n=$(date +%s); s=${{t%.*}}; d=$((s - n)); a=${{d#-}}
  if [ "$a" -le {mini} ]; then echo "clock ok: skew ${{d}}s vs {srcp} (within {min}s)"
  elif sudo -n date -u -s "@$s" >/dev/null 2>&1; then sudo -n fake-hwclock save >/dev/null 2>&1; echo "clock set from {srcp}: corrected ${{d}}s"
  else echo "cannot set the Pi clock: sudo needs a password (skew ${{d}}s)"; exit 3; fi
fi'''


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if args else 64
    host, min_skew, src = args.pop(0), "2", ""
    while args:
        a = args.pop(0)
        if a == "--min-skew" and args:
            min_skew = args.pop(0)
        elif a == "--source" and args:
            src = args.pop(0)
        else:
            print(f"pi-clock: unknown option {a}", file=sys.stderr)
            return 64
    if not re.match(r"^[A-Za-z0-9._@:%-]+$", host) or not re.match(r"^\d{1,3}$", min_skew):
        print("pi-clock: bad host or --min-skew", file=sys.stderr)
        return 64
    src = re.sub(r"[^A-Za-z0-9 @._:-]", "", src or f"{os.environ.get('ISR_STATION', 'mac')} "
                 f"{os.environ.get('ISR_HOST') or socket.gethostname().split('.')[0]}")[:48]
    remote = REMOTE.format(src=shlex.quote(src), srcp=src.replace('"', ""), min=min_skew, mini=int(min_skew))
    ssh = os.environ.get("ISR_SSH", "ssh").split()
    cmd = ssh + ["-o", "ConnectTimeout=10", "-o", "BatchMode=yes", host, remote]
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as e:
        print(f"pi-clock: {e}", file=sys.stderr)
        return 1
    try:
        line = p.stdout.readline()
        if line.strip() != "ready":
            p.kill()
            err = (p.stderr.read() or "").strip().splitlines()
            print(f"pi-clock: couldn't reach {host}" + (f" ({err[-1]})" if err else ""), file=sys.stderr)
            return 1
        p.stdin.write(f"{time.time():.3f}\n")
        p.stdin.flush()
        p.stdin.close()
        out = p.stdout.read().strip()
        rc = p.wait(timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        p.kill()
        print(f"pi-clock: {e}", file=sys.stderr)
        return 1
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    for l in out.splitlines() or ["(no reply)"]:
        print(f"{stamp} pi-clock {host}: {l}")
    return 0 if rc == 0 and ("clock ok" in out or "clock set" in out or "NTP-synced" in out) else 1


if __name__ == "__main__":
    sys.exit(main())
