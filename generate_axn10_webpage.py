#!/usr/bin/env python3
"""Generate an HTML playback report from the AXN10 SQLite playback database."""

import sqlite3
import html
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATABASE_FILE = Path("<PLAYBACK_DATABASE>")
OUTPUT_HTML = Path("<HTML_FILE_LOCATION>")
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

def number(value, default=0.0):
    try:
        return float(value) if value is not None and value != "" else default
    except (TypeError, ValueError):
        return default


def parse_datetime(value):
    try:
        return datetime.strptime((value or "").strip(), DATETIME_FORMAT)
    except (TypeError, ValueError):
        return None


def load_rows(path):
    """Read recorded playback from SQLite without creating or modifying it."""
    path = Path(path)
    rows = []
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                         uri=True, timeout=10) as connection:
        connection.row_factory = sqlite3.Row
        records = connection.execute("""
            SELECT rowid AS playback_id,
                   "Start Time" AS start, "Stop Time" AS end,
                   "Artist" AS artist, "Track" AS track, "Album" AS album,
                   "Played Duration (Sec)" AS seconds,
                   "Played Duration (Min)" AS minutes
            FROM playback
            ORDER BY rowid
        """)
        for raw in records:
            start = parse_datetime(raw["start"])
            end = parse_datetime(raw["end"])
            seconds = number(raw["seconds"])
            if seconds <= 0:
                seconds = number(raw["minutes"]) * 60
            if seconds <= 0 and start is not None and end is not None:
                seconds = max(0.0, (end - start).total_seconds())
            if start is None:
                continue
            rows.append({
                "start": start,
                "end": end,
                "artist": (raw["artist"] or "").strip() or "Unknown Artist",
                "track": (raw["track"] or "").strip() or "Unknown Track",
                "album": (raw["album"] or "").strip() or "Unknown Album",
                "seconds": max(0.0, seconds),
                "line": raw["playback_id"],
            })
    return rows


def duration_label(seconds):
    minutes = seconds / 60
    if minutes >= 60:
        hours = int(minutes // 60)
        remaining = minutes - hours * 60
        return f"{hours} hr {remaining:.1f} min"
    return f"{minutes:.1f} min"


def esc(value):
    return html.escape(str(value), quote=True)


def normalise(value):
    return " ".join((value or "").split()).casefold()


def load_album_catalogue(path):
    """Only use albums with a complete, unambiguous numbered track list."""
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",
                         uri=True, timeout=10) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(asset_library)")}
        if not {"track_number", "disc_number"}.issubset(columns):
            return {}, "Refresh the AssetUPnP cache with the updated sync to load track order."
        records = connection.execute("""
            SELECT "Artist", "Title", "Album", track_number, disc_number
            FROM asset_library
        """).fetchall()
    albums = defaultdict(list)
    for artist, title, album, track, disc in records:
        albums[normalise(album)].append((artist or "", title or "", album or "", track, disc))
    catalogue = {}
    excluded = 0
    for key, tracks in albums.items():
        slots = defaultdict(set)
        valid = bool(key)
        for artist, title, album, track, disc in tracks:
            if (not title or not isinstance(track, int) or track < 1
                    or not isinstance(disc, int) or disc < 1):
                valid = False
                break
            slots[(disc, track)].add((normalise(title), normalise(artist)))
        if not valid or any(len(values) != 1 for values in slots.values()):
            excluded += 1
            continue
        discs = sorted({disc for disc, _ in slots})
        if discs != list(range(1, max(discs) + 1)):
            excluded += 1
            continue
        for disc in discs:
            numbers = sorted(track for d, track in slots if d == disc)
            if numbers != list(range(1, max(numbers) + 1)):
                valid = False
        ordered = [next(iter(slots[slot]))[0] for slot in sorted(slots)]
        # Playback lacks disc/track IDs: repeated titles cannot be distinguished.
        if not valid or len(set(ordered)) != len(ordered):
            excluded += 1
            continue
        artists = {artist.strip() for artist, *_ in tracks if artist.strip()}
        catalogue[key] = {
            "title": tracks[0][2],
            "artist": next(iter(artists)) if len(artists) == 1 else "Various Artists",
            "tracks": ordered,
        }
    note = f"{excluded} albums excluded because track order is missing or ambiguous." if excluded else ""
    return catalogue, note


def count_album_plays(rows, catalogue):
    """Require the entire ordered list; adjacent repeats do not advance it."""
    stats = defaultdict(lambda: {"plays": 0, "seconds": 0.0})
    active_album = None
    position = -1
    for row in sorted(rows, key=lambda item: (item["start"], item.get("line", 0))):
        album = normalise(row["album"])
        definition = catalogue.get(album)
        if definition is None:
            active_album, position = None, -1
            continue
        display_key = (definition["artist"], definition["title"])
        # Listening time includes partial sessions as well as complete plays.
        stats[display_key]["seconds"] += row["seconds"]
        track = normalise(row["track"])
        sequence = definition["tracks"]
        if album != active_album:
            active_album, position = album, -1
        if position >= 0 and track == sequence[position]:
            continue  # A pause/stop may produce another row for this track.
        if position + 1 < len(sequence) and track == sequence[position + 1]:
            position += 1
            if position == len(sequence) - 1:
                stats[display_key]["plays"] += 1
        elif track == sequence[0]:
            position = 0
            if len(sequence) == 1:
                stats[display_key]["plays"] += 1
        else:
            position = -1
    return {key: value for key, value in stats.items() if value["plays"] > 0}


def build_report(rows, catalogue=None, catalogue_note=""):
    total_seconds = sum(row["seconds"] for row in rows)
    total_minutes = total_seconds / 60
    total_hours = total_minutes / 60

    # Group by both track and artist so identically titled tracks by different
    # artists remain separate.
    aggregates = defaultdict(lambda: {"plays": 0, "seconds": 0.0, "last": None})
    for row in rows:
        key = (row["artist"], row["track"])
        item = aggregates[key]
        item["plays"] += 1
        item["seconds"] += row["seconds"]
        if item["last"] is None or row["start"] > item["last"]:
            item["last"] = row["start"]

    by_plays = sorted(
        aggregates.items(),
        key=lambda pair: (-pair[1]["plays"], -pair[1]["seconds"],
                          pair[0][1].casefold(), pair[0][0].casefold()),
    )[:5]
    most_recent = sorted(rows, key=lambda row: row["start"], reverse=True)[:5]

    # Count playback entries per artist, excluding "Various Artists" regardless
    # of capitalization or surrounding whitespace.
    artist_stats = defaultdict(lambda: {"plays": 0, "seconds": 0.0})
    for row in rows:
        artist_name = row["artist"].strip()
        if artist_name.casefold() != "various artists":
            artist_stats[artist_name]["plays"] += 1
            artist_stats[artist_name]["seconds"] += row["seconds"]
    by_artist_plays = sorted(
        artist_stats.items(),
        key=lambda item: (-item[1]["plays"], -item[1]["seconds"], item[0].casefold())
    )[:5]

    album_stats = count_album_plays(rows, catalogue or {})

    by_album_plays = sorted(
        album_stats.items(),
        key=lambda item: (
            -item[1]["plays"],
            -item[1]["seconds"],
            item[0][1].casefold(),
            item[0][0].casefold(),
        ),
    )[:5]

    def ranking_rows(items, metric):
        if not items:
            return '<li class="empty">No playback entries found.</li>'
        result = []
        max_value = max((data[metric] for _, data in items), default=1) or 1
        for index, (key, data) in enumerate(items, start=1):
            artist, track = key
            value = data[metric]
            if metric == "seconds":
                display = duration_label(value)
            else:
                display = f'{value} play{"s" if value != 1 else ""}'
            width = max(3, value / max_value * 100)
            result.append(
                '<li class="rank-item">'
                f'<span class="rank">{index}</span>'
                '<div class="rank-main">'
                f'<div class="track">{esc(track)}</div>'
                f'<div class="artist">{esc(artist)}</div>'
                f'<div class="bar"><span style="width:{width:.1f}%"></span></div>'
                '</div>'
                f'<span class="metric">{esc(display)}</span>'
                '</li>'
            )
        return "<br>".join(result)

    if by_artist_plays:
        max_artist_plays = max(stats["plays"] for _, stats in by_artist_plays) or 1
        artist_html = []
        for index, (artist_name, stats) in enumerate(by_artist_plays, start=1):
            count = stats["plays"]
            total_artist_seconds = stats["seconds"]
            width = max(3, count / max_artist_plays * 100)
            artist_html.append(
                '<li class="rank-item">'
                f'<span class="rank">{index}</span>'
                '<div class="rank-main">'
                f'<div class="track">{esc(artist_name)}</div>'
                f'<div class="artist">{esc(duration_label(total_artist_seconds))} total listening time</div>'
                f'<div class="bar"><span style="width:{width:.1f}%"></span></div>'
                '</div>'
                f'<span class="metric">{count} play{"s" if count != 1 else ""}</span>'
                '</li>'
            )
        artist_list = "<br>".join(artist_html)
    else:
        artist_list = '<li class="empty">No artist playback entries found.</li>'

    if by_album_plays:
        max_album_plays = max(stats["plays"] for _, stats in by_album_plays) or 1
        album_html = []
        for index, ((artist_name, album_name), stats) in enumerate(by_album_plays, start=1):
            count = stats["plays"]
            total_album_seconds = stats["seconds"]
            width = max(3, count / max_album_plays * 100)
            album_html.append(
                '<li class="rank-item">'
                f'<span class="rank">{index}</span>'
                '<div class="rank-main">'
                f'<div class="track">{esc(album_name)}</div>'
                f'<div class="artist">{esc(artist_name)} · {esc(duration_label(total_album_seconds))} total listening time</div>'
                f'<div class="bar"><span style="width:{width:.1f}%"></span></div>'
                '</div>'
                f'<span class="metric">{count} play{"s" if count != 1 else ""}</span>'
                '</li>'
            )
        album_list = "<br>".join(album_html)
    else:
        album_list = '<li class="empty">No complete album plays found.</li>'

    if most_recent:
        recent_html = []
        for row in most_recent:
            stamp = row["start"].strftime("%d %b %Y, %H:%M:%S")
            recent_html.append(
                '<li class="recent-item">'
                '<div class="recent-date">' + esc(stamp) + '</div>'
                '<div class="track">' + esc(row["track"]) + '</div>'
                '<div class="artist">' + esc(row["artist"]) + '</div>'
                '<div class="recent-duration">' + esc(duration_label(row["seconds"])) + '</div>'
                '</li>'
            )
        recent_list = "<br>".join(recent_html)
    else:
        recent_list = '<li class="empty">No playback entries found.</li>'

    last_updated = datetime.now().astimezone().strftime("%d %b %Y, %H:%M:%S %Z")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Cambridge Audio AXN10 Playback Report</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #0b1120; --panel: #121c2e; --panel2: #19263a;
  --text: #edf3fb; --muted: #9eafc5; --accent: #70c1ff; --line: #28384f;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
main {{ max-width: 1050px; margin: 0 auto; padding: 28px 18px 48px; }}
header {{ text-align: center; padding: 18px 0 26px; }}
.eyebrow {{ color: var(--accent); text-transform: uppercase; letter-spacing: .16em;
  font-size: .76rem; font-weight: 700; }}
h1 {{ margin: 6px 0; font-size: clamp(1.7rem, 4vw, 2.5rem); line-height: 1.15; }}
.subtitle {{ color: var(--muted); margin: 8px 0 0; }}
.stats {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }}
.stat, section {{ background: var(--panel); border: 1px solid var(--line);
  border-radius: 16px; }}
.stat {{ padding: 18px; }}
.stat-label {{ color: var(--muted); font-size: .86rem; }}
.stat-value {{ font-size: clamp(1.35rem, 3vw, 2rem); font-weight: 750; margin-top: 3px; }}
section {{ padding: 20px; margin-top: 18px; }}
h2 {{ font-size: 1.15rem; margin: 0 0 4px; }}
.section-note {{ color: var(--muted); font-size: .9rem; margin: 0 0 18px; }}
ol, ul {{ list-style: none; padding: 0; margin: 0; }}
.rank-item {{ display: flex; align-items: center; gap: 13px; padding: 12px 0;
  border-top: 1px solid var(--line); }}
.rank {{ color: var(--muted); font-weight: 750; width: 22px; flex: 0 0 22px; }}
.rank-main {{ min-width: 0; flex: 1; }}
.track {{ font-weight: 650; overflow-wrap: anywhere; }}
.artist {{ color: var(--muted); font-size: .9rem; overflow-wrap: anywhere; }}
.metric {{ font-size: .9rem; white-space: nowrap; text-align: right; }}
.bar {{ background: var(--panel2); height: 5px; border-radius: 99px; margin-top: 8px; overflow: hidden; }}
.bar span {{ display: block; height: 100%; border-radius: inherit; background: var(--accent); }}
.recent-item {{ display: grid; grid-template-columns: 165px minmax(0, 1fr) auto;
  gap: 3px 14px; padding: 13px 0; border-top: 1px solid var(--line); }}
.recent-date {{ color: var(--accent); font-size: .85rem; grid-row: span 2; }}
.recent-duration {{ color: var(--muted); font-size: .85rem; white-space: nowrap; grid-column: 3; grid-row: 1 / span 2; }}
.empty {{ color: var(--muted); padding: 12px 0; }}
footer {{ color: var(--muted); font-size: .8rem; text-align: center; padding-top: 22px; }}
@media (max-width: 650px) {{
  .stats {{ grid-template-columns: 1fr; gap: 9px; }}
  .stat {{ padding: 13px 16px; }}
  .recent-item {{ grid-template-columns: 1fr auto; }}
  .recent-date {{ grid-row: auto; grid-column: 1 / -1; }}
  .recent-duration {{ grid-column: 2; grid-row: 2 / span 2; align-self: center; }}
}}
</style>
</head>
<body>
<main>
<header>
  <div class="eyebrow">Listening history</div>
  <h1>Cambridge Audio AXN10</h1>
  <p class="subtitle">Playback Report</p>
</header>
<div class="stats">
  <div class="stat"><div class="stat-label">Total listening time</div>
    <div class="stat-value">{total_hours:.2f} hours</div>
    <div class="stat-label">{total_minutes:.0f} minutes</div></div>
  <div class="stat"><div class="stat-label">Track plays</div>
    <div class="stat-value">{len(rows):,}</div><div class="stat-label">Recorded playback entries</div></div>
  <div class="stat"><div class="stat-label">Unique tracks</div>
    <div class="stat-value">{len(aggregates):,}</div><div class="stat-label">Distinct artist and track pairs</div></div>
</div>
<section>
  <h2>Top 5 Most Played Tracks</h2>
  <p class="section-note">Ranked by number of playback entries. Ties are broken by total duration.</p>
  <ol>{ranking_rows(by_plays, "plays")}</ol>
</section>
<section>
  <h2>Top 5 Most Played Albums</h2>
  <p class="section-note">Complete plays: every track in album order; consecutive repeats allowed.
  Listening time includes partial plays. {esc(catalogue_note)}</p>
  <ol>{album_list}</ol>
</section>
<section>
  <h2>Top 5 most played artists</h2>
  <p class="section-note">Ranked by number of playback entries. “Various Artists” is excluded.</p>
  <ol>{artist_list}</ol>
</section>
<section>
  <h2>Top 5 most recent</h2>
  <p class="section-note">Latest five entries, newest first.</p>
  <ul>{recent_list}</ul>
</section>
<footer>
  Source: {esc(DATABASE_FILE.name)} · Generated {esc(last_updated)} · Page refreshes every 5 minutes
</footer>
</main>
</body>
</html>
"""


def main():
    if not DATABASE_FILE.is_file():
        raise SystemExit(f"SQLite database not found: {DATABASE_FILE}")
    rows = load_rows(DATABASE_FILE)
    catalogue, note = load_album_catalogue(DATABASE_FILE)
    page = build_report(rows, catalogue, note)
    if note:
        print(note)
    OUTPUT_HTML.write_text(page, encoding="utf-8")
    print(f"Report generated: {OUTPUT_HTML}")
    print(f"Rows included: {len(rows)}")


if __name__ == "__main__":
    main()

