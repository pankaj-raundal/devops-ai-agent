# GitHub Models API — Limitations & Claude Availability

## Two Separate AI Platforms

GitHub operates two distinct AI platforms under the Copilot umbrella:

### 1. GitHub Copilot Chat (IDE-only)

- **Endpoint:** `api.githubcopilot.com`
- **Available models:** Claude Opus 4.6, Claude Sonnet 4.6, GPT-5.4, GPT-5.3-Codex, etc.
- **Auth:** Internal OAuth session token managed by the IDE
- **Access:** VS Code, JetBrains, GitHub.com web chat only
- **Cannot be called from:** Scripts, CLI tools, REST API, custom applications
- **Reason:** Models are licensed per-seat for the Copilot product. External API access is blocked to prevent bypassing the product.

### 2. GitHub Models (Developer API)

- **Endpoint:** `models.github.ai/inference/v1`
- **Available models:** GPT-4o, GPT-4o-mini, Llama, Mistral, Phi — **no Claude models**
- **Auth:** OAuth token (`gh auth token`) or PAT with `models:read` scope
- **Access:** Open API — any script, app, or CLI tool
- **Reason Claude is absent:** Anthropic has not made Claude available on the GitHub Models marketplace. Claude is only accessible via Anthropic's own API or through the Copilot Chat product (special licensing deal).

## Impact on This Project

| Model | GitHub Models API | Copilot Chat (IDE) | Our Pipeline |
|-------|-------------------|--------------------|--------------|
| GPT-4o | ✅ Available | ✅ Available | ✅ **Used** |
| Claude Opus 4.6 | ❌ Not available | ✅ Available | ❌ Cannot use |
| Claude Sonnet 4.6 | ❌ Not available | ✅ Available | ❌ Cannot use |
| GPT-4o-mini | ✅ Available | ✅ Available | ✅ Can use |

## Verified Test Results (March 2026)

```
# OAuth token (gho_...) against GitHub Models API:
gpt-4o          → 200 OK ✓
gpt-4o-mini     → 200 OK ✓
claude-opus-4   → 404 "Unknown model"   (not on the platform)

# Same token against Copilot Chat API:
claude-opus-4   → 400 "PATs not supported"  (IDE-only)
gpt-4o          → 403 "Forbidden"            (IDE-only)
```

## Alternatives for Claude Access

| Option | How | Notes |
|--------|-----|-------|
| **Anthropic API** | Set `ANTHROPIC_API_KEY`, use `provider: "anthropic"` | Requires API key + billing |
| **GPT-4o via GitHub Models** | Current setup — free with Copilot subscription | Working now |
| **Wait for marketplace** | Anthropic may join GitHub Models in the future | Unknown timeline |

## Configuration

Current working config in `config/config.local.yaml`:

```yaml
ai_agent:
  provider: "copilot"
  model: "gpt-4o"
  review_model: "gpt-4o"
```

Auth is handled by `gh auth token` (OAuth) — no PAT needed.

## References

- [GitHub Models — Prototyping with AI models](https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models)
- [GitHub Copilot — Model hosting](https://docs.github.com/en/copilot/reference/ai-models/model-hosting)
