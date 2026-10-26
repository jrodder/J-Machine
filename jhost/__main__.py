"""python3 -m jhost <games-dir> [--data-dir DIR] [--name NAME] [--seed N]
[--port N] [--announce-interval MIN] [--propagation-node HASH]
[--propagation-pull MIN]"""
import argparse

from .host import Host


def main():
    ap = argparse.ArgumentParser(prog="jhost")
    ap.add_argument("games_dir")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--name", default="🕹️ J-Machine Games 🕹️")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--port", type=int, default=4242)
    ap.add_argument("--announce-interval", type=int, default=60,
                    metavar="MIN",
                    help="re-announce cadence in minutes (default 60; "
                         "RNS rate-limits announces to ~1/hour)")
    ap.add_argument("--propagation-node", default=None, metavar="HASH",
                    help="lxmf/delivery hash of a propagation node; a reply a "
                         "player's link cannot receive is handed to this node "
                         "for the player's router to hold (default: drop it)")
    ap.add_argument("--propagation-pull", type=int, default=5, metavar="MIN",
                    help="how often to ask --propagation-node for messages it "
                         "is holding for each game (default 5; needs "
                         "--propagation-node; 0 disables the poll)")
    a = ap.parse_args()
    host = Host(a.data_dir, a.games_dir, a.name, a.seed, a.port,
                announce_interval=a.announce_interval * 60,
                propagation_node=a.propagation_node,
                propagation_pull=a.propagation_pull * 60)
    host.start()
    host.run()


if __name__ == "__main__":
    main()
