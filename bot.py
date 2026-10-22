import discord
from discord.ext import commands
from core.config import DISCORD_TOKEN

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(intents=intents)

COGS = ["cogs.registry", "cogs.permissions", "cogs.siege", "cogs.general"]


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    synced = await bot.sync_commands(force=True)
    print("Slash commands synced")


def load_cogs():
    for cog in COGS:
        bot.load_extension(cog)


if __name__ == "__main__":
    load_cogs()
    bot.run(DISCORD_TOKEN)