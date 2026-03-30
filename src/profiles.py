"""Framework profiles — configurable per-language/framework defaults.

Each profile provides framework-specific prompts, tool names, coding standards,
and test commands.  The active profile is selected via ``project.framework``
in config (default: ``drupal``).

To add a new framework, just add an entry to ``PROFILES``.
"""

from __future__ import annotations

PROFILES: dict[str, dict] = {
    # ── Drupal / PHP ──
    "drupal": {
        "language": "PHP",
        "language_version": "8.4+",
        "framework_label": "Drupal",
        "package_type": "module",
        "coding_standard": "PSR-12 with Drupal conventions",
        "linter": "phpcs --standard=Drupal,DrupalPractice",
        "test_tool": "phpunit",
        "static_analysis": "phpstan",
        "file_extensions": [".php", ".module", ".install", ".inc", ".theme", ".test"],
        "dev_notes": (
            "- Use `{cli_prefix}` for Drush commands\n"
            "- Use `{cli_prefix} cr` to clear cache after changes\n"
            "- Follow Drupal coding standards (PSR-12 with Drupal conventions)\n"
            "- Ensure PHP 8.4 compatibility\n"
            "- Use dependency injection (services.yml), never global functions"
        ),
        "system_prompt_prefix": (
            "You are an expert Drupal developer. You implement features and fix bugs "
            "in Drupal modules following Drupal coding standards (PSR-12 with Drupal "
            "conventions). You write clean, tested, secure PHP code compatible with PHP 8.4."
        ),
        "review_criteria": (
            "1. Correctness — does the code do what the story requires?\n"
            "2. Security — OWASP Top 10, SQL injection, XSS, access control.\n"
            "3. Drupal standards — PSR-12, Drupal coding conventions, proper DI.\n"
            "4. PHP 8.4 compatibility.\n"
            "5. Performance — unnecessary DB queries, N+1 issues, missing caching.\n"
            "6. Test coverage — are there tests for the changes?"
        ),
        "checks": ["phpunit", "phpcs", "phpstan", "drush_cr"],
    },

    # ── Python ──
    "python": {
        "language": "Python",
        "language_version": "3.10+",
        "framework_label": "Python",
        "package_type": "package",
        "coding_standard": "PEP 8 (enforced via ruff/black)",
        "linter": "ruff check",
        "test_tool": "pytest",
        "static_analysis": "mypy",
        "file_extensions": [".py", ".pyi"],
        "dev_notes": (
            "- Use type hints (Python 3.10+ syntax: `X | None`, `list[str]`)\n"
            "- Use `from __future__ import annotations` for forward references\n"
            "- Use `dataclass` for data containers\n"
            "- Use `logging` module (one logger per module)\n"
            "- Follow PEP 8 / ruff conventions"
        ),
        "system_prompt_prefix": (
            "You are an expert Python developer. You implement features and fix bugs "
            "following PEP 8 conventions. You write clean, typed, tested, secure Python "
            "code compatible with Python 3.10+."
        ),
        "review_criteria": (
            "1. Correctness — does the code do what the story requires?\n"
            "2. Security — OWASP Top 10, injection, input validation.\n"
            "3. Python standards — PEP 8, type hints, clean imports.\n"
            "4. Python 3.10+ compatibility.\n"
            "5. Performance — efficient algorithms, no unnecessary I/O.\n"
            "6. Test coverage — are there tests for the changes?"
        ),
        "checks": ["pytest", "ruff", "mypy"],
    },

    # ── React / TypeScript ──
    "react": {
        "language": "TypeScript",
        "language_version": "5.0+",
        "framework_label": "React",
        "package_type": "component",
        "coding_standard": "ESLint + Prettier",
        "linter": "eslint",
        "test_tool": "jest",
        "static_analysis": "tsc --noEmit",
        "file_extensions": [".ts", ".tsx", ".js", ".jsx", ".css", ".scss"],
        "dev_notes": (
            "- Use functional components with hooks\n"
            "- Use TypeScript strict mode\n"
            "- Follow ESLint + Prettier conventions\n"
            "- Use proper state management (useState/useReducer/context)\n"
            "- Ensure accessibility (a11y) compliance"
        ),
        "system_prompt_prefix": (
            "You are an expert React/TypeScript developer. You implement features and "
            "fix bugs in React applications following modern best practices. You write "
            "clean, typed, tested, accessible TypeScript code compatible with React 18+."
        ),
        "review_criteria": (
            "1. Correctness — does the code do what the story requires?\n"
            "2. Security — XSS prevention, proper sanitization, no dangerouslySetInnerHTML.\n"
            "3. React standards — hooks rules, proper key usage, no prop drilling.\n"
            "4. TypeScript strict mode compliance.\n"
            "5. Performance — memoization, lazy loading, no unnecessary re-renders.\n"
            "6. Test coverage — are there tests for the changes?"
        ),
        "checks": ["jest", "eslint", "tsc"],
    },

    # ── Java / Spring ──
    "java": {
        "language": "Java",
        "language_version": "17+",
        "framework_label": "Spring Boot",
        "package_type": "module",
        "coding_standard": "Google Java Style",
        "linter": "checkstyle",
        "test_tool": "mvn test",
        "static_analysis": "spotbugs",
        "file_extensions": [".java", ".xml", ".properties", ".yml"],
        "dev_notes": (
            "- Use Spring dependency injection (@Autowired, constructor injection)\n"
            "- Follow Google Java Style Guide\n"
            "- Use JUnit 5 for tests\n"
            "- Use Lombok where appropriate\n"
            "- Ensure Java 17+ compatibility"
        ),
        "system_prompt_prefix": (
            "You are an expert Java/Spring Boot developer. You implement features and "
            "fix bugs following Google Java Style Guide. You write clean, tested, secure "
            "Java code compatible with Java 17+ and Spring Boot 3."
        ),
        "review_criteria": (
            "1. Correctness — does the code do what the story requires?\n"
            "2. Security — OWASP Top 10, SQL injection, input validation.\n"
            "3. Java/Spring standards — proper DI, annotations, error handling.\n"
            "4. Java 17+ compatibility.\n"
            "5. Performance — proper use of streams, caching, connection pools.\n"
            "6. Test coverage — are there JUnit tests for the changes?"
        ),
        "checks": ["mvn_test", "checkstyle", "spotbugs"],
    },

    # ── .NET / C# ──
    "dotnet": {
        "language": "C#",
        "language_version": "12+",
        "framework_label": ".NET",
        "package_type": "project",
        "coding_standard": "Microsoft C# Coding Conventions",
        "linter": "dotnet format",
        "test_tool": "dotnet test",
        "static_analysis": "dotnet build /warnaserror",
        "file_extensions": [".cs", ".csproj", ".json", ".razor"],
        "dev_notes": (
            "- Use dependency injection (Microsoft.Extensions.DependencyInjection)\n"
            "- Follow Microsoft C# Coding Conventions\n"
            "- Use xUnit or NUnit for tests\n"
            "- Use nullable reference types\n"
            "- Ensure .NET 8+ compatibility"
        ),
        "system_prompt_prefix": (
            "You are an expert .NET/C# developer. You implement features and fix bugs "
            "following Microsoft C# coding conventions. You write clean, tested, secure "
            "C# code compatible with .NET 8+ and C# 12."
        ),
        "review_criteria": (
            "1. Correctness — does the code do what the story requires?\n"
            "2. Security — OWASP Top 10, input validation, proper auth.\n"
            "3. .NET standards — proper DI, async/await patterns, nullable types.\n"
            "4. .NET 8+ / C# 12 compatibility.\n"
            "5. Performance — async I/O, proper disposal, no blocking calls.\n"
            "6. Test coverage — are there tests for the changes?"
        ),
        "checks": ["dotnet_test", "dotnet_format", "dotnet_build"],
    },

    # ── Angular ──
    "angular": {
        "language": "TypeScript",
        "language_version": "5.0+",
        "framework_label": "Angular",
        "package_type": "component",
        "coding_standard": "Angular Style Guide + ESLint",
        "linter": "ng lint",
        "test_tool": "ng test",
        "static_analysis": "tsc --noEmit",
        "file_extensions": [".ts", ".html", ".scss", ".css", ".spec.ts"],
        "dev_notes": (
            "- Use Angular CLI conventions (ng generate)\n"
            "- Follow Angular Style Guide\n"
            "- Use standalone components where possible\n"
            "- Use RxJS properly (unsubscribe, async pipe)\n"
            "- Use Angular signals for reactivity"
        ),
        "system_prompt_prefix": (
            "You are an expert Angular/TypeScript developer. You implement features and "
            "fix bugs in Angular applications following the Angular Style Guide. You write "
            "clean, typed, tested TypeScript code compatible with Angular 17+."
        ),
        "review_criteria": (
            "1. Correctness — does the code do what the story requires?\n"
            "2. Security — XSS prevention, proper sanitization, CSP compliance.\n"
            "3. Angular standards — style guide, proper module/component structure.\n"
            "4. TypeScript strict mode compliance.\n"
            "5. Performance — lazy loading, OnPush change detection, trackBy.\n"
            "6. Test coverage — are there tests for the changes?"
        ),
        "checks": ["ng_test", "ng_lint", "tsc"],
    },
}


def get_profile(config: dict) -> dict:
    """Get the active framework profile from config.

    Falls back to ``"drupal"`` if not set. If an unknown framework is
    specified, returns a generic profile built from the framework name.
    """
    framework = config.get("project", {}).get("framework", "drupal")
    if framework in PROFILES:
        return PROFILES[framework]

    # Unknown framework — return a generic profile.
    return {
        "language": framework.title(),
        "language_version": "",
        "framework_label": framework.title(),
        "package_type": "project",
        "coding_standard": "project conventions",
        "linter": "",
        "test_tool": "",
        "static_analysis": "",
        "file_extensions": [],
        "dev_notes": f"- Follow {framework.title()} coding conventions",
        "system_prompt_prefix": (
            f"You are an expert {framework.title()} developer. You implement features "
            f"and fix bugs following best practices. You write clean, tested, secure code."
        ),
        "review_criteria": (
            "1. Correctness — does the code do what the story requires?\n"
            "2. Security — OWASP Top 10, injection, input validation.\n"
            "3. Coding standards — follow project conventions.\n"
            "4. Performance — efficient algorithms.\n"
            "5. Test coverage — are there tests for the changes?"
        ),
        "checks": [],
    }
