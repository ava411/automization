"""Download a player's full game history from the public chess.com API.

Usage:
    python -m chess_coach.fetch <username> [--months N]

Games are saved to data/<username>/games.json. Monthly archives that were
already downloaded (and are not the current month) are reused, so re-running
only fetches what's new.
"""

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.chess.com/pub/player/{user}"
# chess.com asks API clients to identify themselves via User-Agent.
HEADERS = {"User-Agent": "chess-coach/1.0 (personal game-history analysis)"}


def get_json(url, retries=4):
    delay = 2
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise SystemExit(f"Not found: {url} (check the username)")
            if e.code != 429 and e.code < 500 or attempt == retries:
                raise
        except urllib.error.URLError:
            if attempt == retries:
                raise
        time.sleep(delay)
        delay *= 2


def fetch(username, months=None, data_dir=Path("data")):
    username = username.lower()
    out_dir = data_dir / username
    cache_dir = out_dir / "archives"
    cache_dir.mkdir(parents=True, exist_ok=True)

    archives = get_json(API.format(user=username) + "/games/archives")["archives"]
    if months:
        archives = archives[-months:]

    current = datetime.now(timezone.utc).strftime("%Y/%m")
    games = []
    for url in archives:
        ym = "/".join(url.rstrip("/").split("/")[-2:])
        cache = cache_dir / (ym.replace("/", "-") + ".json")
        if cache.exists() and ym != current:
            month_games = json.loads(cache.read_text())
        else:
            print(f"  fetching {ym} ...")
            month_games = get_json(url).get("games", [])
            cache.write_text(json.dumps(month_games))
            time.sleep(0.5)  # be polite: chess.com rate-limits parallel/fast clients
        games.extend(month_games)

    games.sort(key=lambda g: g.get("end_time", 0))
    (out_dir / "games.json").write_text(json.dumps(games))
    print(f"Saved {len(games)} games from {len(archives)} month(s) to {out_dir / 'games.json'}")
    return games


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("username")
    p.add_argument("--months", type=int, help="only fetch the most recent N months")
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    args = p.parse_args()
    fetch(args.username, args.months, args.data_dir)


if __name__ == "__main__":
    main()
