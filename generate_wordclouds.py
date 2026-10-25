#!/usr/bin/env python3
"""Generate artist and genre word-cloud webpages from AXN10 playback.

Install once: python3 -m pip install wordcloud
Run:          python3 generate_axn10_wordclouds.py

Weights are playback-entry counts, not listening duration. Each artist name
stays together. Semicolon-separated genres receive one count each per play.
The HTML files embed their PNGs and need no external images or JavaScript.
"""

import argparse
import base64
from collections import Counter
from datetime import datetime
import html
import io
from pathlib import Path
import sqlite3
import sys
import tempfile


DATABASE_FILE = Path('<PLAYBACK_DATABASE>')
OUTPUT_DIRECTORY = Path('<ROOT_HTML_FILE_LOCATION>l')
IGNORED = {'', 'unknown', 'unknown artist', 'unknown genre', 'none', 'null'}


def load_counts(database):
    """Read a consistent playback snapshot without changing the database."""
    artists, genres = Counter(), Counter()
    artist_labels, genre_labels = {}, {}
    total = 0
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro',
                         uri=True, timeout=10) as connection:
        for artist, genre in connection.execute('SELECT "Artist", "Genre" FROM playback'):
            total += 1
            # Keep artist collaborations intact, rather than guessing how names split.
            label = ' '.join((artist or '').split())
            key = label.casefold()
            if key not in IGNORED and key != 'various artists':
                artist_labels.setdefault(key, label)
                artists[key] += 1
            # The logger joins multiple genre values with semicolons. Deduplicate
            # within a playback row so the same genre is counted only once.
            seen = set()
            for value in (genre or '').split(';'):
                label = ' '.join(value.split())
                key = label.casefold()
                if key not in IGNORED and key not in seen:
                    genre_labels.setdefault(key, label)
                    genres[key] += 1
                    seen.add(key)
    return ({artist_labels[k]: v for k, v in artists.items()},
            {genre_labels[k]: v for k, v in genres.items()}, total)


def cloud_image(frequencies, font_path=None):
    from wordcloud import WordCloud

    cloud = WordCloud(
        width=1600, height=900, scale=1.5,
        background_color='#0b1120', colormap='Set2',
        max_words=200, min_font_size=12, max_font_size=200,
        relative_scaling=0.5, prefer_horizontal=0.9,
        collocations=False, repeat=False, random_state=42,
        margin=10, font_path=font_path,
    ).generate_from_frequencies(frequencies)
    buffer = io.BytesIO()
    cloud.to_image().save(buffer, format='PNG')
    return base64.b64encode(buffer.getvalue()).decode('ascii')


def build_page(kind, frequencies, total, generated_at, font_path=None):
    title = f'AXN10 {kind} word cloud'
    if frequencies:
        encoded = cloud_image(frequencies, font_path)
        visual = f'<img class="cloud" src="data:image/png;base64,{encoded}" alt="{html.escape(title)}. Larger names have more plays; exact counts follow below.">'
    else:
        visual = '<p class="empty">No matching playback metadata yet.</p>'
    counts = ''.join(
        f'<tr><th scope="row">{html.escape(name)}</th><td>{count:,}</td></tr>'
        for name, count in sorted(frequencies.items(), key=lambda pair: (-pair[1], pair[0].casefold()))
    )
    note = ('Unknown artists and Various Artists are excluded.' if kind == 'Artist'
            else 'Unknown genres are excluded. Each genre on a track receives one count per play.')
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>{title}</title>
<style>
:root {{color-scheme:dark}}
* {{box-sizing:border-box}}
body {{margin:0;background:#0b1120;color:#edf3fb;font:16px/1.5 system-ui,sans-serif}}
main {{max-width:1300px;margin:auto;padding:30px 18px}}
header {{text-align:center}}
h1 {{font-size:clamp(1.8rem,4vw,2.8rem);margin:12px 0}}
p,footer {{color:#9eafc5}}
nav {{display:flex;gap:20px;justify-content:center;flex-wrap:wrap}}
a {{color:#70c1ff}}
.panel {{margin-top:24px;border:1px solid #28384f;border-radius:18px;overflow:hidden;background:#0b1120}}
.cloud {{display:block;width:100%;height:auto}}
.empty {{text-align:center;padding:100px 20px}}
details {{margin-top:22px;padding:18px;background:#121c2e;border:1px solid #28384f;border-radius:14px}}
summary {{cursor:pointer;color:#70c1ff}}
table {{width:100%;border-collapse:collapse;margin-top:16px}}
th,td {{text-align:left;padding:9px;border-bottom:1px solid #28384f;overflow-wrap:anywhere}}
td {{text-align:right;font-variant-numeric:tabular-nums}}
footer {{text-align:center;font-size:.8rem;margin-top:24px}}
</style>
</head>
<body><main>
<header>
<nav><a href="artist_wordcloud.html">Artists</a><a href="genre_wordcloud.html">Genres</a><a href="axn.html">Playback report</a></nav>
<h1>{kind} word cloud</h1>
<p>Cambridge Audio AXN10 · Bigger words mean more plays.</p>
<p>{len(frequencies):,} distinct names across {total:,} playback entries. Up to 200 names shown.</p>
</header>
<div class="panel">{visual}</div>
<p>{note}</p>
<details><summary>View all play counts</summary><table><thead><tr><th>{kind}</th><th>Plays</th></tr></thead><tbody>{counts}</tbody></table></details>
<footer>Generated {html.escape(generated_at)} · Page reloads every 5 minutes.<br>Run the Python script again to update the clouds.</footer>
</main></body></html>
'''


def write_pages(directory, pages):
    """Stage all outputs before atomically replacing each HTML file."""
    directory.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for name, content in pages:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=directory, suffix='.tmp', delete=False) as handle:
                staged.append((Path(handle.name), directory / name))
                handle.write(content)
            Path(handle.name).chmod(0o644)
        for temporary, target in staged:
            temporary.replace(target)
            print(f'Created: {target}')
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--database', type=Path, default=DATABASE_FILE)
    parser.add_argument('--output-dir', type=Path, default=OUTPUT_DIRECTORY)
    parser.add_argument('--font-path', help='Optional TrueType/OpenType font supporting your artist names')
    args = parser.parse_args()
    try:
        artists, genres, total = load_counts(args.database)
        stamp = datetime.now().astimezone().strftime('%d %b %Y, %H:%M:%S %Z')
        pages = [(filename, build_page(kind, frequencies, total, stamp, args.font_path))
                 for filename, kind, frequencies in (
                     ('artist_wordcloud.html', 'Artist', artists),
                     ('genre_wordcloud.html', 'Genre', genres))]
        write_pages(args.output_dir, pages)
        return 0
    except ImportError:
        print('Install the word-cloud dependency: python3 -m pip install wordcloud', file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f'Could not generate word clouds: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

