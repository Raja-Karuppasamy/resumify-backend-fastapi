import time
from fastapi import HTTPException
from supabase_client import validate_api_key

# Tier-based limits - FREE IS NOW DAILY
TIER_LIMITS = {
    "free": {"daily": 5},
    "pro": {"hourly": 20, "monthly": 100},
    "enterprise": {"hourly": 50, "monthly": 500},
}

# In-memory usage tracking
_usage_store = {}

def get_user_tier(api_key: str) -> tuple[str, dict]:
    """
    Returns (tier_name, user_profile)
    """
    if api_key.startswith("anon_"):
        return "free", None
    
    profile = validate_api_key(api_key)
    if not profile:
        return "free", None  # Invalid key = free tier
    
    tier = profile.get("subscription_tier", "free")
    return tier, profile

def check_rate_limit(api_key: str):
    """Check and enforce rate limits based on tier"""
    tier, profile = get_user_tier(api_key)
    limits = TIER_LIMITS.get(tier, TIER_LIMITS["free"])
    
    # Ensure record exists
    if api_key not in _usage_store:
        _usage_store[api_key] = {
            "hourly_timestamps": [],
            "daily_timestamps": [],
            "monthly_count": 0,
            "month_start": time.time()
        }
    
    rec = _usage_store[api_key]
    now = time.time()
    
    # Reset monthly if needed (30 days)
    if now - rec["month_start"] > 30 * 24 * 3600:
        rec["monthly_count"] = 0
        rec["month_start"] = now
    
    # Clean old timestamps
    hour_ago = now - 3600
    day_ago = now - 86400  # 24 hours
    rec["hourly_timestamps"] = [ts for ts in rec["hourly_timestamps"] if ts >= hour_ago]
    rec["daily_timestamps"] = [ts for ts in rec["daily_timestamps"] if ts >= day_ago]
    
    hourly_count = len(rec["hourly_timestamps"])
    daily_count = len(rec["daily_timestamps"])
    monthly_count = rec["monthly_count"]
    
    # Check limits based on tier
    if tier == "free":
        if daily_count >= limits["daily"]:
            raise HTTPException(
                status_code=429,
                detail=f"Daily limit reached: {limits['daily']} requests per day for free tier. Upgrade to continue."
            )
    else:
        # Pro/Enterprise - check hourly
        if hourly_count >= limits.get("hourly", 999):
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded: {limits['hourly']} requests per hour for {tier} tier"
            )
        
        # Check monthly
        if monthly_count >= limits.get("monthly", 999999):
            raise HTTPException(
                status_code=429,
                detail=f"Monthly limit reached: {limits['monthly']} requests for {tier} tier. Upgrade to continue."
            )
    
    # Record usage
    rec["hourly_timestamps"].append(now)
    rec["daily_timestamps"].append(now)
    rec["monthly_count"] += 1