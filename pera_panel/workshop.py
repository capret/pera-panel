"""Read public Workshop metadata without coupling Steam availability to world settings."""
from collections import OrderedDict
from datetime import datetime, timezone
from html import unescape
from http.client import HTTPException
import json
import re
import threading
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .storage import PanelError


DETAILS_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
CACHE_SECONDS = 3600
MAX_CACHE_ITEMS = 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def plain_text(value, limit):
    if not isinstance(value, str):
        return ""
    # Workshop descriptions use BBCode. Display a short, plain-text excerpt only.
    value = re.sub(r"\[/?[a-zA-Z*][^\]\r\n]*\]", " ", value)
    value = re.sub(r"<[^>]*>", " ", value)
    value = " ".join(unescape(value).split())
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def parse_details(item):
    if item.get("result") != 1 or str(item.get("consumer_app_id")) != "322330":
        return None
    title = item.get("title")
    title = " ".join(title.split())[:300] if isinstance(title, str) else ""
    if not title:
        return None
    updated_at = None
    timestamp = item.get("time_updated")
    if type(timestamp) is int and 0 < timestamp <= 253402300799:
        updated_at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    return {"status": "available", "title": title,
            "description": plain_text(item.get("description"), 500), "updated_at": updated_at}


class Workshop:
    def __init__(self):
        self.cache = OrderedDict()
        self.lock = threading.Lock()

    def details(self, identifiers):
        if not isinstance(identifiers, list) or len(identifiers) > 100:
            raise PanelError("Use at most 100 mods.")
        ids = [str(identifier) for identifier in identifiers]
        if any(not re.fullmatch(r"[1-9][0-9]{4,19}", identifier) for identifier in ids) or len(set(ids)) != len(ids):
            raise PanelError("Workshop IDs must be unique numbers (5–20 digits).")
        if not ids:
            return []

        # This lock is separate from world operations; metadata never blocks saving/starting.
        with self.lock:
            now = time.monotonic()
            missing = [identifier for identifier in ids
                       if identifier not in self.cache or self.cache[identifier][0] <= now]
            results = {}
            if missing:
                fields = {"itemcount": len(missing),
                          **{f"publishedfileids[{index}]": identifier for index, identifier in enumerate(missing)}}
                request = Request(DETAILS_URL, data=urlencode(fields).encode("ascii"),
                                  headers={"Content-Type": "application/x-www-form-urlencoded",
                                           "User-Agent": "Pera-Panel/0.1"})
                try:
                    with urlopen(request, timeout=8) as response:
                        payload = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(payload) > MAX_RESPONSE_BYTES:
                        raise ValueError("Workshop response too large")
                    data = json.loads(payload)
                    envelope = data.get("response") if isinstance(data, dict) else None
                    items = envelope.get("publishedfiledetails") if isinstance(envelope, dict) else None
                    if not isinstance(items, list):
                        raise ValueError("Invalid Workshop response")
                    details = {str(item.get("publishedfileid")): item for item in items if isinstance(item, dict)}
                    for identifier in missing:
                        info = parse_details(details.get(identifier, {}))
                        if info:
                            self.cache[identifier] = (time.monotonic() + CACHE_SECONDS, info)
                        else:
                            self.cache.pop(identifier, None)
                        results[identifier] = info or {"status": "unavailable"}
                except (OSError, URLError, HTTPException, ValueError):
                    for identifier in missing:
                        cached = self.cache.get(identifier)
                        results[identifier] = {**cached[1], "stale": True} if cached else {"status": "error"}
            for identifier in ids:
                if identifier not in results:
                    results[identifier] = self.cache[identifier][1]
                if identifier in self.cache:
                    self.cache.move_to_end(identifier)
            while len(self.cache) > MAX_CACHE_ITEMS:
                self.cache.popitem(last=False)
            return [{"id": identifier, **results[identifier]} for identifier in ids]
