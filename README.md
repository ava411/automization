# chess_coach

Download your chess.com games and get a report on where you lose points.

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# optional, for engine analysis: install Stockfish (apt install stockfish / brew install stockfish)
```

## Usage

```sh
# download every game (re-runs only fetch new months)
.venv/bin/python -m chess_coach.fetch obo411
.venv/bin/python -m chess_coach.fetch avaa411

# report for one or more accounts of the same player
.venv/bin/python -m chess_coach.analyze obo411 avaa411 --since 2025-01-01 --engine-games 150
```

The report goes to `reports/<names>.md` (plus raw stats in `.json`). It covers results by time
control, color and opening, how games end, clock trouble, tilt and session fatigue, and a
Stockfish pass over your most recent games: blunders by phase, hung pieces, missed tactics and
winning positions thrown away. It finishes with prioritised recommendations and a training plan.

`data/` and `reports/` are git-ignored.
