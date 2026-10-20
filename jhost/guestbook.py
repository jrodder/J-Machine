"""Guestbook (spec §6): one board for the whole server. A visitor types one
line into a micron field, taps the post link, and the board shows the last few.

Pure — no RNS import, so all of it unit-tests without a network; host.py is
the only RNS glue. Reads happen per request (like player_stats): no cache, no
restart needed to edit or prune the log.

Prior art and mechanism, verified in source rather than assumed:

  * micron fields are native (NomadNet >= 0.3.4, "added input fields to micron
    format [...] ability for node-side applications to process input"). Syntax
    from the reference parser nomadnet/ui/textui/MicronParser.py:955-1035 and
    its Guide: `<48|name`value>` is a single-line text field (width <= 256,
    the backtick part is the field's STARTING VALUE, not a hint — so leave it
    empty or the visitor has to delete your placeholder before posting);
    `<!` masked, `<40x5|n`>` multiline (meshchat has no multiline inputs, so
    the board uses a single-line field).
  * a link's THIRD backtick component lists the fields to submit — `*` for all
    of them — and the client posts them to that link's path as
    request_data["field_<name>"]: NomadNet ui/textui/Browser.py:425-440, and
    meshchat meshchat.py:2198-2212 prefixes identically. One handler on the
    page destination therefore serves the view (no payload) and the post.
  * a form post is USUALLY anonymous, and no server-side fix exists. RNS reveals
    who you are only if the client calls Link.identify() (RNS/Link.py:454-469;
    the responder adopts it on LINKIDENTIFY at :971-987, passes it to handlers at
    :829), clients do that only for protocols they opted into (LXMF propagation
    retrieval, per thatSFguy/reticulum-webclient's identify()-d /get), and while
    browsing: NomadNet identifies only where the user ticked "Identify when
    connecting", false by default (Directory.py:377-382, Network.py:747), meshchat
    has the action as an API route no UI calls (meshchat.py:1869-1889). A page
    handler cannot ask: Destination offers ALLOW_NONE/ALL/LIST only
    (Destination.py:74-77) — it can refuse unidentified peers, not request proof,
    which is why application protocols carry an explicit "not identified on link"
    error instead. So the board labels the unidentified `anon` rather than
    pretending, and refuses nobody.
  * there is a second, VERIFIED door: any LXMF message starts with a proved
    sender hash (host.py: sender = msg.source_hash.hex()), so a 'sign:' message
    (protocol.handle_message) appends a line tagged #<6 hex> that is
    cryptographically the poster's — the same scheme the two message boards in the
    wild use (cotteux/MicronCMS and chengtripp/lxmf_messageboard both set
    username = source_hash[0:5]). The page does not advertise it, so posting
    never requires knowing it exists.
  * posted text MUST be micron-escaped before it is rendered back: the Reticulum
    community template has a commit named "fix micron styling in user input
    breaking pages". Backtick is micron's only escape/format character, so
    escaping it is sufficient to stop a line restyling the page or forging a
    link. RNS's own generator escapes the same way
    (RNS/Utilities/rngit/highlight.py:209).

Moderation posture: light on purpose. Minting a Reticulum identity is free, so
no per-author rule stops a determined spammer; the limits below exist to keep
the board and the log usable, and the operator's real tool is deleting a line
from the log (read per request, so no restart)."""
import json
import os
import time
from pathlib import Path

from .protocol import DIV, GREY, RESET, TEAL, ago

CAP = 200      # stored/displayed chars per entry (one line is what the board
               # renders, and LoRa pages stay ~1 KB)
RATE = 600     # min seconds between posts by one identified player
ANON_RATE = 60 # an anonymous post has no author to key on, so its slot is
               # server-wide: keeps a busy board human and a scripted one useless
KEEP = 200     # records kept in the log (trimmed on write; bounds the file +
               # the rate-limit scan, so no index or rotation is needed)
SHOW = 10      # entries rendered on the board
TAG = 6        # leading hex chars of a verified author destination — the same
               # prefix a player's own client shows them, and not a name

NOTES = {"saved": "signed — thanks!",
         "empty": "nothing to sign",
         "banned": "not posted",
         "rate": f"one line per {RATE // 60} min",
         "busy": f"one line per {ANON_RATE} seconds on this board"}


def clean(text):
    """One line, no micron markup, capped. Capped BEFORE escaping, so the
    escape backslashes don't eat the player's characters."""
    t = " ".join(str(text).replace("\r", " ").replace("\n", " ").split())
    return t[:CAP].replace("`", "\\`")


def banned(ban_path, author):
    """Denylist: one destination hash per line, '#' comments allowed. Read per
    post — it is tiny, and editing it takes effect without a restart. It can
    only name identified players; there is nothing verified to ban otherwise."""
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
    """One post -> a NOTES key for the board to show (or None for a plain
    view). Every rejection happens before anything is written, so a spam pass
    costs the node nothing but a file read, and rejected text is never echoed.

    author = the sender's destination hash hex when the link identified itself
    (rare while browsing, see the module docstring), else None -> anonymous."""
    if banned(ban_path, author):
        return "banned"
    body = clean(text) if text else ""
    if not body:
        return "empty"
    now = time.time() if now is None else now
    # entries are newest-first, so the first one outside the window ends the
    # scan: a player cannot post their way past the limit by being busy
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
    """The board and its one field. Width 48 is display width, not the data cap
    (CAP is that), and the field's backtick part is its starting value, left
    empty on purpose. The submit link's third component `*` is what makes the
    client send every field, and meshchat skips its page cache for that request
    — so the poster sees their own line at the top."""
    L = [">`!" + "🖊️ Guestbook" + "`!", ">",
         "`[« games`" + page_hex + ":/page/index.mu]", DIV,
         "Leave me a message letting me know what you think!",
         f"Type one line and tap Post. Max {CAP} chars.",
         "A post is marked with your destination hash when your",
         "client has identified to this node, otherwise 'anon'.",
         DIV]
    if note:
        L.append("`!" + NOTES.get(note, NOTES["banned"]) + "`!")
        L.append(">")
    L.append("`<48|message`>")
    L.append("`[Post it`" + page_hex + ":/page/guestbook.mu`*]")
    L.append(DIV)
    if not es:
        L.append("No lines yet — yours would be the first.")
    else:
        L.append(f">Last {len(es)}")
        for e in es:
            # a verified tag carries the accent; an anonymous mark stays in the
            # theme's own grey, so the difference is visible at a glance
            who = str(e.get("who") or "")[:TAG]
            mark = (TEAL + "#" + who + RESET) if who else (GREY + "anon" + RESET)
            L.append(mark + " · " + ago(e.get("ts", 0), now))
            L.append("  " + str(e.get("text", "")))
    return "\n".join(L) + "\n"
