#!/usr/bin/env python3
"""Refresh the AXN10 tracker's AssetUPnP SQLite cache once, then exit.

Uses only Python's standard library. Run on a machine that can reach AssetUPnP:
    python3 update_asset_library.py
    python3 update_asset_library.py --database /path/to/metadata.db

Defaults match axn10_tracker_assetupnp_sqlite.py (version 4).
Only asset_library and asset_library_staging are changed. Playback history
and CSV logs are not modified. A failed/empty scan preserves the old cache.
The network scan happens before the short SQLite write transaction.
Only the dynamically discovered Title -> [All Titles] branch is scanned.
"""
import argparse
import json
import logging
from collections import deque
from pathlib import Path
import sqlite3
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

DATABASE_FILE = "<PLAYBACK_DATABASE>"
ASSETUPNP_DESCRIPTION_URL = "http://<ASSET_UPNP_HOST>:26125/DeviceDescription.xml"
ASSETUPNP_PAGE_SIZE = 100
ASSETUPNP_TIMEOUT = 10

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



def update_database(database, rows):
    """Commit the complete replacement atomically, leaving other tables alone."""
    if not rows:
        raise RuntimeError("Scan returned zero tracks; existing cache preserved")
    # Do not silently create a different database after a path typo.
    if not database.is_file():
        raise FileNotFoundError(f"Database does not exist: {database}")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=rw",
                         uri=True, timeout=5) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for table in ("asset_library", "asset_library_staging"):
            connection.execute(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    "Artist" TEXT,
                    "Title" TEXT NOT NULL,
                    "Album" TEXT NOT NULL,
                    "Genre" TEXT,
                    title_key TEXT NOT NULL,
                    album_key TEXT NOT NULL
                )
            """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_asset_library_album_title
            ON asset_library (album_key, title_key)
        """)
        ensure_asset_order_columns(connection)
        connection.execute("DELETE FROM asset_library_staging")
        connection.executemany("""
            INSERT INTO asset_library_staging
            ("Artist", "Title", "Album", "Genre", title_key, album_key, track_number, disc_number)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        connection.execute("DELETE FROM asset_library")
        connection.execute("""
            INSERT INTO asset_library
            ("Artist", "Title", "Album", "Genre", title_key, album_key, track_number, disc_number)
            SELECT DISTINCT "Artist", "Title", "Album", "Genre", title_key, album_key, track_number, disc_number
            FROM asset_library_staging
        """)
        count = connection.execute("SELECT COUNT(*) FROM asset_library").fetchone()[0]
        connection.commit()
    return count


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def main():
    global ASSETUPNP_DESCRIPTION_URL, ASSETUPNP_PAGE_SIZE, ASSETUPNP_TIMEOUT
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", type=Path, default=Path(DATABASE_FILE),
                        help="Existing tracker SQLite database (default: %(default)s)")
    parser.add_argument("--description-url", default=ASSETUPNP_DESCRIPTION_URL,
                        help="AssetUPnP DeviceDescription.xml URL")
    parser.add_argument("--page-size", type=positive_int, default=ASSETUPNP_PAGE_SIZE)
    parser.add_argument("--timeout", type=positive_int, default=ASSETUPNP_TIMEOUT,
                        help="Timeout per HTTP request in seconds")
    parser.add_argument("--debug-log", type=Path,
                        help="Write detailed UTF-8 debug log (overwritten each run)")
    parser.add_argument("--debug-title", default="",
                        help="Also log accepted items whose title contains this text")
    args = parser.parse_args()
    if args.debug_title and not args.debug_log:
        parser.error("--debug-title requires --debug-log")
    logger = logging.getLogger("asset_sync")
    if args.debug_log:
        try:
            handler = logging.FileHandler(args.debug_log, mode="w", encoding="utf-8")
        except OSError as exc:
            parser.error(f"Cannot open debug log: {exc}")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.debug("Sync starting: database=%s description_url=%s",
                     args.database, args.description_url)
        print(f"Debug log: {args.debug_log}", flush=True)
    ASSETUPNP_DESCRIPTION_URL = args.description_url
    ASSETUPNP_PAGE_SIZE = args.page_size
    ASSETUPNP_TIMEOUT = args.timeout
    try:
        if not args.database.is_file():
            raise FileNotFoundError(f"Database does not exist: {args.database}")
        rows = AssetUPnPMetadataLookup(args.debug_title).scan_library()
        count = update_database(args.database, rows)
        print(f"Updated {count} unique tracks in {args.database}", flush=True)
        return 0
    except KeyboardInterrupt:
        print("Cancelled; an uncommitted refresh is rolled back.", file=sys.stderr)
        return 130
    except Exception as exc:
        logger.exception("Sync failed")
        print(f"Refresh failed; existing cache preserved: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

