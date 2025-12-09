import os
import re
import time
import logging
import tempfile
import os
from typing import Dict, Any, List

from fastapi import (
    FastAPI,
    UploadFile,
    File,
    HTTPException,
    Depends,
    Request,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from pdfminer.high_level import extract_text

# --------------------------------------------------------------------
# Config
# --------------------------------------------------------------------

API_KEY = os.getenv("API_KEY")  # we'll set this in Railway

# allow multiple origins via env later if needed
ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "https://resumify-working.vercel.app",
    "https://resumify-working-git-main-raja-karuppasamys-projects.vercel.app",  # optional preview
    "https://resumify.co",            # if you later host frontend here
    "https://www.resumify.co",        # "
    "https://api.resumifyapi.com",    # for internal calls if needed
]

RATE_LIMIT_WINDOW_SECONDS = 60  # 1 minute window
RATE_LIMIT_MAX_REQUESTS = 60    # per IP per minute (adjust as you like)

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
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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

def verify_api_key(request: Request):
    """
    If API_KEY is set in env, require it via x-api-key header.
    If API_KEY is not set (dev), this becomes a no-op.
    """
    if not API_KEY:
        return

    header_key = request.headers.get("x-api-key")
    if header_key != API_KEY:
        logger.warning("Invalid API key from %s", request.client.host)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


def check_rate_limit(request: Request):
    """
    Very simple per-IP rate limiter (in-memory).
    Good enough for MVP on a single Railway instance.
    """
    if RATE_LIMIT_MAX_REQUESTS <= 0:
        return

    ip = request.client.host
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
    Attach this to protected routes like /parse.
    """
    check_rate_limit(request)
    verify_api_key(request)


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

# --------------------------------------------------------------------
# Resume parsing helpers
# --------------------------------------------------------------------

def extract_text_from_pdf_bytes(data: bytes) -> str:
    if not data:
        raise ValueError("Empty file")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        text = extract_text(tmp_path) or ""
    finally:
        os.remove(tmp_path)

    if not text.strip():
        raise ValueError("No readable text extracted")

    return text


# ---------- small utilities ----------

YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
DATE_RANGE_RE = re.compile(
    r"\b(?P<start>(19|20)\d{2})\s*[-–]\s*(?P<end>(19|20)\d{2}|present|current)\b",
    re.IGNORECASE,
)


def normalize_year_token(token: str) -> str | None:
    """
    Fix truncated year tokens like '20' -> None (we'll ignore),
    but keep 4-digit years as is.
    """
    token = token.strip()
    if re.fullmatch(r"(19|20)\d{2}", token):
        return token
    return None


def extract_first_email(text: str) -> str | None:
    match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    return match.group(0) if match else None


def extract_first_phone(text: str) -> str | None:
    match = re.search(
        r"(\+\d{1,3}[\s-]?)?(\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{4})", text
    )
    return match.group(0) if match else None


# ---------- A: Experience parsing ----------

def parse_experience_section(full_text: str) -> list[dict[str, Any]]:
    """
    Heuristic experience parser:
    - Find 'WORK EXPERIENCE' / 'EXPERIENCE' section
    - Split into job blocks
    - Extract job title, company, date range, and bullet responsibilities
    """

    section_match = re.search(r"(?i)(work experience|experience)", full_text)
    if not section_match:
        return []

    exp_text = full_text[section_match.end():].strip()

    # Stop at EDUCATION if present (so we don't eat that section)
    edu_match = re.search(r"(?i)education", exp_text)
    if edu_match:
        exp_text = exp_text[:edu_match.start()].strip()

    # Split into chunks using double newlines
    raw_blocks = re.split(r"\n{2,}", exp_text)
    jobs: list[dict[str, Any]] = []

    for block in raw_blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if len(" ".join(lines)) < 25:
            continue

        # 1. Title + company (first 1–2 lines)
        title = lines[0]
        company = lines[1] if len(lines) > 1 else None

        # 2. Date range (search entire block)
        start_year = None
        end_year = None
        for m in DATE_RANGE_RE.finditer(block):
            start_year = m.group("start")
            end_raw = m.group("end")
            if re.fullmatch(r"(19|20)\d{2}", end_raw):
                end_year = end_raw
            else:
                # “present/current”
                end_year = "Present"
            break  # first match is usually fine

        # Fallback: single years
        if not start_year:
            years = YEAR_RE.findall(block)
            if years:
                start_year = years[0][0] + years[0][1:] if isinstance(years[0], tuple) else years[0]

        # 3. Responsibilities = remaining lines that look like bullets/sentences
        responsibilities: list[str] = []
        for l in lines[2:]:
            if len(l) < 20:
                continue
            responsibilities.append(l)

        jobs.append(
            {
                "job_title": title,
                "company": company,
                "start_date": start_year,
                "end_date": end_year,
                "responsibilities": responsibilities,
                "job_title_confidence": 0.85 if title else 0.0,
                "company_confidence": 0.8 if company else 0.0,
            }
        )

        if len(jobs) >= 5:
            break

    return jobs


# ---------- B: Education parsing ----------

def parse_education_section(full_text: str) -> list[dict[str, Any]]:
    """
    Find 'EDUCATION' section and extract simple degree / institution / year.
    """

    match = re.search(r"(?i)education", full_text)
    if not match:
        return []

    edu_text = full_text[match.end():].strip()

    # Stop at SKILLS or EXPERIENCE if present
    stop_match = re.search(r"(?i)(skills|experience|work experience)", edu_text)
    if stop_match:
        edu_text = edu_text[:stop_match.start()].strip()

    blocks = re.split(r"\n{2,}", edu_text)
    educations: list[dict[str, Any]] = []

    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue

        text_block = " ".join(lines)
        # Degree heuristics
        degree_match = re.search(
            r"(Bachelor|Master|B\.?S\.?|B\.?Tech|B\.?E\.?|M\.?S\.?|M\.?Tech|BSc|MSc)[^,\n]*",
            text_block,
            re.IGNORECASE,
        )
        degree = degree_match.group(0).strip() if degree_match else lines[0]

        # Institution: next line or something with 'University' / 'College'
        institution = None
        for l in lines[1:4]:
            if re.search(r"(?i)(university|college|institute|school)", l):
                institution = l
                break
        if not institution and len(lines) > 1:
            institution = lines[1]

        # Year: first 4-digit year in block
        year_match = YEAR_RE.search(text_block)
        year = year_match.group(0) if year_match else ""

        educations.append(
            {
                "degree": degree,
                "institution": institution,
                "year": year,
                "degree_confidence": 0.85 if degree else 0.0,
                "institution_confidence": 0.8 if institution else 0.0,
            }
        )

        if len(educations) >= 5:
            break

    return educations


# ---------- C: Skills parsing ----------

def parse_skills_section(full_text: str) -> dict[str, list[str]]:
    """
    Parse SKILLS section and group into:
    - programming_languages
    - frameworks_and_libraries
    - cloud_and_infra
    - databases
    - dev_tools
    """

    match = re.search(r"(?i)skills", full_text)
    if not match:
        return {
            "programming_languages": [],
            "frameworks_and_libraries": [],
            "cloud_and_infra": [],
            "databases": [],
            "dev_tools": [],
        }

    skills_text = full_text[match.end():].strip()

    # Stop at EDUCATION / EXPERIENCE
    stop_match = re.search(r"(?i)(education|experience|work experience)", skills_text)
    if stop_match:
        skills_text = skills_text[:stop_match.start()].strip()

    tokens = re.split(r"[,\n•\-•;]+", skills_text)
    skills = [t.strip().lower() for t in tokens if t.strip()]

    langs_ref = {"java", "javascript", "js", "typescript", "python", "go", "c++", "c#", "php", "ruby"}
    fw_ref = {"react", "react.js", "reactjs", "vue", "angular", "django", "flask", "next.js", "spring"}
    cloud_ref = {"aws", "gcp", "azure", "docker", "kubernetes", "k8s", "terraform"}
    db_ref = {"mysql", "postgres", "postgresql", "mongodb", "redis", "oracle", "sql server"}
    tools_ref = {"git", "jira", "jenkins", "github", "gitlab", "figma"}

    programming_languages: list[str] = []
    frameworks_and_libraries: list[str] = []
    cloud_and_infra: list[str] = []
    databases: list[str] = []
    dev_tools: list[str] = []

    for s in skills:
        s_clean = s.replace("react.js", "reactjs")
        if s_clean in langs_ref:
            programming_languages.append(s)
        elif s_clean in fw_ref:
            frameworks_and_libraries.append(s)
        elif s_clean in cloud_ref:
            cloud_and_infra.append(s)
        elif s_clean in db_ref:
            databases.append(s)
        elif s_clean in tools_ref:
            dev_tools.append(s)

    return {
        "programming_languages": programming_languages,
        "frameworks_and_libraries": frameworks_and_libraries,
        "cloud_and_infra": cloud_and_infra,
        "databases": databases,
        "dev_tools": dev_tools,
    }


# ---------- Contact + summary + overall aggregator ----------

def parse_basic_fields(text: str) -> Dict[str, Any]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    full_text = "\n".join(lines)

    # Contact
    name = lines[0] if lines else None
    email = extract_first_email(full_text)
    phone = extract_first_phone(full_text)

    # Location: first line in top 6 that has a comma and isn't email/phone
    location = None
    for l in lines[:6]:
        if "," in l and email not in l and (not phone or phone not in l):
            location = l
            break

    # Summary: line or short paragraph after contact block
    summary = None
    if len(lines) > 3:
        summary_candidates = lines[2:8]
        for cand in summary_candidates:
            if len(cand.split()) >= 6:
                summary = cand
                break

    # A — Experience
    experience = parse_experience_section(full_text)

    # B — Education
    education = parse_education_section(full_text)

    # C — Skills
    skills = parse_skills_section(full_text)

    return {
        "name": name,
        "email": email,
        "phone": phone,
        "location": location,
        "role_level": None,          # we can infer later (AI layer)
        "primary_role": None,        # later
        "years_of_experience_total": None,
        "years_of_experience_in_tech": None,
        "github": "",
        "portfolio": "",
        "summary": summary,
        "experience": experience,
        "education": education,
        **skills,
        "name_confidence": 0.92 if name else 0.0,
        "email_confidence": 0.98 if email else 0.0,
        "phone_confidence": 0.88 if phone else 0.0,
        "location_confidence": 0.82 if location else 0.0,
        "summary_confidence": 0.85 if summary else 0.0,
        "raw": text,
    }

# --------------------------------------------------------------------
# /parse endpoint (protected)
# --------------------------------------------------------------------

@app.post("/parse")
async def parse_resume(
    request: Request,
    file: UploadFile = File(...),
    _secure: None = Depends(secure_request),
):
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only PDF files allowed")

    contents = await file.read()
    try:
        text = extract_text_from_pdf_bytes(contents)
        parsed = parse_basic_fields(text)
        return parsed
    except ValueError as ve:
        logger.exception("Parsing error: %s", ve)
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.exception("Unexpected parse error")
        raise HTTPException(status_code=500, detail="Internal parse error")
