# SAS EuroBonus Monitor – Railway Deploy Guide

## Struktur
```
railway-deploy/
  scraper.py          ← Backend + frontend server
  requirements.txt    ← Python-avhengigheter
  Dockerfile          ← Railway bruker denne
  static/
    index.html        ← PWA-appen
```

## Deploy til Railway (10 minutter)

### Steg 1 – Lag GitHub-repo
1. Gå til github.com og logg inn
2. Klikk "New repository" → gi den et navn, f.eks. "sas-monitor"
3. Klikk "Create repository"
4. Last opp alle filene i denne mappen til repoet

### Steg 2 – Deploy på Railway
1. Gå til railway.app og logg inn med GitHub
2. Klikk "New Project" → "Deploy from GitHub repo"
3. Velg ditt "sas-monitor"-repo
4. Railway oppdager Dockerfile automatisk og starter deploy
5. Vent ca. 3–5 minutter (Playwright er stor)

### Steg 3 – Hent din URL
1. Gå til prosjektet i Railway-dashbordet
2. Klikk "Settings" → "Networking" → "Generate Domain"
3. Du får en URL som f.eks.: https://sas-monitor-production.up.railway.app

### Steg 4 – Åpne på telefonen
1. Åpne URL-en i Safari (iPhone) eller Chrome (Android)
2. iPhone: Trykk Del-ikonet → "Legg til på hjemskjerm"
3. Android: Trykk meny → "Legg til på startskjerm"

Appen ser nå ut og oppfører seg som en ekte app!

## Lokalt (for testing)
```bash
pip install -r requirements.txt
playwright install chromium
uvicorn scraper:app --port 8000
```
Åpne http://localhost:8000 i nettleseren.
