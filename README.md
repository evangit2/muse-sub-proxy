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

- `GET /v1/models` — live ids from Meta (statics are fallback only, so new
  models survive CLI/API updates with no proxy change)
- `GET /v1/usage` — latest subscription quota snapshot (see below), persisted
  to `~/.config/muse-sub-proxy/usage.json` after every chat completion
- `POST /v1/chat/completions` — OpenAI chat format, `stream: true/false`.
  Both modes return the token `usage` object; both also carry an extra
  `subscription` block (quota snapshot for that exact call).

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
- **Usage: yes, via SSE.** `strings` on `muse-bin-*` shows
  `SubscriptionUsageSnapshot{window, weekly}` /
  `SubscriptionWindowSnapshot{window_duration_mins, used_percent, resets_at}` /
  `SubscriptionWeeklySnapshot{used_percent, resets_at}` sitting next to the
  Responses stream-event structs. MITM (`--base-url` at a logging forwarder +
  one `muse exec`) confirmed: every streamed `POST /v1/responses` ends with
  `event: response.subscription_usage`, e.g.
  `{"subscription":{"tier":"…","weekly":{"resets_at":…,"used_percent":0},
  "window":{"resets_at":…,"used_percent":1,"window_duration_mins":300}}}`.
  `window_duration_mins: 300` = the 5-hour prompt bucket. Trigger = `stream`
  alone — no special headers needed (verified: bare streamed call returns it,
  non-stream never does). The proxy therefore always streams upstream and
  exposes the snapshot per-call + via `GET /v1/usage`.
- No REST usage endpoint exists: every `/v1/usage`, `/v1/billing`,
  `/v1/subscription`, `/v1/me`-style guess 404s; response headers carry no
  quota fields. Subscription *management* stays web-only (Accounts Center;
  `/upgrade` opens `accountscenter.meta.com/muse_code/` with an `ep=` tag;
  `/subscription` is retired). Bonus find: `GET https://api.meta.ai/muse-code/models`
  (note: NOT under `/v1/`) returns the model catalog with `muse-code` metadata
  (capabilities, limits, reasoning variants).

## Real session mechanics (MITM'd live sessions)

- One session = one UUID: `prompt_cache_key: tbh:main:<uuid>`, stable across
  prompts (`muse exec --session-id <uuid>` continues headlessly). No other
  endpoints during a run: `GET /muse-code/models` once, then only
  `POST /v1/responses` (streamed).
- Every turn re-sends everything (`store: false`, no `previous_response_id`):
  ~37KB `instructions` + ~33KB workspace-identity reminder + full history
  (user msgs, reasoning items, function_call/outputs, assistant msgs).
- Turn 1 full-price (~23K tokens uncached); turns 2+ ~99% prefix-cached.
  A follow-up prompt in the same session starts 99% cached (23.4/23.6K).
  Two full user prompts (6 turns, ~140K tokens) moved the meter 0 points.
- The meter counts uncached-weighted spend, not prompts: ~$0.06-0.09
  standard per window point. Cached tokens are ~free for quota. The session
  key itself creates no discount — exact byte prefixes do (changing one
  opening line zeroed the hit rate); automatic prefix cache works with or
  without the key.
- Calibration (1.3-standard, exact `usage` tokens, both price tiers):
  window 100% ≈ $6.50 std / ~$0.50 contrib (±30%); weekly ticks ~1/3-1/4
  the rate → ≈ $17-25 std. Contributor drains identically to standard —
  always use standard via the sub (same quota, no training on your data).
- Reasoning cost per trivial turn: minimal ≈ 40-170 output tokens,
  high ≈ 160-490. Output ($4.25/M) dominates small-call cost.

## Update-survival
`muse` CLI updates cannot break the proxy: it never executes, imports, or
reads version state from the CLI — only the `LLM|` key (keychain/env/file)
and `https://api.meta.ai` REST. Concretely:

- model list is fetched live (`/v1/models`), statics are fallback only;
- SSE parsing ignores unknown events; usage snapshot is optional — a missing
  `response.subscription_usage` degrades to "no quota data", never an error;
  if `response.completed` ever disappears the reply is rebuilt from deltas;
- HTTP 401 drops the cached key and re-reads once (key rotation / re-login
  needs no proxy restart);
- the only future breakage vector is Meta changing the REST contract itself,
  which would break the CLI too and show up immediately as upstream errors.

## Value test

Each proxied request = one metered sub prompt (Everyday $5: 10–50 / 5h).
Compare drained prompts vs pay-as-you-go token cost to find the crossover.

---
MIT — not affiliated with Meta. Subscription use outside the Muse Code CLI
violates Meta's sub terms; you accept that risk by running this.
