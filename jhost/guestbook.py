"""Guestbook (spec §6): a micron form on a second page lets a visitor leave a
line for the other explorers, and the board shows the last few.

Pure — no RNS import, so all of it unit-tests without a network; host.py is
the only RNS glue. Reads happen per request (like player_stats): no cache, no
restart needed to edit or prune the log.

Prior art, verified in source 2026-10-20 rather than from a blog post:
  * micron fields are native (NomadNet >= 0.3.4, "added input fields to micron
    format [...] ability for node-side applications to process input"). Syntax
    from the reference parser nomadnet/ui/textui/MicronParser.py:955-1035 and
    its Guide: `<48|name`default>` is a single-line text field (width <= 256,
    `<!` masked, `<40x5|n`> multiline); `<?|n|v`>` checkbox; `<^|n|v`>` radio.
  * a link's THIRD backtick component lists the fields to submit — `*` for all
    of them — and the client posts them to that link's path as
    request_data["field_<name>"]: NomadNet ui/textui/Browser.py:425-440, and
    meshchat meshchat.py:2198-2212 does the identical prefixing. So one handler
    on the page destination sees both views and posts, with no new protocol.
  * authorship is a BONUS, never a requirement: RNS reveals who you are only if
    the client calls Link.identify() (RNS/Link.py:454-469; the responder stores
    it on LINKIDENTIFY at Link.py:971-987 and passes it at Link.py:829), and
    neither mainstream client does that unprompted — NomadNet identifies only
    for nodes the user toggled, False by default (Directory.py:377-382), and
    meshchat makes it a separate "identify to this node" action
    (meshchat.py:1877-1884). So a posting rule of "must be identified" is a
    board nobody can sign; anonymous entries are the normal case here.
  * submitted text MUST be micron-escaped before it is rendered back: the
    Reticulum community template has a commit named "fix micron styling in user
    input breaking pages". Backtick is micron's only escape/format character,
    so escaping it is sufficient to stop user text restyling the page or
    forging a link. RNS's own generator escapes the same way
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

CAP = 200      # stored/displayed chars per entry (INPUT_CAP parity: short
               # lines)
RATE = 600     # min seconds between posts by one IDENTIFIED identity
ANON_RATE = 60 # anonymous posts share one author slot, so the throttle has to
               # be global: one line per minute keeps a busy board human and a
               # scripted one pointless
KEEP = 200     # records kept in the log (trimmed on write; bounds the file +
               # the rate-limit scan, so no index or rotation is needed)
SHOW = 10      # entries rendered on the board (LoRa: the page stays ~1 KB)
TAG = 6        # leading hex chars of the author destination shown — a tag,
               # never a name, and nothing links it to a real identity

NOTES = {"saved": "🖊️ signed — thanks!",
         "empty": "✏️ nothing to sign",
         "banned": "🚫 not posted",
         "rate": f"🚫 one line per {RATE // 60} min",
         "busy": f"🚫 one line per minute on this board"}


def clean(text):
    """One line, no micron markup, capped. Capped BEFORE escaping, so the
    escape backslashes don't eat the player's characters."""
    t = " ".join(str(text).replace("\r", " ").replace("\n", " ").split())
    return t[:CAP].replace("`", "\\`")


def banned(ban_path, author):
    """Denylist: one destination hash per line, '#' comments allowed. Read per
    post — it is tiny, and editing it takes effect without a restart."""
    if author is None:
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
    """One submit -> a NOTES key for the board to show, or None for a plain
    view. Every rejection is checked before anything is written, so a spam
    pass costs the node nothing but a file read.

    author is the requester's destination hash hex when they identified to the
    node (see the module docstring: usually they have not), else None -> the
    entry is anonymous and shares the global ANON_RATE slot instead of a
    per-identity one."""
    if author is not None and banned(ban_path, author):
        return "banned"
    body = clean(text) if text else ""
    if not body:
        return "empty"
    now = time.time() if now is None else now
    # entries are newest-first, so the first one outside the window ends the
    # scan: one identity cannot post its way past the limit by being busy
    recent = entries(log_path, 1000)
    if author is None:
        if recent and now - recent[0].get("ts", 0) < ANON_RATE:
            return "busy"
    else:
        for e in recent:
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


def render(name, page_hex, es, note=None, now=None):
    """The board and its form.

    The field is single-line on purpose (meshchat does not implement multiline
    inputs) and width 48 is display width, not a data cap. The submit link's
    third backtick component `*` is what makes the client send every field it
    rendered, and meshchat bypasses its page cache for that request — so the
    poster sees their own line at the top immediately.
    """
    L = [">`!🖊️ Guestbook`!", ">",
         "`[« games`" + page_hex + ":/page/index.mu]", ">",
         "Leave me a message letting me know what you think!",
         f"Max {CAP} chars. Identified posts show the first {TAG} chars of",
         f"your destination hash (one per {RATE // 60} min); anonymous posts",
         f"show 'anon' and share one line per {ANON_RATE} seconds.",
         ">"]
    if note:
        L.append("`!" + NOTES.get(note, NOTES["banned"]) + "`!")
        L.append(">")
    L.append("`<48|message`>")
    L.append("`[Sign the guestbook`" + page_hex + ":/page/guestbook.mu`*]")
    L.append(">")
    if not es:
        L.append("No lines yet — yours would be the first.")
    else:
        L.append(f">Last {len(es)}")
        for e in es:
            who = str(e.get("who") or "")[:TAG] or "anon"
            L.append(f"{who} · {ago(e.get('ts', 0), now)}")
            L.append("  " + str(e.get("text", "")))
    return "\n".join(L) + "\n"
