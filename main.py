from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pdfminer.high_level import extract_text
import tempfile
import os
import re
from typing import List, Dict, Any, Optional

app = FastAPI()

ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "https://resumify-working.vercel.app",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"status": "backend ok"}


# ------------ PARSER LOGIC (ported from TS) -----------------


def parse_resume_text(text: str) -> Dict[str, Any]:
    # basic line cleaning
    raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lower_lines = [ln.lower() for ln in raw_lines]
    clean_text = " ".join(lower_lines)

    # ---- Name ----
    name = ""
    for line in raw_lines:
        words = line.split()
        if 2 <= len(words) <= 5 and re.search(r"[a-zA-Z]", line):
            name = line
            break

    # ---- Email ----
    email_match = re.search(
        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", clean_text
    )
    email = email_match.group(0) if email_match else ""

    # ---- Phone ----
    phone_regex = re.compile(
        r"(\+?\d{1,3}[-.\s]?)?(\(?\d{2,4}\)?[-.\s]?)?\d{3,4}[-.\s]?\d{3,4}"
    )
    phone_matches = [
        re.sub(r"[^\d+]", "", m.group(0))
        for m in phone_regex.finditer(clean_text)
    ]
    phone = ""
    for p in phone_matches:
        if 10 <= len(p) <= 15:
            phone = p
            break
    if not phone and phone_matches:
        phone = phone_matches[0]

    # ---- Location ----
    location = ""
    for line in raw_lines:
        match = re.search(r"[A-Za-z .]+,\s*[A-Za-z]{2,}", line)
        if match and "email" not in match.group(0).lower():
            location = match.group(0).strip()
            break

    # ---- Experience Years ----
    total_years: Optional[float] = None
    exp_patterns = [
        re.compile(r"(\d+(?:\.\d+)?)\s*(?:\+?\s*)?(?:years?|yrs?|yoe)"),
        re.compile(r"experience[:\s]+(\d+(?:\.\d+)?)"),
    ]
    for pat in exp_patterns:
        m = pat.search(clean_text)
        if m:
            try:
                total_years = float(m.group(1))
                break
            except ValueError:
                pass

    # ---- Role level / primary role ----
    role_patterns = {
        "Senior": re.compile(r"(senior|sr\.?|lead|principal|staff)"),
        "Junior": re.compile(r"(junior|jr\.?)"),
        "Manager": re.compile(r"(manager|director|head)"),
    }
    role_level = "Mid"
    for lvl, rgx in role_patterns.items():
        if rgx.search(clean_text):
            role_level = lvl
            break

    role_keywords = [
        "fullstack",
        "full stack",
        "frontend",
        "front end",
        "backend",
        "back end",
        "devops",
        "ml",
        "ai",
        "data engineer",
        "data scientist",
        "sysops",
        "sre",
        "cloud engineer",
    ]
    primary_role = "Software Developer"
    for kw in role_keywords:
        if kw in clean_text:
            if kw == "sysops":
                primary_role = "AWS SysOps Administrator"
            else:
                primary_role = " ".join(
                    w.capitalize() for w in kw.split()
                )
            break

    # ---- Skills ----
    def collect(pattern: str) -> List[str]:
        rgx = re.compile(pattern, re.IGNORECASE)
        vals = [
            rgx_match.group(0).lower().replace(".", "")
            for rgx_match in rgx.finditer(clean_text)
        ]
        # unique & length > 1
        uniq = []
        for v in vals:
            if v not in uniq and len(v) > 1:
                uniq.append(v)
        return uniq

    programming_languages = collect(
        r"\b(typescript|javascript|js|python|java|go|rust|c\+\+|c#|php|ruby|swift|kotlin|scala|dart)\b"
    )
    frameworks_and_libraries = collect(
        r"\b(react\.?js?|next\.?js?|vue\.?js?|angular|svelte|django|flask|fastapi|spring|laravel|rails|tensorflow|pytorch|numpy|pandas|nuxt)\b"
    )
    cloud_and_infra = collect(
        r"\b(aws|amazon web services|azure|gcp|google cloud|docker|kubernetes|k8s|terraform|jenkins|github actions|gitlab ci|circleci)\b"
    )
    databases = collect(
        r"\b(postgresql|postgres|mysql|mariadb|mongodb|redis|dynamodb|firebase|supabase|prisma)\b"
    )
    dev_tools = collect(
        r"\b(git|github|gitlab|bitbucket|webpack|vite|npm|yarn|vercel|netlify|railway|jira)\b"
    )

    # ---- Summary ----
    section_keywords = [
        "career objective",
        "professional summary",
        "summary",
        "profile",
        "objective",
    ]
    end_section_keywords = [
        "work experience",
        "experience",
        "employment",
        "education",
        "skills",
    ]
    summary = ""
    summary_start_idx = -1
    for i, line in enumerate(lower_lines):
        if any(kw in line for kw in section_keywords):
            summary_start_idx = i + 1
            break

    if summary_start_idx != -1:
        summary_end_idx = len(lower_lines)
        for i in range(summary_start_idx, len(lower_lines)):
            if any(kw in lower_lines[i] for kw in end_section_keywords):
                summary_end_idx = i
                break
        summary = " ".join(raw_lines[summary_start_idx:summary_end_idx])
    else:
        # fallback: first few lines after name
        summary = " ".join(raw_lines[2:10])
    summary = summary[:600]

    # ---- Experience (very simple heuristic) ----
    experience: List[Dict[str, Any]] = []
    exp_index = -1
    for i, line in enumerate(lower_lines):
        if any(kw in line for kw in ["work experience", "experience", "employment"]):
            exp_index = i
            break

    if exp_index != -1:
        exp_lines = raw_lines[exp_index + 1 :]
        for i in range(0, len(exp_lines) - 2):
            role_line = exp_lines[i]
            company_line = exp_lines[i + 1]
            date_line = exp_lines[i + 2]
            if (
                re.search(r"[A-Za-z]", role_line)
                and re.search(r"[A-Za-z]", company_line)
                and re.search(r"\d{4}", date_line)
            ):
                years = re.findall(r"(19|20)\d{2}", date_line)
                start_year = years[0] if years else ""
                end_year = years[-1] if len(years) > 1 else "Present"
                experience.append(
                    {
                        "job_title": role_line,
                        "company": company_line,
                        "start_date": start_year,
                        "end_date": end_year,
                        "responsibilities": [],
                        "job_title_confidence": 0.85,
                        "company_confidence": 0.8,
                    }
                )

    if not experience:
        experience.append(
            {
                "job_title": "Software Engineer",
                "company": "Company",
                "start_date": "",
                "end_date": "Present",
                "responsibilities": [],
                "job_title_confidence": 0.7,
                "company_confidence": 0.6,
            }
        )

    # ---- Education ----
    education: List[Dict[str, Any]] = []
    edu_index = -1
    for i, line in enumerate(lower_lines):
        if any(kw in line for kw in ["education", "academics"]):
            edu_index = i
            break

    if edu_index != -1:
        edu_lines = raw_lines[edu_index + 1 : edu_index + 8]
        for line in edu_lines:
            degree_match = re.search(
                r"(bachelor|master|b\.?s\.?|m\.?s\.?|bsc|msc|phd)[^,]*",
                line,
                re.IGNORECASE,
            )
            year_match = re.search(r"\b(19|20)\d{2}\b", line)
            if degree_match or year_match:
                degree = (
                    degree_match.group(0)
                    .replace("  ", " ")
                    .strip()
                )
                degree = re.sub(
                    r"\scs$", " Computer Science", degree, flags=re.IGNORECASE
                )
                if not degree:
                    degree = "B.S. Computer Science"
                education.append(
                    {
                        "degree": degree,
                        "institution": "University",
                        "year": year_match.group(0) if year_match else "",
                        "degree_confidence": 0.85,
                        "institution_confidence": 0.8,
                    }
                )

    if not education:
        education.append(
            {
                "degree": "B.S. Computer Science",
                "institution": "University",
                "year": "",
                "degree_confidence": 0.8,
                "institution_confidence": 0.75,
            }
        )

    # ---- URLs ----
    github = ""
    github_match = re.search(r"github\.com/[a-zA-Z0-9_-]+", clean_text)
    if github_match:
        github = "https://" + github_match.group(0)

    portfolio = ""
    portfolio_regex = re.compile(
        r"https?://(?:www\.)?[-a-zA-Z0-9@:%._+~#=]{1,256}"
        r"\.[a-zA-Z0-9()]{2,6}\b(?!.*github\.com)[^\s]*"
    )
    portfolio_match = portfolio_regex.search(clean_text)
    if portfolio_match:
        portfolio = portfolio_match.group(0)

    result: Dict[str, Any] = {
        "name": name.strip(),
        "email": email,
        "phone": phone,
        "location": location,
        "role_level": role_level,
        "primary_role": primary_role,
        "years_of_experience_total": total_years if total_years is not None else None,
        "years_of_experience_in_tech": None,
        "github": github,
        "portfolio": portfolio,
        "summary": summary.strip(),
        "programming_languages": programming_languages,
        "frameworks_and_libraries": frameworks_and_libraries,
        "cloud_and_infra": cloud_and_infra,
        "databases": databases,
        "dev_tools": dev_tools,
        "experience": experience,
        "education": education,
        "name_confidence": 0.92 if name else 0.6,
        "email_confidence": 0.98 if email else 0.3,
        "phone_confidence": 0.88 if phone else 0.4,
        "location_confidence": 0.82 if location else 0.5,
        "summary_confidence": 0.85 if summary else 0.6,
        "years_of_experience_total_confidence": 0.8 if total_years else 0.5,
        "github_confidence": 0.9 if github else 0.4,
        "portfolio_confidence": 0.9 if portfolio else 0.4,
        "raw": text[:1500] + ("..." if len(text) > 1500 else ""),
    }

    return result


# ------------ /parse endpoint -----------------


@app.post("/parse")
async def parse_resume(file: UploadFile = File(...)):
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only PDF files allowed")

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty file uploaded")

    # Write to temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    try:
        text = extract_text(tmp_path) or ""
    finally:
        os.remove(tmp_path)

    if not text.strip():
        raise HTTPException(
            status_code=400,
            detail="No readable text extracted (possibly a scanned/image-only PDF).",
        )

    parsed = parse_resume_text(text)
    return parsed
