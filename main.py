import asyncio
import logging
import shutil

import discord
from discord import app_commands

from config import GUILD_ID, PREFIX, TOKEN, log
from src.commands import PREFIX_ALIASES, PrefixInteraction, _reply, setup_commands
from src.player import background_tasks, drop_player, idle_leave, load_opus, players, set_bot


class ZeroBot(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        try:
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("Slash commands synced to guild %s", GUILD_ID)
            else:
                await self.tree.sync()
                log.info("Global slash commands synced")
        except Exception:
            log.exception("Slash command sync failed")

    async def close(self) -> None:
        for task in list(background_tasks):
            task.cancel()
        await super().close()


intents = discord.Intents.default()
intents.message_content = True
bot = ZeroBot(intents=intents)
set_bot(bot)
PREFIX_COMMANDS = setup_commands(bot)


def _parse_prefix(content: str) -> tuple[str, str] | None:
    text = content.lstrip()
    if not text.lower().startswith(PREFIX.lower()):
        return None
    body = text[len(PREFIX) :].strip()
    if not body:
        return None
    name, _, args = body.partition(" ")
    return name.lower(), args.strip()


@bot.event
async def on_ready():
    log.info("Logged in as %s (%d guilds)", bot.user, len(bot.guilds))


@bot.event
async def on_guild_remove(guild: discord.Guild):
    drop_player(guild.id)


@bot.event
async def on_voice_state_update(member, before, after):
    guild = member.guild
    if bot.user is not None and member.id == bot.user.id and after.channel is None:
        drop_player(guild.id)
        return

    vc = guild.voice_client
    player = players.get(guild.id)
    if vc is None or player is None or not vc.is_connected() or vc.channel is None:
        return

    if any(not channel_member.bot for channel_member in vc.channel.members):
        if player.idle_task is not None:
            player.idle_task.cancel()
            player.idle_task = None
        return

    if player.idle_task is None or player.idle_task.done():
        player.idle_task = asyncio.create_task(idle_leave(guild.id))


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original
    log.error(
        "Command /%s failed in guild %s",
        interaction.command.name if interaction.command else "?",
        interaction.guild_id,
        exc_info=(type(error), error, error.__traceback__),
    )
    if isinstance(error, app_commands.NoPrivateMessage):
        message = "This command only works in a server."
    else:
        message = "Something went wrong. Check the bot logs."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    content = message.content.strip()
    if content.lower() in {"help", "prefix"}:
        await _reply(
            message,
            f"My prefix is `{PREFIX}`. Try `{PREFIX}menu`, `{PREFIX}play <link or song>`, or `/menu`.",
        )
        return

    parsed = _parse_prefix(content)
    if parsed is None:
        return
    name, args = parsed
    command_name = PREFIX_ALIASES.get(name, name)

    handler = PREFIX_COMMANDS.get(command_name)
    if handler is None:
        return

    context = PrefixInteraction(message)
    try:
        if command_name == "play":
            if not args:
                await _reply(message, f"Usage: `{PREFIX}play <song name or link>`")
                return
            await handler(context, args, None)
        elif command_name == "filter":
            await handler(context, args or "bassboost")
        elif command_name == "lyrics":
            await handler(context, args or None)
        elif command_name in {"volume", "seek"}:
            try:
                val = int(args.split()[0])
            except (IndexError, ValueError):
                await _reply(message, f"Usage: `{PREFIX}{command_name} <number>`")
                return
            await handler(context, val)
        else:
            await handler(context)
    except Exception:
        log.exception("Prefix command %s failed in guild %s", name, message.guild.id)
        await _reply(message, "Something went wrong. Check the bot logs.")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing in .env file.")
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed or not on PATH.")
    load_opus()
    try:
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        raise SystemExit("Discord rejected the token.")


if __name__ == "__main__":
    main()