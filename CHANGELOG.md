# Changelog

This project follows [Semantic Versioning](https://semver.org/).

## 1.3.2 — 2026-11-20

- Download and cache LXGW WenKai Screen in the background instead of shipping the font in the plugin archive. Cards use an available system font while the download is pending or unavailable.

## 1.3.1 — 2026-11-20

- Correct the README heading for the change log.
- Bundle LXGW WenKai Screen under SIL OFL 1.1 and use it as the default rounded card font. The `font_path` plugin setting remains available as an override.

## 1.3.0 — 2026-11-20

- Detect when a Bilibili number resolves to different room and UID creators; ask the user to choose instead of silently subscribing to one.
- Add an explicit `房间号` / `UID` selector to Bilibili subscribe, cancel, and query commands.
- Use platform-specific referers for cover and avatar downloads so live and end cards can render consistently across supported platforms.
- Publish repository metadata under the GitHub author `PiaoL`.
