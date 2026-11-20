"""The anomaly file: what the unified update noticed and could not settle by itself, for a human to look at now and then.

An anomaly never stops the data from being published. It is written to a text file of the repository (anomalies/ANOMALIES.txt),
grouped by day, and each anomaly is written ONCE: its `key` identifies it, and a key already in the file is never written again.
(If the situation changes, e.g. the supply gap becomes another number, the key changes and a new line appears.)

    10:05Z  ccip / mint above    message 0xab.. 8453->56 sent 2026-11-20 09:41  burned 5  minted 40  send 0x.. receive 0x..   [key=ccip:mint above:0xab..]
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

HEADER = """# EXPLORIZE ANOMALIES
# Written by the "Unified update explorize" workflow. Every anomaly appears once, under the day it was first seen.
# They never block the publication of the data: look at them from time to time and fix the causes (labels, pools, data).
# The tag at the end of each line (the word key, an equal sign, then the identifier, in square brackets) identifies the anomaly:
# do not edit it, or the anomaly will be written again.
"""
KEY_RE = re.compile(r"\[key=([^\]]+)\]")


def known_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return set(KEY_RE.findall(path.read_text(encoding="utf-8")))


def append(path: Path, items: list[tuple[str, str, str]], now: datetime | None = None) -> int:
    """items = [(key, kind, detail)]. Writes only the keys not in the file yet; returns how many lines were added."""
    now = now or datetime.now(timezone.utc)
    seen = known_keys(path)
    fresh, taken = [], set()
    for key, kind, detail in items:
        if key in seen or key in taken:
            continue
        taken.add(key)
        fresh.append((key, kind, detail))
    if not fresh:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else HEADER
    day = f"## {now:%Y-%m-%d}"
    if day not in text:
        text = text.rstrip("\n") + f"\n\n{day}\n"
    elif not text.endswith("\n"):
        text += "\n"
    # the new lines go at the end of today's section, which is the end of the file when today is the latest day
    lines = "".join(f"{now:%H:%M}Z  {kind:<22} {detail}   [key={key}]\n" for key, kind, detail in fresh)
    if text.rstrip("\n").split("\n## ")[-1].startswith(day[3:]):
        text = text.rstrip("\n") + "\n" + lines
    else:                                                          # today's header exists but an older/newer one follows: append in place
        head, _, rest = text.partition(day + "\n")
        nxt = rest.find("\n## ")
        body, tail = (rest, "") if nxt < 0 else (rest[:nxt + 1], rest[nxt + 1:])
        text = head + day + "\n" + body.rstrip("\n") + ("\n" if body.strip() else "") + lines + ("\n" + tail if tail else "")
    path.write_text(text, encoding="utf-8")
    return len(fresh)
