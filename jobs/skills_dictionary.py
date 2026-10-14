"""
Skills dictionary for deterministic skill extraction from job postings.
Avoids calling Gemini just to identify standard technologies.
"""

import re

# Comprehensive skill dictionary
SKILLS_DICT = {
    # Languages
    "Python": ["python"],
    "JavaScript": ["javascript", "js"],
    "TypeScript": ["typescript", "ts"],
    "Java": ["java"],
    "C++": ["c\\+\\+", "cpp"],
    "C#": ["c#", "csharp"],
    "Go": ["golang", "go"],
    "Rust": ["rust"],
    "PHP": ["php"],
    "Ruby": ["ruby"],
    "Kotlin": ["kotlin"],
    "Swift": ["swift"],
    "R": ["\\br\\b"],  # word boundary to avoid matching "error"

    # Frontend Frameworks
    "React": ["react", "react\\.js", "reactjs"],
    "Vue": ["vue", "vue\\.js", "vuejs"],
    "Angular": ["angular"],
    "Svelte": ["svelte"],
    "Next.js": ["next\\.js", "nextjs"],
    "Nuxt": ["nuxt"],

    # Backend Frameworks
    "Django": ["django"],
    "Flask": ["flask"],
    "FastAPI": ["fastapi"],
    "Express.js": ["express", "express\\.js", "expressjs"],
    "Spring": ["spring", "spring boot"],
    "Laravel": ["laravel"],
    "Rails": ["rails", "ruby on rails"],
    "ASP.NET": ["asp\\.net", "aspnet"],
    "Node.js": ["node", "node\\.js", "nodejs"],

    # Databases
    "SQL": ["sql"],
    "PostgreSQL": ["postgresql", "postgres"],
    "MySQL": ["mysql"],
    "MongoDB": ["mongodb", "mongo"],
    "Redis": ["redis"],
    "Elasticsearch": ["elasticsearch"],
    "DynamoDB": ["dynamodb"],
    "Cassandra": ["cassandra"],
    "Oracle": ["oracle"],
    "SQLite": ["sqlite"],

    # Cloud & DevOps
    "AWS": ["aws", "amazon web services", "amazon web service"],
    "Azure": ["azure", "microsoft azure"],
    "GCP": ["gcp", "google cloud", "google cloud platform"],
    "Docker": ["docker"],
    "Kubernetes": ["kubernetes", "k8s"],
    "Jenkins": ["jenkins"],
    "CI/CD": ["ci/cd", "cicd", "continuous integration"],
    "Terraform": ["terraform"],
    "CloudFormation": ["cloudformation"],

    # APIs & Protocols
    "REST API": ["rest", "rest api", "restful"],
    "GraphQL": ["graphql"],
    "gRPC": ["grpc"],
    "SOAP": ["soap"],
    "WebSocket": ["websocket", "websockets"],

    # Data & ML
    "Machine Learning": ["machine learning", "ml"],
    "Deep Learning": ["deep learning"],
    "TensorFlow": ["tensorflow"],
    "PyTorch": ["pytorch"],
    "Scikit-learn": ["scikit-learn", "sklearn"],
    "Pandas": ["pandas"],
    "NumPy": ["numpy"],
    "Data Science": ["data science"],
    "NLP": ["nlp", "natural language processing"],
    "Computer Vision": ["computer vision"],

    # Version Control
    "Git": ["git"],
    "GitHub": ["github"],
    "GitLab": ["gitlab"],
    "Bitbucket": ["bitbucket"],

    # Testing
    "Jest": ["jest"],
    "pytest": ["pytest"],
    "unittest": ["unittest"],
    "Mocha": ["mocha"],
    "Selenium": ["selenium"],
    "JUnit": ["junit"],

    # Other Tools
    "Linux": ["linux"],
    "AWS Lambda": ["lambda", "aws lambda"],
    "Microservices": ["microservices"],
    "Agile": ["agile"],
    "Scrum": ["scrum"],
    "Jira": ["jira"],
    "Figma": ["figma"],
    "Webpack": ["webpack"],
    "Babel": ["babel"],
    "Sass": ["sass", "scss"],
    "HTML": ["html", "html5"],
    "CSS": ["css", "css3"],
}


def extract_skills_from_text(text: str) -> list[str]:
    """
    Extract known skills from text using the skills dictionary.
    
    Args:
        text: Job description or resume text to search
        
    Returns:
        List of skill names found in the text (deduplicated)
    """
    if not text:
        return []
    
    text_lower = text.lower()
    found_skills = set()
    
    for skill_name, patterns in SKILLS_DICT.items():
        for pattern in patterns:
            # Use word boundary regex to avoid partial matches
            # e.g., "Python" matches "Python" but not "Pythonic"
            regex_pattern = r"\b" + pattern + r"\b"
            if re.search(regex_pattern, text_lower, re.IGNORECASE):
                found_skills.add(skill_name)
                break  # Found this skill, no need to check other patterns
    
    # Return sorted for consistency
    return sorted(list(found_skills))
