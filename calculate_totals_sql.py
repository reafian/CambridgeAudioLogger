import sqlite3
from pathlib import Path
from collections import defaultdict

DATABASE_FILE = "<PLAYBACK_DATABASE>"

def analyze_logs():
    total_seconds = 0
    track_counts = defaultdict(int)
    track_durations = defaultdict(float)
    
    try:
        # Read-only mode prevents accidentally creating an empty database.
        database_uri = Path(DATABASE_FILE).resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(database_uri, uri=True, timeout=10) as connection:
            connection.row_factory = sqlite3.Row
            reader = connection.execute("""
                SELECT "Track", "Artist", "Played Duration (Sec)"
                FROM playback ORDER BY rowid
            """)

            for row in reader:
                # Calculate overall cumulative time
                seconds = float(row["Played Duration (Sec)"] or 0)
                total_seconds += seconds
                
                # Group data by track identifier
                track = (row["Track"] or "").strip() or "Unknown Track"
                artist = (row["Artist"] or "").strip() or "Unknown Artist"
                track_identifier = f"'{track}' by {artist}"
                track_counts[track_identifier] += 1
                track_durations[track_identifier] += seconds
                
    except sqlite3.Error as exc:
        print(f"Error reading playback database '{DATABASE_FILE}': {exc}")
        return

    # Convert total seconds into hours and minutes
    total_minutes = total_seconds / 60
    total_hours = total_minutes / 60
    
    print("=" * 50)
    print("      CAMBRIDGE AUDIO AXN10 PLAYBACK REPORT      ")
    print("=" * 50)
    print(f"Total Listening Time: {total_hours:.2f} Hours ({int(total_minutes)} Minutes)")
    print("-" * 50)
    
    # Sort and find the tracks listened to for the longest total duration
    print("Top 5 Most Listened-To Tracks (By Actual Duration):")
    sorted_by_duration = sorted(track_durations.items(), key=lambda x: x[1], reverse=True)
    
    for i, (track, seconds) in enumerate(sorted_by_duration[:5], 1):
        mins = seconds / 60
        print(f" {i}. {track} — Listened for {mins:.1f} mins total")

if __name__ == "__main__":
    analyze_logs()

