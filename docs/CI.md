# GitHub Actions

PRs into main run Python 3.11/3.13, Node/Bash checks and browser integration on every update. Browser tests use a simulated firmware bundle and start independently of ESP32-C3 builds. The changed-file planner skips the native Debian smoke test and/or firmware build only for known low-risk paths; unknown paths and unavailable diffs request all checks. The version file alone does not force both heavy checks on a PR.

Pushes to main and manual runs always run the complete suite, including the real firmware bundle browser test. Publication remains push-to-main only, and waits for firmware validation and both browser jobs. Each release bundle is rebuilt for the exact main commit; no binary artifact is reused across revisions. PlatformIO's content-based build cache reuses derived objects instead of restoring a stale build tree. Python, Playwright browser downloads, and npm downloads are cached. Browser OS dependencies are still installed/verified on each runner.

Timing depends on runner availability, cache warmth, and changed files. Check the Actions job durations to measure improvement; a cold cache may be slower. Hardware testing remains separate.
