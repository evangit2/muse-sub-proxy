# muse-sub-proxy (standalone)

Expose Meta API access through OpenAI-compatible endpoints. **No `muse` CLI anywhere** — pure Python stdlib talking direct REST to `https://api.meta.ai/v1`.

```
External tool (OpenCode/Hermes/etc)     muse-sub-proxy              Meta API
  POST /v1/chat/completions  --->  POST /v1/responses (Bearer LLM|) --->  Muse Spark
  OpenAI JSON (+SSE)         <---  output_text translated     <---
```

## Setup

No CLI needed at runtime. You need one Meta API key (`LLM|...`), resolved as:

1. `MUSE_SUB_API_KEY` env var (Linux/portable), or
2. macOS keychain item `ai.meta.dev.credentials` / account `meta` (read via
   `security` CLI — one-time Always-Allow approval), or
3. `~/.config/muse-sub-proxy/api_key.txt` (mode 0600).

Run:

```
python3 proxy.py                        # :8920
SUB_PROXY_PORT=8921 python3 proxy.py    # custom port
curl -s localhost:8920/v1/models
```

LaunchAgent: `cp com.muse-sub-proxy.plist ~/Library/LaunchAgents/ && launchctl load ...`

## Endpoints

- `GET /v1/models` — live list from Meta, filtered to known Muse Spark ids
- `POST /v1/chat/completions` — OpenAI chat format, `stream: true/false`

## OpenCode / Hermes example

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "muse-sub": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Muse Sub",
      "options": { "baseURL": "http://127.0.0.1:8920/v1" },
      "models": {
        "muse-spark-1.2": { "name": "Muse Spark 1.2 (sub)", "limit": { "context": 1048576, "output": 131072 } }
      }
    }
  }
}
```

Hermes: provider entry `muse-sub` with `base_url: http://127.0.0.1:8920/v1`
(set via `hermes config set providers.muse-sub.base_url ...`).

## Reverse-engineering notes (how we got here)

- Subscription page says "10–50 **prompts** every 5 hours" — weighted user
  prompts, each fanning out into dozens/hundreds of internal model calls.
- The `muse` launcher (`~/.local/bin/muse`, plain bash) contains the whole
  OAuth device flow in cleartext: `https://auth.meta.com` +
  `/oidc/device/authorization/` + `/oidc/device/token/`, public
  `client_id 1031625952748946`. No scopes accepted
  (`invalid_scope` if you try).
- A self-minted device token (`dca:...`) gets `invalid_api_key` on **every**
  `api.meta.ai` path — the raw OAuth token is NOT an API credential.
- `muse login` stores `{"api_key": "LLM|...", "access_token": "dca:..."}`
  in keychain service `ai.meta.dev.credentials`. The `LLM|` key is what
  actually authorizes `/v1/responses` + `/v1/models` (both verified 200).
- The mint endpoint for that key was never found (no exchange paths on either
  host; launcher handles downloads only). So onboarding a fresh machine still
  takes one Meta OAuth login to populate the key — after that the CLI is
  unnecessary: this proxy reads the key and talks REST directly.
- Keychain reads from a new binary trigger one macOS approval prompt;
  grant Always Allow once and `security find-generic-password` works headless.
- `muse serve`/`schema` show the CLI's MSP plane (`session/*`, `turn/*`),
  irrelevant once you go direct REST.

## Value test

Each proxied request = one metered sub prompt (Everyday $5: 10–50 / 5h).
Compare drained prompts vs pay-as-you-go token cost to find the crossover.

---
MIT — not affiliated with Meta. Subscription use outside the Muse Code CLI
violates Meta's sub terms; you accept that risk by running this.
