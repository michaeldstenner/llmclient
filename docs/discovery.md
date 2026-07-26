# Discovery — what can this machine call?

As more backends accumulate (Ollama locally, Anthropic direct,
OpenRouter, an AIP-style OpenAI-compatible endpoint), the hard question
stops being "how do I call a model" and becomes "**what do I have to
play with?**" — especially for an agent driving llmclient, which cannot
see the keychain, the environment, or `config.yaml`.

Discovery answers that in two layers:

- **Providers** — which backends are on the table at all, from
  `_DEFAULT_URLS` + `config.yaml` + the key resolution chain.
- **Named models** — which specific models this machine has blessed
  with a short name, from the `models:` section of `config.yaml`.

Plus a live **catalog** per provider, fetched from the backend itself.

**No API key value is ever returned, printed, or logged** — only where
a key came from (`env:OPENROUTER_API_KEY`, `keychain:default`,
`config:openrouter.api_key`). That is deliberate: the point is to tell
a caller *that* OpenRouter is usable, not to hand it the credential.

---

## CLI

### `llmc providers`

Static, no network. One line per provider: resolved URL, and whether a
key resolves and from where. `*` marks providers that are usable right
now (URL present, and a key if one is needed).

```
  provider           url                        key
* anthropic          https://api.anthropic.com  keychain:default
  openai             https://api.openai.com     MISSING
  openai_compatible  (unset)                    MISSING
* ollama             http://localhost:11434     none needed
* openrouter         https://openrouter.ai/api  keychain:default

* usable now: anthropic, ollama, openrouter
  openai: no key. Set OPENAI_API_KEY, or add one with
  `security add-generic-password -s llmclient:openai -a default -w`
```

### `llmc models`

With no arguments: the named models, then a live count per provider.

```
Named models:
  cheap-oss  openrouter  qwen/qwen3-32b
  fast       openrouter  anthropic/claude-haiku-4.5   [timeout=30]
  local      ollama      qwen3:32b

Providers:
  anthropic          11 models
  openai             — no key
  openai_compatible  — not configured (no url)
  ollama             2 models (2 loaded)
  openrouter         345 models
```

With `-p PROVIDER`, the full catalog for one backend; `*` marks an
Ollama model currently resident in VRAM:

```sh
llmc models -p ollama
llmc models -p openrouter --filter claude    # substring match on the id
llmc models -p anthropic --all               # ignore --limit
llmc models --no-probe                       # config only, no network
llmc models --json                           # everything, machine-readable
```

Catalog endpoints by provider: Ollama `/api/tags` (+ `/api/ps` for the
loaded flag), Anthropic `/v1/models`, everything else the
OpenAI-compatible `/v1/models`. A provider stanza in `config.yaml`
that llmclient doesn't know natively (e.g. `aip:`) is discovered from
the config file and treated as OpenAI-compatible.

`llmc --config-dir PATH ...` overlays another `config.yaml` on the
global one — useful for a project that defines its own model set.

Note: `aip_models -t MODEL|all` remains the tool for *testing* whether
an OpenAI-compatible endpoint actually answers; `llmc models` only
lists.

---

## Named models in `config.yaml`

```yaml
models:
  fast:
    provider: openrouter
    model: anthropic/claude-haiku-4.5
    timeout: 30
  local:
    provider: ollama
    model: qwen3:32b
    priority: 90
  cheap-oss:
    provider: openrouter
    model: qwen/qwen3-32b
```

Each stanza is a name plus `provider`, `model`, and any other
`LLMConfig` field. Values are coerced to the type of the matching
`LLMConfig` default, so `timeout: 30` arrives as an int and
`num_ctx_auto: false` as a bool. `provider` may be omitted if `url` is
given (it is inferred). A stanza without `model` is skipped.

The config parser is llmclient's own YAML subset (no PyYAML
dependency): indentation-nested `key: value` mappings, scalar values
only — no lists, anchors, or multi-line strings.

Use them from the library:

```python
from llmclient import LLMClient, configured_models

configured_models()          # {"fast": {"provider": ..., "model": ...}, ...}

client = LLMClient.from_name("fast")            # KeyError lists known names
client = LLMClient.from_name("fast", timeout=5) # kwargs override the stanza
```

…and from the CLI — a bare `-m` with no `-p` is looked up as a name
first, so this works with no provider flag:

```sh
llmc call -m fast "Reply with exactly: ok"
```

`queue_mode` still defaults per provider: `cooperative` for Ollama
(which has finite slots), `off` for hosted APIs.

---

## Library API

```python
import llmclient

llmclient.configured_models()      # named models from config.yaml
llmclient.provider_status("openrouter")
llmclient.all_provider_status()    # [{provider, url, has_key, key_source,
                                   #   needs_key, env_var, listable, usable}]
llmclient.list_provider_models("openrouter", timeout=15)
                                   # ([{id, loaded, detail}, ...], error)
llmclient.catalog(live=True)       # {"named": {...}, "providers": [...]}
```

`catalog()` is the one-call version — the same structure `llmc models
--json` prints. `live=False` skips every network call.

`list_provider_models()` returns `(models, error)` rather than raising;
`error` uses the same vocabulary as `LLMResult.outcome`
(`error:unreachable`, `http_401`, `timeout`) plus `error:no_key`,
`error:no_url`, and `error:not_listable` (for `claude_code`/`claude_p`,
which have no catalog endpoint).

---

## Testing one system against many models

The pattern this was built for:

```python
from llmclient import LLMClient, list_provider_models

candidates, err = list_provider_models("openrouter")
targets = [m["id"] for m in candidates if "claude" in m["id"]]

for model in targets:
    client = LLMClient.openrouter(model, timeout=60, log_caller="bench")
    result = client.call(prompt)
    ...
```

OpenRouter is what makes this cheap — one key, one transport, hundreds
of models — but an agent can only take that path if it can *tell* that
OpenRouter is configured. That is what `provider_status("openrouter")
["usable"]` is for.
