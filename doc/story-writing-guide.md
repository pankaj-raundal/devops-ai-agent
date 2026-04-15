# Story Writing Guide for DevOps AI Agent

> How to write Azure DevOps stories that the AI pipeline can actually implement.

---

## Who Is This For?

Project managers, team leads, and developers who create stories in Azure DevOps for teams working in **C#, Java, PHP, .NET, Python, SQL/MySQL**, and **localization workflows**. If your team uses the `dai` pipeline to automate story-to-code, the quality of the story directly determines whether the AI produces working code or rejects it.

---

## How the System Uses Your Story

The pipeline reads these fields from every Azure DevOps work item:

| Field | How the AI Uses It |
|-------|--------------------|
| **Title** | First-pass understanding of what to build |
| **Description** | Primary input — the AI reads this as its requirements spec |
| **Acceptance Criteria** | Defines "done" — the AI uses this to decide what to test and validate |
| **Comments/Discussion** | Additional context — clarifications, links, decisions |
| **Tags** | Must include `auto` tag to be picked up by the pipeline |
| **Work Item Type** | Helps the AI judge scope (Bug vs. User Story vs. Task) |

### The Quality Gate

Every story is scored 1-10 before the AI spends any time implementing. Stories scoring below **4/10** are rejected with coaching feedback posted back to the story. The scoring criteria:

| Criteria | Points | What Earns It |
|----------|--------|---------------|
| Description exists | 1 | At least 20 characters |
| Description has detail | 1 | At least 200 characters |
| Acceptance criteria present | 1 | Keywords: given, when, then, should, expected |
| Acceptance criteria detailed | 1 | Multiple criteria or structured format |
| AI confidence is high | 1 | Story is unambiguous — AI knows what to do |
| Specific areas identified | 1 | AI can identify which files/modules to change |
| No open questions | 1 | AI doesn't need to ask clarifying questions |
| Known complexity | 1 | Complexity is clear (not "unknown" or "unclear") |
| Description length bonus | 2 | Description > 200 chars |

**Minimum to proceed: 4/10.** Practically, a story with a title and one sentence will be rejected. A story with 3-4 sentences of description and basic acceptance criteria will pass.

---

## What Makes a Bad Story (for AI)

### The Title-Only Story

```
Title: Fix translation issue
Description: (empty)
Acceptance Criteria: (empty)
```

**Score: 0/10 — REJECTED.** The AI has no idea what "translation issue" means, which file to look at, or what "fixed" looks like.

### The Vague Story

```
Title: Update the connector
Description: The connector needs to be updated because it's not working properly.
Acceptance Criteria: It should work.
```

**Score: 2/10 — REJECTED.** "Not working properly" and "should work" give the AI nothing to act on. Which connector? What's broken? What does "working" mean?

### The Kitchen-Sink Story

```
Title: Refactor authentication, add caching, migrate database, update UI, and fix localization
Description: We need to improve the whole module. Everything should be better.
```

**Score: 3/10 — REJECTED.** The AI works best on focused, single-responsibility changes. This is 5 stories crammed into one.

---

## What Makes a Good Story (for AI)

The AI needs three things to succeed:

1. **What** — a clear description of the change
2. **Where** — which files, modules, classes, or database tables are involved
3. **Done** — what "finished" looks like (acceptance criteria)

---

## Ideal Story Templates by Technology

### PHP / Drupal

```
Title: Add language fallback logic to TranslationProvider service

Description:
The TranslationProvider::getTranslation() method in the
lionbridge_translation_provider module currently returns NULL when a
translation is not available in the requested language. It should fall
back to the default language (English) before returning NULL.

File: src/Service/TranslationProvider.php
Method: getTranslation(string $langcode, int $entityId): ?string

Current behavior: Returns NULL if no translation exists for $langcode.
Expected behavior: If no translation exists for $langcode, check for
'en' translation. If that also doesn't exist, return NULL.

Acceptance Criteria:
- Given a request for a German translation that doesn't exist
- When getTranslation('de', 123) is called
- Then it should return the English translation if available
- And return NULL only if no English translation exists either
- Existing translations in the requested language should still be returned directly
```

**Score: 9/10.** The AI knows exactly which file, which method, the current behavior, the expected behavior, and the edge cases.

### C# / .NET

```
Title: Add retry logic to ExternalApiClient.SendAsync

Description:
The ExternalApiClient in Connectors.Integration project does not retry
on transient HTTP failures (408, 429, 502, 503, 504). Add Polly-based
retry with exponential backoff.

File: src/Connectors.Integration/Services/ExternalApiClient.cs
Method: Task<HttpResponseMessage> SendAsync(HttpRequestMessage request)

Requirements:
- Use Polly library (already in project dependencies)
- Retry up to 3 times with exponential backoff (2s, 4s, 8s)
- Only retry on transient status codes: 408, 429, 502, 503, 504
- Log each retry attempt at Warning level using ILogger
- Do not retry on 4xx client errors (except 408, 429)

Acceptance Criteria:
- When the API returns 503, the client retries up to 3 times before throwing
- When the API returns 400, the client does NOT retry
- When the API returns 429, the client retries with backoff
- All retry attempts are logged with the attempt number and status code
```

### Java

```
Title: Add batch insert method to TranslationRepository

Description:
The TranslationRepository class currently only supports inserting one
translation record at a time via save(). For the nightly sync job that
processes 5000+ records, we need a batch insert method.

File: src/main/java/com/company/connectors/repository/TranslationRepository.java

Requirements:
- Add method: void saveBatch(List<TranslationEntity> entities)
- Use JDBC batch insert (not JPA saveAll) for performance
- Batch size: 500 records per executeBatch() call
- Wrap in a single transaction — rollback all on failure
- Log total inserted count at INFO level

Acceptance Criteria:
- Given a list of 1200 TranslationEntity records
- When saveBatch() is called
- Then 3 batch executions occur (500 + 500 + 200)
- And all 1200 records are inserted in a single transaction
- And if any batch fails, no records are committed
```

### Python

```
Title: Add CSV export to translation status report

Description:
The translation_report module currently only prints status to console.
Add a CSV export option that writes the report to a file.

File: src/reports/translation_report.py
Function: generate_report(project_id: int, output: str = "console")

Requirements:
- Add output="csv" option to generate_report()
- CSV columns: project_id, language_code, source_text, translated_text, status, updated_at
- Output file: reports/{project_id}_translation_status.csv
- Use Python csv module (no pandas dependency)
- Include UTF-8 BOM for Excel compatibility with non-Latin characters

Acceptance Criteria:
- When generate_report(42, output="csv") is called
- Then reports/42_translation_status.csv is created
- And the CSV opens correctly in Excel with Japanese/German characters
- When output="console" (default), behavior is unchanged
```

### SQL / MySQL

```
Title: Add index on translations table for language+status lookup

Description:
The query in TranslationService.getByLanguageAndStatus() is doing a full
table scan on the translations table (currently 2M+ rows). Add a composite
index to speed up the WHERE clause.

Table: translations
Slow query: SELECT * FROM translations WHERE language_code = ? AND status = ?

Requirements:
- Add composite index: (language_code, status) on translations table
- Migration file: database/migrations/2026_04_01_add_lang_status_index.sql
- Include both UP (CREATE INDEX) and DOWN (DROP INDEX) statements
- Index name: idx_translations_lang_status

Acceptance Criteria:
- The migration runs without error on MySQL 8.0
- The EXPLAIN plan for the query shows index usage instead of full scan
- The migration is reversible (DOWN drops the index)
```

### Localization Workflow

```
Title: Auto-detect source language when creating translation job

Description:
When a new translation job is created via the TranslationJobService,
the source language is always hardcoded to 'en'. For projects that have
content in other source languages (DE, FR, JA), the service should
detect the source language from the content entity.

File: src/Service/TranslationJobService.php
Method: createJob(int $entityId, array $targetLanguages): TranslationJob

Current behavior: source_language is always set to 'en'.
Expected behavior: Read the entity's original language from the
content_languages table. If not found, default to 'en'.

SQL involved:
  SELECT source_language FROM content_languages WHERE entity_id = ?

Acceptance Criteria:
- Given an entity with source_language = 'de' in content_languages
- When createJob(entityId, ['en', 'fr']) is called
- Then the job's source_language is 'de', not 'en'
- Given an entity with no entry in content_languages
- When createJob(entityId, ['fr']) is called
- Then the job's source_language defaults to 'en'
- No existing translation jobs are affected
```

---

## Story Sizing — What Works and What Doesn't

### AI works well for:

| Story Type | Example | Why It Works |
|-----------|---------|--------------|
| **Add a method/function** | "Add saveBatch() to TranslationRepository" | Clear scope, single file, testable |
| **Bug fix with reproduction steps** | "getTranslation() returns NULL instead of fallback" | Current vs. expected behavior is clear |
| **Add configuration/settings** | "Add retry count to appsettings.json and use it in ApiClient" | Mechanical change, well-defined |
| **Database migration** | "Add index on translations.language_code" | SQL is deterministic |
| **Add validation** | "Validate language_code is ISO 639-1 before creating job" | Input/output is clear |
| **DTO/model changes** | "Add lastModifiedBy field to TranslationEntity" | Simple data structure change |
| **API endpoint addition** | "Add GET /api/translations/{id}/status endpoint" | Request/response is specifiable |

### AI struggles with:

| Story Type | Example | Why It's Hard |
|-----------|---------|---------------|
| **UI/UX redesign** | "Make the dashboard look better" | Subjective, visual, needs design mockups |
| **Performance tuning** | "Make the app faster" | Requires profiling, measurement, iteration |
| **Architecture refactoring** | "Refactor to microservices" | Too broad, affects everything |
| **Cross-system integration** | "Integrate with SAP" | Needs API docs, credentials, testing against live system |
| **"Investigate" stories** | "Figure out why translations fail sometimes" | No clear code change — it's research |

---

## Quick Checklist for Story Authors

Before tagging a story with `auto`, check:

- [ ] **Description has at least 3-4 sentences** explaining what needs to change
- [ ] **File or module is mentioned** — the AI needs to know where to look
- [ ] **Current behavior is described** (for bugs) or **requirement is specific** (for features)
- [ ] **Acceptance criteria exist** — at least 2-3 "when X then Y" statements
- [ ] **Scope is single-responsibility** — one logical change, not a bundle
- [ ] **No ambiguous words** without definition ("improve", "fix", "update", "handle properly")
- [ ] **Tech details are included** when relevant — table names, class names, method signatures

---

## How the Pipeline Responds to Story Quality

| Score | Pipeline Action |
|-------|----------------|
| **8-10** | Implements immediately with high confidence |
| **5-7** | Implements but may ask clarifying questions in the code review |
| **4** | Borderline — implements but results may need more manual review |
| **1-3** | **Rejects the story.** Posts feedback to Azure DevOps explaining what's missing. Moves story to "Evaluation" state. |
| **0** | Title-only story — rejected with a "please add a description" note |

When a story is rejected, the feedback comment posted to Azure DevOps includes:
- The score and why it failed
- Specific issues found (missing description, no acceptance criteria, ambiguous scope)
- AI's clarifying questions (if any)
- Suggestions for improvement

The PM or developer fixes the story, and the pipeline picks it up on the next run.

---

## Localization-Specific Guidance

Since your team works heavily with localization workflows, here are patterns the AI handles well:

### Language/Locale Stories
Always specify: source language, target language(s), the table or entity involved, and the expected behavior per language.

### Translation Status Stories
Mention the exact statuses involved (e.g., `pending`, `in_progress`, `completed`, `failed`) and which transitions are changing.

### Fallback Logic Stories
Describe the fallback chain explicitly: "Try `de-AT` → `de` → `en` → NULL" rather than "use the right language."

### Content Sync Stories
Specify: the source system, destination, sync direction, conflict resolution strategy, and batch size.

### Character Encoding Stories
Mention the specific encoding (UTF-8, UTF-16, Shift-JIS) and the formats involved (CSV, XML, JSON, database).

---

## Summary

The AI is a **literal executor, not a mind reader**. It does exactly what the story describes. The difference between a failed pipeline run and a working feature branch is almost always the quality of the story — not the quality of the AI.

Write stories as if you're explaining the task to a skilled developer who just joined the team today: they know the language and framework, but they don't know your codebase. That's exactly the AI's situation.
