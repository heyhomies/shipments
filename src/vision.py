"""Extraktion der gescannten Lager-Packlisten (Lieferscheine + Zusammenfassung)
mit Claude Vision. Liefert strukturierte Daten für Manifest und Packliste.

Die Scans enthalten:
- Artikelzeilen (gedruckt): Pos., Artikelnr., Bezeichnung, Menge
- handschriftliche Kartonzuordnung je Artikel: in welche Karton-Nr. wie viele Stück
- pro Karton (meist letzte Seite): Gewicht (kg) und Maße (L x B x H, cm)
- pro Palette: Höhe, Gewicht, Kartonbereich (von-bis)
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass, field
from typing import List, Optional

import anthropic
import pypdfium2 as pdfium
from PIL import Image

MODEL = "claude-sonnet-4-6"

# Strukturiertes JSON-Schema, das wir vom Modell verlangen.
EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "belegnummer": {
            "type": "string",
            "description": "Belegnummer / Vorgangsnummer des Lieferscheins, falls erkennbar, sonst leerer String.",
        },
        "items": {
            "type": "array",
            "description": "Eine Zeile je Artikel aus den gedruckten Lieferschein-Tabellen.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "artikelnr": {
                        "type": "string",
                        "description": "Gedruckte Artikelnummer (Artikelnr./SKU), exakt wie abgedruckt, OHNE Zusätze.",
                    },
                    "bezeichnung": {
                        "type": "string",
                        "description": "Produktbezeichnung (gekürzt erlaubt).",
                    },
                    "menge": {
                        "type": "integer",
                        "description": "Gedruckte Gesamtmenge (Spalte Menge / Menge ME).",
                    },
                    "kartons": {
                        "type": "array",
                        "description": (
                            "Handschriftliche Kartonzuordnung. Jeder Eintrag = ein Karton mit Stückzahl. "
                            "Beispiel: '69+70 je 6 Stk' -> zwei Einträge {nummer:69,stueck:6},{nummer:70,stueck:6}. "
                            "Beispiel: '(63) 4 Stk' -> {nummer:63,stueck:4}. Leer lassen, wenn keine Handschrift erkennbar."
                        ),
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "nummer": {"type": "integer", "description": "Eingekreiste Kartonnummer."},
                                "stueck": {"type": "integer", "description": "Stückzahl dieses Artikels in diesem Karton."},
                            },
                            "required": ["nummer", "stueck"],
                        },
                    },
                },
                "required": ["artikelnr", "bezeichnung", "menge", "kartons"],
            },
        },
        "boxes": {
            "type": "array",
            "description": "Kartondaten (meist letzte Seite): Gewicht und Maße je Kartonnummer.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "nummer": {"type": "integer", "description": "Eingekreiste Kartonnummer."},
                    "gewicht_kg": {"type": "number", "description": "Kartongewicht in kg."},
                    "laenge_cm": {"type": "number", "description": "Länge in cm (oft erste Zahl)."},
                    "breite_cm": {"type": "number", "description": "Breite in cm (oft zweite Zahl)."},
                    "hoehe_cm": {"type": "number", "description": "Höhe in cm (oft dritte/letzte Zahl)."},
                },
                "required": ["nummer", "gewicht_kg", "laenge_cm", "breite_cm", "hoehe_cm"],
            },
        },
        "pallets": {
            "type": "array",
            "description": "Palettendaten, falls vorhanden.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "nummer": {"type": "integer"},
                    "hoehe_cm": {"type": "number"},
                    "gewicht_kg": {"type": "number"},
                    "karton_von": {"type": "integer"},
                    "karton_bis": {"type": "integer"},
                },
                "required": ["nummer", "hoehe_cm", "gewicht_kg", "karton_von", "karton_bis"],
            },
        },
    },
    "required": ["belegnummer", "items", "boxes", "pallets"],
}

SYSTEM_PROMPT = """Du bist ein präziser Extraktionsassistent für Amazon-FBA-Lieferscheine eines Logistiklagers.

Du erhältst gescannte Seiten (Fotos/Scans). Sie bestehen aus:
1. Gedruckten Lieferschein-Tabellen mit den Spalten Pos., Artikelnr., Bezeichnung, Termin, Menge ME.
2. HANDSCHRIFTLICHEN Notizen neben/über jeder Artikelzeile: eingekreiste Kartonnummern und Stückzahlen
   (z.B. ein Häkchen, "(63)" und daneben "4 Stk"; oder "(69)+(70)" und "je 6 Stk").
3. Einer Zusammenfassungsseite mit eingekreisten Kartonnummern, daneben Gewicht (kg) und Maßen (z.B. "27,5 x 27 x 19,5 cm"),
   sowie evtl. einer Palettentabelle (Palette / Höhe / Gewicht / von-bis Kartonnummern).

Regeln:
- Lies die gedruckte Artikelnummer EXAKT ab, ohne Zusätze wie "-FBA".
- Deutsche Dezimalzahlen nutzen Komma; gib Zahlen als echte Zahlen mit Punkt zurück (z.B. 27,5 -> 27.5).
- "je X Stk" bei mehreren Kartonnummern heißt: X Stück in JEDEM genannten Karton (ein Eintrag pro Karton).
- Wenn eine Artikelzeile nur eine Kartonnummer und keine extra Stückzahl hat, nimm die gedruckte Menge als Stückzahl dieses Kartons.
- Erfinde nichts. Wenn etwas unleserlich ist, lass das Feld weg bzw. den Eintrag aus, statt zu raten.
- Maße: oft Reihenfolge L x B x H. Wenn unklar, trage die Zahlen in der abgedruckten Reihenfolge in laenge/breite/hoehe ein.
"""


@dataclass
class Box:
    nummer: int
    gewicht_kg: float
    laenge_cm: float
    breite_cm: float
    hoehe_cm: float


@dataclass
class KartonZuordnung:
    nummer: int
    stueck: int


@dataclass
class Item:
    artikelnr: str
    bezeichnung: str
    menge: int
    kartons: List[KartonZuordnung] = field(default_factory=list)


@dataclass
class Pallet:
    nummer: int
    hoehe_cm: float
    gewicht_kg: float
    karton_von: int
    karton_bis: int


@dataclass
class Shipment:
    belegnummer: str
    items: List[Item]
    boxes: List[Box]
    pallets: List[Pallet]


def _pil_to_png_b64(img: Image.Image, max_edge: int = 2200) -> str:
    """Skaliert ein Bild herunter (lange Kante <= max_edge) und gibt Base64-PNG zurück."""
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, max_edge / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")


def file_to_images(name: str, data: bytes) -> List[Image.Image]:
    """Wandelt eine hochgeladene Datei (JPG/PNG/PDF) in eine Liste von PIL-Bildern um."""
    lower = name.lower()
    if lower.endswith(".pdf"):
        images: List[Image.Image] = []
        pdf = pdfium.PdfDocument(data)
        try:
            for i in range(len(pdf)):
                page = pdf[i]
                # scale ~ 200 DPI für gute Handschrift-Erkennung
                bitmap = page.render(scale=200 / 72)
                images.append(bitmap.to_pil())
        finally:
            pdf.close()
        return images
    # Bildformat
    return [Image.open(io.BytesIO(data))]


def _image_blocks(images: List[Image.Image]) -> List[dict]:
    blocks = []
    for idx, img in enumerate(images, start=1):
        blocks.append({"type": "text", "text": f"--- Seite {idx} ---"})
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": _pil_to_png_b64(img),
                },
            }
        )
    return blocks


def extract_shipment(images: List[Image.Image], api_key: str) -> Shipment:
    """Schickt alle Seiten an Claude und gibt die strukturierten Sendungsdaten zurück."""
    if not images:
        raise ValueError("Keine Seiten zum Auswerten übergeben.")

    client = anthropic.Anthropic(api_key=api_key)

    content = _image_blocks(images) + [
        {
            "type": "text",
            "text": (
                "Werte ALLE Seiten gemeinsam aus und gib die vollständige Sendung als JSON gemäß Schema zurück. "
                "Fasse Artikelzeilen über alle Seiten zusammen (jede Tabellenzeile = ein Item)."
            ),
        }
    ]

    message = _create_with_schema(client, content)
    return _parse_response(message)


def _create_with_schema(client: "anthropic.Anthropic", content: List[dict]):
    """Messages-Aufruf mit erzwungenem JSON-Schema (structured outputs).

    Wird gestreamt, weil das Denken (adaptive thinking) auf das max_tokens-Budget
    angerechnet wird; bei vielen Artikeln muss genug Platz für das JSON bleiben.
    """
    with client.messages.stream(
        model=MODEL,
        max_tokens=32000,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
        },
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    ) as stream:
        return stream.get_final_message()


def _parse_response(message) -> Shipment:
    stop = getattr(message, "stop_reason", None)
    if stop == "max_tokens":
        raise RuntimeError(
            "Die Antwort war zu lang und wurde abgeschnitten. Bitte weniger Seiten "
            "auf einmal hochladen (z.B. in zwei Durchgängen)."
        )
    if stop == "refusal":
        raise RuntimeError("Die Auswertung wurde vom Modell aus Sicherheitsgründen abgelehnt.")

    text = next((b.text for b in message.content if b.type == "text"), None)
    if not text or not text.strip():
        raise RuntimeError("Leere Antwort vom Modell erhalten.")
    text = text.strip()
    # Defensiv: evtl. vorhandene Code-Fences entfernen.
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Antwort konnte nicht als JSON gelesen werden ({e}). "
            "Bitte erneut versuchen oder weniger Seiten auf einmal hochladen."
        ) from e
    items = [
        Item(
            artikelnr=str(it.get("artikelnr", "")).strip(),
            bezeichnung=str(it.get("bezeichnung", "")).strip(),
            menge=int(it.get("menge", 0) or 0),
            kartons=[
                KartonZuordnung(nummer=int(k["nummer"]), stueck=int(k["stueck"]))
                for k in it.get("kartons", [])
            ],
        )
        for it in data.get("items", [])
    ]
    boxes = [
        Box(
            nummer=int(b["nummer"]),
            gewicht_kg=float(b["gewicht_kg"]),
            laenge_cm=float(b["laenge_cm"]),
            breite_cm=float(b["breite_cm"]),
            hoehe_cm=float(b["hoehe_cm"]),
        )
        for b in data.get("boxes", [])
    ]
    pallets = [
        Pallet(
            nummer=int(p["nummer"]),
            hoehe_cm=float(p["hoehe_cm"]),
            gewicht_kg=float(p["gewicht_kg"]),
            karton_von=int(p["karton_von"]),
            karton_bis=int(p["karton_bis"]),
        )
        for p in data.get("pallets", [])
    ]
    return Shipment(
        belegnummer=str(data.get("belegnummer", "")).strip(),
        items=items,
        boxes=boxes,
        pallets=pallets,
    )
