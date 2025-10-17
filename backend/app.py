# backend/app.py
"""
FastAPI backend used in the LLM Code Deployment project.
Runs on Hugging Face Spaces; this copy is for transparency/review only.
Secrets (GH_TOKEN, SHARED_SECRET, optional GITHUB_OWNER) are read from environment.
"""

import os
import re
import time
import base64
from typing import Dict, Any
from fastapi import FastAPI, Request, HTTPException
import httpx

app = FastAPI(title="LLM Code Deployment Endpoint")

GH_API = "https://api.github.com"
OWNER = os.getenv("GITHUB_OWNER")  # optional override
GH_TOKEN = os.getenv("GH_TOKEN")
SHARED_SECRET = os.getenv("SHARED_SECRET")

MIT_TEXT = """MIT License

Copyright (c) {year} {owner}

Permission is hereby granted, free of charge, to any person obtaining a copy ...
"""

PAGES_WORKFLOW = """name: Deploy Pages
on:
  push:
    branches: [ "main" ]
  workflow_dispatch:
permissions:
  contents: read
  pages: write
  id-token: write
concurrency:
  group: "pages"
  cancel-in-progress: true
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/configure-pages@v5
        with:
          enablement: enabled
      - uses: actions/upload-pages-artifact@v3
        with:
          path: .
  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - id: deployment
        uses: actions/deploy-pages@v4
"""

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Captcha Solver</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <script src="https://unpkg.com/tesseract.js@v4.0.2/dist/tesseract.min.js"></script>
  <style>body{font-family:system-ui,Segoe UI,Arial;margin:2rem;max-width:800px}</style>
</head>
<body>
  <h1>Captcha Solver</h1>
  <p>Pass an image via <code>?url=...</code>. Falls back to <code>sample.png</code> if present.</p>
  <img id="captcha" alt="captcha" style="max-width:100%;border:1px solid #ccc;padding:8px">
  <pre id="status" aria-live="polite">idle…</pre>
  <h2>Result</h2>
  <div id="result" style="font-size:1.25rem;font-weight:700;"></div>
  <script>
    const qs = new URLSearchParams(location.search);
    const url = qs.get('url') || 'sample.png';
    const img = document.getElementById('captcha');
    const status = document.getElementById('status');
    const result = document.getElementById('result');
    img.src = url;
    (async () => {
      try {
        status.textContent = 'Solving…';
        const watchdog = setTimeout(() => { throw new Error('Timeout after 15s'); }, 15000);
        const { data: { text } } = await Tesseract.recognize(url, 'eng', {
          logger: m => { if (m.status) status.textContent = m.status; }
        });
        clearTimeout(watchdog);
        result.textContent = (text || '').trim();
        status.textContent = 'Done';
      } catch (e) {
        status.textContent = 'Failed: ' + (e.message || e);
      }
    })();
  </script>
</body>
</html>
"""

README_MD = """# Captcha Solver
Static page that accepts ?url= and OCRs image using Tesseract.js.
Falls back to sample.png if provided.

## License
MIT
"""

def _need_env():
  if not GH_TOKEN or not SHARED_SECRET:
    raise HTTPException(status_code=500, detail="Missing GH_TOKEN or SHARED_SECRET")

def _headers():
  return {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}

def _parse_data_uri(uri: str) -> bytes:
  m = re.match(r"^data:[^;]+;base64,(.*)$", uri)
  if not m:
    raise ValueError("Invalid data URI")
  return base64.b64decode(m.group(1))

async def _gh_owner():
  if OWNER:
    return OWNER
  async with httpx.AsyncClient() as c:
    r = await c.get(f"{GH_API}/user", headers=_headers())
    r.raise_for_status()
    return r.json()["login"]

async def _gh_create_repo(owner, repo):
  async with httpx.AsyncClient() as c:
    r = await c.post(f"{GH_API}/user/repos", headers=_headers(),
                     json={"name": repo, "private": False, "auto_init": False,
                           "description": "LLM Code Deployment output"})
    if r.status_code != 201 and "exists" not in r.text.lower():
      raise HTTPException(status_code=500, detail=f"Create repo failed: {r.text}")

async def _gh_put_file(owner, repo, path, content, message):
  import base64 as b64
  async with httpx.AsyncClient() as c:
    r = await c.put(f"{GH_API}/repos/{owner}/{repo}/contents/{path}",
                    headers=_headers(),
                    json={"message": message,
                          "content": b64.b64encode(content).decode(),
                          "branch": "main"})
    r.raise_for_status()
    return r.json()["commit"]["sha"]

async def _gh_pages_url(owner, repo):
  async with httpx.AsyncClient() as c:
    r = await c.get(f"{GH_API}/repos/{owner}/{repo}/pages", headers=_headers())
    return r.json().get("html_url") if r.status_code == 200 else None

async def _post_with_backoff(url, payload):
  delay = 1
  async with httpx.AsyncClient(timeout=20) as c:
    for _ in range(6):
      try:
        resp = await c.post(url, json=payload, headers={"Content-Type":"application/json"})
        if resp.status_code == 200:
          return True
      except Exception:
        pass
      time.sleep(delay); delay *= 2
  return False

@app.get("/")
def health():
  return {"ok": True, "use": "POST /request"}

@app.post("/request")
async def handle(req: Request):
  _need_env()
  data = await req.json()
  if data.get("secret") != SHARED_SECRET:
    raise HTTPException(403, "Invalid secret")

  email = data.get("email"); task = data.get("task"); nonce = data.get("nonce")
  evaluation_url = data.get("evaluation_url"); round_idx = int(data.get("round", 1))
  attachments = data.get("attachments", [])
  if not all([email, task, nonce, evaluation_url]):
    raise HTTPException(400, "Missing required fields")

  owner = await _gh_owner()
  repo = task.replace("/", "-")
  await _gh_create_repo(owner, repo)

  year = time.gmtime().tm_year
  files = [
    ("LICENSE", MIT_TEXT.format(year=year, owner=owner).encode()),
    ("README.md", README_MD.encode()),
    (".github/workflows/pages.yml", PAGES_WORKFLOW.encode()),
    ("index.html", INDEX_HTML.encode())
  ]
  for att in attachments:
    try:
      files.append((att["name"], _parse_data_uri(att["url"])))
    except Exception:
      pass

  last_commit = ""
  for p, c in files:
    last_commit = await _gh_put_file(owner, repo, p, c, f"add {p}")

  pages_url = None
  for _ in range(12):
    pages_url = await _gh_pages_url(owner, repo)
    if pages_url: break
    time.sleep(5)

  payload = {
    "email": email,
    "task": task,
    "round": round_idx,
    "nonce": nonce,
    "repo_url": f"https://github.com/{owner}/{repo}",
    "commit_sha": last_commit,
    "pages_url": pages_url or f"https://{owner}.github.io/{repo}/"
  }
  await _post_with_backoff(evaluation_url, payload)
  return {"ok": True, **payload}
