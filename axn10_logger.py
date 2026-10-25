import asyncio
import json
import logging
from collections import deque
import csv
import os
import sqlite3
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from xml.sax.saxutils import escape as xml_escape
from aiostreammagic import StreamMagicClient

# CONFIGURATION
AXN10_IP = "<IP>"  # Replace with your exact AXN10 IP
LOG_FILE = "<PLAYBACK_LOG_FILE>"
# Separate SQLite metadata database; the original playback CSV is left unchanged.
DATABASE_FILE = "<PLAYBACK_DATABASE>"
ASSETUPNP_DESCRIPTION_URL = "http://<ASSET_UPNP_HOST>:26125/DeviceDescription.xml"
ASSETUPNP_BASE_URL = "http://<ASSET_UPNP_HOST>:26125/"
ASSETUPNP_PAGE_SIZE = 100
ASSETUPNP_TIMEOUT = 10
ASSETUPNP_REFRESH_INTERVAL = 6 * 60 * 60  # Refresh cached library every 6 hours.
HEARTBEAT_INTERVAL = 10
MIN_LOG_DURATION_SECONDS = 5.0  # Ignore transient playback states shorter than this.

CSV_HEADERS = [
    "Start Time",
    "Stop Time",
    "Artist",
    "Track",
    "Album",
    "Played Duration (Sec)",
    "Played Duration (Min)",
]


def ensure_asset_order_columns(connection):
    """Upgrade existing cache tables without changing playback history."""
    for table in ("asset_library", "asset_library_staging"):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for column in ("track_number", "disc_number"):
            if column not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER")


class AssetUPnPMetadataLookup:
    """Cache AssetUPnP track metadata in SQLite and query the local cache."""

    DEVICE_NS = "urn:schemas-upnp-org:device-1-0"
    CONTENT_DIRECTORY_TYPE = "urn:schemas-upnp-org:service:ContentDirectory:1"
    DIDL_NS = "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
    DC_NS = "http://purl.org/dc/elements/1.1/"
    UPNP_NS = "urn:schemas-upnp-org:metadata-1-0/upnp/"

    def __init__(self, debug_title=""):
        self.control_url = None
        self._refresh_lock = threading.Lock()
        self.debug_title = self._normalise(debug_title)
        self.debug = logging.getLogger("asset_sync")

    def _debug(self, event, **details):
        if self.debug.isEnabledFor(logging.DEBUG):
            self.debug.debug(json.dumps({"event": event, **details}, ensure_ascii=False))


    @staticmethod
    def _local_name(element):
        return element.tag.rsplit("}", 1)[-1]

    @classmethod
    def _child_text(cls, element, local_name):
        for child in element:
            if cls._local_name(child) == local_name:
                return (child.text or "").strip()
        return ""

    @staticmethod
    def _normalise(value):
        return " ".join((value or "").split()).casefold()

    def _discover_control_url(self):
        request = urllib.request.Request(
            ASSETUPNP_DESCRIPTION_URL,
            headers={"User-Agent": "AXN10-AssetUPnP-Test/1.0"},
        )
        with urllib.request.urlopen(request, timeout=ASSETUPNP_TIMEOUT) as response:
            description = ET.fromstring(response.read())

        for service in description.iter():
            if self._local_name(service) != "service":
                continue
            service_type = self._child_text(service, "serviceType")
            if service_type == self.CONTENT_DIRECTORY_TYPE:
                control_path = self._child_text(service, "controlURL")
                if not control_path:
                    raise RuntimeError("ContentDirectory service has no controlURL")
                self.control_url = urllib.parse.urljoin(
                    ASSETUPNP_DESCRIPTION_URL, control_path
                )
                print(f"AssetUPnP ContentDirectory control URL: {self.control_url}", flush=True)
                return
        raise RuntimeError("Could not find the ContentDirectory service in DeviceDescription.xml")

    def _browse(self, object_id, starting_index=0):
        if not self.control_url:
            self._discover_control_url()

        envelope = ET.Element("{http://schemas.xmlsoap.org/soap/envelope/}Envelope", {
            "{http://schemas.xmlsoap.org/soap/envelope/}encodingStyle":
            "http://schemas.xmlsoap.org/soap/encoding/"
        })
        body = ET.SubElement(envelope, "{http://schemas.xmlsoap.org/soap/envelope/}Body")
        browse = ET.SubElement(body, "{urn:schemas-upnp-org:service:ContentDirectory:1}Browse")
        ET.SubElement(browse, "ObjectID").text = str(object_id)
        ET.SubElement(browse, "BrowseFlag").text = "BrowseDirectChildren"
        ET.SubElement(browse, "Filter").text = "*"
        ET.SubElement(browse, "StartingIndex").text = str(starting_index)
        ET.SubElement(browse, "RequestedCount").text = str(ASSETUPNP_PAGE_SIZE)
        ET.SubElement(browse, "SortCriteria").text = ""
        payload = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)

        request = urllib.request.Request(
            self.control_url,
            data=payload,
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"',
                "User-Agent": "AXN10-AssetUPnP-Test/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=ASSETUPNP_TIMEOUT) as response:
            soap_response = ET.fromstring(response.read())

        result_text = None
        result_element = None
        number_returned = None
        total_matches = None
        for element in soap_response.iter():
            name = self._local_name(element)
            if name == "Result":
                result_text = element.text or ""
                result_element = element
            elif name == "NumberReturned":
                number_returned = int(element.text or "0")
            elif name == "TotalMatches":
                total_matches = int(element.text or "0")

        if any(self._local_name(e) == "Fault" for e in soap_response.iter()):
            raise RuntimeError("AssetUPnP returned a SOAP fault")
        if result_text is None or number_returned is None or total_matches is None:
            raise RuntimeError("Incomplete AssetUPnP Browse response")

        didl_items = []
        nested_didl = next((e for e in result_element
                            if self._local_name(e) == "DIDL-Lite"), None)
        if nested_didl is not None or result_text.strip():
            # Accept escaped DIDL text and an actual nested DIDL element.
            didl = nested_didl if nested_didl is not None else ET.fromstring(result_text)
            for element in didl:
                if self._local_name(element) in ("container", "item"):
                    didl_items.append(element)
        return didl_items, number_returned or 0, total_matches or 0

    def _extract_item_metadata(self, item):
        title = ""
        album = ""
        album_artists = []
        track_artists = []
        genres = []
        for element in item.iter():
            local_name = self._local_name(element)
            value = (element.text or "").strip()
            if not value:
                continue
            if local_name == "title":
                title = value
            elif local_name == "album":
                album = value
            elif (element.tag == f"{{{self.UPNP_NS}}}artist"
                  and element.get("role") == "AlbumArtist"):
                album_artists.append(value)
            elif (element.tag == f"{{{self.UPNP_NS}}}artist"
                  and not (element.get("role") or "").strip()):
                track_artists.append(value)
            elif local_name == "genre":
                genres.append(value)
        # Asset identifies compilations using AlbumArtist = Various Artists.
        # Only role-free artist entries supply the actual track performer;
        # composer and other role-qualified entries are excluded.
        compilation = any(self._normalise(value) == "various artists"
                          for value in album_artists)
        selected_artists = track_artists if compilation else album_artists
        artist = "; ".join(dict.fromkeys(selected_artists))
        genre = "; ".join(dict.fromkeys(genres))
        return artist, title, album, genre

    def _find_container(self, parent_id, name):
        """Discover a named direct child across every page, never hard-code IDs."""
        matches = []
        starting_index = 0
        while True:
            items, returned, total = self._browse(parent_id, starting_index)
            self._debug("discovery_page", parent_id=parent_id, target=name,
                        starting_index=starting_index, returned=returned, total=total)
            if returned != len(items) or (returned == 0 and starting_index < total):
                raise RuntimeError("Incomplete container discovery; cache preserved")
            for item in items:
                if (self._local_name(item) == "container"
                        and self._normalise(self._child_text(item, "title")) == self._normalise(name)):
                    matches.append(item)
            starting_index += returned
            if returned == 0 or starting_index >= total:
                break
        if len(matches) != 1 or not matches[0].get("id"):
            raise RuntimeError(f"Expected one {name!r} container under {parent_id!r}; "
                               f"found {len(matches)}. Existing cache preserved.")
        return matches[0]

    @staticmethod
    def _positive_number(value):
        try:
            result = int(value.split("/", 1)[0].strip())
            return result if result > 0 else None
        except (AttributeError, TypeError, ValueError):
            return None

    def _track_order(self, item):
        track = self._positive_number(self._child_text(item, "originalTrackNumber"))
        disc = None
        for name in ("originalDiscNumber", "discNumber", "discnumber"):
            disc = self._positive_number(self._child_text(item, name))
            if disc is not None:
                break
        return track, disc or 1

    def scan_library(self):
        """Return all usable AssetUPnP tracks without changing SQLite."""
        print("AssetUPnP: scanning ContentDirectory for library metadata...", flush=True)
        rows = []
        # Carry album context separately from track metadata. Album containers
        # use dc:title, and some child items omit their own upnp:album.
        title_container = self._find_container("0", "Title")
        all_titles = self._find_container(title_container.get("id"), "[All Titles]")
        start_id = all_titles.get("id")
        path = [{"id": "0", "title": "[root]"},
                {"id": title_container.get("id"), "title": "Title"},
                {"id": start_id, "title": "[All Titles]"}]
        print(f"AssetUPnP: scanning only Title → [All Titles] ({start_id}).", flush=True)
        queue = deque([(start_id, ("", "", ""), path)])
        visited = set()
        skipped = 0

        while queue:
            object_id, album_context, path = queue.popleft()
            if object_id in visited:
                self._debug("container_already_visited", object_id=object_id, path=path)
                continue
            visited.add(object_id)
            starting_index = 0

            while True:
                try:
                    items, returned, total = self._browse(object_id, starting_index)
                except Exception:
                    self.debug.exception("Browse failed: object_id=%r start=%r path=%r",
                                         object_id, starting_index, path)
                    raise
                self._debug("browse_page", object_id=object_id, path=path,
                            starting_index=starting_index, returned=returned, total=total,
                            album_context=album_context)
                for element in items:
                    kind = self._local_name(element)
                    if kind == "container":
                        child_id = element.attrib.get("id")
                        if child_id and child_id not in visited:
                            context = album_context
                            container_class = self._child_text(element, "class")
                            if container_class == "object.container.album.musicAlbum" or container_class.startswith("object.container.album.musicAlbum."):
                                creator, name, _, container_genre = self._extract_item_metadata(element)
                                context = (name, creator, container_genre)
                            child_path = path + [{
                                "id": child_id, "title": self._child_text(element, "title"),
                                "class": container_class,
                            }]
                            self._debug("container", path=child_path,
                                        album_context=context,
                                        xml=ET.tostring(element, encoding="unicode"))
                            queue.append((child_id, context, child_path))
                    elif kind == "item":
                        artist, title, album, genre = self._extract_item_metadata(element)
                        raw_metadata = {"artist": artist, "title": title,
                                        "album": album, "genre": genre}
                        parent_album, parent_artist, parent_genre = album_context
                        # Never borrow metadata from a different explicit album.
                        if parent_album and (not album or self._normalise(album) == self._normalise(parent_album)):
                            album = album or parent_album
                            artist = artist or parent_artist
                            genre = genre or parent_genre
                        accepted = bool(title and album)
                        # Log every skipped item, plus every occurrence of a requested
                        # title, including accepted entries from other browse paths.
                        if not accepted or (self.debug_title and self.debug_title in self._normalise(title)):
                            self._debug(
                                "item", accepted=accepted,
                                reason="" if accepted else "missing title or album",
                                item_id=element.get("id"), parent_id=element.get("parentID"),
                                ref_id=element.get("refID"), browsed_container=object_id,
                                path=path, starting_index=starting_index,
                                raw_metadata=raw_metadata, album_context=album_context,
                                effective_metadata={"artist": artist, "title": title,
                                                    "album": album, "genre": genre},
                                title_codepoints=["U+%04X" % ord(c) for c in title],
                                xml=ET.tostring(element, encoding="unicode"),
                            )
                        if accepted:
                            rows.append((
                                artist, title, album, genre,
                                self._normalise(title), self._normalise(album),
                                *self._track_order(element),
                            ))
                        else:
                            skipped += 1
                            if skipped <= 10:
                                print(f"AssetUPnP: skipped item {element.get('id')!r}: title={title!r}, album={album!r}", file=sys.stderr)

                if returned != len(items) or (returned == 0 and starting_index < total):
                    raise RuntimeError("Incomplete AssetUPnP page; cache preserved")
                starting_index += returned
                if returned == 0 or starting_index >= total:
                    break

        self._debug("scan_summary", visited_containers=len(visited),
                    usable_items=len(rows), skipped_items=skipped)
        raw_count = len(rows)
        # AssetUPnP can expose the same track through multiple ContentDirectory
        # paths. Keep one cache row for each identical Artist/Title/Album/Genre
        # record while preserving the first-seen order.
        rows = list(dict.fromkeys(rows))
        duplicate_count = raw_count - len(rows)
        if skipped:
            print(f"AssetUPnP: skipped {skipped} items without a title or album (first 10 shown).", file=sys.stderr)
        print(
            f"AssetUPnP: scan found {raw_count} usable track items; "
            f"{len(rows)} unique ({duplicate_count} duplicates removed).",
            flush=True,
        )
        return rows


    def refresh_database(self):
        """Safely replace the cached library only after a successful non-empty scan."""
        if not self._refresh_lock.acquire(blocking=False):
            return False
        try:
            rows = self.scan_library()
            if not rows:
                print(
                    "AssetUPnP: refresh returned zero tracks; keeping existing library cache unchanged.",
                    file=sys.stderr, flush=True,
                )
                return False

            with sqlite3.connect(DATABASE_FILE) as connection:
                connection.execute("BEGIN IMMEDIATE")
                ensure_asset_order_columns(connection)
                connection.execute("DELETE FROM asset_library_staging")
                connection.executemany(
                    """
                    INSERT INTO asset_library_staging
                    ("Artist", "Title", "Album", "Genre", title_key, album_key, track_number, disc_number)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                staged = connection.execute(
                    "SELECT COUNT(*) FROM asset_library_staging"
                ).fetchone()[0]
                if staged == 0:
                    connection.rollback()
                    print("AssetUPnP: staging table is empty; existing cache preserved.", flush=True)
                    return False
                connection.execute("DELETE FROM asset_library")
                connection.execute(
                    """
                    INSERT INTO asset_library
                    ("Artist", "Title", "Album", "Genre", title_key, album_key, track_number, disc_number)
                    SELECT DISTINCT
                        "Artist", "Title", "Album", "Genre", title_key, album_key, track_number, disc_number
                    FROM asset_library_staging
                    """
                )
                connection.commit()
            print(f"AssetUPnP: library cache refreshed with {staged} tracks.", flush=True)
            return True
        except Exception as exc:
            print(
                f"AssetUPnP: refresh failed; existing library cache preserved: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr, flush=True,
            )
            return False
        finally:
            self._refresh_lock.release()

    def lookup(self, album, track):
        """Look up artist and genre from the local SQLite AssetUPnP cache only."""
        if not album or not track:
            return "", ""
        album_key = self._normalise(album)
        title_key = self._normalise(track)
        with sqlite3.connect(DATABASE_FILE) as connection:
            row = connection.execute(
                """
                SELECT "Artist", "Genre"
                FROM asset_library
                WHERE album_key = ? AND title_key = ?
                ORDER BY rowid
                LIMIT 1
                """,
                (album_key, title_key),
            ).fetchone()
        if row is None:
            print(f"AssetUPnP cache: no match for album={album!r}, track={track!r}", flush=True)
            return "", ""
        return row[0] or "", row[1] or ""

    @staticmethod
    def cache_has_rows():
        """Return True when a usable AssetUPnP cache already exists."""
        try:
            with sqlite3.connect(DATABASE_FILE) as connection:
                row = connection.execute("SELECT 1 FROM asset_library LIMIT 1").fetchone()
            return row is not None
        except sqlite3.Error:
            return False


class PlaybackTracker:
    def __init__(self):
        self.current_track = None
        self.current_album = None
        self.current_artist = None
        self.play_start_time = None
        self.last_state = None
        self.last_update_time = datetime.now()

        self.pause_started_at = None
        self.pause_task = None
        self.total_paused_seconds = 0.0
        self.asset_metadata = AssetUPnPMetadataLookup()
        self.metadata_tasks = set()
        # Playback never waits on this; only background enrichment tasks may wait.
        self.asset_cache_ready = asyncio.Event()

    def prepare_log_file(self):
        """Create the main CSV and the separate SQLite metadata database."""
        directory = os.path.dirname(LOG_FILE)
        os.makedirs(directory, exist_ok=True)

        with open(LOG_FILE, "a+", newline="", encoding="utf-8") as f:
            f.seek(0, os.SEEK_END)

            if f.tell() == 0:
                csv.writer(f).writerow(CSV_HEADERS)
                f.flush()
                os.fsync(f.fileno())

        print(f"CSV log file ready: {LOG_FILE}", flush=True)
        self.prepare_database()

    def prepare_database(self):
        """Create the SQLite playback table if it does not already exist."""
        os.makedirs(os.path.dirname(DATABASE_FILE), exist_ok=True)
        with sqlite3.connect(DATABASE_FILE) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS playback (
                    "Start Time" TEXT NOT NULL,
                    "Stop Time" TEXT NOT NULL,
                    "Artist" TEXT,
                    "Track" TEXT NOT NULL,
                    "Album" TEXT NOT NULL,
                    "Genre" TEXT,
                    "Played Duration (Sec)" REAL NOT NULL,
                    "Played Duration (Min)" REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS asset_library (
                    "Artist" TEXT,
                    "Title" TEXT NOT NULL,
                    "Album" TEXT NOT NULL,
                    "Genre" TEXT,
                    title_key TEXT NOT NULL,
                    album_key TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_asset_library_album_title
                ON asset_library (album_key, title_key)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS asset_library_staging (
                    "Artist" TEXT,
                    "Title" TEXT NOT NULL,
                    "Album" TEXT NOT NULL,
                    "Genre" TEXT,
                    title_key TEXT NOT NULL,
                    album_key TEXT NOT NULL
                )
                """
            )
            connection.commit()
        print(f"SQLite metadata database ready: {DATABASE_FILE}", flush=True)
        if self.asset_metadata.cache_has_rows():
            self.asset_cache_ready.set()
            print("AssetUPnP: existing library cache is ready for lookups.", flush=True)

    def log_to_csv(
        self, start_time, stop_time, artist, track, album, duration_seconds
    ):
        """Append a completed playback record to the original CSV file."""
        try:
            self.prepare_log_file()

            duration_minutes = round(duration_seconds / 60, 2)

            row = [
                start_time.strftime("%Y-%m-%d %H:%M:%S"),
                stop_time.strftime("%Y-%m-%d %H:%M:%S"),
                artist or "Unknown Artist",
                track or "Unknown Track",
                album or "Unknown Album",
                round(duration_seconds, 1),
                duration_minutes,
            ]

            with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)
                f.flush()
                os.fsync(f.fileno())

            # AssetUPnP enrichment runs in a background task so a full
            # ContentDirectory scan cannot block playback tracking.
            task = asyncio.create_task(
                self.write_metadata_database_row(
                    start_time,
                    stop_time,
                    track,
                    album,
                    duration_seconds,
                )
            )
            self.metadata_tasks.add(task)
            task.add_done_callback(self.metadata_tasks.discard)

            print(
                f"CSV WRITE OK: '{track or 'Unknown Track'}' by "
                f"{artist or 'Unknown Artist'} | "
                f"{duration_minutes} mins | {LOG_FILE}",
                flush=True,
            )

        except Exception as e:
            print(
                f"CSV WRITE FAILED: {LOG_FILE}\n"
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
                flush=True,
            )

    async def write_metadata_database_row(
        self, start_time, stop_time, track, album, duration_seconds
    ):
        """Insert playback immediately, then enrich it when the cache is available."""
        try:
            duration_minutes = round(duration_seconds / 60, 2)
            values = (
                start_time.strftime("%Y-%m-%d %H:%M:%S"),
                stop_time.strftime("%Y-%m-%d %H:%M:%S"),
                "",
                track or "Unknown Track",
                album or "Unknown Album",
                "",
                round(duration_seconds, 1),
                duration_minutes,
            )
            rowid = await asyncio.to_thread(self._insert_database_row, values)

            if not self.asset_cache_ready.is_set():
                print(
                    f"AssetUPnP cache not ready; queued metadata enrichment for "
                    f"{album!r} / {track!r} (playback row {rowid}).",
                    flush=True,
                )
                await self.asset_cache_ready.wait()

            asset_artist, genre = await asyncio.to_thread(
                self.asset_metadata.lookup, album, track
            )
            await asyncio.to_thread(
                self._update_database_metadata, rowid, asset_artist, genre
            )

            print(
                f"AssetUPnP metadata DB update: artist={asset_artist or '[not found]'}; "
                f"genre={genre or '[not found]'} | row {rowid} | {DATABASE_FILE}",
                flush=True,
            )
        except Exception as lookup_error:
            print(
                f"AssetUPnP lookup/database write failed for {album!r} / {track!r}: "
                f"{type(lookup_error).__name__}: {lookup_error}",
                file=sys.stderr,
                flush=True,
            )

    @staticmethod
    def _insert_database_row(values):
        """Insert one playback record immediately and return its SQLite rowid."""
        with sqlite3.connect(DATABASE_FILE) as connection:
            cursor = connection.execute(
                """
                INSERT INTO playback (
                    "Start Time", "Stop Time", "Artist", "Track", "Album",
                    "Genre", "Played Duration (Sec)", "Played Duration (Min)"
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            rowid = cursor.lastrowid
            connection.commit()
            return rowid

    @staticmethod
    def _update_database_metadata(rowid, artist, genre):
        """Fill AssetUPnP metadata into an already-recorded playback row."""
        with sqlite3.connect(DATABASE_FILE) as connection:
            connection.execute(
                'UPDATE playback SET "Artist" = ?, "Genre" = ? WHERE rowid = ?',
                (artist or "", genre or "", rowid),
            )
            connection.commit()

    async def wait_for_metadata_tasks(self):
        """Allow pending enrichment tasks to finish during shutdown."""
        if self.metadata_tasks:
            await asyncio.gather(*list(self.metadata_tasks), return_exceptions=True)

    def cancel_pause_timer(self):
        """Cancel any pending pause timeout."""
        task = self.pause_task
        self.pause_task = None

        if task and not task.done():
            task.cancel()

    def force_stop_active_stream(
        self,
        reason="Playback ended",
        subtract_seconds=0,
        stop_time=None,
    ):
        """Write the active track using its actual playback end time."""
        if self.last_state == "playing" and self.play_start_time:
            # If paused, the effective end time is when the pause began.
            if stop_time is None:
                stop_time = self.pause_started_at or datetime.now()

            duration = (
                stop_time - self.play_start_time
            ).total_seconds()

            duration -= self.total_paused_seconds + subtract_seconds

            if duration < MIN_LOG_DURATION_SECONDS:
                print(
                    f"Ignoring short playback record: {reason}. "
                    f"Adjusted duration: {duration:.1f}s "
                    f"(minimum {MIN_LOG_DURATION_SECONDS:.1f}s).",
                    flush=True,
                )
            else:
                print(
                    f"Closing playback record: {reason}. "
                    f"Duration: {duration:.1f}s.",
                    flush=True,
                )

                self.log_to_csv(
                    self.play_start_time,
                    stop_time,
                    self.current_artist,
                    self.current_track,
                    self.current_album,
                    duration,
                )

        self.cancel_pause_timer()

        self.pause_started_at = None
        self.total_paused_seconds = 0.0
        self.current_track = None
        self.current_artist = None
        self.current_album = None
        self.play_start_time = None
        self.last_state = "stopped"

    async def wait_for_pause_timeout(self, timeout):
        """End the track if the pause lasts through the power-down timeout."""
        try:
            await asyncio.sleep(timeout)

            if (
                self.pause_started_at is not None
                and self.last_state == "playing"
            ):
                pause_started = self.pause_started_at

                # Do not add the current pause to total_paused_seconds:
                # stop_time is already at the start of this pause.
                self.pause_task = None

                print(
                    f"Pause exceeded auto-power-down timeout ({timeout}s). "
                    "Closing track at the start of the pause.",
                    flush=True,
                )

                self.force_stop_active_stream(
                    "Pause exceeded auto-power-down timeout",
                    stop_time=pause_started,
                )

        except asyncio.CancelledError:
            pass

    async def refresh_asset_library_periodically(self):
        """Refresh the local AssetUPnP cache now, then at the configured interval."""
        while True:
            refreshed = await asyncio.to_thread(self.asset_metadata.refresh_database)
            if refreshed or self.asset_metadata.cache_has_rows():
                if not self.asset_cache_ready.is_set():
                    self.asset_cache_ready.set()
                    print(
                        "AssetUPnP: initial cache ready; processing queued metadata lookups.",
                        flush=True,
                    )
            await asyncio.sleep(ASSETUPNP_REFRESH_INTERVAL)

    async def monitor_heartbeat(self, client):
        """Detect a lost connection when playback is not paused."""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)

            time_since_update = (
                datetime.now() - self.last_update_time
            ).total_seconds()

            if (
                self.last_state == "playing"
                and self.pause_started_at is None
                and time_since_update > HEARTBEAT_INTERVAL * 2.5
            ):
                print(
                    f"Watchdog alert: no updates for "
                    f"{time_since_update:.1f}s.",
                    flush=True,
                )

                self.force_stop_active_stream(
                    "AXN10 lost connection / switched off",
                    subtract_seconds=HEARTBEAT_INTERVAL * 2.5,
                )

    async def on_state_change(self, client, callback_type=None):
        """Process AXN10 playback updates."""
        self.last_update_time = datetime.now()
        ps_obj = client.play_state

        if not ps_obj or not hasattr(ps_obj, "state"):
            return

        play_state = str(ps_obj.state).lower()
        metadata = getattr(ps_obj, "metadata", None)

        track_title = (
            getattr(metadata, "title", None) if metadata else None
        )

        album_title = (
            getattr(metadata, "album", None) if metadata else None
        )

        artist_name = (
            getattr(metadata, "track_artist", None) if metadata else None
        )

        if not artist_name and metadata:
            artist_name = getattr(metadata, "artist", None)

        if (
            not artist_name
            and metadata
            and hasattr(metadata, "__dict__")
        ):
            artist_name = metadata.__dict__.get("performer")

        print(
            f"DEBUG Update [{callback_type}] -> "
            f"State: {play_state} | "
            f"Artist: {artist_name} | Track: {track_title} | Album: {album_title}",
            flush=True,
        )

        # PLAYING
        if play_state in ("playing", "play"):
            # Preserve the current session during temporary metadata gaps.
            if not track_title:
                print(
                    "Playback active, but track metadata is unavailable.",
                    flush=True,
                )
                return

            # If playback resumes, count the pause as non-listening time.
            if self.pause_started_at is not None:
                paused_seconds = (
                    datetime.now() - self.pause_started_at
                ).total_seconds()

                self.total_paused_seconds += paused_seconds
                self.pause_started_at = None
                self.cancel_pause_timer()

                print(
                    f"Playback resumed after {paused_seconds:.1f}s pause.",
                    flush=True,
                )

            # Treat the track title as the session identity. AXN10 metadata can
            # arrive in stages, so a later artist/album update must not end the
            # current listening session.
            track_changed = track_title != self.current_track

            if self.last_state != "playing":
                self.play_start_time = datetime.now()
                self.current_track = track_title
                self.current_album = album_title
                self.current_artist = artist_name
                self.total_paused_seconds = 0.0
                self.last_state = "playing"

                print(
                    f"Now tracking: '{track_title}' by {artist_name}",
                    flush=True,
                )

            elif track_changed:
                self.force_stop_active_stream(
                    "Track changed"
                )

                self.play_start_time = datetime.now()
                self.current_track = track_title
                self.current_album = album_title
                self.current_artist = artist_name
                self.last_state = "playing"

                print(
                    f"Now tracking: '{track_title}' by {artist_name}",
                    flush=True,
                )

            else:
                # Metadata can arrive in stages. For the same track title, let
                # artist and album improve without resetting the session.
                artist_value = (artist_name or "").strip()
                artist_is_known = artist_value.casefold() not in {
                    "", "unknown", "unknown artist", "none", "null"
                }
                current_artist_is_unknown = (
                    not self.current_artist
                    or self.current_artist.strip().casefold() in {
                        "unknown", "unknown artist", "none", "null"
                    }
                )

                if artist_is_known and current_artist_is_unknown:
                    self.current_artist = artist_value
                    print(
                        f"Artist metadata updated for '{track_title}': "
                        f"{artist_value}",
                        flush=True,
                    )

                album_value = (album_title or "").strip()
                album_is_known = album_value.casefold() not in {
                    "", "unknown", "unknown album", "none", "null"
                }
                current_album_is_unknown = (
                    not self.current_album
                    or self.current_album.strip().casefold() in {
                        "unknown", "unknown album", "none", "null"
                    }
                )

                if album_is_known and current_album_is_unknown:
                    self.current_album = album_value
                    print(
                        f"Album metadata updated for '{track_title}': "
                        f"{album_value}",
                        flush=True,
                    )

        # PAUSED: wait for resume or the configured power-down timeout.
        elif play_state in ("paused", "pause"):
            if (
                self.last_state == "playing"
                and self.pause_started_at is None
            ):
                self.pause_started_at = datetime.now()

                try:
                    timeout = int(client.state.auto_power_down_time)
                except (AttributeError, TypeError, ValueError):
                    timeout = 1200

                timeout = max(0, timeout)

                print(
                    f"Playback paused. Waiting {timeout}s for "
                    "playback to resume or the device to power down.",
                    flush=True,
                )

                self.pause_task = asyncio.create_task(
                    self.wait_for_pause_timeout(timeout)
                )

        # EXPLICIT STOP
        elif play_state in ("stopped", "stop"):
            if self.last_state == "playing":
                self.force_stop_active_stream("Playback stopped")


async def main():
    tracker = PlaybackTracker()
    client = StreamMagicClient(AXN10_IP)

    tracker.prepare_log_file()

    await client.register_state_update_callbacks(
        tracker.on_state_change
    )

    try:
        print(
            f"Connecting to Cambridge Audio AXN10 at {AXN10_IP}...",
            flush=True,
        )

        await client.connect()

        print(
            "Connected successfully. Monitoring playback...",
            flush=True,
        )

        if client.play_state and getattr(
            client.play_state, "state", None
        ):
            await tracker.on_state_change(client, "STARTUP")

        asyncio.create_task(tracker.monitor_heartbeat(client))
        asyncio.create_task(tracker.refresh_asset_library_periodically())
        await asyncio.Event().wait()

    except asyncio.CancelledError:
        raise

    except Exception as e:
        print(
            f"Connection exception: {type(e).__name__}: {e}",
            file=sys.stderr,
            flush=True,
        )

    finally:
        tracker.force_stop_active_stream("Tracker shutting down")
        await tracker.wait_for_metadata_tasks()

        try:
            await client.disconnect()
        except Exception as e:
            print(
                f"Disconnect warning: {e}",
                file=sys.stderr,
                flush=True,
            )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Script stopped manually.", flush=True)

