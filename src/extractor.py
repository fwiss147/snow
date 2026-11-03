import asyncio
from dataclasses import dataclass
import json
import re
import urllib.parse
import urllib.request

import discord
import yt_dlp

from config import (
    AVATAR_URL,
    DEFAULT_COLOR,
    PLATFORMS,
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    log,
    ytdl_options,
)

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
except ImportError:
    spotipy = None


def _init_spotify():
    if spotipy and SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        try:
            auth = SpotifyClientCredentials(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET,
            )
            return spotipy.Spotify(auth_manager=auth)
        except Exception:
            log.warning("Failed to initialize Spotipy client; using oEmbed fallback.")
    return None


sp_client = _init_spotify()


@dataclass
class Song:
    query: str
    title: str
    artist: str
    stream_url: str
    webpage_url: str
    duration: int
    thumbnail: str | None
    requester: discord.abc.User
    platform: str = "youtube"

    @property
    def length(self) -> str:
        total = int(self.duration or 0)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


def get_platform_meta(platform_key: str) -> dict:
    return PLATFORMS.get(
        platform_key.lower(),
        {
            "name": "Music Source",
            "color": DEFAULT_COLOR,
            "icon": AVATAR_URL,
            "search_prefix": "ytsearch:",
        },
    )


def detect_platform(query: str, preferred_source: str = "youtube") -> str:
    q = query.strip().lower()
    if q.startswith(("http://", "https://")):
        if "spotify.com" in q:
            return "spotify"
        if "youtube.com" in q or "youtu.be" in q:
            return "youtube"
        if "soundcloud.com" in q:
            return "soundcloud"
        if "music.apple.com" in q:
            return "applemusic"
        if "deezer.com" in q or "deezer.page.link" in q:
            return "deezer"
        return "youtube"
    return preferred_source.lower() if preferred_source.lower() in PLATFORMS else "youtube"


def _fetch_oembed_metadata(url: str) -> dict | None:
    try:
        if "spotify.com" in url:
            oembed_url = f"https://open.spotify.com/oembed?url={urllib.parse.quote(url)}"
            req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return {
                    "title": data.get("title") or "Spotify Track",
                    "artist": "Spotify",
                    "thumbnail": data.get("thumbnail_url"),
                    "webpage_url": url,
                }
        if "deezer.com" in url:
            oembed_url = f"https://api.deezer.com/oembed?url={urllib.parse.quote(url)}"
            req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return {
                    "title": data.get("title") or "Deezer Track",
                    "artist": data.get("author_name") or "Deezer",
                    "thumbnail": data.get("thumbnail_url"),
                    "webpage_url": url,
                }
        if "music.apple.com" in url:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
                title_match = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                thumb_match = re.search(r'<meta property="og:image" content="([^"]+)"', html)
                if title_match:
                    return {
                        "title": title_match.group(1),
                        "artist": "Apple Music",
                        "thumbnail": thumb_match.group(1) if thumb_match else None,
                        "webpage_url": url,
                    }
    except Exception:
        log.debug("oEmbed metadata extraction failed for %s", url)
    return None


def _resolve_spotify_items(url: str, max_items: int = 10) -> list[dict]:
    if sp_client is not None:
        try:
            if "/track/" in url:
                track = sp_client.track(url)
                artists = ", ".join(a["name"] for a in track.get("artists", []))
                images = track.get("album", {}).get("images", [])
                return [
                    {
                        "title": track.get("name", "Unknown Track"),
                        "artist": artists or "Unknown Artist",
                        "thumbnail": images[0]["url"] if images else None,
                        "webpage_url": track.get("external_urls", {}).get("spotify", url),
                        "search_query": f"{track.get('name', '')} {artists}".strip(),
                    }
                ]
            if "/playlist/" in url:
                results = sp_client.playlist_items(url, limit=max_items)
                items = []
                for item in results.get("items", []):
                    track = item.get("track")
                    if not track:
                        continue
                    artists = ", ".join(a["name"] for a in track.get("artists", []))
                    images = track.get("album", {}).get("images", [])
                    items.append(
                        {
                            "title": track.get("name", "Unknown Track"),
                            "artist": artists or "Unknown Artist",
                            "thumbnail": images[0]["url"] if images else None,
                            "webpage_url": track.get("external_urls", {}).get("spotify", url),
                            "search_query": f"{track.get('name', '')} {artists}".strip(),
                        }
                    )
                if items:
                    return items
            if "/album/" in url:
                album = sp_client.album(url)
                images = album.get("images", [])
                thumb = images[0]["url"] if images else None
                items = []
                for track in album.get("tracks", {}).get("items", [])[:max_items]:
                    artists = ", ".join(a["name"] for a in track.get("artists", []))
                    items.append(
                        {
                            "title": track.get("name", "Unknown Track"),
                            "artist": artists or "Unknown Artist",
                            "thumbnail": thumb,
                            "webpage_url": track.get("external_urls", {}).get("spotify", url),
                            "search_query": f"{track.get('name', '')} {artists}".strip(),
                        }
                    )
                if items:
                    return items
        except Exception:
            log.warning("Spotipy lookup failed for %s; falling back to oEmbed.", url)

    meta = _fetch_oembed_metadata(url)
    if meta:
        return [
            {
                "title": meta["title"],
                "artist": meta["artist"],
                "thumbnail": meta["thumbnail"],
                "webpage_url": meta["webpage_url"],
                "search_query": meta["title"],
            }
        ]
    raise RuntimeError("Could not resolve Spotify URL.")


def _extract(query: str) -> dict:
    with yt_dlp.YoutubeDL(ytdl_options()) as ydl:
        return ydl.extract_info(query, download=False)


async def _extract_async(query: str, timeout: float = 45) -> dict:
    data = await asyncio.wait_for(asyncio.to_thread(_extract, query), timeout=timeout)
    entries = data.get("entries") if isinstance(data, dict) else None
    if entries:
        data = entries[0]
    if not isinstance(data, dict) or not data.get("url"):
        raise RuntimeError("No playable result found.")
    return data


async def fetch_song(
    query: str,
    requester: discord.abc.User,
    preferred_source: str = "youtube",
) -> list[Song]:
    raw = query.strip()
    is_url = raw.startswith(("http://", "https://"))
    platform = detect_platform(raw, preferred_source)

    # 1. Handle Spotify Links (Track, Album, Playlist)
    if is_url and platform == "spotify":
        sp_items = await asyncio.to_thread(_resolve_spotify_items, raw)
        songs: list[Song] = []
        for item in sp_items:
            yt_data = await _extract_async(f"ytsearch:{item['search_query']}")
            songs.append(
                Song(
                    query=f"ytsearch:{item['search_query']}",
                    title=f"{item['title']} - {item['artist']}" if item["artist"] != "Spotify" else item["title"],
                    artist=item["artist"],
                    stream_url=yt_data["url"],
                    webpage_url=item["webpage_url"],
                    duration=int(yt_data.get("duration") or 0),
                    thumbnail=item["thumbnail"] or yt_data.get("thumbnail"),
                    requester=requester,
                    platform="spotify",
                )
            )
        return songs

    # 2. Handle Apple Music & Deezer Links
    if is_url and platform in {"applemusic", "deezer"}:
        meta = await asyncio.to_thread(_fetch_oembed_metadata, raw)
        if meta:
            yt_data = await _extract_async(f"ytsearch:{meta['title']} {meta['artist']}")
            return [
                Song(
                    query=f"ytsearch:{meta['title']} {meta['artist']}",
                    title=meta["title"],
                    artist=meta["artist"],
                    stream_url=yt_data["url"],
                    webpage_url=meta["webpage_url"],
                    duration=int(yt_data.get("duration") or 0),
                    thumbnail=meta["thumbnail"] or yt_data.get("thumbnail"),
                    requester=requester,
                    platform=platform,
                )
            ]

    # 3. Handle Direct YouTube / SoundCloud URLs or Search Queries
    search_target = raw
    if not is_url:
        prefix = get_platform_meta(platform)["search_prefix"]
        search_target = f"{prefix}{raw}"

    data = await _extract_async(search_target)
    extractor_name = str(data.get("extractor_key") or data.get("extractor") or "").lower()
    if "soundcloud" in extractor_name:
        platform = "soundcloud"

    return [
        Song(
            query=search_target,
            title=str(data.get("title") or "Unknown Title"),
            artist=str(data.get("uploader") or data.get("artist") or get_platform_meta(platform)["name"]),
            stream_url=data["url"],
            webpage_url=str(data.get("webpage_url") or raw),
            duration=int(data.get("duration") or 0),
            thumbnail=data.get("thumbnail"),
            requester=requester,
            platform=platform,
        )
    ]
