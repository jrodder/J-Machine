"""Guestbook (spec §6): one board for the whole server. Players post by
message, the page shows the last few lines.

Pure — no RNS import, so all of it unit-tests without a network; host.py is
the only RNS glue. Reads happen per request (like player_stats): no cache, no
restart needed to edit or prune the log.

Why posting is a MESSAGE and not a form on the page — verified in source
2026-10-20, not assumed:

  * a page request cannot carry an author. RNS reveals who you are only if the
    client calls Link.identify() (RNS/Link.py:454-469; the responder adopts it
    on LINKIDENTIFY at Link.py:971-987, passed to handlers at :829), and
    neither mainstream client does that while browsing: NomadNet identifies
    only for nodes whose "Identify when connecting" box the user ticked,
    default false (Directory.py:377-382, Network.py:747), and meshchat has the
    action as an API route only, no UI calling it (meshchat.py:1869-1889).
    The only other hash on the wire is the link's own id, which is a truncated
    hash of the ephemeral key exchange and so is new every connection
    (Link.py:336-346) — showing it would look like a handle and prove nothing.
  * a message DOES carry an author: LXMF messages are signed and name their
    source destination, which is why the game can restore your save
    (host.py: sender = msg.source_hash.hex()). So tagging a post with the
    poster's hash works in every client, with no checkbox and no account.
  * posted text MUST be micron-escaped before it is rendered back: the
    Reticulum community template has a commit named "fix micron styling in user
    input breaking pages". Backtick is micron's only escape/format character,
    so escaping it is sufficient to stop a line restyling the page or forging
    a link. RNS's own generator escapes the same way
    (RNS/Utilities/rngit/highlight.py:209).

Moderation posture: light on purpose. Minting a Reticulum identity is free, so
no per-author rule stops a determined spammer; the limits below exist to keep
the board and the log usable, and the operator's real tool is deleting a line
from the log (read per request, so no restart)."""
import json
import os
import time
from pathlib import Path

from .protocol import ago

CAP = 200      # stored/displayed chars per entry ( INPUT_CAP parity: short
               # lines, and one line is what the board renders)
RATE = 600     # min seconds between posts by one player
KEEP = 200     # records kept in the log (trimmed on write; bounds the file +
               # the rate-limit scan, so no index or rotate-when-large needed)
SHOW = 10      # entries rendered on the board (LoRa: the page stays ~1 KB)
TAG = 6        # leading hex chars of the poster's destination — the same
               # prefix their own client shows them, and not a name

NOTES = {"saved": "signed — thanks!",
         "empty": "nothing to sign",
         "banned": "not posted",
         "rate": f"one line per {RATE // 60} min"}


def clean(text):
    """One line, no micron markup, capped. Capped BEFORE escaping, so the
    escape backslashes don't eat the player's characters."""
    t = " ".join(str(text).replace("\r", " ").replace("\n", " ").split())
    return t[:CAP].replace("`", "\\`")


def banned(ban_path, author):
    """Denylist: one destination hash per line, '#' comments allowed. Read per
    post — it is tiny, and editing it takes effect without a restart."""
    if not author:
        return False
    try:
        lines = Path(ban_path).read_text().splitlines()
    except OSError:
        return False
    want = author.strip().lower()
    return any(ln.split("#")[0].strip().lower() == want for ln in lines)


def entries(log_path, n=SHOW):
    """Newest first, up to n. A missing log is the fresh-host state; a corrupt
    line is skipped, never fatal (the board still renders if someone hand
    edits the file)."""
    try:
        lines = Path(log_path).read_text().splitlines()
    except OSError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("text"):
            out.append(r)
            if len(out) >= n:
                break
    return out


def post(log_path, ban_path, author, text, now=None):
    """One 'sign:' message -> a NOTES key to reply with. Every rejection is
    checked before anything is written, so a spam pass costs the node nothing
    but a file read.

    author = sender destination hash hex: the LXMF signature already proves it,
    which is the entire reason posting rides messages rather than page views."""
    if banned(ban_path, author):
        return "banned"
    body = clean(text) if text else ""
    if not body:
        return "empty"
    now = time.time() if now is None else now
    # entries are newest-first, so the first one outside the window ends the
    # scan: a player cannot post their way past the limit by being busy
    for e in entries(log_path, 1000):
        if now - e.get("ts", 0) >= RATE:
            break
        if e.get("who") == author:
            return "rate"
    p = Path(log_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # one O_APPEND write of one short line: atomic at this size, and the file
    # is bounded by KEEP so a reader never sees a torn line (trim rewrites it
    # whole, atomically, via temp + os.replace)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": round(now, 3), "who": author, "text": body},
                           ensure_ascii=False) + "\n")
    _trim(p)
    return "saved"


def _trim(log_path, keep=KEEP):
    p = Path(log_path)
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return
    if len(lines) <= keep:
        return
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def render(name, page_hex, es, sign=None, now=None):
    """The board. `sign` = (stem, pretty game address) of one game to post
    through, i.e. games[0]; the link opens that conversation (meshchat and
    Sideband turn an lxmf@ target into an open-conversation action), so
    posting is one tap plus typing 'sign: '.

    No form on purpose: a page view has no author to tag (module docstring),
    and dropping the input also drops the grey-on-grey field meshchat renders
    from the page's micron style (MicronParser.js:276-284)."""
    L = [">`!Guestbook`!", ">",
         "`[« games`" + page_hex + ":/page/index.mu]", ">",
         "Leave me a message letting me know what you think!"]
    if sign:
        # concatenated, not an f-string: a literal '[' plus a subscript
        # expression trips PEP 701 bracket matching in CPython 3.14
        L.append("Open " + "`[" + sign[0] + "`lxmf@"
                 + sign[1].strip("<>").replace(":", "").lower()
                 + "] and send:  sign: your line")
    L.append(f"Max {CAP} chars, one line per {RATE // 60} min.")
    L.append(">")
    if not es:
        L.append("No lines yet — yours would be the first.")
    else:
        L.append(f">Last {len(es)}")
        for e in es:
            L.append("#" + str(e.get("who", ""))[:TAG]
                     + " · " + ago(e.get("ts", 0), now))
            L.append("  " + str(e.get("text", "")))
    return "\n".join(L) + "\n"
