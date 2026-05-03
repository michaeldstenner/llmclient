# llmclient — developer notes

## Versioning and live dependents

`llmclient` is installed as an **editable dependency** in several sibling
projects (watchdog, bouncer, squirrel). Changes to this repo take effect
immediately in all of them — there is no publish/install step.

**Because of this, every change here is a live deployment.**

Rules:
- Bump `version` in `pyproject.toml` for any non-trivial change so that
  `pip show llmclient` can confirm what is deployed in each consumer's venv.
- Use semver intent: patch for bug fixes, minor for additive API changes,
  major for breaking changes.

### What counts as a breaking change

Any of these require coordinated updates to *all* consumers before merging:

- Removing or renaming a field on `LLMConfig`, `LLMResult`, or `EmbedResult`
- Changing the type or semantics of an existing field
- Removing or renaming a public method on `LLMClient`
- Changing what `outcome` strings the library produces

Adding new **optional** fields with defaults, new methods, or new `outcome`
values is safe — consumers that don't use them are unaffected.

### Checking which consumers use a symbol

Before removing or renaming anything, grep across the dependent projects:

```
grep -r "symbol_name" ~/Code/watchdog ~/Code/bouncer ~/Code/squirrel
```
