from supabase import create_client
from core.config import SUPABASE_URL, SUPABASE_KEY
from rapidfuzz import fuzz

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------- Accounts ----------

def add_account(discord_id, discord_username, discord_name, ingame_name, ingame_id):
    data = {
        "discord_id": str(discord_id),
        "discord_username": discord_username,
        "discord_name": discord_name,
        "ingame_name": ingame_name,
        "ingame_id": ingame_id,
    }
    return supabase.table("accounts").insert(data).execute()


def get_account_by_ingame_id(ingame_id):
    result = (
        supabase.table("accounts")
        .select("*")
        .eq("ingame_id", ingame_id)
        .execute()
    )
    return result.data[0] if result.data else None


def get_all_accounts():
    result = supabase.table("accounts").select("*").order("added_at", desc=True).execute()
    return result.data


def get_account_count():
    result = supabase.table("accounts").select("id", count="exact").execute()
    return result.count


def get_accounts_page(page, per_page=5):
    offset = (page - 1) * per_page
    result = (
        supabase.table("accounts")
        .select("*")
        .order("added_at", desc=True)
        .range(offset, offset + per_page - 1)
        .execute()
    )
    return result.data


def search_accounts(query):
    all_accounts = get_all_accounts()
    query_lower = query.lower()
    matches = []

    for acc in all_accounts:
        name_score = fuzz.partial_ratio(query_lower, acc["ingame_name"].lower())
        id_match = query_lower in acc["ingame_id"].lower()

        if id_match or name_score >= 60:
            matches.append((acc, max(name_score, 100 if id_match else 0)))

    matches.sort(key=lambda x: x[1], reverse=True)
    return [m[0] for m in matches]


def find_exact_account(query):
    all_accounts = get_all_accounts()
    query_lower = query.lower()

    for acc in all_accounts:
        if acc["ingame_name"].lower() == query_lower or acc["ingame_id"] == query:
            return acc
    return None


def get_accounts_by_discord_id(discord_id):
    result = (
        supabase.table("accounts")
        .select("*")
        .eq("discord_id", str(discord_id))
        .order("added_at", desc=True)
        .execute()
    )
    return result.data


def transfer_account(ingame_id, discord_id, discord_username, discord_name):
    data = {
        "discord_id": str(discord_id),
        "discord_username": discord_username,
        "discord_name": discord_name,
    }
    return supabase.table("accounts").update(data).eq("ingame_id", ingame_id).execute()


def delete_account(ingame_id):
    return supabase.table("accounts").delete().eq("ingame_id", ingame_id).execute()


# ---------- Blacklist ----------

def add_blacklist(ingame_name, ingame_id):
    data = {
        "ingame_name": ingame_name,
        "ingame_id": ingame_id,
    }
    return supabase.table("blacklist").insert(data).execute()


def get_blacklist_entry(ingame_id):
    result = (
        supabase.table("blacklist")
        .select("*")
        .eq("ingame_id", ingame_id)
        .execute()
    )
    return result.data[0] if result.data else None


# ---------- Siege board ----------

def get_siege_systems():
    result = supabase.table("siege_systems").select("*").order("name").execute()
    return result.data


def add_siege_system(name):
    return supabase.table("siege_systems").insert({"name": name}).execute()


def remove_siege_system(system_id):
    return supabase.table("siege_systems").delete().eq("id", system_id).execute()


def set_siege_cleared(system_id, cleared_at):
    # cleared_at is an ISO timestamp string, or None to mark the system pending
    return (
        supabase.table("siege_systems")
        .update({"cleared_at": cleared_at})
        .eq("id", system_id)
        .execute()
    )


def reset_siege_systems():
    return supabase.table("siege_systems").update({"cleared_at": None}).gt("id", 0).execute()


def get_siege_state():
    result = supabase.table("siege_state").select("*").eq("id", 1).execute()
    return result.data[0] if result.data else None


def set_siege_state(last_pending_post_cycle):
    return (
        supabase.table("siege_state")
        .upsert({"id": 1, "last_pending_post_cycle": last_pending_post_cycle})
        .execute()
    )


# ---------- Permissions ----------

def set_permission(command_name, role_id, role_name):
    existing = (
        supabase.table("permissions")
        .select("*")
        .eq("command_name", command_name)
        .eq("role_id", str(role_id))
        .execute()
    )
    if existing.data:
        return existing.data[0]

    data = {
        "command_name": command_name,
        "role_id": str(role_id),
        "role_name": role_name,
    }
    return supabase.table("permissions").insert(data).execute()


def remove_permission(command_name, role_id):
    return (
        supabase.table("permissions")
        .delete()
        .eq("command_name", command_name)
        .eq("role_id", str(role_id))
        .execute()
    )


def get_all_permissions():
    result = supabase.table("permissions").select("*").order("command_name").execute()
    return result.data


def get_roles_for_command(command_name):
    result = (
        supabase.table("permissions")
        .select("*")
        .eq("command_name", command_name)
        .execute()
    )
    return result.data