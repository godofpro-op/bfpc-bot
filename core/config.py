import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not all([DISCORD_TOKEN, SUPABASE_URL, SUPABASE_KEY]):
    raise ValueError("Missing one or more required environment variables in .env")