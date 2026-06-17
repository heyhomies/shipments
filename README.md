# 📦 Sendungen erstellen

Kleine Streamlit-App für den Amazon-FBA-Workflow „An Amazon senden":

1. **Manifest erstellen** – Lade die **gescannten Seiten des Lager-Lieferscheins**
   (JPG / PNG / PDF) hoch. Claude Vision liest Artikelnummern, Mengen und die
   **handschriftliche Kartonzuordnung** aus und erzeugt die Amazon-Manifest-Upload-Datei
   (`.xlsx`) für Seller Central.
2. **Packliste befüllen** – Nachdem Amazon die Sendung erstellt hat, lädst du die
   generierte, **sendungsspezifische Packliste** (`.xlsx`) herunter und hier hoch.
   Die App trägt **Stückzahlen je Karton** sowie **Gewicht und Maße** automatisch ein.

Vor jedem Download lassen sich alle erkannten Daten in editierbaren Tabellen prüfen
und korrigieren (Handschrift-Erkennung sollte immer gegengeprüft werden).

## Lokal starten

```bash
cd app
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# API-Key hinterlegen
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
#  -> ANTHROPIC_API_KEY in .streamlit/secrets.toml eintragen

streamlit run app.py
```

Die App öffnet sich unter http://localhost:8501.

## Auf Streamlit Community Cloud veröffentlichen

1. Den Ordner `app/` in ein **GitHub-Repository** legen (Public oder Private).
   > Wichtig: `.streamlit/secrets.toml` **nicht** committen (steht in `.gitignore`).
2. Auf https://share.streamlit.io einloggen → **New app**.
3. Repository, Branch und als **Main file path** `app/app.py` wählen
   (bzw. `app.py`, falls das Repo direkt den App-Ordner enthält).
4. Unter **Advanced settings → Secrets** den Inhalt von `secrets.toml.example`
   einfügen und den echten `ANTHROPIC_API_KEY` eintragen. Optional ein
   `APP_PASSWORD` setzen, damit nur Kunden mit Passwort die App nutzen können.
5. **Deploy** klicken.

### Kosten / Hinweis
Die Auswertung der Scans nutzt die Anthropic-Claude-API. Die Kosten trägt der
Inhaber des hinterlegten API-Keys (heyhome), pro Auswertung fallen Vision-Tokens an.

## Struktur

```
app/
├── app.py                 # Streamlit-UI (Schritt 1 & 2)
├── requirements.txt
├── assets/
│   └── ManifestFileUpload_Template_MPL.xlsx   # offizielle Amazon-Vorlage
├── src/
│   ├── vision.py          # Claude-Vision-Extraktion der Scans
│   ├── manifest.py        # Manifest-Upload-Datei erzeugen
│   └── packliste.py       # Amazon-Packliste befüllen
└── .streamlit/
    ├── config.toml
    └── secrets.toml.example
```
