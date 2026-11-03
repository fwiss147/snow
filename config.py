import logging
import os
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("zero")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _clean_env_url(env_key: str, default_url: str) -> str:
    val = (os.getenv(env_key) or "").strip()
    if not val or "YourAvatarImage" in val or "YourBannerImage" in val:
        return default_url
    return val


TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = _env_int("GUILD_ID", 0) or None
IDLE_LEAVE_SECONDS = _env_int("IDLE_LEAVE_SECONDS", 60)
MAX_QUEUE = _env_int("MAX_QUEUE", 100)
COOKIEFILE = os.getenv("YTDL_COOKIEFILE") or None
PREFIX = (os.getenv("BOT_PREFIX") or "z!").strip() or "z!"

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID") or None
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET") or None

BOT_NAME = os.getenv("BOT_NAME") or "CoolDown Music"
AVATAR_URL = _clean_env_url("BOT_AVATAR_URL", "")
BANNER_URL = _clean_env_url(
    "BOT_BANNER_URL",
    "https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=1200&auto=format&fit=crop&q=80",
)

DEFAULT_COLOR = 0x2B2D31

PLATFORM_LOGOS = {
    "spotify": os.getenv("LOGO_SPOTIFY") or "",
    "youtube": os.getenv("LOGO_YOUTUBE") or "",
    "soundcloud": os.getenv("LOGO_SOUNDCLOUD") or "",
    "applemusic": os.getenv("LOGO_APPLE") or "",
    "deezer": os.getenv("LOGO_DEEZER") or "",
}

PLATFORMS = {
    "spotify": {
        "name": "Spotify",
        "color": 0x2B2D31,
        "icon": "https://cdn-icons-png.flaticon.com/512/174/174872.png",
        "search_prefix": "ytsearch:",
    },
    "youtube": {
        "name": "YouTube",
        "color": 0x2B2D31,
        "icon": "https://cdn-icons-png.flaticon.com/512/1384/1384060.png",
        "search_prefix": "ytsearch:",
    },
    "soundcloud": {
        "name": "SoundCloud",
        "color": 0x2B2D31,
        "icon": "https://cdn-icons-png.flaticon.com/512/145/145809.png",
        "search_prefix": "scsearch:",
    },
    "applemusic": {
        "name": "Apple Music",
        "color": 0x2B2D31,
        "icon": "https://cdn-icons-png.flaticon.com/512/732/732068.png",
        "search_prefix": "ytsearch:",
    },
    "deezer": {
        "name": "Deezer",
        "color": 0x2B2D31,
        "icon": "https://cdn-icons-png.flaticon.com/512/5968/5968860.png",
        "search_prefix": "ytsearch:",
    },
}

# Real-time FFmpeg DSP Audio Filters
AUDIO_FILTERS = {
    "flat": {
        "label": "Studio Flat (Default)",
        "description": "Original uncolored master audio",
        "ffmpeg": "aresample=async=1:first_pts=0",
    },
    "bassboost": {
        "label": "BassBoost (Sub Punch)",
        "description": "Deep low-end sub-bass enhancement",
        "ffmpeg": "aresample=async=1:first_pts=0,bass=g=10:f=110:w=0.6",
    },
    "nightcore": {
        "label": "Nightcore (Speed & Pitch)",
        "description": "1.25x tempo with pitched vocals",
        "ffmpeg": "aresample=48000,asetrate=48000*1.22,aresample=48000",
    },
    "slowed": {
        "label": "Slowed + Reverb",
        "description": "Atmospheric 0.85x speed with echo",
        "ffmpeg": "aresample=48000,asetrate=48000*0.86,aresample=48000,aecho=0.8:0.88:60:0.4",
    },
    "8d": {
        "label": "8D Surround Audio",
        "description": "Rotating spatial stereo panorama",
        "ffmpeg": "aresample=async=1:first_pts=0,apulsator=hz=0.125",
    },
    "vocal": {
        "label": "Vocal & Treble Clarity",
        "description": "Crisp mid-high presence boost",
        "ffmpeg": "aresample=async=1:first_pts=0,treble=g=6:f=3000",
    },
}

YTDL_OPTIONS = {
    "format": "bestaudio[acodec=opus]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "default_search": "ytsearch",
    "socket_timeout": 20,
}

FFMPEG_BASE_BEFORE = (
    "-nostdin -reconnect 1 -reconnect_streamed 1 "
    "-reconnect_on_network_error 1 -reconnect_delay_max 5 "
    "-thread_queue_size 1024 -rw_timeout 15000000"
)


def build_ffmpeg_options(filter_key: str = "flat", seek_seconds: int = 0) -> dict:
    f_chain = AUDIO_FILTERS.get(filter_key, AUDIO_FILTERS["flat"])["ffmpeg"]
    before = FFMPEG_BASE_BEFORE
    if seek_seconds > 0:
        before = f"{before} -ss {int(seek_seconds)}"
    return {
        "before_options": before,
        "options": f"-vn -ar 48000 -ac 2 -af {f_chain} -loglevel error",
    }


def ytdl_options() -> dict:
    options = dict(YTDL_OPTIONS)
    if COOKIEFILE:
        if os.path.exists(COOKIEFILE):
            options["cookiefile"] = COOKIEFILE
        else:
            log.warning("YTDL_COOKIEFILE is set but %s does not exist", COOKIEFILE)
    return options