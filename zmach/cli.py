"""CLI REPL (plan Task 11): `python3 -m zmach story.z5 [options]`.

- REPL prints the VM's byte stream verbatim (Text -> stdout). The plan's
  "Prompt -> print '> '" is deliberately NOT done: the stream already
  contains the prompt cell at each seam (byte-exact dfrotz -t parity),
  so printing again would double it.
- Meta commands (never fed to the game): @save <file>, @restore <file>,
  @info, @quit. Unknown @commands are reported on stderr and ignored.
- In-game save/restore opcodes: the handlers prompt "Save to: " /
  "Restore from: " on stderr and read one line from stdin as the
  filename (works in piped mode); the line is consumed by the handler,
  not the game. The hint from the opcode's string operand is the
  default when the line is empty.
- --save/--restore: save after load / restore before running (API path).
- Exit codes: 0 normal/EndOfGame/EOF; 1 StoryFileError/SaveFileError or
  a failed meta command (clean message, no traceback).
"""
import argparse
import sys

from . import __version__
from .events import EndOfGame, Error, SaveFileError, StoryFileError, Text
from .session import Session

META_HELP = "@save <file> | @restore <file> | @info | @quit"


def _emit(events, out=None):
    out = out or sys.stdout
    for e in events:
        if isinstance(e, Text):
            out.write(e.data)
        elif isinstance(e, Error):
            print(e.message, file=sys.stderr)
        elif isinstance(e, EndOfGame):
            if e.status:
                print(f"[game ended, status {e.status}]", file=sys.stderr)
        # Prompt: nothing — the stream already carries the prompt cell.


def _in_game_save(session):
    def save(hint):
        name = _prompt("Save to: ", hint or "zmach-save.zmsv")
        try:
            with open(name, "wb") as f:
                f.write(session.save())
            return True
        except (OSError, SaveFileError) as e:
            print(f"zmach: {e}", file=sys.stderr)
            return False
    return save


def _in_game_restore(session):
    def restore(hint):
        name = _prompt("Restore from: ", hint or "zmach-save.zmsv")
        try:
            with open(name, "rb") as f:
                session.restore_image(f.read())  # decode only; the VM
            return True                         # mid-turn keeps running
        except (OSError, SaveFileError) as e:
            print(f"zmach: {e}", file=sys.stderr)
            return False
    return restore


def _prompt(label, default):
    sys.stderr.write(label)
    sys.stderr.flush()
    line = sys.stdin.readline()
    if line == "":          # stdin EOF: fall back to the default name
        return default
    name = line.strip()
    return name or default


def _meta(session, line):
    """Handle a meta command. Returns None (handled), "quit", or 1
    (error: print a message and exit 1)."""
    parts = line.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd == "@quit":
        return "quit"
    if cmd == "@save":
        if not arg:
            print("zmach: @save needs a file argument", file=sys.stderr)
            return None
        try:
            with open(arg, "wb") as f:
                f.write(session.save())
        except (OSError, SaveFileError) as e:
            print(f"zmach: {e}", file=sys.stderr)
            return 1
        return None
    if cmd == "@restore":
        if not arg:
            print("zmach: @restore needs a file argument", file=sys.stderr)
            return None
        try:
            with open(arg, "rb") as f:
                _emit(session.restore(f.read()))
        except (OSError, SaveFileError) as e:
            print(f"zmach: {e}", file=sys.stderr)
            return 1
        return None
    if cmd == "@info":
        si = session.story
        print(f"Story:       {si.name}")
        print(f"Version:     v{si.version} (release {si.release})")
        print(f"Serial:      {si.serial}")
        print(f"sha256:      {si.file_sha256.hex()}")
        print(f"Interpreter: zmach {__version__} "
              f"(Z-machine standard 1.1, ZMSAVE v1)")
        return None
    print(f"zmach: unknown command: {line} ({META_HELP})", file=sys.stderr)
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="zmach",
        description="Z-machine interpreter (v3/v5/v8) — dfrotz -t parity")
    ap.add_argument("story", help="story file (.z3/.z5/.z8)")
    ap.add_argument("--strict", action="store_true",
                    help="verify the story file's checksum")
    ap.add_argument("--seed", type=int,
                    help="interpreter random seed (reproducible runs)")
    ap.add_argument("--save", metavar="FILE",
                    help="write a ZMSAVE v1 snapshot after load")
    ap.add_argument("--restore", metavar="FILE",
                    help="restore a ZMSAVE v1 snapshot before running")
    a = ap.parse_args(argv)

    session = Session()
    # handlers are installed before load so an in-game save during the
    # load batch also works
    session.set_save_handler(_in_game_save(session))
    session.set_restore_handler(_in_game_restore(session))

    try:
        _emit(session.load(a.story, seed=a.seed, strict=a.strict))
        if a.restore:
            with open(a.restore, "rb") as f:
                _emit(session.restore(f.read()))
        if a.save:
            with open(a.save, "wb") as f:
                f.write(session.save())
    except (StoryFileError, SaveFileError, OSError) as e:
        print(f"zmach: {e}", file=sys.stderr)
        return 1

    while not session.done:
        line = sys.stdin.readline()
        if line == "":            # EOF: normal exit
            break
        line = line.rstrip("\n")
        if line.startswith("@"):
            rc = _meta(session, line)
            if rc == "quit":
                break
            if rc == 1:
                return 1
        else:
            _emit(session.input(line))
    return 0


if __name__ == "__main__":
    sys.exit(main())