"""
Phase 12 (deterministic-first extraction), expanded in Phase 17 to be
the PRIMARY resume skill extractor - not just a job-description helper.

Layers, all built on ONE shared dictionary so there's never a second,
conflicting skill representation:

  SKILLS    - canonical skill names (what gets stored/displayed/matched)
  ALIASES   - other spellings/abbreviations -> canonical name
  STACKS    - named tech stacks (MERN, MEAN, ...) -> their component skills
  STACK_ALIASES - phrasing variants ("MERN stack development") -> stack name

match_skills(text) keeps its EXACT original signature and behavior
contract (used by extraction_pipeline.py for every scraped job) - it
just now also recognizes aliases and any runtime-learned skills, so
existing callers get strictly more coverage, never less.

extract_resume_skills(text) is the new Phase 17 entrypoint used by
resume_parser.py: it layers stack detection and an "unknown candidate"
scanner on top of match_skills(), splitting results into KNOWN (no
Gemini needed) and UNKNOWN (candidates for optional Gemini validation).
"""

import re

# ============================================================
# SKILLS - canonical names, organized by category (comments only,
# still one flat list/pattern set under the hood - no behavior
# difference from grouping, just easier to maintain/extend).
# ============================================================

SKILLS = [
    # Programming languages
    "Python", "Java", "JavaScript", "TypeScript", "C", "C++", "C#", "Go", "Rust",
    "PHP", "Ruby", "Kotlin", "Swift", "Scala", "R",
    # Frontend
    "HTML", "CSS", "React", "Angular", "Vue", "Bootstrap", "Tailwind CSS",
    "jQuery", "Next.js",
    # Backend
    "Django", "Flask", "FastAPI", "Node.js", "Express.js", "Spring", "Spring Boot",
    "Laravel", "Rails",
    # APIs / protocols
    "REST API", "GraphQL", "SOAP", "HTTP", "HTTPS", "WebSocket", "JSON", "OAuth", "JWT",
    # Databases
    "MySQL", "PostgreSQL", "SQLite", "MongoDB", "Redis", "Oracle", "SQL Server",
    "Cassandra", "SQL",
    # Cloud / DevOps
    "AWS", "Azure", "GCP", "Docker", "Kubernetes", "Jenkins", "GitHub Actions",
    "Terraform", "CI/CD", "Linux",
    # Data / ML
    "NumPy", "Pandas", "Matplotlib", "Scikit-learn", "TensorFlow", "PyTorch",
    "OpenCV", "Machine Learning", "Data Analysis",
    # Version control
    "Git", "GitHub", "GitLab", "Bitbucket",
    # Testing
    "PyTest", "unittest", "Selenium", "Jest", "JUnit", "Cypress", "Playwright",
    # Tools
    "VS Code", "Postman", "Figma", "Jira",
    # Practices
    "Microservices", "Agile", "Scrum",
]

# ============================================================
# ALIASES - lowercase variant -> canonical name (as it appears in
# SKILLS above). Keep this easy to extend: one line per alias.
# ============================================================

ALIASES = {
    "js": "JavaScript",
    "javascript": "JavaScript",
    "ts": "TypeScript",
    "node": "Node.js",
    "nodejs": "Node.js",
    "node js": "Node.js",
    "node.js": "Node.js",
    "reactjs": "React",
    "react js": "React",
    "react.js": "React",
    "vuejs": "Vue",
    "vue.js": "Vue",
    "angularjs": "Angular",
    "mongo": "MongoDB",
    "mongo db": "MongoDB",
    "mongodb": "MongoDB",
    "postgres": "PostgreSQL",
    "postgre sql": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "py": "Python",
    "restful api": "REST API",
    "restful apis": "REST API",
    "rest apis": "REST API",
    "rest api": "REST API",
    "expressjs": "Express.js",
    "express js": "Express.js",
    "express.js": "Express.js",
    "nextjs": "Next.js",
    "next js": "Next.js",
    "tailwindcss": "Tailwind CSS",
    "tailwind": "Tailwind CSS",
    "sklearn": "Scikit-learn",
    "scikit learn": "Scikit-learn",
    "pytorch": "PyTorch",
    "tensorflow": "TensorFlow",
    "ml": "Machine Learning",
    "k8s": "Kubernetes",
    "csharp": "C#",
    "c sharp": "C#",
    "cpp": "C++",
    "golang": "Go",
    "vscode": "VS Code",
    "vs code": "VS Code",
}

# ============================================================
# STACKS - canonical stack name -> its component skills (canonical
# names, must exist in SKILLS above so matching stays consistent).
# ============================================================

STACKS = {
    "MERN": ["MongoDB", "Express.js", "React", "Node.js"],
    "MEAN": ["MongoDB", "Express.js", "Angular", "Node.js"],
    "MEVN": ["MongoDB", "Express.js", "Vue", "Node.js"],
    "PERN": ["PostgreSQL", "Express.js", "React", "Node.js"],
    "LAMP": ["Linux", "PHP", "MySQL"],
    "LEMP": ["Linux", "PHP", "MySQL"],
    "Django Stack": ["Django", "Python", "PostgreSQL"],
    "T3 Stack": ["TypeScript", "Next.js", "React"],
    "JAMstack": ["JavaScript", "REST API"],
}

# variant phrasing -> canonical stack name. "MERN stack", "MERN Stack
# development", "MERN developer" etc. all normalize to just "MERN".
STACK_ALIASES = {
    stack.lower(): stack for stack in STACKS
} | {
    f"{stack.lower()} stack": stack for stack in STACKS
} | {
    f"{stack.lower()} stack development": stack for stack in STACKS
} | {
    f"{stack.lower()} developer": stack for stack in STACKS
}

# ============================================================
# Runtime-learned skills (Phase 17, point 10): a Gemini-confirmed
# unknown term gets added here so it's recognized for the REST OF
# THIS PROCESS - not written to disk. See resume_parser.py and the
# "limitations" note in the implementation report for why this
# doesn't persist across a server restart, and what a persistent
# version would need.
# ============================================================

_LEARNED_SKILLS: set[str] = set()


def register_learned_skill(canonical_name: str) -> None:
    """Adds a Gemini-confirmed skill to the in-process dictionary so it's
    recognized by match_skills()/extract_resume_skills() for the rest of
    this server process's lifetime (resume AND job extraction both use
    the same patterns, so this benefits both)."""
    name = canonical_name.strip()
    if not name:
        return
    _LEARNED_SKILLS.add(name)
    _rebuild_patterns()


def _all_canonical_skills() -> list[str]:
    return SKILLS + sorted(_LEARNED_SKILLS)


def _build_patterns():
    patterns = [
        (skill, re.compile(r"(?<![A-Za-z0-9+#.])" + re.escape(skill) + r"(?![A-Za-z0-9+#])", re.IGNORECASE))
        for skill in _all_canonical_skills()
    ]
    # Alias patterns match the ALIAS text but report the CANONICAL name -
    # this is what lets "NodeJS", "Node JS", "node.js" etc. all collapse
    # to one "Node.js" entry instead of being treated as different skills.
    alias_patterns = [
        (canonical, re.compile(r"(?<![A-Za-z0-9+#.])" + re.escape(alias) + r"(?![A-Za-z0-9+#])", re.IGNORECASE))
        for alias, canonical in ALIASES.items()
    ]
    return patterns + alias_patterns


_PATTERNS = _build_patterns()


def _rebuild_patterns():
    global _PATTERNS
    _PATTERNS = _build_patterns()


_STACK_PATTERNS = [
    (canonical, re.compile(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", re.IGNORECASE))
    for alias, canonical in STACK_ALIASES.items()
]


def normalize_skill(term: str) -> str | None:
    """
    Resolves a raw term to its canonical skill name if it's recognized -
    via an exact alias match first, then an exact SKILLS match. Returns
    None if the term isn't recognized at all (a genuinely unknown
    candidate). This is the ONE normalization function every part of
    the app should go through (resume extraction, job extraction,
    profile skills) so "NodeJS" and "Node.js" are never treated as two
    different skills anywhere in the system.
    """
    if not term:
        return None
    key = term.strip().lower()
    if key in ALIASES:
        return ALIASES[key]
    for skill in _all_canonical_skills():
        if skill.lower() == key:
            return skill
    return None


def match_skills(text: str) -> list[str]:
    """
    UNCHANGED CONTRACT: returns canonical-cased skill names found in
    text, in dictionary order, deduped. This is what
    extraction_pipeline.py calls for every scraped job - existing
    behavior is preserved exactly, just with a larger dictionary and
    alias awareness than before, so it can only find MORE real skills
    than it used to, never fewer or differently-shaped results.
    """
    if not text:
        return []
    found = []
    seen = set()
    for canonical, pattern in _PATTERNS:
        if canonical not in seen and pattern.search(text):
            found.append(canonical)
            seen.add(canonical)
    return found


def detect_stacks(text: str) -> list[str]:
    """Returns canonical stack names (e.g. "MERN") mentioned in text,
    recognizing phrasing variants like "MERN stack", "MERN Stack
    development", "MERN developer" - all normalize to just "MERN"."""
    if not text:
        return []
    found, seen = [], set()
    for canonical, pattern in _STACK_PATTERNS:
        if canonical not in seen and pattern.search(text):
            found.append(canonical)
            seen.add(canonical)
    return found


def expand_stack_components(stack_names: list[str]) -> list[str]:
    """Given detected stack names, returns their component skills
    (deduped, canonical). E.g. ["MERN"] -> ["MongoDB", "Express.js",
    "React", "Node.js"]. This is what lets a resume saying "MERN Stack"
    match a job listing "React, Node.js, MongoDB" - see matching.py,
    which compares plain skill lists and doesn't need to know about
    stacks at all once they're expanded into components here."""
    components, seen = [], set()
    for stack in stack_names:
        for skill in STACKS.get(stack, []):
            if skill not in seen:
                components.append(skill)
                seen.add(skill)
    return components


# Section headings a resume commonly uses right before a skills list -
# deliberately a small, bounded set rather than trying to detect
# "any heading", to keep this simple and predictable.
_SKILL_SECTION_HEADINGS = re.compile(
    r"(?im)^\s*(skills?|technical skills?|programming skills?|technologies|"
    r"tools|frameworks|databases)\s*:?\s*$"
)

# A candidate token has to look plausibly like a tech term to be
# treated as an "unknown skill candidate" at all - short, mostly
# alnum/./+/#/- characters, not a common English filler word. This is
# a deliberately simple heuristic (point 7: prefer word boundaries and
# simple rules over complex NLP), not an attempt at true NLP.
_COMMON_NON_SKILL_WORDS = {
    "and", "or", "the", "with", "using", "etc", "including", "such", "as",
    "strong", "good", "excellent", "basic", "knowledge", "of", "in", "a", "an",
}


def _looks_like_skill_candidate(token: str) -> bool:
    token = token.strip(" .")
    if not token or len(token) > 40:
        return False
    if token.lower() in _COMMON_NON_SKILL_WORDS:
        return False
    if len(token.split()) > 4:
        return False  # a whole sentence fragment, not a skill name
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9+#./\- ]*$", token))


def _find_unknown_candidates(text: str) -> list[str]:
    """
    Bounded, deterministic unknown-candidate scan (point 8): looks at
    the text immediately following a recognized skills-section heading
    (Skills / Technical Skills / Technologies / Tools / Frameworks /
    Databases), splits it into comma/bullet/pipe/newline-separated
    tokens, and returns whichever tokens aren't already recognized by
    normalize_skill(). Deliberately does NOT scan the entire resume for
    candidates - only text that's already signaled as a skills list, to
    avoid flagging random resume prose as "unknown skills".
    """
    candidates, seen = [], set()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not _SKILL_SECTION_HEADINGS.match(line):
            continue
        section_lines = []
        for later_line in lines[i + 1:]:
            if not later_line.strip():
                break
            if _SKILL_SECTION_HEADINGS.match(later_line) or re.match(r"(?i)^\s*(experience|education|projects|certifications)\s*:?\s*$", later_line):
                break
            section_lines.append(later_line)
        section_text = "\n".join(section_lines)
        tokens = re.split(r"[,\u2022|/\n]", section_text)
        for raw in tokens:
            token = raw.strip(" -\t")
            if not token or not _looks_like_skill_candidate(token):
                continue
            if normalize_skill(token) is not None:
                continue
            key = token.lower()
            if key not in seen:
                candidates.append(token)
                seen.add(key)
    return candidates


def extract_resume_skills(text: str) -> dict:
    """
    Phase 17 PRIMARY resume extractor - pure Python, no Gemini call.

    Returns {"known": [...], "unknown": [...]}:
      known   - canonical skills found via the dictionary/aliases,
                PLUS any stack's component skills for a detected stack
                (e.g. "MERN Stack" in the text adds MongoDB, Express.js,
                React, Node.js to known, even if only "MERN Stack" was
                literally written).
      unknown - candidate terms from a recognized skills section that
                aren't in the dictionary - never auto-added anywhere;
                callers decide whether to send these to Gemini for
                optional validation.
    """
    if not text:
        return {"known": [], "unknown": []}

    known = match_skills(text)
    stacks = detect_stacks(text)
    if stacks:
        known_set = set(known)
        for skill in expand_stack_components(stacks):
            if skill not in known_set:
                known.append(skill)
                known_set.add(skill)

    unknown = _find_unknown_candidates(text)
    return {"known": known, "unknown": unknown}
