"""Analyze a downloaded chess.com game history and write an improvement report.

Usage:
    python -m chess_coach.analyze <username> [--engine-games N] [--depth D] [--tz Area/City]

Reads data/<username>/games.json (created by chess_coach.fetch) and writes
reports/<username>.md plus reports/<username>.json (raw stats).

With --engine-games N (default 40) the N most recent games are run through
Stockfish to find where blunders happen (opening/middlegame/endgame, time
trouble, hung pieces, missed tactics, thrown winning positions). Pass 0 to
skip engine analysis. Results are cached, so re-runs are fast.
"""

import argparse
import io
import json
import math
import os
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import chess
import chess.engine
import chess.pgn

DRAW_RESULTS = {"agreed", "repetition", "stalemate", "insufficient", "50move", "timevsinsufficient"}
LOSS_REASONS = {"checkmated": "checkmated", "resigned": "resigned", "timeout": "lost on time",
                "abandoned": "abandoned", "lose": "lost", "kingofthehill": "variant", "threecheck": "variant",
                "bughousepartnerlose": "variant"}
SESSION_GAP = 45 * 60  # seconds between games that starts a new playing session
MIN_GAMES = 5          # minimum sample before an opening/bucket is reported


# ---------------------------------------------------------------- loading

def load_games(username, data_dir):
    path = data_dir / username.lower() / "games.json"
    if not path.exists():
        raise SystemExit(f"{path} not found - run `python -m chess_coach.fetch {username}` first")
    raw = json.loads(path.read_text())
    games = []
    for g in raw:
        if g.get("rules", "chess") != "chess" or "pgn" not in g:
            continue
        me_white = g["white"]["username"].lower() == username.lower()
        me, opp = (g["white"], g["black"]) if me_white else (g["black"], g["white"])
        if me["result"] == "win":
            score, how = 1.0, LOSS_REASONS.get(opp["result"], opp["result"])
        elif me["result"] in DRAW_RESULTS:
            score, how = 0.5, me["result"]
        else:
            score, how = 0.0, LOSS_REASONS.get(me["result"], me["result"])
        games.append({
            "url": g.get("url"),
            "pgn": g["pgn"],
            "end_time": g.get("end_time", 0),
            "time_class": g.get("time_class", "unknown"),
            "time_control": g.get("time_control", ""),
            "rated": g.get("rated", True),
            "color": "white" if me_white else "black",
            "my_rating": me.get("rating"),
            "opp_rating": opp.get("rating"),
            "score": score,
            "how": how,
            "opening": opening_name(g),
            "accuracy": (g.get("accuracies") or {}).get("white" if me_white else "black"),
        })
    games.sort(key=lambda x: x["end_time"])
    return games


def opening_name(g):
    """Return (family, variation) from chess.com's ECO opening URL."""
    url = g.get("eco") or ""
    if not url:
        m = re.search(r'\[ECOUrl "([^"]+)"\]', g.get("pgn", ""))
        url = m.group(1) if m else ""
    slug = url.rstrip("/").split("/")[-1]
    if not slug:
        return ("Unknown", "Unknown")
    words = []
    for w in slug.split("-"):
        if w[:1].isdigit():  # move list like "3...Nf6" starts here
            break
        words.append(w)
    words = words or ["Unknown"]
    family = words[:3]
    for i, w in enumerate(words):
        if w in {"Defense", "Game", "Opening", "Gambit", "Attack", "System", "Variation", "Countergambit"}:
            family = words[: i + 1]
            break
    return (" ".join(family), " ".join(words))


def base_seconds(time_control):
    """'180+2' -> 180, '600' -> 600, daily '1/86400' -> None."""
    if not time_control or "/" in time_control:
        return None
    try:
        return int(time_control.split("+")[0])
    except ValueError:
        return None


def parse(game):
    if "_parsed" not in game:
        game["_parsed"] = chess.pgn.read_game(io.StringIO(game["pgn"]))
    return game["_parsed"]


# ---------------------------------------------------------------- helpers

def score_of(gs):
    return sum(g["score"] for g in gs) / len(gs) if gs else float("nan")


def wdl(gs):
    c = Counter(g["score"] for g in gs)
    return f"{c[1.0]}W / {c[0.5]}D / {c[0.0]}L"


def pct(x):
    return "n/a" if x != x else f"{100 * x:.0f}%"


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


# ---------------------------------------------------------------- stats

def basic_stats(games, tz):
    s = {}
    s["total"] = len(games)
    s["first"] = datetime.fromtimestamp(games[0]["end_time"], tz).date().isoformat()
    s["last"] = datetime.fromtimestamp(games[-1]["end_time"], tz).date().isoformat()

    by_tc = defaultdict(list)
    for g in games:
        by_tc[g["time_class"]].append(g)
    s["time_classes"] = {}
    for tc, gs in sorted(by_tc.items(), key=lambda kv: -len(kv[1])):
        rated = [g for g in gs if g["rated"] and g["my_rating"]]
        recent = rated[-50:]
        s["time_classes"][tc] = {
            "games": len(gs), "score": score_of(gs), "wdl": wdl(gs),
            "white": score_of([g for g in gs if g["color"] == "white"]),
            "black": score_of([g for g in gs if g["color"] == "black"]),
            "rating_start": rated[0]["my_rating"] if rated else None,
            "rating_now": rated[-1]["my_rating"] if rated else None,
            "rating_peak": max(g["my_rating"] for g in rated) if rated else None,
            "recent_score": score_of(recent),
        }
    s["white"] = score_of([g for g in games if g["color"] == "white"])
    s["black"] = score_of([g for g in games if g["color"] == "black"])
    s["overall"] = score_of(games)
    return s


def opening_stats(games):
    res = {}
    for color in ("white", "black"):
        fam, var = defaultdict(list), defaultdict(list)
        for g in games:
            if g["color"] == color:
                fam[g["opening"][0]].append(g)
                var[g["opening"][1]].append(g)
        res[color] = {
            "families": sorted(((k, len(v), score_of(v)) for k, v in fam.items() if len(v) >= MIN_GAMES),
                               key=lambda r: -r[1]),
            "variations": sorted(((k, len(v), score_of(v)) for k, v in var.items() if len(v) >= MIN_GAMES),
                                 key=lambda r: -r[1]),
        }
    return res


def ending_stats(games):
    losses = [g for g in games if g["score"] == 0]
    wins = [g for g in games if g["score"] == 1]
    return {
        "losses": Counter(g["how"] for g in losses).most_common(),
        "wins": Counter(g["how"] for g in wins).most_common(),
        "n_losses": len(losses), "n_wins": len(wins),
    }


def opponent_stats(games):
    buckets = {"much lower (-150+)": [], "lower (-50..-150)": [], "similar (±50)": [],
               "higher (+50..+150)": [], "much higher (+150+)": []}
    for g in games:
        if not (g["my_rating"] and g["opp_rating"]):
            continue
        d = g["opp_rating"] - g["my_rating"]
        key = ("much lower (-150+)" if d <= -150 else "lower (-50..-150)" if d < -50 else
               "similar (±50)" if d <= 50 else "higher (+50..+150)" if d < 150 else "much higher (+150+)")
        buckets[key].append(g)
    return [(k, len(v), score_of(v)) for k, v in buckets.items() if v]


def behaviour_stats(games, tz):
    """Tilt, session fatigue and time-of-day effects (live games only)."""
    live = [g for g in games if g["time_class"] != "daily"]
    after_win, after_loss, after_2loss = [], [], []
    by_index = defaultdict(list)
    by_hour = defaultdict(list)
    idx, streak = 0, 0
    for i, g in enumerate(live):
        new_session = i == 0 or g["end_time"] - live[i - 1]["end_time"] > SESSION_GAP
        if new_session:
            idx, streak = 0, 0
        else:
            prev = live[i - 1]
            (after_win if prev["score"] == 1 else after_loss if prev["score"] == 0 else []).append(g)
            if streak >= 2:
                after_2loss.append(g)
        idx += 1
        by_index[min(idx, 6)].append(g)
        streak = streak + 1 if g["score"] == 0 else 0
        hour = datetime.fromtimestamp(g["end_time"], tz).hour
        by_hour[hour // 3 * 3].append(g)
    return {
        "after_win": (len(after_win), score_of(after_win)),
        "after_loss": (len(after_loss), score_of(after_loss)),
        "after_2_losses": (len(after_2loss), score_of(after_2loss)),
        "session_index": [(("6+" if k == 6 else str(k)), len(v), score_of(v)) for k, v in sorted(by_index.items())],
        "hours": [(f"{h:02d}-{h + 3:02d}h", len(v), score_of(v)) for h, v in sorted(by_hour.items())],
    }


def length_and_clock_stats(games):
    short_losses, losses, time_trouble_losses, clocked_losses = 0, 0, 0, 0
    len_buckets = defaultdict(list)
    for g in games:
        game = parse(g)
        if game is None:
            continue
        plies = sum(1 for _ in game.mainline_moves())
        moves = (plies + 1) // 2
        b = "1-20" if moves <= 20 else "21-35" if moves <= 35 else "36-50" if moves <= 50 else "51+"
        len_buckets[b].append(g)
        if g["score"] != 0:
            continue
        losses += 1
        short_losses += moves <= 25
        base = base_seconds(g["time_control"])
        if base:
            my_clocks = [n.clock() for n in game.mainline()
                         if n.clock() is not None and (n.ply() % 2 == 1) == (g["color"] == "white")]
            if my_clocks:
                clocked_losses += 1
                time_trouble_losses += my_clocks[-1] < max(0.1 * base, 5)
    order = ["1-20", "21-35", "36-50", "51+"]
    return {
        "length": [(b, len(len_buckets[b]), score_of(len_buckets[b])) for b in order if len_buckets[b]],
        "short_loss_share": short_losses / losses if losses else float("nan"),
        "time_trouble_loss_share": time_trouble_losses / clocked_losses if clocked_losses else float("nan"),
    }


# ---------------------------------------------------------------- engine

def win_pct(cp):
    """Lichess win-probability model, cp from the side to move's perspective."""
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def find_engine(path=None):
    for p in (path, os.environ.get("STOCKFISH"), shutil.which("stockfish"), "/usr/games/stockfish",
              "/opt/homebrew/bin/stockfish", "/usr/local/bin/stockfish"):
        if p and Path(p).exists():
            return p
    return None


def phase_of(board):
    pieces = sum(len(board.pieces(t, c)) for t in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)
                 for c in chess.COLORS)
    if board.fullmove_number <= 12 and pieces > 10:
        return "opening"
    return "endgame" if pieces <= 6 else "middlegame"


def analyse_game(engine, g, depth):
    """Return a list of the player's significant mistakes in this game."""
    game = parse(g)
    my_color = chess.WHITE if g["color"] == "white" else chess.BLACK
    base = base_seconds(g["time_control"])
    board = game.board()
    nodes = list(game.mainline())
    limit = chess.engine.Limit(depth=depth)

    # Evaluate every position once: evals[i] is the position before ply i.
    infos = []
    for node in [None] + nodes:
        if node is not None:
            board.push(node.move)
        if board.is_game_over():
            infos.append({"cp": None, "best": None, "board": board.copy(stack=False), "over": board.outcome()})
            continue
        info = engine.analyse(board, limit)
        pov = info["score"].white().score(mate_score=10000)
        pv = info.get("pv") or [None]
        infos.append({"cp": pov, "best": pv[0], "board": board.copy(stack=False), "over": None})

    def white_cp(i):
        inf = infos[i]
        if inf["cp"] is not None:
            return inf["cp"]
        o = inf["over"]
        return 0 if o.winner is None else (10000 if o.winner == chess.WHITE else -10000)

    mistakes, moves_counted = [], Counter()
    for i, node in enumerate(nodes):
        before = infos[i]["board"]
        if before.turn != my_color:
            continue
        sign = 1 if my_color == chess.WHITE else -1
        wp_before = win_pct(sign * white_cp(i))
        wp_after = win_pct(sign * white_cp(i + 1))
        loss = wp_before - wp_after
        phase = phase_of(before)
        moves_counted[phase] += 1
        if loss < 10:
            continue
        kind = "blunder" if loss >= 30 else "mistake" if loss >= 20 else "inaccuracy"
        best = infos[i]["best"]
        tags = []
        if best is not None and best != node.move and (before.is_capture(best) or before.gives_check(best)):
            tags.append("missed tactic")
        reply = infos[i + 1]["best"]
        if reply is not None and loss >= 20 and infos[i + 1]["board"].is_capture(reply):
            tags.append("hung material")
        if wp_before >= 70 and wp_after <= 50:
            tags.append("threw winning position")
        clk = node.clock()
        if base and clk is not None and clk < max(0.1 * base, 5):
            tags.append("time trouble")
        mistakes.append({
            "url": g["url"], "move_no": before.fullmove_number, "color": g["color"],
            "played": before.san(node.move), "best": before.san(best) if best else None,
            "loss": round(loss, 1), "kind": kind, "phase": phase, "tags": tags,
            "fen": before.fen(), "time_class": g["time_class"], "result": g["score"],
        })
    return {"mistakes": mistakes, "moves": dict(moves_counted)}


def engine_stats(games, n, depth, engine_path, cache_path):
    if n <= 0:
        return None
    path = find_engine(engine_path)
    if not path:
        print("Stockfish not found - skipping engine analysis (install it or pass --engine PATH)")
        return None
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    sample = games[-n:]
    todo = [g for g in sample if f"{g['url']}@{depth}" not in cache]
    if todo:
        print(f"Engine-analysing {len(todo)} game(s) at depth {depth} (cached: {len(sample) - len(todo)}) ...")
        with chess.engine.SimpleEngine.popen_uci(path) as engine:
            engine.configure({"Threads": max(1, (os.cpu_count() or 2) - 1), "Hash": 128})
            for k, g in enumerate(todo, 1):
                cache[f"{g['url']}@{depth}"] = analyse_game(engine, g, depth)
                if k % 5 == 0 or k == len(todo):
                    print(f"  {k}/{len(todo)}")
                    cache_path.write_text(json.dumps(cache))
    results = [cache[f"{g['url']}@{depth}"] for g in sample]
    all_m = [m for r in results for m in r["mistakes"]]
    moves = Counter()
    for r in results:
        moves.update(r["moves"])
    by_phase = {}
    for ph in ("opening", "middlegame", "endgame"):
        ms = [m for m in all_m if m["phase"] == ph]
        by_phase[ph] = {
            "moves": moves[ph],
            "blunders": sum(m["kind"] == "blunder" for m in ms),
            "mistakes": sum(m["kind"] == "mistake" for m in ms),
            "inaccuracies": sum(m["kind"] == "inaccuracy" for m in ms),
        }
    serious = [m for m in all_m if m["kind"] != "inaccuracy"]
    worst = sorted(serious, key=lambda m: -m["loss"])[:12]
    return {
        "games": len(sample), "depth": depth, "total_moves": sum(moves.values()),
        "by_phase": by_phase,
        "tags": Counter(t for m in serious for t in m["tags"]).most_common(),
        "serious": len(serious),
        "blunders_per_game": sum(m["kind"] == "blunder" for m in all_m) / len(sample),
        "worst": worst,
    }


# ---------------------------------------------------------------- coaching

def recommendations(s):
    """Turn the numbers into prioritised, concrete advice."""
    recs = []
    end, beh, clk, eng = s["endings"], s["behaviour"], s["clock"], s.get("engine")

    if eng:
        rates = {ph: (v["blunders"] + v["mistakes"]) / v["moves"] for ph, v in eng["by_phase"].items() if v["moves"] >= 30}
        if rates:
            worst_phase = max(rates, key=rates.get)
            tips = {
                "opening": "Learn the *ideas* (not just moves) of your 1-2 main openings; after each game, check where you left theory and what the engine wanted.",
                "middlegame": "Do 15-20 min of tactics puzzles daily and use a blunder-check before every move: *what are all checks, captures and threats for both sides?*",
                "endgame": "Study fundamental endgames (king+pawn opposition, Lucena/Philidor rook endings) and practise converting won positions vs. the computer.",
            }
            recs.append((90, f"Most of your serious errors per move happen in the **{worst_phase}** "
                             f"({pct(rates[worst_phase])} of moves there are mistakes/blunders). {tips[worst_phase]}"))
        tags = dict(eng["tags"])
        if eng["serious"]:
            if tags.get("hung material", 0) / eng["serious"] >= 0.25:
                recs.append((95, f"**{tags['hung material']}** of your {eng['serious']} serious errors let the opponent win material "
                                 "immediately. Before releasing a move, ask *“what does this move leave undefended?”* — this is the single "
                                 "highest-value habit to build."))
            if tags.get("missed tactic", 0) / eng["serious"] >= 0.25:
                recs.append((80, f"In **{tags['missed tactic']}** serious errors a forcing move (check/capture) was best and you missed it. "
                                 "Train pattern recognition: forks, pins, skewers, discovered attacks (puzzle themes on chess.com)."))
            if tags.get("threw winning position", 0) >= 3:
                recs.append((85, f"You threw away **{tags['threw winning position']}** winning positions. When ahead: trade pieces "
                                 "(not pawns), remove counterplay first, and slow down — don't hurry to finish."))
            if tags.get("time trouble", 0) / eng["serious"] >= 0.2:
                recs.append((75, "A large share of your blunders come in time trouble — see the time-management advice."))

    if end["n_losses"] >= 10:
        timeouts = dict(end["losses"]).get("lost on time", 0) / end["n_losses"]
        if timeouts >= 0.2 or clk["time_trouble_loss_share"] >= 0.4:
            recs.append((88, f"**{pct(timeouts)}** of your losses are on time and {pct(clk['time_trouble_loss_share'])} end with you "
                             "low on the clock. Budget time: play opening moves quickly, spend it on critical moments, and if you "
                             "keep flagging, switch to an increment time control (e.g. 10+5 or 15+10)."))
    abandoned = dict(end["losses"]).get("abandoned", 0)
    if end["n_losses"] >= 10 and abandoned / end["n_losses"] >= 0.08:
        recs.append((84, f"**{abandoned}** of your losses ({pct(abandoned / end['n_losses'])}) are games you abandoned. "
                         "Play every game to the end (or resign): you lose rating for free, and bad positions are where "
                         "opponents at your level most often blunder back."))
    if clk["short_loss_share"] >= 0.3:
        recs.append((70, f"**{pct(clk['short_loss_share'])}** of your losses are over by move 25 — early disasters. Prioritise "
                         "opening principles (develop, castle, don't move a piece twice, don't grab pawns with the queen early)."))

    n_al, s_al = beh["after_loss"]
    n_aw, s_aw = beh["after_win"]
    if n_al >= 20 and n_aw >= 20 and s_aw - s_al >= 0.07:
        n2, s2 = beh["after_2_losses"]
        recs.append((86, f"Tilt: you score {pct(s_aw)} after a win but {pct(s_al)} after a loss"
                         + (f" and {pct(s2)} after two losses in a row" if n2 >= 10 else "")
                         + ". Adopt a hard rule: **stop (or take a 15-min break) after 2 consecutive losses.**"))
    sess = {k: (n, sc) for k, n, sc in beh["session_index"]}
    early = [sess[k] for k in ("1", "2") if k in sess]
    late = [sess[k] for k in ("5", "6+") if k in sess]
    if early and late and sum(n for n, _ in late) >= 20:
        e = sum(n * sc for n, sc in early) / sum(n for n, _ in early)
        l = sum(n * sc for n, sc in late) / sum(n for n, _ in late)
        if e - l >= 0.07:
            recs.append((72, f"Fatigue: you score {pct(e)} in the first two games of a session vs {pct(l)} from game 5 on. "
                             "Cap sessions at ~4 games."))

    if abs(s["basic"]["white"] - s["basic"]["black"]) >= 0.08:
        weak = "black" if s["basic"]["black"] < s["basic"]["white"] else "white"
        recs.append((65, f"You're much weaker as **{weak}** ({pct(s['basic'][weak])} vs "
                         f"{pct(s['basic']['white' if weak == 'black' else 'black'])}). Your repertoire for {weak} deserves attention first."))

    overall = s["basic"]["overall"]
    for color in ("white", "black"):
        bad = [r for r in s["openings"][color]["families"] if r[1] >= 8 and r[2] <= overall - 0.1]
        good = [r for r in s["openings"][color]["families"] if r[1] >= 8 and r[2] >= overall + 0.08]
        for name, n, sc in bad[:2]:
            recs.append((68, f"As {color}, you score only {pct(sc)} in **{name}** positions over {n} games (your average is "
                             f"{pct(overall)}). Learn the typical plans and traps for your side of this opening, or pick a "
                             "different reply that avoids it."))
        if good:
            name, n, sc = good[0]
            recs.append((40, f"As {color}, you do well in **{name}** positions ({pct(sc)} in {n} games)."))

    opp = {k: (n, sc) for k, n, sc in s["opponents"]}
    lower = [opp[k] for k in ("much lower (-150+)", "lower (-50..-150)") if k in opp]
    if lower and sum(n for n, _ in lower) >= 15:
        sc = sum(n * x for n, x in lower) / sum(n for n, _ in lower)
        if sc < 0.6:
            recs.append((60, f"You only score {pct(sc)} against lower-rated opponents (expected ~65-75%). "
                             "Treat every opponent seriously and don't play for cheap traps."))

    recs.sort(key=lambda r: -r[0])
    return [r[1] for r in recs]


def training_plan(s):
    eng = s.get("engine")
    phase_focus = "tactics"
    if eng:
        rates = {ph: (v["blunders"] + v["mistakes"]) / v["moves"] for ph, v in eng["by_phase"].items() if v["moves"] >= 30}
        if rates:
            phase_focus = {"opening": "openings", "middlegame": "tactics", "endgame": "endgames"}[max(rates, key=rates.get)]
    main_tc = next(iter(s["basic"]["time_classes"]), "rapid")
    return f"""A weekly routine (~5 hours) built around your weak spots:

| Day | Session |
|---|---|
| Mon | 20 min puzzles + 2 {('rapid' if main_tc in ('bullet', 'blitz') else main_tc)} games, review both losses *without* the engine first |
| Tue | 30 min focus study: **{phase_focus}** |
| Wed | 20 min puzzles + 2 games |
| Thu | 30 min: replay one master game in your main opening, guess-the-move style |
| Fri | 20 min puzzles + 2 games |
| Sat | Longer games (15+10 or 30+0) — practise the blunder-check on every move |
| Sun | Re-run this report; compare the numbers with last week |

Rules while playing: blunder-check (checks, captures, threats) before every move; stop after 2 losses in a row;
review every loss and write one sentence on *why* you lost."""


# ---------------------------------------------------------------- report

def render(username, s):
    b = s["basic"]
    out = [f"# Chess improvement report — {username}",
           f"_{b['total']} standard games, {b['first']} → {b['last']}. Generated {datetime.now():%Y-%m-%d}._", ""]

    out += ["## Top priorities", ""]
    recs = recommendations(s)
    out += [f"{i}. {r}" for i, r in enumerate(recs, 1)] or ["No strong weaknesses detected — play more games for a bigger sample."]
    out += ["", "## Suggested training plan", "", training_plan(s), ""]

    out += ["## Results by time control", "",
            table(["Time class", "Games", "W/D/L", "Score", "As white", "As black", "Rating start → now (peak)", "Last 50"],
                  [[tc, v["games"], v["wdl"], pct(v["score"]), pct(v["white"]), pct(v["black"]),
                    f"{v['rating_start']} → {v['rating_now']} ({v['rating_peak']})", pct(v["recent_score"])]
                   for tc, v in b["time_classes"].items()]), ""]

    for color in ("white", "black"):
        fams = s["openings"][color]["families"][:12]
        if fams:
            out += [f"## Openings as {color} (≥{MIN_GAMES} games)", "",
                    table(["Opening", "Games", "Score"], [[n, c, pct(x)] for n, c, x in fams]), ""]

    e = s["endings"]
    out += ["## How your games end", "",
            table(["Your losses", "Count", "Share"], [[k, v, pct(v / e["n_losses"])] for k, v in e["losses"]]), "",
            table(["Your wins", "Count", "Share"], [[k, v, pct(v / e["n_wins"])] for k, v in e["wins"]]), ""]

    c = s["clock"]
    out += ["## Game length & clock", "",
            table(["Moves", "Games", "Score"], [[k, n, pct(x)] for k, n, x in c["length"]]), "",
            f"- Losses over by move 25: **{pct(c['short_loss_share'])}**",
            f"- Losses where you finished with <10% of your clock: **{pct(c['time_trouble_loss_share'])}**", ""]

    out += ["## Against stronger / weaker opponents", "",
            table(["Opponent rating vs yours", "Games", "Score"], [[k, n, pct(x)] for k, n, x in s["opponents"]]), ""]

    h = s["behaviour"]
    out += ["## Tilt, fatigue & time of day (live games)", "",
            f"- After a win: {pct(h['after_win'][1])} ({h['after_win'][0]} games)",
            f"- After a loss: {pct(h['after_loss'][1])} ({h['after_loss'][0]} games)",
            f"- After 2+ losses in a row: {pct(h['after_2_losses'][1])} ({h['after_2_losses'][0]} games)", "",
            table(["Game # in session", "Games", "Score"], [[k, n, pct(x)] for k, n, x in h["session_index"]]), "",
            table([f"Time of day ({s['tz']})", "Games", "Score"], [[k, n, pct(x)] for k, n, x in h["hours"]]), ""]

    eng = s.get("engine")
    if eng:
        out += [f"## Engine analysis (last {eng['games']} games, Stockfish depth {eng['depth']})", "",
                f"- Blunders per game: **{eng['blunders_per_game']:.2f}**",
                f"- Serious errors (mistakes + blunders): **{eng['serious']}** in {eng['total_moves']} moves", "",
                table(["Phase", "Your moves", "Blunders", "Mistakes", "Inaccuracies", "Error rate"],
                      [[ph, v["moves"], v["blunders"], v["mistakes"], v["inaccuracies"],
                        pct((v["blunders"] + v["mistakes"]) / v["moves"]) if v["moves"] else "n/a"]
                       for ph, v in eng["by_phase"].items()]), ""]
        if eng["tags"]:
            out += [table(["Error pattern", "Count"], eng["tags"]), ""]
        out += ["### Your biggest mistakes — go through these on a board", "",
                table(["Game", "Move", "Played", "Best", "Win% lost", "Phase", "Notes"],
                      [[f"[link]({m['url']})", f"{m['move_no']}{'.' if m['color'] == 'white' else '...'}",
                        m["played"], m["best"] or "", f"{m['loss']:.0f}", m["phase"], ", ".join(m["tags"])]
                       for m in eng["worst"]]), ""]
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("username")
    p.add_argument("--engine-games", type=int, default=40, help="recent games to run through Stockfish (0 = skip)")
    p.add_argument("--depth", type=int, default=12, help="Stockfish search depth")
    p.add_argument("--engine", help="path to the Stockfish binary")
    p.add_argument("--tz", default="UTC", help="your time zone for time-of-day stats, e.g. America/New_York")
    p.add_argument("--time-class", choices=["bullet", "blitz", "rapid", "daily"], help="only analyse this time class")
    p.add_argument("--since", help="only analyse games on/after this date (YYYY-MM-DD)")
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--out-dir", type=Path, default=Path("reports"))
    args = p.parse_args()

    tz = ZoneInfo(args.tz)
    games = load_games(args.username, args.data_dir)
    if args.since:
        cutoff = datetime.fromisoformat(args.since).replace(tzinfo=tz).timestamp()
        games = [g for g in games if g["end_time"] >= cutoff]
    if args.time_class:
        games = [g for g in games if g["time_class"] == args.time_class]
    if not games:
        raise SystemExit("No games to analyse.")

    s = {"tz": args.tz,
         "basic": basic_stats(games, tz),
         "openings": opening_stats(games),
         "endings": ending_stats(games),
         "opponents": opponent_stats(games),
         "behaviour": behaviour_stats(games, tz),
         "clock": length_and_clock_stats(games)}
    cache = args.data_dir / args.username.lower() / "engine_cache.json"
    s["engine"] = engine_stats(games, args.engine_games, args.depth, args.engine, cache)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    md = args.out_dir / f"{args.username.lower()}.md"
    md.write_text(render(args.username, s))
    (args.out_dir / f"{args.username.lower()}.json").write_text(json.dumps(s, indent=1, default=str))
    print(f"Report written to {md}")


if __name__ == "__main__":
    main()
