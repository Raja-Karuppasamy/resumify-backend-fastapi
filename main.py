# main.py
import os
import re
import time
import logging
import tempfile
import asyncio  # Added for non-blocking execution
from typing import Dict, Any, List, Optional, Tuple

from fastapi import (
    FastAPI,
    UploadFile,
    File,
    HTTPException,
    Depends,
    Request,
    status,
    Response,
    Query,
)
from fastapi.middleware.cors import CORSMiddleware
from pdfminer.high_level import extract_text

# --------------------------------------------------------------------
# Config
# --------------------------------------------------------------------

API_KEY = os.getenv("API_KEY")  # set this in Railway (private)

ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "https://resumify-working.vercel.app",
    "https://resumify-working-git-main-raja-karuppasamys-projects.vercel.app",
    "https://resumify.co",
    "https://www.resumify.co",
    "https://resumifyapi.com",
    "https://www.resumifyapi.com",
    "https://api.resumifyapi.com",
]

RATE_LIMIT_WINDOW_SECONDS = 60  # 1 minute window
RATE_LIMIT_MAX_REQUESTS = 60    # per IP per minute

# simple in-memory store (good enough for single instance)
_rate_limit_store: Dict[str, List[float]] = {}

# logger
logger = logging.getLogger("resumify-backend")
logging.basicConfig(level=logging.INFO)

# --------------------------------------------------------------------
# App + CORS
# --------------------------------------------------------------------

app = FastAPI(title="Resumify Backend API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://resumifyapi.com",
        "https://www.resumifyapi.com",
        "https://api.resumifyapi.com",
        "http://localhost:3000",
    ],
    allow_credentials=False,   # 🔴 IMPORTANT
    allow_methods=["POST", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "X-API-Key",
        "Authorization",
    ],
)

# --------------------------------------------------------------------
# Middleware: request logging
# --------------------------------------------------------------------

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    path = request.url.path

    response = None
    try:
        response = await call_next(request)
        return response
    finally:
        duration = (time.time() - start) * 1000
        status_code = response.status_code if response else 500
        logger.info(
            f"{request.client.host} {request.method} {path} -> {status_code} "
            f"({duration:.1f} ms)"
        )

# --------------------------------------------------------------------
# Dependencies: API key + rate limit
# --------------------------------------------------------------------

def _client_ip_from_request(request: Request) -> str:
    """
    Prefer X-Forwarded-For (first value) if present, otherwise request.client.host.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # take the first IP in the list
        return xff.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"

def verify_api_key(request: Request):
    """
    If API_KEY is set in env, require it via x-api-key header.
    If API_KEY is not set (dev), this becomes a no-op.
    """
    if not API_KEY:
        # dev mode: no API key required
        return

    header_key = request.headers.get("x-api-key")

    if not header_key:
        logger.warning("Missing API key from %s", _client_ip_from_request(request))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
        )

    if header_key != API_KEY:
        logger.warning("Invalid API key from %s", _client_ip_from_request(request))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


def check_rate_limit(request: Request):
    """
    Very simple per-IP rate limiter (in-memory).
    Good enough for MVP on a single Railway instance.
    """
    if RATE_LIMIT_MAX_REQUESTS <= 0:
        return

    ip = _client_ip_from_request(request)
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS

    timestamps = _rate_limit_store.get(ip, [])
    # keep only recent timestamps
    timestamps = [t for t in timestamps if t >= window_start]

    if len(timestamps) >= RATE_LIMIT_MAX_REQUESTS:
        logger.warning("Rate limit exceeded for IP %s", ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please slow down.",
        )

    timestamps.append(now)
    _rate_limit_store[ip] = timestamps


async def secure_request(request: Request):
    """
    Combined dependency: rate-limit + API key.
    Allow preflight OPTIONS to pass early.
    """
    if request.method == "OPTIONS":
        return

    check_rate_limit(request)
    verify_api_key(request)

# --------------------------------------------------------------------
# Simple usage tracking (in-memory) - suitable for single-instance MVP
# --------------------------------------------------------------------

# Plan config (for MVP)
PLANS = {
    "free": {"limit_per_minute": 60, "limit_per_month": 1000, "display_name": "Free (Beta)"},
    "pro": {"limit_per_minute": 600, "limit_per_month": 100000, "display_name": "Pro"},
}

_usage_store: Dict[str, Dict[str, Any]] = {}

def _ensure_key_record(api_key: str) -> None:
    if api_key not in _usage_store:
        _usage_store[api_key] = {
            "minute_timestamps": [],
            "month_count": 0,
            "plan": "free",
            "last_reset_month": time.localtime().tm_mon,
        }

def _maybe_reset_month(api_key: str):
    rec = _usage_store[api_key]
    current_month = time.localtime().tm_mon
    if rec.get("last_reset_month") != current_month:
        rec["month_count"] = 0
        rec["last_reset_month"] = current_month

def increment_usage(api_key: str, amount: int = 1) -> Tuple[int, int]:
    _ensure_key_record(api_key)
    _maybe_reset_month(api_key)

    now = time.time()
    rec = _usage_store[api_key]

    window_start = now - 60
    rec["minute_timestamps"] = [ts for ts in rec["minute_timestamps"] if ts >= window_start]

    for _ in range(amount):
        rec["minute_timestamps"].append(now)

    rec["month_count"] += amount

    minute_count = len(rec["minute_timestamps"])
    month_count = rec["month_count"]
    return minute_count, month_count

def get_usage_for_key(api_key: str) -> Dict[str, Any]:
    _ensure_key_record(api_key)
    _maybe_reset_month(api_key)

    rec = _usage_store[api_key]
    minute_count = len([ts for ts in rec["minute_timestamps"] if ts >= time.time() - 60])
    month_count = rec["month_count"]
    plan = rec.get("plan", "free")
    plan_info = PLANS.get(plan, PLANS["free"])
    remaining_minute = max(plan_info["limit_per_minute"] - minute_count, 0)
    remaining_month = max(plan_info["limit_per_month"] - month_count, 0)
    return {
        "api_key": api_key,
        "plan": plan,
        "plan_display_name": plan_info["display_name"],
        "limit_per_minute": plan_info["limit_per_minute"],
        "limit_per_month": plan_info["limit_per_month"],
        "used_minute": minute_count,
        "used_month": month_count,
        "remaining_minute": remaining_minute,
        "remaining_month": remaining_month,
    }

def set_plan_for_key(api_key: str, plan: str):
    _ensure_key_record(api_key)
    if plan not in PLANS:
        raise ValueError("plan unknown")
    _usage_store[api_key]["plan"] = plan

# Admin / public usage routes

@app.get("/usage")
def usage_admin(request: Request, _ = Depends(verify_api_key)):
    return {"usage": _usage_store, "plans": PLANS}

@app.get("/usage/public")
def public_usage(request: Request):
    header_key = request.headers.get("x-api-key") or "anonymous"
    return get_usage_for_key(header_key)

@app.post("/admin/usage/reset")
def admin_reset_usage(request: Request, key: str = Query(...), _ = Depends(verify_api_key)):
    if key in _usage_store:
        _usage_store[key]["minute_timestamps"] = []
        _usage_store[key]["month_count"] = 0
        return {"ok": True}
    return {"ok": False, "msg": "no such key"}

# --------------------------------------------------------------------
# Health + root
# --------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "backend ok"}

@app.get("/health")
def health():
    return {
        "status": "ok",
        "uptime_ms": int(time.time() * 1000),
        "version": "v1",
    }

@app.api_route("/parse", methods=["OPTIONS"])
def parse_options():
    return Response(status_code=200)

# --------------------------------------------------------------------
# Parser helpers (experience/education/skills extraction)
# --------------------------------------------------------------------

YEAR_PATTERN = re.compile(r"(?:19|20)\d{2}")
TWO_DIGIT_YEAR_PATTERN = re.compile(r"\b(\d{2})\b", re.IGNORECASE)

SKILL_CATALOG = {
    "programming_languages": {
        "java": "Java",
        "javascript": "JavaScript",
        "js": "JavaScript",
        "typescript": "TypeScript",
        "python": "Python",
        "c++": "C++",
        "c#": "C#",
        "go": "Go",
        "ruby": "Ruby",
    },
    "frameworks_and_libraries": {
        "react": "React.js",
        "reactjs": "React.js",
        "react.js": "React.js",
        "next": "Next.js",
        "next.js": "Next.js",
        "angular": "Angular",
        "vue": "Vue.js",
        "django": "Django",
        "flask": "Flask",
        "spring": "Spring",
    },
    "cloud_and_infra": {
        "aws": "AWS",
        "azure": "Azure",
        "gcp": "GCP",
        "docker": "Docker",
        "kubernetes": "Kubernetes",
        "k8s": "Kubernetes",
        "terraform": "Terraform",
    },
    "databases": {
        "mysql": "MySQL",
        "postgres": "PostgreSQL",
        "postgresql": "PostgreSQL",
        "mongodb": "MongoDB",
        "redis": "Redis",
    },
    "dev_tools": {
        "git": "Git",
        "jira": "Jira",
        "jenkins": "Jenkins",
        "github": "GitHub",
        "gitlab": "GitLab",
    },
}

def extract_text_from_pdf_bytes(data: bytes) -> str:
    if not data:
        raise ValueError("Empty file")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        text = extract_text(tmp_path) or ""
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    if not text.strip():
        raise ValueError("No readable text extracted")

    return text

# Experience helpers

def _normalize_year_pair(start: Optional[str], end: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not start and not end:
        return None, None

    if end:
        end_lower = end.lower()
        if "present" in end_lower or "current" in end_lower or "now" in end_lower:
            end = "Present"

    if start and end and len(end) == 2 and len(start) == 4 and start.startswith(("19", "20")):
        end = start[:2] + end

    return start, end

def _parse_date_range(text_block: str) -> Tuple[Optional[str], Optional[str]]:
    years = YEAR_PATTERN.findall(text_block)
    start: Optional[str] = None
    end: Optional[str] = None

    if years:
        start = years[0]
        if len(years) > 1:
            end = years[1]
        else:
            two_digit = TWO_DIGIT_YEAR_PATTERN.findall(text_block)
            if two_digit:
                end = two_digit[-1]
            elif re.search(r"present|current|now", text_block, re.IGNORECASE):
                end = "Present"
        return _normalize_year_pair(start, end)

    two_digit = TWO_DIGIT_YEAR_PATTERN.findall(text_block)
    if two_digit:
        start = None
        end = two_digit[-1]
        return _normalize_year_pair(start, end)

    if re.search(r"present|current|now", text_block, re.IGNORECASE):
        return None, "Present"

    return None, None

def _extract_responsibilities(lines: List[str]) -> List[str]:
    bullets: List[str] = []
    for line in lines:
        clean = line.strip("•- \t")
        if len(clean) < 25:
            continue
        if re.search(r"experience|responsibilit|summary|education|skills", clean, re.IGNORECASE):
            continue
        bullets.append(clean)
    return bullets[:8]

def parse_experience_section(full_text: str) -> List[Dict[str, Any]]:
    experience_blocks: List[Dict[str, Any]] = []
    parts = re.split(r"(?i)work experience|professional experience|experience", full_text, maxsplit=1)
    if len(parts) < 2:
        return experience_blocks

    exp_text = parts[1]
    job_chunks = re.split(r"\n{2,}", exp_text)

    for chunk in job_chunks:
        chunk = chunk.strip()
        if len(chunk) < 30:
            continue

        lines = [l.strip() for l in chunk.splitlines() if l.strip()]
        if not lines:
            continue

        job_title = lines[0]

        company: Optional[str] = None
        if len(lines) > 1:
            second = lines[1]
            if not YEAR_PATTERN.search(second) and not re.search(r"\d", second):
                company = second

        start_date, end_date = _parse_date_range(chunk)
        responsibilities = _extract_responsibilities(lines[2:])

        experience_blocks.append(
            {
                "job_title": job_title,
                "company": company,
                "start_date": start_date,
                "end_date": end_date,
                "responsibilities": responsibilities,
                "job_title_confidence": 0.85 if job_title else 0.0,
                "company_confidence": 0.8 if company else 0.0,
            }
        )

        if len(experience_blocks) >= 5:
            break

    return experience_blocks

# Education helpers

def parse_education_section(full_text: str) -> List[Dict[str, Any]]:
    education: List[Dict[str, Any]] = []
    parts = re.split(r"(?i)education", full_text, maxsplit=1)
    if len(parts) < 2:
        return education

    edu_text = parts[1]
    blocks = re.split(r"\n{2,}", edu_text)

    for block in blocks:
        block = block.strip()
        if len(block) < 20:
            continue

        lines = [l.strip() for l in block.splitlines() if l.strip()]
        lower_block = block.lower()

        degree: Optional[str] = None
        if re.search(r"bachelor|b\.s\.|b\.sc|bsc|b\.tech|btech|b\.e\.|be", lower_block):
            degree_line = None
            for l in lines:
                if re.search(r"bachelor|b\.s\.|b\.sc|bsc|b\.tech|btech|b\.e\.|be", l, re.IGNORECASE):
                    degree_line = l
                    break
            degree = degree_line or lines[0]

        if not degree:
            continue

        institution: Optional[str] = None
        for l in lines[1:3]:
            if re.search(r"[A-Za-z]", l):
                institution = l
                break

        uni_of_match = re.search(r"(University of [A-Za-z ,]+)", block)
        if uni_of_match:
            institution = uni_of_match.group(1).strip()

        year_val = ""
        year_match = YEAR_PATTERN.search(block)
        if year_match:
            year_val = year_match.group(0)

        education.append(
            {
                "degree": degree,
                "institution": institution,
                "year": year_val,
                "degree_confidence": 0.85 if degree else 0.0,
                "institution_confidence": 0.8 if institution else 0.0,
            }
        )

        if len(education) >= 3:
            break

    return education

# Skills helpers

def extract_skills(full_text: str) -> Dict[str, List[str]]:
    text_lower = full_text.lower()
    result: Dict[str, List[str]] = {
        "programming_languages": [],
        "frameworks_and_libraries": [],
        "cloud_and_infra": [],
        "databases": [],
        "dev_tools": [],
    }

    for category, items in SKILL_CATALOG.items():
        seen = set()
        for raw, pretty in items.items():
            if re.search(r"\b" + re.escape(raw) + r"\b", text_lower):
                seen.add(pretty)
        result[category] = sorted(seen)

    return result

# Main parsing

def parse_basic_fields(text: str) -> Dict[str, Any]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    full_text = "\n".join(lines)

    name = lines[0] if lines else None

    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", full_text)
    email = email_match.group(0) if email_match else None

    phone_match = re.search(
        r"(\+\d{1,3}[\s-]?)?(\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{4})", full_text
    )
    phone = phone_match.group(0) if phone_match else None

    location = None
    for line in lines[:10]:
        if "," in line and "@" not in line:
            location = line
            break

    summary = None
    for line in lines[3:15]:
        if re.search(r"summary|objective|profile", line, re.IGNORECASE):
            idx = lines.index(line)
            if idx + 1 < len(lines):
                summary = lines[idx + 1]
            break

    experience_blocks = parse_experience_section(full_text)
    education_blocks = parse_education_section(full_text)
    skills = extract_skills(full_text)

    if re.search(r"\bsenior\b", full_text, re.IGNORECASE):
        role_level = "Senior"
    elif re.search(r"\bjunior\b|\bentry\b", full_text, re.IGNORECASE):
        role_level = "Junior"
    else:
        role_level = "Mid-level"

    primary_role = None
    if re.search(r"devops|sysops|system administrator|infrastructure", full_text, re.IGNORECASE):
        primary_role = "Cloud / SysOps"
    elif re.search(r"frontend|react|ui", full_text, re.IGNORECASE):
        primary_role = "Frontend"
    elif re.search(r"backend|api|microservices", full_text, re.IGNORECASE):
        primary_role = "Backend"

    result: Dict[str, Any] = {
        "name": name,
        "email": email,
        "phone": phone,
        "location": location,
        "role_level": role_level,
        "primary_role": primary_role,
        "years_of_experience_total": None,
        "years_of_experience_in_tech": None,
        "github": "",
        "portfolio": "",
        "summary": summary,
        "experience": experience_blocks,
        "education": education_blocks,
        "raw": text,
        "name_confidence": 0.9 if name else 0.0,
        "email_confidence": 0.95 if email else 0.0,
        "phone_confidence": 0.9 if phone else 0.0,
        "location_confidence": 0.8 if location else 0.0,
        "summary_confidence": 0.8 if summary else 0.0,
    }

    result.update(skills)

    return result

# /parse endpoint

@app.post("/parse")
async def parse_resume(
    request: Request,
    response: Response,  # Moved before arguments with defaults to fix SyntaxError
    file: UploadFile = File(...),
    _secure: None = Depends(secure_request),
):
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only PDF files allowed")

    contents = await file.read()
    try:
        # Run blocking PDF extraction in a thread pool
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, extract_text_from_pdf_bytes, contents)
        
        parsed = parse_basic_fields(text)

        # increment usage for the caller key (or 'anonymous')
        header_key = request.headers.get("x-api-key") or "anonymous"
        minute_count, month_count = increment_usage(header_key)

        # attach small usage info for client debugging (can be removed later)
        parsed["_usage"] = {
            "used_minute": minute_count,
            "used_month": month_count,
            "plan": _usage_store.get(header_key, {}).get("plan", "free"),
        }

        # add rate limit headers (helpful for frontend dashboards)
        if response is not None:
            plan_name = _usage_store.get(header_key, {}).get("plan", "free")
            plan_info = PLANS.get(plan_name, PLANS["free"])
            remaining_min = max(plan_info["limit_per_minute"] - minute_count, 0)
            response.headers["X-RateLimit-Limit-Minute"] = str(plan_info["limit_per_minute"])
            response.headers["X-RateLimit-Remaining-Minute"] = str(remaining_min)
            response.headers["X-RateLimit-Used-Minute"] = str(minute_count)

        return parsed
    except ValueError as ve:
        logger.exception("Parsing error: %s", ve)
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception:
        logger.exception("Unexpected parse error")
        raise HTTPException(status_code=500, detail="Internal parse error")

# optional: run locally
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8080)), log_level="info")
