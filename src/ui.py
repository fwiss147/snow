import asyncio
from collections import deque
from itertools import islice
import json
import random
import urllib.parse
import urllib.request
import discord

from config import (
    AUDIO_FILTERS,
    AVATAR_URL,
    BANNER_URL,
    BOT_NAME,
    DEFAULT_COLOR,
    MAX_QUEUE,
    PLATFORM_LOGOS,
    PREFIX,
)
from src.extractor import Song, fetch_song, get_platform_meta


def clean_title(title: str, limit: int = 55) -> str:
    text = " ".join(title.split())
    if len(text) > limit:
        text = text[: limit - 1] + "..."
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


def safe_url(url: str | None) -> str | None:
    if (
        url
        and url.startswith(("http://", "https://"))
        and "YourAvatarImage" not in url
        and "YourBannerImage" not in url
    ):
        return url
    return None


def format_seconds(seconds: int) -> str:
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def build_progress_bar(elapsed: int, total: int, bar_length: int = 10) -> str:
    if total <= 0:
        return "🔘" + "▬" * (bar_length - 1)
    ratio = min(1.0, max(0.0, elapsed / total))
    pos = min(bar_length - 1, int(round(ratio * (bar_length - 1))))
    return "▬" * pos + "🔘" + "▬" * (bar_length - 1 - pos)


def resolve_avatar(bot_user: discord.ClientUser | None = None) -> str | None:
    custom = safe_url(AVATAR_URL)
    if custom:
        return custom
    if bot_user is not None:
        return bot_user.display_avatar.url
    from src.player import bot_instance

    if bot_instance and bot_instance.user:
        return bot_instance.user.display_avatar.url
    return None


def get_logo(platform_key: str) -> str:
    key = platform_key.lower()
    configured = PLATFORM_LOGOS.get(key, "").strip()
    if configured:
        return configured

    from src.player import bot_instance

    if bot_instance:
        for emoji in bot_instance.emojis:
            if emoji.name.lower() == key:
                return str(emoji)
    return "•"


def _fetch_lyrics_sync(query: str) -> dict | None:
    url = f"https://lrclib.net/api/search?q={urllib.parse.quote(query)}"
    req = urllib.request.Request(url, headers={"User-Agent": "CoolDownMusicBot/2.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, list) and data:
            for item in data:
                if item.get("plainLyrics"):
                    return item
    return None


async def fetch_lyrics_embed(song_title: str, artist: str = "") -> discord.Embed:
    avatar = resolve_avatar()
    query = f"{song_title} {artist}".strip()
    try:
        res = await asyncio.to_thread(_fetch_lyrics_sync, query)
        if not res and artist:
            res = await asyncio.to_thread(_fetch_lyrics_sync, song_title)
    except Exception:
        res = None

    if not res or not res.get("plainLyrics"):
        embed = discord.Embed(
            title="Lyrics Not Found",
            description=f"No synced lyrics found for **{clean_title(song_title, 45)}**.",
            color=DEFAULT_COLOR,
        )
        if avatar:
            embed.set_author(name=BOT_NAME, icon_url=avatar)
        return embed

    lyrics_text = res["plainLyrics"].strip()
    if len(lyrics_text) > 3800:
        lyrics_text = lyrics_text[:3800] + "\n\n*... [Truncated]*"

    track_name = res.get("trackName") or song_title
    artist_name = res.get("artistName") or artist

    embed = discord.Embed(
        title=f"{track_name} — {artist_name}",
        description=lyrics_text,
        color=DEFAULT_COLOR,
    )
    if avatar:
        embed.set_author(name=f"{BOT_NAME} • Lyrics", icon_url=avatar)
        embed.set_footer(text=f"{BOT_NAME} • Studio Lyrics", icon_url=avatar)
    return embed


def build_main_menu_embed(
    bot_user: discord.ClientUser | None,
    guild_id: int | None = None,
) -> discord.Embed:
    from src.player import bot_instance, players

    avatar = resolve_avatar(bot_user)
    banner = safe_url(BANNER_URL)
    ping_ms = round(bot_instance.latency * 1000) if bot_instance else 0

    sp = get_logo("spotify")
    yt = get_logo("youtube")
    sc = get_logo("soundcloud")
    am = get_logo("applemusic")
    dz = get_logo("deezer")

    guild = bot_instance.get_guild(guild_id) if (bot_instance and guild_id) else None
    vc = guild.voice_client if guild else None
    player = players.get(guild_id) if guild_id else None

    if vc and vc.is_connected() and vc.channel:
        if player and player.current:
            cur_logo = get_logo(player.current.platform)
            status_block = (
                f"> **State:** `🎵 Playing Audio`\n"
                f"> **Room:** <#{vc.channel.id}>\n"
                f"> **Track:** {cur_logo} [{clean_title(player.current.title, 22)}]({safe_url(player.current.webpage_url) or 'https://discord.com'})"
            )
        else:
            status_block = (
                f"> **State:** `🟢 Sitting in VC`\n"
                f"> **Room:** <#{vc.channel.id}>\n"
                f"> **Mode:** `24/7 Standby Lock`"
            )
    else:
        status_block = (
            "> **State:** `⚪ Standby`\n"
            "> **Engine:** `48kHz Opus DSP`\n"
            "> **Action:** `Tap Join or Play`"
        )

    embed = discord.Embed(
        description=(
            f"### 🎧 {BOT_NAME}\n"
            "Multi-source studio player.\n\n"
            f"**🛰️ Voice Connection**\n"
            f"{status_block}\n\n"
            "**🌐 Supported Sources**\n"
            f"{sp} **Spotify** • `Tracks & Lists`\n"
            f"{yt} **YouTube** • `Videos & Music`\n"
            f"{sc} **SoundCloud** • `Sets & Mixes`\n"
            f"{am} **Apple Music** • `Direct Links`\n"
            f"{dz} **Deezer** • `Audio Streams`\n\n"
            f"`⚡ {ping_ms}ms` • `🔊 48kHz` • `⌨️ {PREFIX}help`"
        ),
        color=DEFAULT_COLOR,
    )

    embed.set_author(name=f"{BOT_NAME} • Studio", icon_url=avatar)

    if avatar:
        embed.set_thumbnail(url=avatar)

    if banner:
        embed.set_image(url=banner)

    embed.set_footer(
        text="Use buttons or menu below",
        icon_url=avatar,
    )
    return embed


def build_now_playing_embed(song: Song, player) -> discord.Embed:
    meta = get_platform_meta(song.platform)
    logo = get_logo(song.platform)
    avatar = resolve_avatar()
    banner = safe_url(BANNER_URL)
    thumb = safe_url(song.thumbnail) or avatar

    elapsed = player.elapsed
    total = int(song.duration or 0)
    remaining = max(0, total - elapsed)

    elapsed_str = format_seconds(elapsed)
    total_str = song.length
    remaining_str = f"-{format_seconds(remaining)}" if total > 0 else "LIVE"
    bar = build_progress_bar(elapsed, total)

    state_icon = "⏸️" if player.paused_at is not None else "▶️"
    loop_map = {"off": "Off", "track": "Track", "queue": "Queue"}
    loop_status = loop_map.get(player.loop_mode, "Off")
    vol_percent = int(player.volume * 100)
    filter_label = AUDIO_FILTERS.get(player.filter_name, AUDIO_FILTERS["flat"])["label"].split(" ")[0]
    auto_status = "ON" if player.autoplay else "OFF"

    embed = discord.Embed(
        title=clean_title(song.title, 50),
        url=safe_url(song.webpage_url),
        description=(
            f"`{state_icon} {elapsed_str} {bar} {total_str}`\n\n"
            f"> **Source:** {logo} **{meta['name']}**\n"
            f"> **Artist:** `{clean_title(song.artist, 22)}`\n"
            f"> **Left:** `{remaining_str}` • **Vol:** `{vol_percent}%`\n"
            f"> **DSP:** `{filter_label}` • **Loop:** `{loop_status}`\n"
            f"> **Radio:** `{auto_status}` • **By:** {song.requester.mention}"
        ),
        color=DEFAULT_COLOR,
    )

    embed.set_author(
        name=f"{meta['name']} Stream",
        icon_url=safe_url(meta["icon"]) or avatar,
    )

    if thumb:
        embed.set_thumbnail(url=thumb)

    if banner:
        embed.set_image(url=banner)

    embed.set_footer(
        text=f"{BOT_NAME} • Queue: {len(player.queue)} waiting",
        icon_url=avatar,
    )
    return embed


def build_queue_embed(player) -> discord.Embed:
    avatar = resolve_avatar()
    banner = safe_url(BANNER_URL)

    lines = [f"### 📋 {BOT_NAME} Queue"]
    if player.current is not None:
        logo = get_logo(player.current.platform)
        lines.append(
            f"**Now Playing**\n"
            f"> {logo} [{clean_title(player.current.title, 32)}]({safe_url(player.current.webpage_url) or 'https://discord.com'})\n"
            f"> `{format_seconds(player.elapsed)} / {player.current.length}`\n"
        )
    if player.queue:
        lines.append("**Up Next**")
        for idx, track in enumerate(islice(player.queue, 8), 1):
            t_logo = get_logo(track.platform)
            lines.append(
                f"`{idx:02d}.` {t_logo} [{clean_title(track.title, 28)}]({safe_url(track.webpage_url) or 'https://discord.com'}) `{track.length}`"
            )
        if len(player.queue) > 8:
            lines.append(f"\n*+{len(player.queue) - 8} more track(s)*")
    else:
        lines.append("*No tracks waiting in queue.*")

    embed = discord.Embed(
        description="\n".join(lines),
        color=DEFAULT_COLOR,
    )
    if avatar:
        embed.set_author(name=f"{BOT_NAME} • Queue", icon_url=avatar)
        embed.set_thumbnail(url=avatar)
    if banner:
        embed.set_image(url=banner)
    embed.set_footer(
        text=f"Queued: {len(player.queue)} • Loop: {player.loop_mode.upper()}",
        icon_url=avatar,
    )
    return embed


class QuickPlayModal(discord.ui.Modal, title="Play Song or Paste Link"):
    query_input = discord.ui.TextInput(
        label="Track Name or URL (Spotify / YT / SC)",
        placeholder="Paste a link or type a song name...",
        required=True,
        max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction):
        from src.player import get_player, play_next, start_progress_updater

        state = getattr(interaction.user, "voice", None)
        channel = getattr(state, "channel", None)
        if channel is None:
            await interaction.response.send_message(
                "Join a voice channel first, then click Play.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        vc = interaction.guild.voice_client
        try:
            if vc is not None and not vc.is_connected():
                await vc.disconnect(force=True)
                vc = None
            if vc is None:
                vc = await channel.connect(timeout=30, reconnect=True, self_deaf=True)
            elif vc.channel != channel:
                await vc.move_to(channel)
        except Exception:
            await interaction.followup.send(
                "Could not join voice. Check my permissions.",
                ephemeral=True,
            )
            return

        player = get_player(interaction.guild.id)
        player.channel_id = interaction.channel_id

        raw_query = self.query_input.value.strip()
        try:
            songs = await fetch_song(raw_query, interaction.user, "youtube")
        except Exception:
            await interaction.followup.send(
                "Could not load that track or link.",
                ephemeral=True,
            )
            return

        was_idle = player.current is None and not vc.is_playing() and not vc.is_paused()
        if was_idle:
            player.suppress_next_panel = True

        for s in songs:
            if len(player.queue) < MAX_QUEUE:
                player.queue.append(s)

        if player.idle_task is not None:
            player.idle_task.cancel()
            player.idle_task = None

        await play_next(interaction.guild)
        first_song = songs[0]

        if player.current is first_song and vc.is_playing() and was_idle:
            msg = await interaction.followup.send(
                embed=build_now_playing_embed(first_song, player),
                view=PlayerControlView(interaction.guild.id),
                wait=True,
            )
            if isinstance(msg, discord.Message):
                player.panel_message = msg
                player.panel_message_id = msg.id
            start_progress_updater(interaction.guild, player)
        else:
            logo = get_logo(first_song.platform)
            embed = discord.Embed(
                description=f"{logo} Queued: **[{clean_title(first_song.title, 38)}]({safe_url(first_song.webpage_url) or 'https://discord.com'})** `{first_song.length}`",
                color=DEFAULT_COLOR,
            )
            await interaction.followup.send(embed=embed)


class MenuSelect(discord.ui.Select):
    def __init__(self, bot: discord.Client):
        self.bot = bot
        options = [
            discord.SelectOption(
                label="Main Dashboard",
                description="Live status & supported sources",
                value="home",
                emoji="🎧",
            ),
            discord.SelectOption(
                label="Studio DSP Filters",
                description="BassBoost, Nightcore, Slowed & 8D",
                value="filters",
                emoji="🎚️",
            ),
            discord.SelectOption(
                label="Command List",
                description="All slash & prefix commands",
                value="commands",
                emoji="⌨️",
            ),
            discord.SelectOption(
                label="System Telemetry",
                description="Latency, codec & voice stats",
                value="status",
                emoji="📡",
            ),
        ]
        super().__init__(
            placeholder="Switch Dashboard View...",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        if choice == "home":
            await interaction.response.edit_message(
                embed=build_main_menu_embed(self.bot.user, interaction.guild_id),
                view=self.view,
            )
            return

        avatar = resolve_avatar(self.bot.user)
        banner = safe_url(BANNER_URL)

        embed = discord.Embed(color=DEFAULT_COLOR)
        if avatar:
            embed.set_author(name=f"{BOT_NAME} • Studio", icon_url=avatar)
            embed.set_thumbnail(url=avatar)
        if banner:
            embed.set_image(url=banner)

        if choice == "filters":
            embed.description = (
                "### 🎚️ Studio DSP Filters\n"
                "Real-time effects without restart.\n\n"
                "> **Flat** • `Original Master`\n"
                "> **BassBoost** • `+10dB Sub Punch`\n"
                "> **Nightcore** • `1.22x Pitch/Speed`\n"
                "> **Slowed** • `0.86x + Reverb`\n"
                "> **8D Audio** • `Spatial Surround`\n"
                "> **Vocal** • `Treble Presence`\n\n"
                f"Use `{PREFIX}filter <name>` or `/filter`"
            )
        elif choice == "commands":
            embed.description = (
                "### ⌨️ Command Suite\n"
                "**Voice & Playback**\n"
                f"> `{PREFIX}join` • `Sit in VC 24/7`\n"
                f"> `{PREFIX}leave` • `Disconnect VC`\n"
                f"> `{PREFIX}play` • `Play link/query`\n"
                f"> `{PREFIX}seek` • `Jump to second`\n"
                f"> `{PREFIX}vol` • `Volume (0-150%)`\n\n"
                "**Studio & Queue**\n"
                f"> `{PREFIX}filter` • `Apply DSP FX`\n"
                f"> `{PREFIX}autoplay` • `Smart Radio`\n"
                f"> `{PREFIX}lyrics` • `Track Lyrics`\n"
                f"> `{PREFIX}q` / `{PREFIX}np` / `{PREFIX}loop`"
            )
        elif choice == "status":
            ping_ms = round(self.bot.latency * 1000)
            active_vcs = len(self.bot.voice_clients)
            embed.description = (
                "### 📡 System Telemetry\n"
                "Live audio engine metrics.\n\n"
                f"> **Ping:** `{ping_ms} ms`\n"
                f"> **Servers:** `{len(self.bot.guilds)}`\n"
                f"> **Active VCs:** `{active_vcs}`\n"
                "> **Codec:** `48kHz Stereo Opus`\n"
                "> **Idle Mode:** `24/7 Supported`"
            )

        embed.set_footer(text="Select 'Main Dashboard' to return", icon_url=avatar)
        await interaction.response.edit_message(embed=embed, view=self.view)


class MainMenuView(discord.ui.View):
    def __init__(self, bot: discord.Client):
        super().__init__(timeout=None)
        self.bot = bot
        self.add_item(MenuSelect(bot))

    @discord.ui.button(label="Play", emoji="🔍", style=discord.ButtonStyle.success, row=1)
    async def quick_play_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(QuickPlayModal())

    @discord.ui.button(label="Join VC", emoji="🔊", style=discord.ButtonStyle.primary, row=1)
    async def join_vc_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        from src.player import get_player

        state = getattr(interaction.user, "voice", None)
        channel = getattr(state, "channel", None)
        if channel is None:
            await interaction.response.send_message(
                "Join a voice channel first, then tap **Join VC**.",
                ephemeral=True,
            )
            return

        vc = interaction.guild.voice_client if interaction.guild else None
        try:
            if vc is not None and not vc.is_connected():
                await vc.disconnect(force=True)
                vc = None
            if vc is None:
                await channel.connect(timeout=30, reconnect=True, self_deaf=True)
            elif vc.channel != channel:
                await vc.move_to(channel)
        except Exception:
            await interaction.response.send_message(
                "Could not join voice. Check my permissions.",
                ephemeral=True,
            )
            return

        player = get_player(interaction.guild.id)
        player.channel_id = interaction.channel_id
        player.stay_247 = True
        if player.idle_task is not None:
            player.idle_task.cancel()
            player.idle_task = None

        await interaction.response.edit_message(
            embed=build_main_menu_embed(self.bot.user, interaction.guild_id),
            view=self,
        )

    @discord.ui.button(label="Leave", emoji="🔇", style=discord.ButtonStyle.danger, row=1)
    async def leave_vc_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        from src.player import drop_player

        vc = interaction.guild.voice_client if interaction.guild else None
        if vc is None or not vc.is_connected():
            await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
            return

        drop_player(interaction.guild.id)
        vc.stop()
        try:
            await vc.disconnect()
        except Exception:
            pass

        await interaction.response.edit_message(
            embed=build_main_menu_embed(self.bot.user, interaction.guild_id),
            view=self,
        )

    @discord.ui.button(label="Player", emoji="🎧", style=discord.ButtonStyle.secondary, row=1)
    async def np_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        from src.player import players

        player = players.get(interaction.guild_id) if interaction.guild_id else None
        if player is None or player.current is None:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=build_now_playing_embed(player.current, player),
            view=PlayerControlView(interaction.guild_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Queue", emoji="📋", style=discord.ButtonStyle.secondary, row=1)
    async def queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        from src.player import players

        player = players.get(interaction.guild_id) if interaction.guild_id else None
        if player is None or (player.current is None and not player.queue):
            await interaction.response.send_message("The queue is currently empty.", ephemeral=True)
            return
        await interaction.response.send_message(embed=build_queue_embed(player), ephemeral=True)


class DSPFilterSelect(discord.ui.Select):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        options = [
            discord.SelectOption(label=meta["label"], description=meta["description"], value=key, emoji="🎚️")
            for key, meta in AUDIO_FILTERS.items()
        ]
        super().__init__(
            placeholder="🎚️ Studio DSP Audio Filter...",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        from src.player import get_player, seek_or_filter_stream

        if not interaction.guild:
            return
        player = get_player(self.guild_id)
        chosen = self.values[0]
        await interaction.response.defer()
        ok = await seek_or_filter_stream(interaction.guild, new_filter=chosen)
        if ok and player.current:
            await interaction.edit_original_response(
                embed=build_now_playing_embed(player.current, player),
                view=self.view,
            )
        else:
            await interaction.followup.send("Could not apply filter right now.", ephemeral=True)


class QuickPlayerSelect(discord.ui.Select):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        options = [
            discord.SelectOption(label="Volume: 25%", description="Quiet background level", value="vol_25", emoji="🔈"),
            discord.SelectOption(label="Volume: 50%", description="Default balanced level", value="vol_50", emoji="🔉"),
            discord.SelectOption(label="Volume: 100%", description="Full master output", value="vol_100", emoji="🔊"),
            discord.SelectOption(label="Volume: 135% (Boost)", description="Extra high gain output", value="vol_135", emoji="📢"),
            discord.SelectOption(label="Toggle 24/7 Sit Mode", description="Stay in voice channel 24/7", value="toggle_247", emoji="🌙"),
            discord.SelectOption(label="Open Queue", description="Inspect upcoming tracks", value="view_queue", emoji="📋"),
        ]
        super().__init__(
            placeholder="🔊 Volume, 24/7 & Queue...",
            min_values=1,
            max_values=1,
            options=options,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        from src.player import get_player

        player = get_player(self.guild_id)
        choice = self.values[0]

        if choice == "view_queue":
            await interaction.response.send_message(embed=build_queue_embed(player), ephemeral=True)
            return

        if choice == "toggle_247":
            player.stay_247 = not player.stay_247
            state = "ENABLED" if player.stay_247 else "DISABLED"
            await interaction.response.send_message(f"🌙 **24/7 Sit-in-Voice Mode** is now **{state}**.", ephemeral=True)
            return

        vc = interaction.guild.voice_client if interaction.guild else None
        if vc is None or not vc.is_connected():
            await interaction.response.send_message("I'm not connected to a voice channel.", ephemeral=True)
            return

        level = int(choice.split("_")[1])
        player.volume = level / 100
        if getattr(vc, "source", None) and hasattr(vc.source, "volume"):
            vc.source.volume = player.volume

        if player.current:
            await interaction.response.edit_message(
                embed=build_now_playing_embed(player.current, player),
                view=self.view,
            )
        else:
            await interaction.response.send_message(f"Volume set to **{level}%**.", ephemeral=True)


class PlayerControlView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.add_item(DSPFilterSelect(guild_id))
        self.add_item(QuickPlayerSelect(guild_id))

    async def _check_voice(self, interaction: discord.Interaction) -> bool:
        vc = interaction.guild.voice_client if interaction.guild else None
        if vc is None or not vc.is_connected():
            await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
            return False
        user_channel = getattr(getattr(interaction.user, "voice", None), "channel", None)
        if user_channel != vc.channel and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Join my voice channel to control playback.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="-15s", emoji="⏪", style=discord.ButtonStyle.secondary, row=0)
    async def rewind_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_voice(interaction):
            return
        from src.player import get_player, seek_or_filter_stream

        player = get_player(self.guild_id)
        if not player.current:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        new_pos = max(0, player.elapsed - 15)
        await interaction.response.defer()
        await seek_or_filter_stream(interaction.guild, target_seconds=new_pos)
        await interaction.edit_original_response(
            embed=build_now_playing_embed(player.current, player),
            view=self,
        )

    @discord.ui.button(label="Pause", emoji="⏯️", style=discord.ButtonStyle.secondary, row=0)
    async def pause_resume_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_voice(interaction):
            return
        from src.player import get_player

        player = get_player(self.guild_id)
        vc = interaction.guild.voice_client
        if vc.is_paused():
            vc.resume()
            player.mark_resumed()
            button.label = "Pause"
            button.style = discord.ButtonStyle.secondary
        elif vc.is_playing():
            vc.pause()
            player.mark_paused()
            button.label = "Resume"
            button.style = discord.ButtonStyle.success
        else:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return

        if player.current:
            await interaction.response.edit_message(
                embed=build_now_playing_embed(player.current, player),
                view=self,
            )

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.primary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_voice(interaction):
            return
        vc = interaction.guild.voice_client
        if not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        from src.player import get_player

        player = get_player(self.guild_id)
        player.skip_loop_once = True
        vc.stop()
        await interaction.response.send_message("⏭️ Skipped track.", ephemeral=True)

    @discord.ui.button(label="+15s", emoji="⏩", style=discord.ButtonStyle.secondary, row=0)
    async def forward_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_voice(interaction):
            return
        from src.player import get_player, seek_or_filter_stream

        player = get_player(self.guild_id)
        if not player.current:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        new_pos = player.elapsed + 15
        await interaction.response.defer()
        await seek_or_filter_stream(interaction.guild, target_seconds=new_pos)
        await interaction.edit_original_response(
            embed=build_now_playing_embed(player.current, player),
            view=self,
        )

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_voice(interaction):
            return
        from src.player import get_player, stop_progress_updater

        vc = interaction.guild.voice_client
        player = get_player(self.guild_id)
        player.queue.clear()
        player.current = None
        stop_progress_updater(player)
        vc.stop()

        if player.stay_247:
            await interaction.response.edit_message(
                content=f"⏹️ Stopped playback — sitting in <#{vc.channel.id}> (24/7 Mode).",
                embed=None,
                view=None,
            )
        else:
            from src.player import drop_player

            drop_player(self.guild_id)
            await interaction.response.edit_message(
                content=f"⏹️ Playback stopped by {interaction.user.mention}.",
                embed=None,
                view=None,
            )
            try:
                await vc.disconnect()
            except Exception:
                pass

    @discord.ui.button(label="Loop", emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_voice(interaction):
            return
        from src.player import get_player

        player = get_player(self.guild_id)
        mode = player.cycle_loop()
        button.label = f"Loop: {mode.capitalize()}"
        button.style = discord.ButtonStyle.success if mode != "off" else discord.ButtonStyle.secondary

        if player.current:
            await interaction.response.edit_message(
                embed=build_now_playing_embed(player.current, player),
                view=self,
            )

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_voice(interaction):
            return
        from src.player import get_player

        player = get_player(self.guild_id)
        if len(player.queue) < 2:
            await interaction.response.send_message("Need at least 2 queued songs to shuffle.", ephemeral=True)
            return
        items = list(player.queue)
        random.shuffle(items)
        player.queue = deque(items)
        await interaction.response.send_message("🔀 Queue shuffled.", ephemeral=True)

    @discord.ui.button(label="Autoplay", emoji="♾️", style=discord.ButtonStyle.secondary, row=1)
    async def autoplay_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_voice(interaction):
            return
        from src.player import get_player

        player = get_player(self.guild_id)
        player.autoplay = not player.autoplay
        button.style = discord.ButtonStyle.success if player.autoplay else discord.ButtonStyle.secondary

        if player.current:
            await interaction.response.edit_message(
                embed=build_now_playing_embed(player.current, player),
                view=self,
            )

    @discord.ui.button(label="Lyrics", emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def lyrics_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        from src.player import get_player

        player = get_player(self.guild_id)
        if not player.current:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        embed = await fetch_lyrics_embed(player.current.title, player.current.artist)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Save", emoji="❤️", style=discord.ButtonStyle.secondary, row=1)
    async def save_dm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        from src.player import get_player

        player = get_player(self.guild_id)
        if not player.current:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        song = player.current
        meta = get_platform_meta(song.platform)
        logo = get_logo(song.platform)
        dm_embed = discord.Embed(
            title=clean_title(song.title, 60),
            url=safe_url(song.webpage_url),
            description=(
                f"> **Source:** {logo} **{meta['name']}**\n"
                f"> **Artist:** `{clean_title(song.artist, 28)}`\n"
                f"> **Length:** `{song.length}`\n"
                f"> **Link:** [Open Track]({safe_url(song.webpage_url) or 'https://discord.com'})"
            ),
            color=DEFAULT_COLOR,
        )
        if song.thumbnail:
            dm_embed.set_thumbnail(url=safe_url(song.thumbnail))
        try:
            await interaction.user.send(embed=dm_embed)
            await interaction.response.send_message("❤️ Saved track to your DMs!", ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message("Could not DM you. Check your privacy settings.", ephemeral=True)