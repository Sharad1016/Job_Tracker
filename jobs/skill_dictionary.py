"""
Phase 12 (deterministic-first extraction): a small, extensible skill
dictionary used to detect technologies mentioned in a job's raw text
WITHOUT calling Gemini. To add more skills, just extend the SKILLS
list below - matching is case-insensitive and uses boundary-aware
regex so short tokens like "R" or "Go" don't match inside unrelated
words (e.g. "R" won't match inside "Developer").
"""

import re

SKILLS = [
    "Python", "Django", "Flask", "FastAPI", "Java", "C++", "C#", "JavaScript",
    "TypeScript", "React", "Angular", "Vue", "Node.js", "Express.js", "SQL",
    "MySQL", "PostgreSQL", "MongoDB", "Redis", "Docker", "Kubernetes", "AWS",
    "Azure", "GCP", "Git", "GitHub", "REST API", "GraphQL", "HTML", "CSS",
    "Linux", "CI/CD", "Machine Learning", "Data Analysis", "Pandas", "NumPy",
    "TensorFlow", "PyTorch", "Selenium", "Playwright", "Go", "Ruby", "PHP",
    "Swift", "Kotlin", "R", "Scala", "Spring Boot", "Microservices", "Agile", "Scrum",
]

_PATTERNS = [
    (skill, re.compile(r"(?<![A-Za-z0-9+#.])" + re.escape(skill) + r"(?![A-Za-z0-9+#])", re.IGNORECASE))
    for skill in SKILLS
]


def match_skills(text: str) -> list[str]:
    """Returns canonical-cased skill names found in text, dictionary order, deduped."""
    if not text:
        return []
    return [skill for skill, pattern in _PATTERNS if pattern.search(text)]
