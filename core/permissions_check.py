import discord
from core.database import get_roles_for_command


async def has_permission(ctx, command_name):
    # server admins always have access, no matter what
    if ctx.author.guild_permissions.administrator:
        return True

    allowed = get_roles_for_command(command_name)
    if not allowed:
        # no roles configured yet, default to admin-only, so deny non-admins
        return False

    allowed_role_ids = {row["role_id"] for row in allowed}
    user_role_ids = {str(role.id) for role in ctx.author.roles}

    return bool(allowed_role_ids & user_role_ids)


async def check_and_respond(ctx, command_name):
    if not await has_permission(ctx, command_name):
        await ctx.respond(
            "You don't have permission to use this command.", ephemeral=True
        )
        return False
    return True