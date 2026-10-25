import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(intents=intents)
bot.exiting = False  # /admin shutdown sets this before closing the bot

COGS = ["cogs.registry", "cogs.permissions", "cogs.siege", "cogs.admin", "cogs.general"]


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    synced = await bot.sync_commands()
    print("Slash commands synced")


def load_cogs():
    for cog in COGS:
        bot.load_extension(cog)


if __name__ == "__main__":
    load_cogs()
    try:
        bot.run(DISCORD_TOKEN)
    except Exception:
        # py-cord can raise "Session is closed" while it shuts down. That is only expected
        # when we closed the bot on purpose, so any other error is still shown.
        if not bot.exiting:
            raise