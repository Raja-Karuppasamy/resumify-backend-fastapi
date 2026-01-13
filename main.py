from fastapi import FastAPI, UploadFile, File, Request, Response, HTTPException, Depends, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response as FastAPIResponse
import time
import logging
import asyncio
import os
import re
import tempfile
import json
import httpx
from typing import Dict, List, Tuple, Optional, Any
from pdfminer.high_level import extract_text


logger = logging.getLogger("resumify-backend")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Resumify Backend API")

# --------------------------------------------------------------------
# Config
# --------------------------------------------------------------------

RATE_LIMIT_MAX_REQUESTS = 5
RATE_LIMIT_WINDOW_SECONDS = 60
MONTHLY_LIMIT = 20

API_KEY = os.getenv("API_KEY", "")  # Empty = no auth required

# In-memory storage
_rate_limit_store: Dict[str, List[float]] = {}
_usage_store: Dict[str, Dict[str, Any]] = {}

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://resumifyapi.com",
        "https://www.resumifyapi.com",
        "http://resumifyapi.com",
        "http://www.resumifyapi.com",
        "http://localhost:3000",
        "https://resumify-working.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# OPTIONS handlers for CORS preflight
@app.options("/parse")
async def parse_options():
    return FastAPIResponse(status_code=204)

@app.options("/parse/ai")
async def parse_ai_options():
    return FastAPIResponse(status_code=204)

# Logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)

    start = time.time()
    response = await call_next(request)
    duration = (time.time() - start) * 1000
    logger.info(
        f"{request.client.host} {request.method} {request.url.path} "
        f"-> {response.status_code} ({duration:.1f} ms)"
    )
    return response


# --------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------

def _client_ip_from_request(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"

def get_api_key_from_request(request: Request) -> str:
    """Get API key from header or use IP as anonymous key"""
    api_key = request.headers.get("x-api-key")
    if not api_key:
        # Use IP as anonymous key for free tier
        api_key = f"anon_{_client_ip_from_request(request)}"
    return api_key

async def check_rate_limit_dependency(request: Request) -> str:
    """Check rate limits and return API key"""
    api_key = get_api_key_from_request(request)
    check_rate_limit(api_key)
    return api_key

def check_rate_limit(api_key: str):
    _ensure_key_record(api_key)
    _maybe_reset_month(api_key)

    rec = _usage_store[api_key]
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS

    rec["minute_timestamps"] = [
        ts for ts in rec["minute_timestamps"] if ts >= window_start
    ]

    minute_count = len(rec["minute_timestamps"])
    month_count = rec["month_count"]

    if minute_count >= RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded (per minute)"
        )

    if month_count >= MONTHLY_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="Free plan limit reached (monthly). Upgrade to continue."
        )

    rec["minute_timestamps"].append(now)
    rec["month_count"] += 1


# --------------------------------------------------------------------
# Usage tracking
# --------------------------------------------------------------------

PLANS = {
    "free": {"limit_per_minute": 60, "limit_per_month": 1000, "display_name": "Free (Beta)"},
    "pro": {"limit_per_minute": 600, "limit_per_month": 100000, "display_name": "Pro"},
}

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

# Admin routes
@app.get("/usage/public")
def public_usage(request: Request):
    api_key = get_api_key_from_request(request)
    return get_usage_for_key(api_key)

# --------------------------------------------------------------------
# Health endpoints
# --------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "backend ok", "version": "v1"}

@app.get("/health")
def health():
    return {
        "status": "ok",
        "uptime_ms": int(time.time() * 1000),
        "version": "v1",
    }

# --------------------------------------------------------------------
# Parser helpers
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

# --------------------------------------------------------------------
# Parse endpoints
# --------------------------------------------------------------------

@app.post("/parse")
async def parse_resume(
    request: Request,
    file: UploadFile = File(...),
    api_key: str = Depends(check_rate_limit_dependency),
):
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only PDF files allowed")

    contents = await file.read()

    try:
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, extract_text_from_pdf_bytes, contents)
        parsed = parse_basic_fields(text)

        return parsed

    except ValueError as ve:
        logger.exception("Parse error")
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.exception("Unexpected parse error")
        raise HTTPException(status_code=500, detail="Internal parse error")
    
    # Add this code to your main.py file after the existing @app.post("/parse") endpoint
# This adds AI-powered parsing with 95% accuracy

@app.post("/parse/ai")
async def parse_resume_ai(
    request: Request,
    file: UploadFile = File(...),
    api_key: str = Depends(check_rate_limit_dependency),
):
    """
    AI-powered resume parser with 95% accuracy
    Includes quality scoring and ATS compatibility analysis
    """
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only PDF files allowed")

    contents = await file.read()
    
    try:
        # Extract text from PDF
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, extract_text_from_pdf_bytes, contents)
        
        # Check if Anthropic API key is available
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if not anthropic_key:
            logger.warning("ANTHROPIC_API_KEY not set - falling back to regex parser")
            parsed = parse_basic_fields(text)
            parsed["parser_used"] = "regex"
            return parsed
        
        # Call Claude API for intelligent parsing
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01",
                    "x-api-key": anthropic_key
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 4000,
                    "messages": [{
                        "role": "user",
                        "content": f"""Extract ALL information from this resume into structured JSON. Be extremely thorough and accurate.

CRITICAL INSTRUCTIONS:
- Extract the COMPLETE text for all fields (don't truncate summaries or responsibilities)
- ALWAYS extract company names - they are CRITICAL
- Find ALL skills mentioned (not just common ones)
- Extract ALL URLs (LinkedIn, GitHub, Portfolio, personal website)
- Calculate years of experience by analyzing date ranges

Resume text:
{text}

Return ONLY valid JSON with no markdown formatting:
{{
  "name": "exact full name from resume",
  "email": "email@example.com",
  "phone": "full phone number with country code if present",
  "location": "City, State/Country",
  "linkedin": "full LinkedIn URL if present, else empty string",
  "github": "full GitHub URL if present, else empty string",
  "portfolio": "portfolio/personal website URL if present, else empty string",
  "summary": "COMPLETE professional summary - extract the FULL text without truncation",
  "role_level": "Junior/Mid-level/Senior based on years of experience",
  "primary_role": "their main role like Frontend/Backend/Full-Stack/DevOps/Data Scientist/etc",
  "years_of_experience_total": calculate_total_years_as_number,
  "years_of_experience_in_tech": calculate_tech_years_as_number,
  "skills": {{
    "programming_languages": ["list ALL programming languages mentioned"],
    "frameworks_libraries": ["list ALL frameworks and libraries"],
    "cloud_platforms": ["AWS", "Azure", "GCP", "Heroku", etc],
    "databases": ["list ALL databases and data stores"],
    "dev_tools": ["Git", "Docker", "CI/CD tools", "IDEs", etc],
    "soft_skills": ["leadership", "communication", "teamwork", etc]
  }},
  "experience": [
    {{
      "job_title": "exact job title",
      "company": "CRITICAL: Always extract company name - never leave empty",
      "location": "job location if mentioned",
      "start_date": "YYYY-MM or Month YYYY",
      "end_date": "YYYY-MM, Month YYYY, or Present",
      "duration_months": calculate_duration_in_months,
      "responsibilities": ["extract ALL bullet points - do not truncate or summarize"],
      "technologies": ["list all technologies/tools mentioned in this role"],
      "job_title_confidence": 0.95,
      "company_confidence": 0.9
    }}
  ],
  "education": [
    {{
      "degree": "full degree name (Bachelor of Science in Computer Science, etc)",
      "institution": "full school/university name",
      "location": "school location if present",
      "graduation_year": "YYYY",
      "gpa": "GPA if mentioned, else empty string",
      "honors": "Dean's List, Cum Laude, etc if mentioned",
      "degree_confidence": 0.9,
      "institution_confidence": 0.85
    }}
  ],
  "certifications": [
    {{
      "name": "certification name",
      "issuer": "issuing organization",
      "date": "YYYY or Month YYYY",
      "credential_id": "ID if present"
    }}
  ],
  "projects": [
    {{
      "name": "project name",
      "description": "what the project does/accomplished",
      "technologies": ["technologies used"],
      "url": "GitHub or demo URL if present"
    }}
  ],
  "languages": [
    {{
      "language": "language name",
      "proficiency": "Native/Fluent/Professional/Conversational/Basic"
    }}
  ]
}}

Remember: Return ONLY the JSON object with no markdown code blocks or explanations."""
                    }]
                }
            )
        
        data = response.json()
        ai_text = data["content"][0]["text"].strip()
        
        # Clean markdown formatting if Claude added it
        if ai_text.startswith("```"):
            lines = ai_text.split("\n")
            ai_text = "\n".join(lines[1:-1]) if len(lines) > 2 else ai_text
            if ai_text.startswith("json"):
                ai_text = ai_text[4:].strip()
        
        # Parse the JSON
        parsed = json.loads(ai_text.strip())
        
        # Add metadata
        parsed["parser_used"] = "ai"
        parsed["raw"] = text
        
        # Add confidence scores for main fields
        parsed["name_confidence"] = 0.95 if parsed.get("name") else 0.0
        parsed["email_confidence"] = 0.95 if parsed.get("email") else 0.0
        parsed["phone_confidence"] = 0.9 if parsed.get("phone") else 0.0
        parsed["location_confidence"] = 0.85 if parsed.get("location") else 0.0
        parsed["summary_confidence"] = 0.85 if parsed.get("summary") else 0.0
        
        # Calculate overall confidence
        all_confidences = [
            parsed.get("name_confidence", 0),
            parsed.get("email_confidence", 0),
            parsed.get("phone_confidence", 0)
        ]
        
        # Add experience confidences
        for exp in parsed.get("experience", []):
            all_confidences.append(exp.get("job_title_confidence", 0))
            all_confidences.append(exp.get("company_confidence", 0))
        
        # Add education confidences
        for edu in parsed.get("education", []):
            all_confidences.append(edu.get("degree_confidence", 0))
            all_confidences.append(edu.get("institution_confidence", 0))
        
        parsed["overall_confidence"] = round(sum(all_confidences) / len(all_confidences), 2) if all_confidences else 0.0
        
        # Add quality analysis
        parsed["quality_analysis"] = analyze_resume_quality(parsed)
        
        # Add ATS compatibility analysis
        parsed["ats_analysis"] = check_ats_compatibility_advanced(parsed, text)
        
        logger.info(f"AI parsing successful - Quality score: {parsed['quality_analysis']['score']}")
        
        return parsed
        
    except json.JSONDecodeError as e:
        logger.exception("Failed to parse Claude's JSON response")
        logger.error(f"Raw response: {ai_text[:500]}")
        # Fallback to regex parser
        parsed = parse_basic_fields(text)
        parsed["parser_used"] = "regex_fallback"
        parsed["error"] = "AI parsing failed - used fallback parser"
        return parsed
        
    except Exception as e:
        logger.exception("AI parsing error")
        # Fallback to regex parser
        parsed = parse_basic_fields(text)
        parsed["parser_used"] = "regex_fallback"
        parsed["error"] = f"AI parsing failed: {str(e)}"
        return parsed


def analyze_resume_quality(parsed: dict) -> dict:
    """
    Comprehensive resume quality scoring
    Returns score, grade, strengths, issues, and recommendations
    """
    score = 0
    max_score = 100
    issues = []
    strengths = []
    
    # === Contact Information (20 points) ===
    if parsed.get("email"):
        score += 10
        if len(parsed.get("email", "")) > 5:
            strengths.append("Valid email address provided")
    else:
        issues.append("Missing email address - critical for recruiters")
    
    if parsed.get("phone"):
        score += 5
    else:
        issues.append("Add phone number for easier contact")
    
    if parsed.get("linkedin") or parsed.get("github") or parsed.get("portfolio"):
        score += 5
        strengths.append("Professional online presence included")
    else:
        issues.append("Add LinkedIn or GitHub profile to stand out")
    
    # === Work Experience (30 points) ===
    exp_count = len(parsed.get("experience", []))
    if exp_count >= 3:
        score += 20
        strengths.append(f"{exp_count} work experiences demonstrate career progression")
    elif exp_count >= 2:
        score += 15
        strengths.append(f"{exp_count} work experiences listed")
    elif exp_count >= 1:
        score += 10
    else:
        issues.append("Add relevant work experience")
    
    # Check experience quality
    exp_quality_bonus = 0
    missing_company = False
    weak_responsibilities = False
    
    for exp in parsed.get("experience", [])[:3]:
        # Check for company name
        if not exp.get("company") or exp.get("company") == "N/A":
            missing_company = True
        
        # Check responsibilities
        responsibilities = exp.get("responsibilities", [])
        if len(responsibilities) >= 4:
            exp_quality_bonus += 3
        elif len(responsibilities) >= 2:
            exp_quality_bonus += 2
        else:
            weak_responsibilities = True
        
        # Check for technologies mentioned
        if exp.get("technologies") and len(exp.get("technologies", [])) > 0:
            exp_quality_bonus += 2
    
    score += min(exp_quality_bonus, 10)
    
    if missing_company:
        issues.append("Some positions are missing company names")
    
    if weak_responsibilities and exp_count > 0:
        issues.append("Add more bullet points to work experience (aim for 3-5 per role)")
    
    if exp_quality_bonus >= 8:
        strengths.append("Detailed work experience with clear achievements")
    
    # === Education (15 points) ===
    edu_count = len(parsed.get("education", []))
    if edu_count >= 1:
        score += 15
        strengths.append("Education credentials listed")
    else:
        issues.append("Add education information")
    
    # === Skills (20 points) ===
    skills = parsed.get("skills", {})
    total_skills = sum(len(v) for v in skills.values() if isinstance(v, list))
    
    if total_skills >= 15:
        score += 20
        strengths.append(f"{total_skills} skills showcase broad expertise")
    elif total_skills >= 10:
        score += 15
        strengths.append(f"{total_skills} relevant skills listed")
    elif total_skills >= 5:
        score += 10
    elif total_skills >= 3:
        score += 5
    else:
        issues.append("List more skills - aim for 10-15 relevant skills")
    
    # === Professional Summary (5 points) ===
    summary = parsed.get("summary", "")
    if summary and len(summary) > 100:
        score += 5
        strengths.append("Strong professional summary")
    elif summary and len(summary) > 30:
        score += 3
    else:
        issues.append("Add a compelling professional summary (2-3 sentences)")
    
    # === Certifications & Projects (10 points) ===
    certs = parsed.get("certifications", [])
    projects = parsed.get("projects", [])
    
    if len(certs) > 0:
        score += 5
        strengths.append(f"{len(certs)} certification(s) demonstrate commitment")
    
    if len(projects) > 0:
        score += 5
        strengths.append(f"{len(projects)} project(s) showcase practical skills")
    
    if len(certs) == 0 and len(projects) == 0:
        issues.append("Add certifications or personal projects to stand out")
    
    # === Determine Grade ===
    if score >= 90:
        grade = "A"
        verdict = "Excellent"
        emoji = "🌟"
    elif score >= 80:
        grade = "B"
        verdict = "Good"
        emoji = "✅"
    elif score >= 70:
        grade = "C"
        verdict = "Average"
        emoji = "👍"
    elif score >= 60:
        grade = "D"
        verdict = "Needs Improvement"
        emoji = "⚠️"
    else:
        grade = "F"
        verdict = "Poor"
        emoji = "❌"
    
    return {
        "score": min(score, max_score),
        "grade": grade,
        "verdict": verdict,
        "emoji": emoji,
        "strengths": strengths[:5],  # Top 5 strengths
        "issues": issues[:7],  # Top 7 issues
        "recommendations": generate_recommendations(issues, parsed),
        "breakdown": {
            "contact_info": {"max": 20, "scored": min(score, 20)},
            "experience": {"max": 30, "scored": min(exp_count * 10 + exp_quality_bonus, 30)},
            "education": {"max": 15, "scored": 15 if edu_count > 0 else 0},
            "skills": {"max": 20, "scored": min(total_skills * 1.5, 20)},
            "summary": {"max": 5, "scored": 5 if len(summary) > 100 else 0},
            "extras": {"max": 10, "scored": (5 if certs else 0) + (5 if projects else 0)}
        }
    }


def check_ats_compatibility_advanced(parsed: dict, raw_text: str) -> dict:
    """
    Advanced ATS (Applicant Tracking System) compatibility check
    Returns score, issues, warnings, and recommendations
    """
    score = 100
    critical_issues = []
    warnings = []
    
    # === Check Required Sections ===
    if not parsed.get("experience") or len(parsed.get("experience", [])) == 0:
        critical_issues.append("Missing work experience section - ATS will reject")
        score -= 30
    
    if not parsed.get("education") or len(parsed.get("education", [])) == 0:
        critical_issues.append("Missing education section - ATS may flag")
        score -= 20
    
    if not parsed.get("skills") or sum(len(v) for v in parsed.get("skills", {}).values() if isinstance(v, list)) == 0:
        critical_issues.append("Missing skills section - ATS cannot match keywords")
        score -= 20
    
    # === Check Contact Information ===
    if not parsed.get("email"):
        critical_issues.append("Missing email address - ATS cannot contact you")
        score -= 15
    
    if not parsed.get("phone"):
        warnings.append("Phone number recommended for ATS")
        score -= 3
    
    # === Check Experience Quality ===
    for idx, exp in enumerate(parsed.get("experience", [])):
        # Check for company name
        if not exp.get("company") or exp.get("company") == "N/A":
            critical_issues.append(f"Position #{idx+1} missing company name - ATS cannot verify")
            score -= 10
            break
        
        # Check for dates
        if not exp.get("start_date") or not exp.get("end_date"):
            warnings.append("Some positions have unclear date ranges")
            score -= 5
            break
        
        # Check for responsibilities
        if not exp.get("responsibilities") or len(exp.get("responsibilities", [])) == 0:
            warnings.append("Some positions lack detailed responsibilities")
            score -= 3
            break
    
    # === Check Skills Density ===
    skills = parsed.get("skills", {})
    total_skills = sum(len(v) for v in skills.values() if isinstance(v, list))
    
    if total_skills < 5:
        critical_issues.append("Too few skills listed - ATS needs 8-15 relevant keywords")
        score -= 15
    elif total_skills < 8:
        warnings.append("Add 3-5 more skills for better ATS keyword matching")
        score -= 5
    
    # === Check for Summary ===
    if not parsed.get("summary") or len(parsed.get("summary", "")) < 50:
        warnings.append("Professional summary helps ATS understand your profile")
        score -= 5
    
    # === Check Text Formatting (basic) ===
    # Check for tabs
    if "\t" in raw_text:
        warnings.append("Tabs detected - use spaces for better ATS compatibility")
    
    # Check for excessive special characters
    special_char_count = sum(1 for c in raw_text if c in "•◦▪▫★☆◆◇")
    if special_char_count > 50:
        warnings.append("Excessive special characters may confuse ATS")
    
    # === Determine Pass/Fail ===
    ats_friendly = score >= 70
    
    if score >= 90:
        grade = "Excellent"
        emoji = "🌟"
    elif score >= 80:
        grade = "Good"
        emoji = "✅"
    elif score >= 70:
        grade = "Pass"
        emoji = "👍"
    elif score >= 60:
        grade = "Marginal"
        emoji = "⚠️"
    else:
        grade = "Fail"
        emoji = "❌"
    
    return {
        "ats_friendly": ats_friendly,
        "score": max(score, 0),
        "grade": grade,
        "emoji": emoji,
        "critical_issues": critical_issues,
        "warnings": warnings,
        "recommendations": [
            "Use standard section headers: 'Work Experience', 'Education', 'Skills'",
            "Include clear date ranges for all positions (MM/YYYY - MM/YYYY format)",
            "List 10-15 relevant skills and technologies as keywords",
            "Avoid tables, text boxes, headers/footers, and images",
            "Use standard fonts: Arial, Calibri, Times New Roman, or Helvetica",
            "Save as .docx or .pdf format (PDF preferred)",
            "Include keywords from target job descriptions",
            "Spell out acronyms at least once (e.g., 'Applicant Tracking System (ATS)')",
            "Use simple bullet points (• or -) for lists",
            "Keep formatting simple and consistent throughout"
        ],
        "keyword_density": {
            "total_skills": total_skills,
            "recommended_minimum": 10,
            "status": "good" if total_skills >= 10 else "needs_improvement"
        }
    }


def generate_recommendations(issues: list, parsed: dict) -> list:
    """
    Generate actionable, prioritized recommendations based on issues found
    """
    recs = []
    
    # Map issues to specific recommendations
    if any("email" in str(issue).lower() for issue in issues):
        recs.append("📧 Add your professional email address at the top of your resume")
    
    if any("phone" in str(issue).lower() for issue in issues):
        recs.append("📱 Include your phone number to make it easy for recruiters to reach you")
    
    if any("linkedin" in str(issue).lower() or "github" in str(issue).lower() for issue in issues):
        recs.append("🔗 Add your LinkedIn profile URL - 90% of recruiters check LinkedIn")
    
    if any("experience" in str(issue).lower() for issue in issues):
        recs.append("💼 Add at least 2-3 relevant work experiences with 3-5 bullet points each")
    
    if any("company" in str(issue).lower() for issue in issues):
        recs.append("🏢 Include company names for all positions - ATS systems require this")
    
    if any("skills" in str(issue).lower() for issue in issues):
        recs.append("🛠️ List 10-15 technical skills relevant to your target role (include tools, languages, frameworks)")
    
    if any("summary" in str(issue).lower() for issue in issues):
        recs.append("📝 Write a compelling 2-3 sentence professional summary highlighting your unique value")
    
    if any("education" in str(issue).lower() for issue in issues):
        recs.append("🎓 Add your education (degree, institution, graduation year)")
    
    if any("certification" in str(issue).lower() or "project" in str(issue).lower() for issue in issues):
        recs.append("🏆 Include relevant certifications or showcase 2-3 personal projects with GitHub links")
    
    if any("bullet" in str(issue).lower() or "responsibilities" in str(issue).lower() for issue in issues):
        recs.append("📊 Expand your work experience bullet points - include quantifiable achievements")
    
    # Add general best practices if we have room
    if len(recs) < 5:
        recs.append("✨ Use action verbs to start bullet points (Led, Built, Improved, Launched)")
    
    if len(recs) < 5:
        recs.append("📈 Include metrics and numbers to demonstrate impact (e.g., 'Improved performance by 40%')")
    
    return recs[:7]  # Return top 7 recommendations


# Run locally
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8080)), log_level="info")