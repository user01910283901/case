# Security Notes

## Important
- If you publish only static `index.html` on GitHub Pages, users can always open DevTools and inspect all frontend code.
- Real anti-cheat protection exists only when economy logic runs on backend.

## What is added in this project
- `secure_server.py`:
  - server-side profile storage in `progress/progress.json`
  - server-side operations for `create`, `buy`, `open`, `sell`, `gift`
  - no endpoint for direct star balance editing
  - rate limit per IP (`120` requests/minute)
- `index.html`:
  - auto-detects `/api/health`
  - if backend is available, uses server API instead of local wallet logic
  - local mode remains fallback

## Run locally
```powershell
python secure_server.py
```
Open:
```text
http://127.0.0.1:8080
```

## GitHub visibility
- Public GitHub repo cannot hide file structure.
- To hide important code:
  - use **private repo**
  - keep secrets only in backend environment variables
  - never commit tokens/keys into frontend or repository history
