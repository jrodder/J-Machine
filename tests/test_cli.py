"""CLI REPL (plan Task 11): `python3 -m zmach`.

Gates: scripted zork1 run, clean exit (1, no traceback) on a bad story,
@save/@restore meta round-trip, @info, @quit, --save/--restore flags,
and the in-game save/restore OPCODE handlers (risorg's SAVE/RESTORE
verbs: handler prompts on stderr and consumes one stdin line as the
filename — piped mode).

Plan-test corrections (documented in task-11-report):
- test_meta_save_restore asserted save size > 500000 for zork1: zork1's
  memory is 84876 + 8192-word v3 stack = 101260 bytes (ZMSAVE images are
  story+stack sized, not the spec's 512 KB assumption) -> assert > 100000
  (risorg, the v8 game, is 705384 and would pass 500000).
- The plan's "Prompt -> print '> '" is dropped: the VM's byte stream
  ALREADY contains the prompt cell at the seam (byte-exact dfrotz -t
  parity — e.g. the batch ends in '>' or 'quit? '); printing again
  would double it. The REPL prints the stream verbatim.
- risorg consumes one startup line before printing its intro (dfrotz
  does the same), so the in-game handler script starts with a blank
  line.
"""
import subprocess
import unittest
from pathlib import Path

C = Path(__file__).parent / "corpus"
ROOT = Path(__file__).parent.parent


def run_cli(args, stdin=""):
    return subprocess.run(
        ["python3", "-m", "zmach"] + args,
        input=stdin, capture_output=True, text=True,
        timeout=120, cwd=ROOT)


class TestCli(unittest.TestCase):

    def test_scripted_run(self):
        p = run_cli([str(C / "zork1.z3"), "--seed", "10"],
                    "look\nopen mailbox\n@quit\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("ZORK I: The Great Underground Empire", p.stdout)
        self.assertIn("Opening the small mailbox reveals a leaflet.", p.stdout)

    def test_bad_story_clean_exit(self):
        p = run_cli(["/nonexistent.z5"])
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)

    def test_meta_save_restore(self):
        save = Path("/tmp/cli_test.zsave")
        save.unlink(missing_ok=True)
        p = run_cli([str(C / "zork1.z3"), "--seed", "10"],
                    f"look\n@save {save}\nlook\n@restore {save}\n"
                    f"open mailbox\n@quit\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(save.exists())
        # ZMSAVE v1 = ~12 KB header + frames + full memory image
        # (zork1 memory = 84876 + 8192-word v3 stack = 101260 bytes)
        self.assertGreater(save.stat().st_size, 100000)
        self.assertIn("Opening the small mailbox reveals a leaflet.",
                      p.stdout)

    def test_meta_info(self):
        p = run_cli([str(C / "zork1.z3"), "--seed", "10"], "@info\n@quit\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("zork1", p.stdout)
        self.assertIn("v3", p.stdout)
        self.assertIn("sha256", p.stdout.lower())

    def test_meta_restore_bad_file_exits_1(self):
        p = run_cli([str(C / "zork1.z3"), "--seed", "10"],
                    "@restore /nonexistent.zmsv\n@quit\n")
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)

    def test_quit_verb_ends_with_0(self):
        p = run_cli([str(C / "planetfall.z5"), "--seed", "10"],
                    "look\nquit\nyes\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_flag_save_restore(self):
        f1 = Path("/tmp/cli_flag.zmsv")
        f1.unlink(missing_ok=True)
        p = run_cli([str(C / "zork1.z3"), "--seed", "10",
                     "--save", str(f1)], "@quit\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(f1.exists())
        self.assertGreater(f1.stat().st_size, 100000)
        p2 = run_cli([str(C / "zork1.z3"), "--seed", "10",
                      "--restore", str(f1)],
                     "open mailbox\n@quit\n")
        self.assertEqual(p2.returncode, 0, p2.stderr)
        self.assertIn("Opening the small mailbox reveals a leaflet.",
                      p2.stdout)

    def test_in_game_save_restore_handlers(self):
        # risorg SAVE/RESTORE verbs: the library calls ext 0/1 mid-turn;
        # the handler prompts on stderr and consumes one stdin line as
        # the filename (piped mode). risorg consumes one startup line
        # before the intro, so the script starts blank.
        save = Path("/tmp/cli_ingame.zsave")
        save.unlink(missing_ok=True)
        p = run_cli([str(C / "risorg.z8"), "--seed", "7"],
                    f"\nsave\n{save}\nrestore\n{save}\nlook\n@quit\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Save to: ", p.stderr)
        self.assertIn("Restore from: ", p.stderr)
        self.assertTrue(save.exists())

    def test_unknown_meta_command(self):
        p = run_cli([str(C / "zork1.z3"), "--seed", "10"],
                    "@bogus\n@quit\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("unknown", p.stderr)


if __name__ == "__main__":
    unittest.main()