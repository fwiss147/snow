from collections import deque
import random
import discord
from discord import app_commands

from config import AUDIO_FILTERS, AVATAR_URL, DEFAULT_COLOR, MAX_QUEUE, PREFIX, log
from src.extractor import fetch_song, get_platform_meta
from src.player import (
    drop_player,
    get_player,
    play_next,
    players,
    seek_or_filter_stream,
    start_progress_updater,
    stop_progress_updater,
)
from src.ui import (
    MainMenuView,
    PlayerControlView,
    build_main_menu_embed,
    build_now_playing_embed,
    build_queue_embed,
    clean_title,
    fetch_lyrics_embed,
    get_logo,
    safe_url,
)


async def require_user_voice(interaction: discord.Interaction) -> discord.VoiceChannel | None:
    state = getattr(interaction.user, "voice", None)
    channel = getattr(state, "channel", None)
    if channel is None:
        await interaction.response.send_message(
            "Join a voice channel first, then try again.", ephemeral=True
        )
        return None
    return channel


async def require_playback_control(interaction: discord.Interaction) -> bool:
    vc = interaction.guild.voice_client
    if vc is None or not vc.is_connected():
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return False
    user_channel = getattr(getattr(interaction.user, "voice", None), "channel", None)
    if user_channel != vc.channel and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "Join my voice channel to control playback.", ephemeral=True
        )
        return False
    return True


def setup_commands(bot: discord.Client):
    @bot.tree.command(name="menu", description="Open the interactive studio dashboard")
    @app_commands.guild_only()
    async def menu_cmd(interaction: discord.Interaction):
        embed = build_main_menu_embed(bot.user, interaction.guild_id)
        await interaction.response.send_message(embed=embed, view=MainMenuView(bot))

    @bot.tree.command(name="join", description="Join your voice channel and sit there 24/7 without playing music")
    @app_commands.guild_only()
    async def join_cmd(interaction: discord.Interaction):
        channel = await require_user_voice(interaction)
        if channel is None:
            return

        vc = interaction.guild.voice_client
        try:
            if vc is not None and not vc.is_connected():
                await vc.disconnect(force=True)
                vc = None
            if vc is None:
                await channel.connect(timeout=30, reconnect=True, self_deaf=True)
            elif vc.channel != channel:
                await vc.move_to(channel)
        except Exception:
            log.exception("Could not join voice in guild %s", interaction.guild_id)
            await interaction.response.send_message(
                "Could not join your voice channel. Check my permissions.",
                ephemeral=True,
            )
            return

        player = get_player(interaction.guild.id)
        player.channel_id = interaction.channel_id
        player.stay_247 = True
        if player.idle_task is not None:
            player.idle_task.cancel()
            player.idle_task = None

        embed = discord.Embed(
            description=(
                f"🔊 **Connected to** <#{channel.id}>\n"
                "> **Mode:** `24/7 Standby (Sitting in Voice)`\n"
                f"> Use `/play` or `{PREFIX}play` anytime to start music."
            ),
            color=DEFAULT_COLOR,
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="leave", description="Disconnect the bot from the voice channel")
    @app_commands.guild_only()
    async def leave_cmd(interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc is None or not vc.is_connected():
            await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
            return
        drop_player(interaction.guild.id)
        vc.stop()
        await interaction.response.send_message("🔇 Disconnected from the voice channel.")
        try:
            await vc.disconnect()
        except Exception:
            pass

    @bot.tree.command(
        name="play",
        description="Play any link (Spotify, YouTube, SoundCloud, Apple Music, Deezer) or search",
    )
    @app_commands.describe(
        query="Paste a music link or type a song name",
        source="Preferred search platform if typing a song name",
    )
    @app_commands.choices(
        source=[
            app_commands.Choice(name="Spotify", value="spotify"),
            app_commands.Choice(name="YouTube", value="youtube"),
            app_commands.Choice(name="SoundCloud", value="soundcloud"),
        ]
    )
    @app_commands.guild_only()
    async def play_cmd(
        interaction: discord.Interaction,
        query: str,
        source: app_commands.Choice[str] | None = None,
    ):
        channel = await require_user_voice(interaction)
        if channel is None:
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
            log.exception("Could not join voice in guild %s", interaction.guild_id)
            await interaction.followup.send(
                "I couldn't join your voice channel. Check my Connect and Speak permissions.",
                ephemeral=True,
            )
            return

        player = get_player(interaction.guild.id)
        player.channel_id = interaction.channel_id

        if len(player.queue) >= MAX_QUEUE:
            await interaction.followup.send(f"The queue is full ({MAX_QUEUE} songs).", ephemeral=True)
            return

        preferred_source = source.value if source else "youtube"
        try:
            songs = await fetch_song(query, interaction.user, preferred_source)
        except Exception:
            log.exception("Could not load song(s) for %r", query)
            await interaction.followup.send(
                "I couldn't load that track or link. Try another link or song name.",
                ephemeral=True,
            )
            return

        was_idle = player.current is None and not vc.is_playing() and not vc.is_paused()
        if was_idle:
            player.suppress_next_panel = True

        added_count = 0
        for s in songs:
            if len(player.queue) < MAX_QUEUE:
                player.queue.append(s)
                added_count += 1

        if player.idle_task is not None:
            player.idle_task.cancel()
            player.idle_task = None

        await play_next(interaction.guild)

        first_song = songs[0]
        meta = get_platform_meta(first_song.platform)
        logo = get_logo(first_song.platform)

        if len(songs) > 1:
            embed = discord.Embed(
                description=f"{logo} Loaded **{added_count}** tracks from **{meta['name']}**.",
                color=DEFAULT_COLOR,
            )
            embed.set_thumbnail(url=safe_url(first_song.thumbnail) or safe_url(AVATAR_URL))
            await interaction.followup.send(embed=embed)
            if was_idle and player.current:
                msg = await interaction.channel.send(
                    embed=build_now_playing_embed(player.current, player),
                    view=PlayerControlView(interaction.guild.id),
                )
                player.panel_message = msg
                player.panel_message_id = msg.id
                start_progress_updater(interaction.guild, player)
            return

        if player.current is first_song and vc.is_playing() and was_idle:
            msg = await interaction.followup.send(
                embed=build_now_playing_embed(first_song, player),
                view=PlayerControlView(interaction.guild.id),
                wait=True,
            )
            if isinstance(msg, discord.Message):
                player.panel_message = msg
                player.panel_message_id = msg.id
            elif hasattr(msg, "id"):
                player.panel_message_id = msg.id
            start_progress_updater(interaction.guild, player)
            return

        try:
            position = list(player.queue).index(first_song) + 1
        except ValueError:
            position = 1

        added_embed = discord.Embed(
            description=(
                f"{logo} Queued (**#{position}**): "
                f"**[{clean_title(first_song.title, 36)}]({safe_url(first_song.webpage_url) or 'https://discord.com'})** "
                f"`{first_song.length}`"
            ),
            color=DEFAULT_COLOR,
        )
        added_embed.set_author(name=f"{meta['name']} Queue", icon_url=safe_url(meta["icon"]))
        if first_song.thumbnail:
            added_embed.set_thumbnail(url=safe_url(first_song.thumbnail))
        await interaction.followup.send(embed=added_embed)

    @bot.tree.command(name="filter", description="Apply a real-time studio DSP audio filter")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Studio Flat (Normal)", value="flat"),
            app_commands.Choice(name="BassBoost (Sub Punch)", value="bassboost"),
            app_commands.Choice(name="Nightcore (Speed & Pitch)", value="nightcore"),
            app_commands.Choice(name="Slowed + Reverb", value="slowed"),
            app_commands.Choice(name="8D Surround Audio", value="8d"),
            app_commands.Choice(name="Vocal & Treble Clarity", value="vocal"),
        ]
    )
    @app_commands.guild_only()
    async def filter_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
        if not await require_playback_control(interaction):
            return
        await interaction.response.defer()
        ok = await seek_or_filter_stream(interaction.guild, new_filter=mode.value)
        if ok:
            label = AUDIO_FILTERS[mode.value]["label"]
            await interaction.followup.send(f"🎚️ Applied DSP filter: **{label}**.")
        else:
            await interaction.followup.send("Nothing is playing right now.", ephemeral=True)

    @bot.tree.command(name="seek", description="Jump to a specific timestamp in seconds")
    @app_commands.describe(seconds="Timestamp in seconds (e.g. 60 for 1:00)")
    @app_commands.guild_only()
    async def seek_cmd(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 36000]):
        if not await require_playback_control(interaction):
            return
        await interaction.response.defer()
        ok = await seek_or_filter_stream(interaction.guild, target_seconds=seconds)
        if ok:
            await interaction.followup.send(f"⏩ Jumped to **{seconds}s**.")
        else:
            await interaction.followup.send("Nothing is playing right now.", ephemeral=True)

    @bot.tree.command(name="autoplay", description="Toggle endless smart radio when the queue ends")
    @app_commands.guild_only()
    async def autoplay_cmd(interaction: discord.Interaction):
        player = get_player(interaction.guild.id)
        player.autoplay = not player.autoplay
        state = "ON" if player.autoplay else "OFF"
        await interaction.response.send_message(f"♾️ Smart Autoplay Radio is now **{state}**.")

    @bot.tree.command(name="247", description="Toggle 24/7 stay-in-voice mode")
    @app_commands.guild_only()
    async def stay247_cmd(interaction: discord.Interaction):
        player = get_player(interaction.guild.id)
        player.stay_247 = not player.stay_247
        state = "ON" if player.stay_247 else "OFF"
        await interaction.response.send_message(f"🌙 24/7 Mode is now **{state}**.")

    @bot.tree.command(name="lyrics", description="Fetch lyrics for the current song or a search query")
    @app_commands.describe(query="Optional song name to search lyrics for")
    @app_commands.guild_only()
    async def lyrics_cmd(interaction: discord.Interaction, query: str | None = None):
        player = players.get(interaction.guild.id)
        if not query:
            if player is None or player.current is None:
                await interaction.response.send_message(
                    "Nothing is playing right now. Provide a song title!", ephemeral=True
                )
                return
            title, artist = player.current.title, player.current.artist
        else:
            title, artist = query, ""

        await interaction.response.defer()
        embed = await fetch_lyrics_embed(title, artist)
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="pause", description="Pause playback")
    @app_commands.guild_only()
    async def pause_cmd(interaction: discord.Interaction):
        if not await require_playback_control(interaction):
            return
        vc = interaction.guild.voice_client
        if vc.is_playing():
            vc.pause()
            player = get_player(interaction.guild.id)
            player.mark_paused()
            await interaction.response.send_message("⏸️ Paused.")
        else:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

    @bot.tree.command(name="resume", description="Resume playback")
    @app_commands.guild_only()
    async def resume_cmd(interaction: discord.Interaction):
        if not await require_playback_control(interaction):
            return
        vc = interaction.guild.voice_client
        if vc.is_paused():
            vc.resume()
            player = get_player(interaction.guild.id)
            player.mark_resumed()
            await interaction.response.send_message("▶️ Resumed.")
        else:
            await interaction.response.send_message("Playback is not paused.", ephemeral=True)

    @bot.tree.command(name="skip", description="Skip the current song")
    @app_commands.guild_only()
    async def skip_cmd(interaction: discord.Interaction):
        if not await require_playback_control(interaction):
            return
        vc = interaction.guild.voice_client
        if not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        player = get_player(interaction.guild.id)
        player.skip_loop_once = True
        vc.stop()
        await interaction.response.send_message("⏭️ Skipped.")

    @bot.tree.command(name="queue", description="Show the active song queue")
    @app_commands.guild_only()
    async def queue_cmd(interaction: discord.Interaction):
        player = players.get(interaction.guild.id)
        if player is None or (player.current is None and not player.queue):
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return
        await interaction.response.send_message(embed=build_queue_embed(player))

    @bot.tree.command(name="nowplaying", description="Show the current song panel")
    @app_commands.guild_only()
    async def nowplaying_cmd(interaction: discord.Interaction):
        player = players.get(interaction.guild.id)
        if player is None or player.current is None:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=build_now_playing_embed(player.current, player),
            view=PlayerControlView(interaction.guild.id),
        )
        try:
            msg = await interaction.original_response()
            player.panel_message = msg
            player.panel_message_id = msg.id
            start_progress_updater(interaction.guild, player)
        except discord.HTTPException:
            pass

    @bot.tree.command(name="loop", description="Cycle loop mode (Off / Track / Queue)")
    @app_commands.guild_only()
    async def loop_cmd(interaction: discord.Interaction):
        player = get_player(interaction.guild.id)
        mode = player.cycle_loop()
        await interaction.response.send_message(f"🔁 Loop mode set to **{mode.upper()}**.")

    @bot.tree.command(name="shuffle", description="Shuffle the queue")
    @app_commands.guild_only()
    async def shuffle_cmd(interaction: discord.Interaction):
        player = players.get(interaction.guild.id)
        if player is None or len(player.queue) < 2:
            await interaction.response.send_message(
                "I need at least two queued songs to shuffle.", ephemeral=True
            )
            return
        items = list(player.queue)
        random.shuffle(items)
        player.queue = deque(items)
        await interaction.response.send_message("🔀 Queue shuffled.")

    @bot.tree.command(name="volume", description="Set the volume (0-150)")
    @app_commands.describe(level="Volume from 0 to 150")
    @app_commands.guild_only()
    async def volume_cmd(interaction: discord.Interaction, level: app_commands.Range[int, 0, 150]):
        player = get_player(interaction.guild.id)
        player.volume = level / 100
        vc = interaction.guild.voice_client
        source = getattr(vc, "source", None) if vc is not None else None
        if source is not None and hasattr(source, "volume"):
            source.volume = player.volume
        await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")

    @bot.tree.command(name="stop", description="Stop playback and clear the queue")
    @app_commands.guild_only()
    async def stop_cmd(interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc is None or not vc.is_connected():
            await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
            return
        player = get_player(interaction.guild.id)
        player.queue.clear()
        player.current = None
        stop_progress_updater(player)
        vc.stop()

        if player.stay_247:
            await interaction.response.send_message(
                f"⏹️ Stopped playback and cleared the queue (staying in <#{vc.channel.id}> 24/7)."
            )
        else:
            drop_player(interaction.guild.id)
            await interaction.response.send_message("⏹️ Stopped and left the voice channel.")
            try:
                await vc.disconnect()
            except Exception:
                log.exception("Could not disconnect from voice in guild %s", interaction.guild_id)

    async def prefix_filter(interaction, arg: str = "flat"):
        key = arg.lower().strip()
        if key not in AUDIO_FILTERS:
            valid = ", ".join(f"`{k}`" for k in AUDIO_FILTERS)
            await interaction.response.send_message(f"Choose a filter: {valid}")
            return
        if not await require_playback_control(interaction):
            return
        ok = await seek_or_filter_stream(interaction.guild, new_filter=key)
        if ok:
            await interaction.response.send_message(f"🎚️ Applied DSP filter: **{AUDIO_FILTERS[key]['label']}**.")

    return {
        "menu": menu_cmd.callback,
        "join": join_cmd.callback,
        "leave": leave_cmd.callback,
        "play": play_cmd.callback,
        "filter": prefix_filter,
        "seek": seek_cmd.callback,
        "autoplay": autoplay_cmd.callback,
        "247": stay247_cmd.callback,
        "lyrics": lyrics_cmd.callback,
        "pause": pause_cmd.callback,
        "resume": resume_cmd.callback,
        "skip": skip_cmd.callback,
        "queue": queue_cmd.callback,
        "nowplaying": nowplaying_cmd.callback,
        "loop": loop_cmd.callback,
        "shuffle": shuffle_cmd.callback,
        "volume": volume_cmd.callback,
        "stop": stop_cmd.callback,
    }


class _PrefixResponse:
    def __init__(self, message: discord.Message):
        self._message = message
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self._done = True

    async def send_message(
        self,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = False,
        **kwargs,
    ):
        self._done = True
        return await _reply(self._message, content, embed=embed, view=view, **kwargs)


class _PrefixFollowup:
    def __init__(self, message: discord.Message):
        self._message = message

    async def send(
        self,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = False,
        wait: bool = False,
        **kwargs,
    ):
        return await _reply(self._message, content, embed=embed, view=view, **kwargs)


async def _reply(
    message: discord.Message,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    **kwargs,
):
    send_kwargs = dict(kwargs)
    if embed is not None:
        send_kwargs["embed"] = embed
    if view is not None:
        send_kwargs["view"] = view
    try:
        return await message.reply(content, mention_author=False, **send_kwargs)
    except discord.HTTPException:
        pass
    try:
        return await message.channel.send(content, **send_kwargs)
    except discord.HTTPException:
        log.warning("Could not send a prefix reply in channel %s", getattr(message.channel, "id", "?"))
        return None


class PrefixInteraction:
    def __init__(self, message: discord.Message):
        self.message = message
        self.user = message.author
        self.guild = message.guild
        self.guild_id = message.guild.id
        self.channel = message.channel
        self.channel_id = message.channel.id
        self.command = None
        self.response = _PrefixResponse(message)
        self.followup = _PrefixFollowup(message)


PREFIX_ALIASES = {
    "p": "play",
    "j": "join",
    "connect": "join",
    "sit": "join",
    "dc": "leave",
    "disconnect": "leave",
    "m": "menu",
    "help": "menu",
    "hel": "menu",
    "h": "menu",
    "commands": "menu",
    "prefix": "menu",
    "fx": "filter",
    "bass": "filter",
    "ap": "autoplay",
    "ly": "lyrics",
    "next": "skip",
    "q": "queue",
    "np": "nowplaying",
    "now": "nowplaying",
    "vol": "volume",
    " '": "stop",
}