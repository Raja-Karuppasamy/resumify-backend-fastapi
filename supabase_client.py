import os
from supabase import create_client, Client

SUPABASE_URL = os.getenv("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

def get_supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

def validate_api_key(api_key: str):
    """
    Validate API key and return user profile
    Returns: dict with user profile or None if invalid
    """
    if not api_key or api_key.startswith("anon_"):
        return None
    
    client = get_supabase_client()
    if not client:
        return None
    
    try:
        response = client.table("users").select("*").eq("api_key", api_key).execute()
        if response.data and len(response.data) > 0:
            return response.data[0]
        return None
    except Exception as e:
        print(f"Error validating API key: {e}")
        return None
