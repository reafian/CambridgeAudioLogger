import csv
from datetime import datetime
from collections import defaultdict

LOG_FILE = "<PLAYBACK_LOG_FILE>"

def analyze_logs():
    total_seconds = 0
    track_counts = defaultdict(int)
    track_durations = defaultdict(float)
    
    try:
        with open(LOG_FILE, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row in reader:
                # Calculate overall cumulative time
                seconds = float(row["Played Duration (Sec)"])
                total_seconds += seconds
                
                # Group data by track identifier
                track_identifier = f"'{row['Track']}' by {row['Artist']}"
                track_counts[track_identifier] += 1
                track_durations[track_identifier] += seconds
                
    except FileNotFoundError:
        print(f"Error: Could not find '{LOG_FILE}'. Make sure the logger script has run and recorded at least one song.")
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

