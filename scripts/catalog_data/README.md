# Method catalog data

`overrides.json` contains manual access classifications and methods missing from
[b24-rest-docs](https://github.com/bitrix-tools/b24-rest-docs) that must remain
callable under the default read-only policy.

Regenerate the committed catalog:

```bash
uv run python scripts/generate_method_catalog.py --docs-ref main --report --strict
```

Use `--check` in CI to ensure the committed `bitrix_mcp/data/method_catalog.json`
matches a fresh generation.
