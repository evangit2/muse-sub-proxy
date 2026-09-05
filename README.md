# muse-sub-proxy

Expose a **Muse Code subscription** through OpenAI-compatible APIs, same idea as the antigravity proxy but for the opposite direction.

```
External tool (OpenCode/etc)          muse-sub-proxy              real `muse` CLI
  POST /v1/chat/completions  --->  translate to one prompt  --->  `muse exec`
  OpenAI JSON (+SSE)         <---  CLI stdout as answer    <---  sub auth (keychain)
```

## Why subprocess, not token theft

The sub credential is keychain OAuth (`device_code`, `api_base_url: https://api.meta.ai/v1`), not a static Bearer key, and usage is entitlement-checked. Driving the real CLI keeps auth/refresh/entitlement on Meta's binary — survives CLI updates. Cost: one HTTP request = one `muse exec` turn, no token streaming.

## Setup

1. One-time login (device-code flow):
   ```
   export PATH="$HOME/.local/bin:$PATH"
   env -u META_API_KEY muse login
   ```
   Approve with the account holding the sub. Verify:
   ```
   cat ~/.config/muse/auth.json   # mechanism: oauth, storage: keychain
   ```
2. Clean sub-mode config (gateway pin would hijack traffic — keep it separate!):
   ```
   # /tmp/muse-sub-test/muse/settings.json
   {"schema_version": 1, "model": "muse-spark-1.2", "provider": "meta"}
   cp ~/.config/muse/auth.json /tmp/muse-sub-test/muse/auth.json
   env -u META_API_KEY XDG_CONFIG_HOME=/tmp/muse-sub-test muse exec "reply with exactly: SUB_OK"
   # -> SUB_OK
   ```
   Your daily-driver `~/.config/muse/settings.json` stays pinned to `:8914`.
3. Run:
   ```
   SUB_PROXY_PORT=8920 SUB_PROXY_XDG=/tmp/muse-sub-test python3 proxy.py
   curl -s localhost:8920/v1/models
   ```

## Endpoints

- `GET /v1/models` — `muse-spark-1.2`, `muse-spark-1.3`
- `POST /v1/chat/completions` — OpenAI chat format, `stream: true/false`

## OpenCode example

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

## Value test

Each proxied request burns one weighted sub prompt (10–50 / 5h on Everyday $5). Log `/5h` drain vs pay-as-you-go token cost to find the crossover.

## LaunchAgent

See `com.muse-sub-proxy.plist` — `launchctl load ~/Library/LaunchAgents/com.muse-sub-proxy.plist`.

---
MIT — not affiliated with Meta. Subscription use outside the Muse Code CLI violates Meta's sub terms; you accept that risk by running this.
