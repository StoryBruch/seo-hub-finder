# Deployment Guide

## Ziel: ein Link, kein Setup beim Empfänger

Der Empfänger klickt einen Streamlit-Link, wählt **»Demo-Daten«** und sieht sofort
das komplette Ergebnis — keine Installation, kein API-Key nötig.

## Lokaler Test

```bash
pip install -r requirements.txt
streamlit run app_streamlit.py
```

## Streamlit Community Cloud

1. Repo auf GitHub pushen.
2. Auf [share.streamlit.io](https://share.streamlit.io) einloggen → **New app**.
3. Repo auswählen, Branch `master`, Main file: `app_streamlit.py`.
4. **Deploy**. Nach jedem Push zieht Streamlit die neue Version automatisch.
5. Den App-Link teilen.

## Optional: KI-Deep-Dive aktivieren

App → **Settings → Secrets** → Zeile einfügen:

```toml
GEMINI_API_KEY = "dein-key"
```

Key kostenlos auf [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
Ohne Key funktioniert alles außer dem optionalen KI-Tab.

## Braucht der Empfänger Claude Code?

Nein. Claude Code ist nur zum Weiterbauen/Erweitern des Prototyps da. Zum Testen
reicht der Streamlit-Link oder ein lokaler `streamlit run`.
