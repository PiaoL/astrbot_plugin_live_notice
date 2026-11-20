# Changelog

This project follows [Semantic Versioning](https://semver.org/).

## 1.3.0 — 2026-11-20

- Detect when a Bilibili number resolves to different room and UID creators; ask the user to choose instead of silently subscribing to one.
- Add an explicit `房间号` / `UID` selector to Bilibili subscribe, cancel, and query commands.
- Use platform-specific referers for cover and avatar downloads so live and end cards can render consistently across supported platforms.
- Publish repository metadata under the GitHub author `PiaoL`.
