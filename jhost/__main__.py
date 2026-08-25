"""python3 -m jhost <games-dir> [--data-dir DIR] [--name NAME] [--seed N]
[--port N] [--announce-interval MIN]"""
import argparse

from .host import Host


def main():
    ap = argparse.ArgumentParser(prog="jhost")
    ap.add_argument("games_dir")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--name", default="J-Machine Games")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--port", type=int, default=4242)
    ap.add_argument("--announce-interval", type=int, default=15,
                    metavar="MIN",
                    help="re-announce cadence in minutes (default 15)")
    a = ap.parse_args()
    host = Host(a.data_dir, a.games_dir, a.name, a.seed, a.port,
                announce_interval=a.announce_interval * 60)
    host.start()
    host.run()


if __name__ == "__main__":
    main()
