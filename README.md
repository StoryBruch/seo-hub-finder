# Striking Distance Finder

Ein kleines, sofort testbares Tool, das einen **Google-Search-Console-Export**
analysiert und die **Striking-Distance-Keywords** herausfiltert: Suchanfragen,
die schon knapp unter den Top-Platzierungen ranken, echtes Impressions-Volumen
haben — und mit überschaubarem Aufwand die meisten zusätzlichen Klicks bringen.

Das Ergebnis ist genau das: **eine sortierte Liste mit den wichtigsten Zahlen
und einer Begründung pro Keyword.**

```txt
GSC-Export → CTR-Baseline aus deinen eigenen Daten → Striking-Distance-Filter → sortierte Liste + Begründung
```

## Was es besonders macht

- **Eigene CTR-Baseline statt generischer Benchmarks.** Das Tool berechnet aus
  deinen eigenen Daten, welche CTR pro Positions-Bereich normal ist. Ein Keyword,
  das auf Platz 7 nur halb so viel geklickt wird wie deine anderen Platz-7-Seiten,
  ist ein anderer Fall als eines mit normaler CTR — und wird als solcher benannt.
- **Begründung ist kostenlos und ohne API-Key.** Jede Zeile bekommt eine
  klare, aus den Zahlen abgeleitete Begründung (Position, Impressionen, CTR vs.
  deine Baseline, geschätztes Klick-Potenzial). Keine Halluzination, sofort da.
- **Meta-Title-Check (kostenlos).** Der aktuelle `<title>` jeder Seite wird
  abgerufen und geprüft, ob das Keyword enthalten ist — fuzzy, also tolerant
  gegenüber Singular/Plural, Satzzeichen, Füllwörtern und anderer Reihenfolge
  (`iphone test` ↔ „iPhone im Test"; `kaffeemaschine vergleich` ↔ „Vergleich:
  die besten Kaffeemaschinen"). Ergibt die Spalten **Keyword enthalten** und
  **Meta Title aktuell**. Kein API-Key nötig.
- **Optionaler Title-Vorschlag (Gemini).** Mit einem kostenlosen Gemini-Key
  wird pro Keyword ein neuer Meta-Title mit **52–59 Zeichen** vorgeschlagen; die
  Länge wird per Code hart nachgeprüft (Double-Check). In der »Nach Seite
  gruppiert«-Ansicht versucht der Vorschlag, mehrere Striking-Distance-Keywords
  einer URL in einem Title abzudecken. Das Tool funktioniert vollständig auch
  ohne Key.
- **Automatische Marken-Erkennung.** Die Marke wird aus der Domain deiner Seiten
  erkannt; markenhaltige Keywords werden aus der CTR-Baseline herausgerechnet und
  lassen sich per Klick aus der Liste ausschließen (einzelne wieder aufnehmbar).
- **Optionaler Umsatz-Hebel.** Ein Wert pro Klick verwandelt das
  Klick-Potenzial in geschätztes Umsatz-Potenzial pro Monat.

## Projektdateien

```txt
striking_distance_finder.py   # CLI + Kernlogik
app_streamlit.py              # Browser-Demo (Streamlit)
sample_gsc.csv                # Beispiel-GSC-Export (Demo-Daten)
test_striking_distance.py     # Tests (Netzwerk gemockt)
requirements.txt              # Abhängigkeiten (nur pandas + streamlit)
```

## Eingabe: der GSC-Export

Search Console → **Leistung → Suchergebnisse** → oben die Dimensionen
**»Suchanfragen«** und **»Seiten«** aktivieren → **Exportieren → CSV**.

Erkannte Spalten (Deutsch und Englisch, `;`- oder `,`-getrennt, Komma-Dezimal):
`Suchanfrage/Query`, `Seite/Page`, `Klicks/Clicks`, `Impressionen/Impressions`,
`Position`, optional `CTR` (wird ansonsten aus Klicks/Impressionen berechnet).

## Browser-Demo

```bash
pip install -r requirements.txt
streamlit run app_streamlit.py
```

In der App **»Demo-Daten«** wählen — der komplette Ablauf läuft ohne eigene
Datei. Für echte Daten »GSC-CSV hochladen« wählen.

## CLI

```bash
python striking_distance_finder.py sample_gsc.csv
python striking_distance_finder.py sample_gsc.csv \
    --pos-min 4 --pos-max 20 --min-impressions 30 \
    --brand-terms cremola --value-per-click 2.50 --out opportunities.csv
```

## Meta-Titles & optionaler Title-Vorschlag

Im Bereich **»Meta-Titles prüfen & optimieren«** (unter den Kennzahlen) auf
**»Meta-Titles abrufen & prüfen«** klicken. Das Tool ruft die `<title>` der
Seiten ab und füllt die Spalten **Keyword enthalten** und **Meta Title aktuell**
— komplett kostenlos und ohne Key. Seiten, die den Abruf blockieren, lassen sich
über das Textfeld manuell ergänzen (`URL-Fragment | Meta Title`).

Für die Spalte **Meta Title neu (Vorschlag)** (52–59 Zeichen) einen kostenlosen
Gemini-Key hinterlegen:

1. Auf [aistudio.google.com/apikey](https://aistudio.google.com/apikey) mit einem
   Google-Konto anmelden → **Create API key** (keine Kreditkarte für den Free Tier).
2. In Streamlit Cloud: App → **Settings → Secrets** → Zeile einfügen:
   `GEMINI_API_KEY = "dein-key"`. Lokal: Umgebungsvariable `GEMINI_API_KEY` setzen.
3. Standardmodell `gemini-3.5-flash` mit automatischer Fallback-Kette
   (`gemini-2.5-flash` / `gemini-2.5-flash-lite`). Der Key geht nur in den
   Request-Header, nie in eine URL. Die 52–59-Zeichen-Grenze wird nach der
   Generierung per Code garantiert.

## Tests

```bash
python -m unittest test_striking_distance -v
```

(Alle Netzwerkaufrufe sind gemockt — die Tests brauchen kein Internet und keinen Key.)

## Kurz erklärt: die Logik

1. **CTR-Baseline** pro Positions-Bucket (`1`, `2`, `3`, `4–5`, `6–8`, `9–11`,
   `12–15`, `16–20`) aus den eigenen Daten; ist ein Bucket zu dünn besetzt, greift
   ein Richtwert (klar gekennzeichnet). Marken-Keywords werden herausgerechnet.
2. **Filter**: Position im gewählten Bereich (Standard 4–20) und genügend
   Impressionen (Standard ≥ 30).
3. **Score**: geschätztes zusätzliches Klick-Potenzial pro Monat, wenn das
   Keyword auf Top-3 gehoben wird (`Impressionen × Top-3-CTR − aktuelle Klicks`).
4. **Unterperformer-Flag**: liegt die CTR deutlich unter der Baseline für die
   Position, wird das in der Begründung benannt (Hebel = Snippet/Title statt Ranking).
