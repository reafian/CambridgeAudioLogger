# CambridgeAudioLogger
A collection of tools to report on the playback and usage of Cambridge Audio streamers.


## Purpose
I have this problem with my AXN10 where it stops playing properly after a few months. It works 
perfectly fine for one track and then for the next it will play for five seconds then buffer for 
five seconds then play for five seconds and buffer again. This continues until it is powered off.

I wanted to know how long between resets it would play for and to do that I needed to log everything
it was playing to a file. Using [Link text](https://github.com/noahhusby/aiostreammagic) it is possible
to see what the AXN is playing and track play time between resets.

My plan is to keep the log file and database for as long as the AXN10 plays properly then, once it stops
again, I'll archive both the log file and the database and repeat until it fails again. Ideally, after a
few runs (which will take a good few months) I'll have an idea of how many hours the AXN10 can play for
before failing. Ideally, it will fail at roughly the same point every time which then might be something
I can get Cambridge Audio to investigate. Hopefully...

## Requirements
Good metadata - This is first and foremost the most important thing. Without this the detailed logging
will be off.
A Cambridge Audio Stream Magic device - This works with both the CXNv2 and the AXN10. I don't see why it
won't work with anything else Stream Magic based.
A UPnP streamer - I'm using AssetUPnP as my primary streamer but this also seems to work with Twonky
given the limited testing I'm done. It will probably work with others too but I've not tested it with
anything else.

## Installation
You'll need to create a python virtual environment, something like:
```bash
cd /usr/local/bin
mkdir axn10-tracker
cd axn10-tracker
python3 -m venv venv
```
Change owenership and permissios as required. My structure is pretty simple:

```bash
axn10-tracker/
├── axn10-logger.py
├── calculate_totals.py
├── calculate_totals_sql.py
├── generate_axn10_webpage.py
├── generate_wordclouds.py
├── now_playing.py
├── streamer_playback_log.csv
├── streamer_playback.db
├── update_asset_library.py
│   
└── venv
```

The log file and database paths are completely configurable so use whatever structure works best for you.

## Tools
### axn10-logger.py
This is the main program. This logs every title played by the AXN / CXN to the streamer_playback_log.csv file.
It also updates the streamer_playback.db with all the titles in the asset library (using the Title list tree)
and keeps track of the number of tracks in albums.
It populates the playlist table with proper artist, title,album, genre data by using the Stream Magic calls
and the information recorded in the database.

### calculate_totals.py
This generates a total play time from the streamer_playback_log.csv.

### calculate_totals_sql.py
This generates a total play time from the streamer_playback.db

### generate_axn10_webpage.py
This generates a webpage showing:
- Top 5 Most Played Tracks
- Top 5 Most Played Albums
- Top 5 Most Played Artists
- Top 5 Most Recent

### generate_wordclouds.py
This generates two wordclouds:
- Artist Word Cloud
- Genre Word Cloud

### now_playing.py
This shows:
- Artist
- Album
- Track
in a shell prompt.

### streamer_playback_log.csv
This records:
- Start Time
- Stop Time
- Artist
- Album
- Played Duration (Sec)
- Played Duration (Min)

### streamer_playback.db
This contains three tables:
- asset_library
-- Album
-- album_key
-- Artist
-- disc_number
-- Genre
-- Title
-- title_key
-- track_number

- asset_library_staging
-- Album
-- album_key
-- Artist
-- disc_number
-- Genre
-- Title
-- title_key
-- track_number

- playback
-- Album
-- Artist
-- Genre
-- Played Duration (Min)
-- Player Duration (Sec)
-- Start Time
-- Stop Time
-- Track

### update_asset_library.py
This is for immediately updating the streamer_playback database with any new tracks that have been added recently.
axn10_logger.py updates the database every six hours but if you need a more immediate update then this is the file
to use.

## Notes
I keep streamer_playback_log.csv around as a paranoia check for the playback database. Doing this helps highlight
metadata issues. I probably don't need it anymore but it's useful to keep around.

## Gotchas
My media is mounted via a CIFS share. This works fine execpt for case sensistivity. The logging requires the asset 
name delivered by the streamer to match the name on the filesystem.

## Finally
This is ChatGPT coded. I'm not a python hacker (not to this degree) but it does what I need and if it's useful to
other people then that's great. I need it for myself more than anything though. I just thought that was worth
mentioning.