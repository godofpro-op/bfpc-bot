import discord
from discord.ext import commands


COMMAND_INFO = [
    ("/account add", "Add an account from a character screenshot, attach the image and pick the Discord user it belongs to"),
    ("/account remove", "Remove an account, exact name or ID match required"),
    ("/account show", "List every registered account in the corp, paginated"),
    ("/account find", "List all accounts belonging to a Discord user, by mention or Discord ID"),
    ("/account transfer", "Transfer an account to a different Discord user, by in-game ID"),
    ("/siege list", "Show which siege fleet systems are pending and which are cleared this week"),
    ("/siege clear", "Mark a system as cleared (siege channel only unless admin)"),
    ("/siege unclear", "Put a cleared system back to pending (siege channel only unless admin)"),
    ("/siege add", "Add a system to the siege board"),
    ("/siege remove", "Remove a system from the siege board"),
    ("/siege reset", "Set every system back to pending"),
    ("/whois", "Search for an account by in-game name or ID, returns all close matches"),
    ("/blacklist", "Add a character to the blacklist from a screenshot"),
    ("/permission add", "Grant a role access to a command"),
    ("/permission remove", "Remove a role's access to a command"),
    ("/permission show", "View which roles can use which commands"),
    ("/ping", "Check if the bot is online"),
    ("/help", "Show this list"),
]


class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @discord.slash_command(name="ping", description="Check if the bot is online")
    async def ping(self, ctx):
        latency_ms = round(self.bot.latency * 1000)
        await ctx.respond(f"Pong. {latency_ms}ms")

    @discord.slash_command(name="help", description="List all available commands")
    async def help(self, ctx):
        embed = discord.Embed(title="BFPC Bot Commands", color=discord.Color.from_rgb(0, 0, 0))
        for name, desc in COMMAND_INFO:
            embed.add_field(name=name, value=desc, inline=False)
        await ctx.respond(embed=embed)


def setup(bot):
    bot.add_cog(General(bot))