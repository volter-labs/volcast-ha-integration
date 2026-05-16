# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Empty `location` from API breaks integration title** — when the Volcast backend returns `attributes.location: ""` (or `null`, or whitespace-only), the title rendered as `"Volcast — "` with a trailing em-dash and space. The `.get("location", "Volcast")` default only catches a missing key, not an empty value. Likewise, `data.get("attributes", {})` only catches a missing key; `{"attributes": null}` would crash with `AttributeError` in the chained `.get("location", ...)`.
  - Title now degrades cleanly to `"Volcast"` when `location` is empty, null, missing, or whitespace.
  - Guards `attributes` type as `dict` before chained access (prevents `AttributeError` → opaque "unknown" config-flow error).
  - Real locations are stripped of surrounding whitespace before rendering.
  - 9 new tests in `tests/test_config_flow_title.py` covering every degenerate input (empty, null, missing key, whitespace, padded, `attributes: null`, non-string location).
- **Test infrastructure**: `tests/conftest.py` now stubs `ConfigFlow`, `OptionsFlowWithConfigEntry`, `ConfigFlowResult`, and `homeassistant.helpers.selector.EntitySelector` so tests can import `config_flow.py` without a live HA installation.

### Notes

- Cosmetic + defensive fix. Does not affect data flow or calibration. Users with the corrupt title will need to delete + re-add the integration to pick up the cleaned title (HA stores the title in the config entry and does not re-derive it on reload).
