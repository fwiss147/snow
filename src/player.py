import asyncio
from collections import deque
import ctypes.util
from dataclasses import dataclass, field
from pathlib import Path
import time

import discord

from config import IDLE_LEAVE_SECONDS, build_ffmpeg_options, log
from src.extractor import Song, fetch_song
from src.ui import PlayerControlView, build_now_playing_embed, clean_title


@dataclass
class GuildPlayer:
    queue: deque[Song] = field(default_factory=deque)
    history: deque[Song] = field(default_factory=lambda: deque(maxlen=15))
    current: Song | None = None
    loop_mode: str = "off"  # "off", "track", "queue"
    skip_loop_once: bool = False
    autoplay: bool = False
    stay_247: bool = False
    filter_name: str = "flat"
    volume: float = 0.5
    channel_id: int | None = None
    panel_message_id: int | None = None
    panel_message: discord.Message | None = None
    suppress_next_panel: bool = False
    started_at: float = 0.0
    seek_offset: int = 0
    paused_at: float | None = None
    paused_total: float = 0.0
    is_seeking: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    idle_task: asyncio.Task | None = None
    progress_task: asyncio.Task | None = None

    @property
    def loop(self) -> bool:
        return self.loop_mode != "off"

    @loop.setter
    def loop(self, val: bool) -> None:
        self.loop_mode = "track" if val else "off"

    def cycle_loop(self) -> str:
        order = ["off", "track", "queue"]
        idx = (order.index(self.loop_mode) + 1) % len(order) if self.loop_mode in order else 0
        self.loop_mode = order[idx]
        return self.loop_mode

    def mark_started(self, offset: int = 0) -> None:
        self.started_at = time.monotonic()
        self.seek_offset = max(0, int(offset))
        self.paused_at = None
        self.paused_total = 0.0

    def mark_paused(self) -> None:
        if self.paused_at is None:
            self.paused_at = time.monotonic()

    def mark_resumed(self) -> None:
        if self.paused_at is not None:
            self.paused_total += time.monotonic() - self.paused_at
            self.paused_at = None

    @property
    def elapsed(self) -> int:
        if self.current is None or self.started_at <= 0.0:
            return 0
        now = self.paused_at if self.paused_at is not None else time.monotonic()
        secs = self.seek_offset + int(max(0.0, now - self.started_at - self.paused_total))
        if self.current.duration:
            return min(secs, int(self.current.duration))
        return secs


players: dict[int, GuildPlayer] = {}
background_tasks: set[asyncio.Task] = set()
bot_instance: discord.Client | None = None


def set_bot(bot: discord.Client) -> None:
    global bot_instance
    bot_instance = bot


def get_player(guild_id: int) -> GuildPlayer:
    return players.setdefault(guild_id, GuildPlayer())


def stop_progress_updater(player: GuildPlayer) -> None:
    if player.progress_task is not None:
        player.progress_task.cancel()
        player.progress_task = None


def drop_player(guild_id: int) -> GuildPlayer | None:
    player = players.pop(guild_id, None)
    if player is not None:
        if player.idle_task is not None:
            player.idle_task.cancel()
            player.idle_task = None
        stop_progress_updater(player)
    return player


async def _progress_loop(guild: discord.Guild, player: GuildPlayer) -> None:
    try:
        while True:
            await asyncio.sleep(5)
            vc = guild.voice_client
            if (
                vc is None
                or not vc.is_connected()
                or player.current is None
                or players.get(guild.id) is not player
            ):
                break

            if vc.is_paused() or player.is_seeking:
                continue

            msg = player.panel_message
            if msg is None and player.channel_id and player.panel_message_id:
                channel = guild.get_channel(player.channel_id)
                if channel:
                    try:
                        msg = await channel.fetch_message(player.panel_message_id)
                        player.panel_message = msg
                    except discord.HTTPException:
                        player.panel_message_id = None
                        break

            if msg is not None:
                try:
                    await msg.edit(embed=build_now_playing_embed(player.current, player))
                except discord.HTTPException:
                    break
    except asyncio.CancelledError:
        raise
    except Exception:
        log.debug("Progress bar updater stopped in guild %s", guild.id, exc_info=True)


def start_progress_updater(guild: discord.Guild, player: GuildPlayer) -> None:
    stop_progress_updater(player)
    player.progress_task = asyncio.create_task(_progress_loop(guild, player))


async def _notify(guild_id: int, message: str) -> None:
    if bot_instance is None:
        return
    player = players.get(guild_id)
    if player is None or player.channel_id is None:
        return
    guild = bot_instance.get_guild(guild_id)
    if guild is None:
        return
    channel = guild.get_channel(player.channel_id)
    if channel is None:
        return
    try:
        await channel.send(message)
    except discord.HTTPException:
        log.warning("Could not send a notice to channel %s", player.channel_id)


async def send_or_replace_panel(guild: discord.Guild, player: GuildPlayer, song: Song) -> None:
    if player.channel_id is None:
        return
    channel = guild.get_channel(player.channel_id)
    if channel is None:
        return

    if player.panel_message is not None:
        try:
            await player.panel_message.delete()
        except discord.HTTPException:
            pass
    elif player.panel_message_id:
        try:
            old_msg = await channel.fetch_message(player.panel_message_id)
            await old_msg.delete()
        except discord.HTTPException:
            pass

    try:
        msg = await channel.send(
            embed=build_now_playing_embed(song, player),
            view=PlayerControlView(guild.id),
        )
        player.panel_message = msg
        player.panel_message_id = msg.id
        start_progress_updater(guild, player)
    except discord.HTTPException:
        log.warning("Could not send now playing panel in guild %s", guild.id)


async def seek_or_filter_stream(
    guild: discord.Guild,
    target_seconds: int | None = None,
    new_filter: str | None = None,
) -> bool:
    """Seeks to a timestamp or hot-swaps the FFmpeg audio filter without losing position."""
    player = players.get(guild.id)
    vc = guild.voice_client
    if player is None or player.current is None or vc is None or not vc.is_connected():
        return False

    song = player.current
    sec = player.elapsed if target_seconds is None else max(0, int(target_seconds))
    if song.duration and sec >= song.duration:
        sec = max(0, int(song.duration) - 2)

    if new_filter is not None:
        player.filter_name = new_filter

    player.is_seeking = True
    if vc.is_playing() or vc.is_paused():
        vc.stop()

    loop = asyncio.get_running_loop()

    def after(error: Exception | None) -> None:
        if player.is_seeking:
            return
        if error is not None:
            log.error("Playback error in guild %s: %s", guild.id, error)
        if loop.is_closed():
            return
        loop.call_soon_threadsafe(
            lambda: loop.create_task(_handle_track_end(guild.id, error))
        )

    try:
        ffmpeg_opts = build_ffmpeg_options(player.filter_name, seek_seconds=sec)
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(song.stream_url, **ffmpeg_opts),
            volume=player.volume,
        )
        player.mark_started(offset=sec)
        player.is_seeking = False
        vc.play(source, after=after)
        return True
    except Exception:
        player.is_seeking = False
        log.exception("Failed to seek or apply filter in guild %s", guild.id)
        return False


async def _handle_track_end(guild_id: int, error: Exception | None) -> None:
    if bot_instance is None:
        return
    player = players.get(guild_id)
    if player is None or player.is_seeking:
        return
    stop_progress_updater(player)
    if error is not None:
        title = player.current.title if player.current is not None else "the track"
        player.skip_loop_once = True
        await _notify(guild_id, f"Could not play **{clean_title(title)}** - skipping.")
    guild = bot_instance.get_guild(guild_id)
    if guild is None:
        drop_player(guild_id)
        return
    await play_next(guild)


async def play_next(guild: discord.Guild) -> None:
    player = players.get(guild.id)
    if player is None:
        return
    async with player.lock:
        if players.get(guild.id) is not player:
            return
        vc = guild.voice_client
        if vc is None or not vc.is_connected():
            return
        if vc.is_playing() or vc.is_paused():
            return

        if player.current is not None:
            player.history.append(player.current)
            if player.loop_mode == "queue" and not player.skip_loop_once:
                player.queue.append(player.current)

        if player.loop_mode == "track" and player.current is not None and not player.skip_loop_once:
            song = player.current
            try:
                fresh_list = await fetch_song(song.query, song.requester, song.platform)
                fresh = fresh_list[0]
            except Exception:
                log.exception("Could not refresh the looped song; using cached URL")
            else:
                song.stream_url = fresh.stream_url
            if players.get(guild.id) is not player or not vc.is_connected():
                return
        elif player.queue:
            song = player.queue.popleft()
            player.current = song
        elif player.autoplay and player.history:
            last_song = player.history[-1]
            try:
                auto_query = f"{last_song.artist} {last_song.title} official audio mix"
                auto_list = await fetch_song(auto_query, last_song.requester, "youtube")
                song = auto_list[0]
                player.current = song
            except Exception:
                log.warning("Autoplay could not find a next song in guild %s", guild.id)
                player.current = None
                stop_progress_updater(player)
                return
        else:
            player.current = None
            stop_progress_updater(player)
            return

        player.skip_loop_once = False
        loop = asyncio.get_running_loop()

        def after(error: Exception | None) -> None:
            if player.is_seeking:
                return
            if error is not None:
                log.error("Playback error in guild %s: %s", guild.id, error)
            if loop.is_closed():
                return
            loop.call_soon_threadsafe(
                lambda: loop.create_task(_handle_track_end(guild.id, error))
            )

        try:
            ffmpeg_opts = build_ffmpeg_options(player.filter_name, seek_seconds=0)
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(song.stream_url, **ffmpeg_opts),
                volume=player.volume,
            )
            player.mark_started(offset=0)
            vc.play(source, after=after)
            if player.suppress_next_panel:
                player.suppress_next_panel = False
            else:
                await send_or_replace_panel(guild, player, song)
        except Exception:
            log.exception("Could not start playback in guild %s", guild.id)
            player.current = None
            stop_progress_updater(player)
            await _notify(
                guild.id,
                f"Could not start **{clean_title(song.title)}**. Check the bot logs.",
            )


async def idle_leave(guild_id: int) -> None:
    try:
        await asyncio.sleep(IDLE_LEAVE_SECONDS)
        if bot_instance is None:
            return
        guild = bot_instance.get_guild(guild_id)
        player = players.get(guild_id)
        if guild is None or player is None:
            return
        if player.stay_247:
            return
        vc = guild.voice_client
        if vc is None or not vc.is_connected() or vc.channel is None:
            return
        if any(not member.bot for member in vc.channel.members):
            return
        player.idle_task = None
        drop_player(guild_id)
        await vc.disconnect()
        log.info("Left voice in guild %s: alone in the channel", guild_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Idle disconnect failed in guild %s", guild_id)


def load_opus() -> None:
    try:
        if discord.opus.is_loaded():
            return
    except Exception:
        pass

    found = ctypes.util.find_library("opus")
    if found:
        try:
            discord.opus.load_opus(found)
            log.info("Loaded libopus from %s", found)
            return
        except Exception:
            log.debug("Could not load libopus from %s", found, exc_info=True)

    try:
        import av

        lib_dir = Path(av.__file__).parent / ".dylibs"
        candidates = list(lib_dir.glob("libopus*.dylib")) + list(lib_dir.glob("libopus*.so*"))
    except Exception:
        candidates = []

    for candidate in candidates:
        try:
            discord.opus.load_opus(str(candidate))
            log.info("Loaded libopus from %s", candidate)
            return
        except Exception:
            log.debug("Could not load libopus from %s", candidate, exc_info=True)

    try:
        if not discord.opus.is_loaded():
            log.warning("libopus is not available; voice playback will fail")
    except Exception:
        pass