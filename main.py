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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"status": "backend ok"}


# --------- PARSER LOGIC (Python version of your TS parseResumeText) ---------


def parse_resume_text(text: str) -> Dict[str, Any]:
    # Lines and lowercased versions
    raw_lines: List[str] = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]
    lower_lines: List[str] = [l.lower() for l in raw_lines]
    clean_text: str = " ".join(lower_lines)

    # ---- Name ----
    name: str = ""
    for line in raw_lines:
        words = line.split()
        if 2 <= len(words) <= 5 and re.search(r"[a-zA-Z]", line):
            name = line.strip()
            break

    # ---- Email ----
    email_match = re.findall(
        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        clean_text,
    )
    email: str = email_match[0] if email_match else ""

    # ---- Phone ----
    phone_matches = re.findall(
        r"(\+?\d{1,3}[-.\s]?)?(\(?\d{2,4}\)?[-.\s]?)?\d{3,4}[-.\s]?\d{3,4}",
        clean_text,
    )
    phones_clean: List[str] = []
    for groups in phone_matches:
        joined = "".join(groups)
        cleaned = re.sub(r"[^\d+]", "", joined)
        if cleaned:
            phones_clean.append(cleaned)

    phone: str = ""
    for p in phones_clean:
        if 10 <= len(p) <= 15:
            phone = p
            break
    if not phone and phones_clean:
        phone = phones_clean[0]

    # ---- Location ----
    location: str = ""
    for line in raw_lines:
        m = re.search(r"[A-Za-z .]+,\s*[A-Za-z]{2,}", line)
        if m and "email" not in m.group(0).lower():
            location = m.group(0).strip()
            break

    # ---- Experience years ----
    total_years: Optional[float] = None
    patterns = [
        r"(\d+(?:\.\d+)?)\s*(?:\+?\s*)?(?:years?|yrs?|yoe)",
        r"experience[:\s]+(\d+(?:\.\d+)?)",
    ]
    for pat in patterns:
        m = re.search(pat, clean_text, re.IGNORECASE)
        if m:
            try:
                total_years = float(m.group(1))
                break
            except ValueError:
                pass

    # ---- Role level & primary role ----
    role_level = "Mid"
    role_patterns = {
        "Senior": r"(senior|sr\.?|lead|principal|staff)",
        "Junior": r"(junior|jr\.?)",
        "Manager": r"(manager|director|head)",
    }
    for lvl, pat in role_patterns.items():
        if re.search(pat, clean_text, re.IGNORECASE):
            role_level = lvl
            break

    primary_role = "Software Developer"
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
    skills_patterns: Dict[str, str] = {
        "programming_languages": r"\b(typescript|javascript|js|python|java|go|rust|c\+\+|c#|php|ruby|swift|kotlin|scala|dart)\b",
        "frameworks_and_libraries": r"\b(react\.?js?|next\.?js?|vue\.?js?|angular|svelte|django|flask|fastapi|spring|laravel|rails|tensorflow|pytorch|numpy|pandas|nuxt)\b",
        "cloud_and_infra": r"\b(aws|amazon web services|azure|gcp|google cloud|docker|kubernetes|k8s|terraform|jenkins|github actions|gitlab ci|circleci)\b",
        "databases": r"\b(postgresql|postgres|mysql|mariadb|mongodb|redis|dynamodb|firebase|supabase|prisma)\b",
        "dev_tools": r"\b(git|github|gitlab|bitbucket|webpack|vite|npm|yarn|vercel|netlify|railway|jira)\b",
    }

    skills: Dict[str, List[str]] = {}
    for key, pat in skills_patterns.items():
        found = re.findall(pat, clean_text, re.IGNORECASE)
        # Normalise and dedupe
        norm = list(
            dict.fromkeys(
                [s.lower().replace(".", "") for s in found if len(s) > 1]
            )
        )
        skills[key] = norm

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
    summary_start = -1
    for i, line in enumerate(lower_lines):
        if any(kw in line for kw in section_keywords):
            summary_start = i + 1
            break

    if summary_start != -1:
        summary_end = len(lower_lines)
        for i in range(summary_start, len(lower_lines)):
            if any(kw in lower_lines[i] for kw in end_section_keywords):
                summary_end = i
                break
        summary = " ".join(raw_lines[summary_start:summary_end])
    else:
        summary = " ".join(raw_lines[2:10])

    summary = summary[:600]

    # ---- Experience (simple heuristic) ----
    experience: List[Dict[str, Any]] = []
    exp_index = -1
    for i, line in enumerate(lower_lines):
        if any(kw in line for kw in ["work experience", "experience", "employment"]):
            exp_index = i
            break

    if exp_index != -1:
        exp_lines = raw_lines[exp_index + 1 :]
        for i in range(len(exp_lines) - 2):
            role_line = exp_lines[i]
            company_line = exp_lines[i + 1]
            date_line = exp_lines[i + 2]

            if (
                re.search(r"[A-Za-z]", role_line)
                and re.search(r"[A-Za-z]", company_line)
                and re.search(r"\d{4}", date_line)
            ):
                start_year_match = re.search(r"\b(19|20)\d{2}\b", date_line)
                end_year_match = None
                all_years = re.findall(r"\b(19|20)\d{2}\b", date_line)
                if len(all_years) >= 2:
                    end_year_match = all_years[-1]

                experience.append(
                    {
                        "job_title": role_line.strip(),
                        "company": company_line.strip(),
                        "start_date": start_year_match.group(0)
                        if start_year_match
                        else "",
                        "end_date": end_year_match or "Present",
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
                degree_text = (
                    degree_match.group(0)
                    .strip()
                    .replace("  ", " ")
                ) if degree_match else "B.S. Computer Science"

                # little cleanup to mimic earlier logic
                degree_text = re.sub(
                    r"\scs$", " Computer Science", degree_text, flags=re.IGNORECASE
                )

                education.append(
                    {
                        "degree": degree_text,
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
    m_github = re.search(r"github\.com/[a-zA-Z0-9_-]+", clean_text)
    if m_github:
        github = "https://" + m_github.group(0)

    portfolio = ""
    # very loose URL matcher, excluding github
    urls = re.findall(
        r"https?://(?:www\.)?[-a-zA-Z0-9@:%._+~#=]{1,256}\.[a-zA-Z0-9()]{2,6}\b[^\s]*",
        text,
    )
    for u in urls:
        if "github.com" not in u:
            portfolio = u
            break

    return {
        "name": name.strip(),
        "email": email,
        "phone": phone,
        "location": location,
        "role_level": role_level,
        "primary_role": primary_role,
        "years_of_experience_total": total_years,
        "years_of_experience_in_tech": None,
        "github": github,
        "portfolio": portfolio,
        "summary": summary.strip(),
        **skills,
        "experience": experience,
        "education": education,
        "name_confidence": 0.92 if name else 0.6,
        "email_confidence": 0.98 if email else 0.3,
        "phone_confidence": 0.88 if phone else 0.4,
        "location_confidence": 0.82 if location else 0.5,
        "summary_confidence": 0.85 if summary else 0.6,
        "raw": text[:1500] + ("..." if len(text) > 1500 else ""),
    }


# ------------------------- /parse ENDPOINT -----------------------------------


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
            detail="No readable text extracted (maybe a scanned/image-only PDF?)",
        )

    parsed = parse_resume_text(text)
    return parsed
