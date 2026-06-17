"""Amazon FBA – Sendungen erstellen.

Schritt 1: Aus gescannten Lager-Packlisten die Amazon Manifest-Upload-Datei erzeugen.
Schritt 2: Die von Amazon generierte, sendungsspezifische Packliste automatisch befüllen.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from src import manifest as manifest_mod
from src import packliste as pack_mod
from src import vision as vision_mod
from src.vision import Box, Item, KartonZuordnung, Shipment

st.set_page_config(page_title="Sendungen erstellen", page_icon="📦", layout="wide")


# --------------------------------------------------------------------------- #
# Zugang & API-Key
# --------------------------------------------------------------------------- #
def check_password() -> bool:
    pw = st.secrets.get("APP_PASSWORD", "")
    if not pw:
        return True  # kein Schutz konfiguriert
    if st.session_state.get("auth_ok"):
        return True
    st.title("📦 Sendungen erstellen")
    entered = st.text_input("Passwort", type="password")
    if entered:
        if entered == pw:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Falsches Passwort.")
    return False


def get_api_key() -> str | None:
    key = st.secrets.get("ANTHROPIC_API_KEY", "")
    if not key:
        st.error(
            "Kein ANTHROPIC_API_KEY hinterlegt. Bitte in den Streamlit-Secrets eintragen "
            "(Settings → Secrets) bzw. lokal in `.streamlit/secrets.toml`."
        )
        return None
    return key


# --------------------------------------------------------------------------- #
# Hilfen: Shipment <-> DataFrames
# --------------------------------------------------------------------------- #
def shipment_to_state(s: Shipment) -> None:
    st.session_state["belegnummer"] = s.belegnummer
    st.session_state["items_df"] = pd.DataFrame(
        [{"Artikelnr.": it.artikelnr, "Bezeichnung": it.bezeichnung, "Menge": it.menge} for it in s.items]
    )
    karton_rows = [
        {"Artikelnr.": it.artikelnr, "Kartonnummer": k.nummer, "Stück": k.stueck}
        for it in s.items
        for k in it.kartons
    ]
    st.session_state["karton_df"] = pd.DataFrame(
        karton_rows, columns=["Artikelnr.", "Kartonnummer", "Stück"]
    )
    st.session_state["boxes_df"] = pd.DataFrame(
        [
            {
                "Kartonnummer": b.nummer,
                "Gewicht (kg)": b.gewicht_kg,
                "Länge (cm)": b.laenge_cm,
                "Breite (cm)": b.breite_cm,
                "Höhe (cm)": b.hoehe_cm,
            }
            for b in s.boxes
        ],
        columns=["Kartonnummer", "Gewicht (kg)", "Länge (cm)", "Breite (cm)", "Höhe (cm)"],
    )
    st.session_state["pallets_df"] = pd.DataFrame(
        [
            {
                "Palette": p.nummer,
                "Höhe (cm)": p.hoehe_cm,
                "Gewicht (kg)": p.gewicht_kg,
                "Karton von": p.karton_von,
                "Karton bis": p.karton_bis,
            }
            for p in s.pallets
        ],
        columns=["Palette", "Höhe (cm)", "Gewicht (kg)", "Karton von", "Karton bis"],
    )
    st.session_state["extracted"] = True


def build_shipment_from_state() -> Shipment:
    """Baut aus den (ggf. bearbeiteten) Tabellen wieder ein Shipment-Objekt."""
    items_df = st.session_state.get("items_df", pd.DataFrame())
    karton_df = st.session_state.get("karton_df", pd.DataFrame())
    boxes_df = st.session_state.get("boxes_df", pd.DataFrame())

    # Kartonzuordnungen je Artikelnr. gruppieren
    karton_map: dict[str, list[KartonZuordnung]] = {}
    for _, r in karton_df.iterrows():
        art = str(r.get("Artikelnr.", "")).strip()
        try:
            num = int(r.get("Kartonnummer"))
            stk = int(r.get("Stück"))
        except (TypeError, ValueError):
            continue
        if not art:
            continue
        karton_map.setdefault(art, []).append(KartonZuordnung(nummer=num, stueck=stk))

    items = []
    for _, r in items_df.iterrows():
        art = str(r.get("Artikelnr.", "")).strip()
        if not art:
            continue
        try:
            menge = int(r.get("Menge") or 0)
        except (TypeError, ValueError):
            menge = 0
        items.append(
            Item(
                artikelnr=art,
                bezeichnung=str(r.get("Bezeichnung", "") or ""),
                menge=menge,
                kartons=karton_map.get(art, []),
            )
        )

    boxes = []
    for _, r in boxes_df.iterrows():
        try:
            boxes.append(
                Box(
                    nummer=int(r["Kartonnummer"]),
                    gewicht_kg=float(r["Gewicht (kg)"]),
                    laenge_cm=float(r["Länge (cm)"]),
                    breite_cm=float(r["Breite (cm)"]),
                    hoehe_cm=float(r["Höhe (cm)"]),
                )
            )
        except (TypeError, ValueError, KeyError):
            continue

    return Shipment(
        belegnummer=st.session_state.get("belegnummer", ""),
        items=items,
        boxes=boxes,
        pallets=[],
    )


# --------------------------------------------------------------------------- #
# Schritt 1: Manifest
# --------------------------------------------------------------------------- #
def step_manifest(api_key: str) -> None:
    st.header("Schritt 1 · Manifest aus Lager-Scans erstellen")
    st.caption(
        "Lade die gescannten Seiten des Lager-Lieferscheins hoch (JPG, PNG oder PDF). "
        "Die App liest Artikelnummern, Mengen und die handschriftliche Kartonzuordnung aus."
    )

    files = st.file_uploader(
        "Gescannte Packlisten-Seiten",
        type=["jpg", "jpeg", "png", "pdf"],
        accept_multiple_files=True,
    )

    if st.button("📄 Scans auswerten", type="primary", disabled=not files):
        images = []
        with st.spinner("Seiten werden vorbereitet …"):
            for f in files:
                images.extend(vision_mod.file_to_images(f.name, f.getvalue()))
        st.info(f"{len(images)} Seite(n) erkannt. Auswertung mit Claude läuft …")
        with st.spinner("Claude liest gedruckte Tabellen und Handschrift … (kann ~1 Min. dauern)"):
            try:
                shipment = vision_mod.extract_shipment(images, api_key)
            except Exception as e:  # noqa: BLE001
                st.error(f"Auswertung fehlgeschlagen: {e}")
                return
        shipment_to_state(shipment)
        st.success(
            f"Erkannt: {len(shipment.items)} Artikel, {len(shipment.boxes)} Kartons, "
            f"{len(shipment.pallets)} Paletten."
        )

    if not st.session_state.get("extracted"):
        return

    st.divider()
    st.subheader("Erkannte Daten prüfen & korrigieren")
    if st.session_state.get("belegnummer"):
        st.caption(f"Belegnummer: **{st.session_state['belegnummer']}**")

    st.markdown("**Artikel (für das Manifest)** – Artikelnr. und Menge bei Bedarf korrigieren:")
    st.session_state["items_df"] = st.data_editor(
        st.session_state["items_df"],
        num_rows="dynamic",
        use_container_width=True,
        key="ed_items",
    )

    with st.expander("Kartonzuordnung (für Schritt 2) prüfen"):
        st.session_state["karton_df"] = st.data_editor(
            st.session_state["karton_df"],
            num_rows="dynamic",
            use_container_width=True,
            key="ed_karton",
        )
    with st.expander("Kartonmaße & Gewichte (für Schritt 2) prüfen"):
        st.session_state["boxes_df"] = st.data_editor(
            st.session_state["boxes_df"],
            num_rows="dynamic",
            use_container_width=True,
            key="ed_boxes",
        )
    if not st.session_state.get("pallets_df", pd.DataFrame()).empty:
        with st.expander("Palettendaten (Info)"):
            st.dataframe(st.session_state["pallets_df"], use_container_width=True)

    st.divider()
    st.subheader("Manifest-Datei erzeugen")
    col1, col2, col3 = st.columns(3)
    with col1:
        suffix = st.text_input("SKU-Suffix", value="-FBA", help="Wird an die Artikelnr. angehängt, z.B. -FBA.")
    with col2:
        prep = st.selectbox("Default prep owner", ["Seller", "Amazon"], index=0)
    with col3:
        label = st.selectbox("Default labeling owner", ["Seller", "Amazon"], index=0)

    shipment = build_shipment_from_state()
    rows = manifest_mod.build_manifest_rows(shipment.items, sku_suffix=suffix)

    st.caption(f"{len(rows)} Manifest-Zeilen · Gesamtmenge: {sum(r.quantity for r in rows)}")
    preview = pd.DataFrame([{"Merchant SKU": r.merchant_sku, "Quantity": r.quantity} for r in rows])
    st.dataframe(preview, use_container_width=True, height=240)

    try:
        xlsx = manifest_mod.build_manifest_xlsx(rows, default_prep_owner=prep, default_labeling_owner=label)
    except Exception as e:  # noqa: BLE001
        st.error(f"Manifest konnte nicht erstellt werden: {e}")
        return

    beleg = st.session_state.get("belegnummer") or dt.date.today().isoformat()
    fname = f"ManifestFileUpload_{_safe(beleg)}.xlsx"
    st.download_button(
        "⬇️ Manifest-Datei herunterladen",
        data=xlsx,
        file_name=fname,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )
    st.info(
        "Lade diese Datei im Seller Central unter „An Amazon senden“ hoch. "
        "Sobald Amazon die Sendung erstellt hat, lade die generierte Packliste herunter "
        "und befülle sie in **Schritt 2**."
    )


# --------------------------------------------------------------------------- #
# Schritt 2: Packliste befüllen
# --------------------------------------------------------------------------- #
def step_packliste() -> None:
    st.header("Schritt 2 · Amazon-Packliste befüllen")

    if not st.session_state.get("extracted"):
        st.warning("Bitte zuerst **Schritt 1** ausführen – die Kartondaten daraus werden hier benötigt.")
        return

    st.caption(
        "Lade die von Amazon generierte, sendungsspezifische Packliste (.xlsx) hoch. "
        "Die App trägt Stückzahlen je Karton sowie Gewicht und Maße ein."
    )

    up = st.file_uploader("Amazon-Packliste (.xlsx)", type=["xlsx"], key="pack_upload")
    if not up:
        return

    raw = up.getvalue()
    try:
        pack = pack_mod.parse_packliste(raw)
    except Exception as e:  # noqa: BLE001
        st.error(f"Packliste konnte nicht gelesen werden: {e}")
        return

    if pack.total_boxes <= 0:
        st.error("Anzahl der Kartons in der Packliste konnte nicht erkannt werden.")
        return

    st.success(
        f"Blatt „{pack.sheet_title}“ · {len(pack.sku_rows)} SKUs · {pack.total_boxes} Kartons."
    )

    # --- Spalten -> Lager-Kartonnummer zuordnen ---
    st.subheader("Kartonnummern zuordnen")
    st.caption(
        "Amazon nummeriert die Kartons dieser Verpackungsgruppe als P1-B1, P1-B2 … "
        "Gib an, welche **Lager-Kartonnummern** dazugehören. Standard: fortlaufend ab Startnummer."
    )
    start = st.number_input("Erste Lager-Kartonnummer (P1-B1 entspricht …)", min_value=1, value=1, step=1)

    map_default = pd.DataFrame(
        {
            "Packliste-Spalte": [f"P1-B{i+1}" for i in range(pack.total_boxes)],
            "Lager-Kartonnummer": [int(start) + i for i in range(pack.total_boxes)],
        }
    )
    with st.expander("Zuordnung anpassen (optional)", expanded=False):
        map_df = st.data_editor(
            map_default,
            use_container_width=True,
            hide_index=True,
            key="ed_map",
            column_config={"Packliste-Spalte": st.column_config.TextColumn(disabled=True)},
        )

    # col index (echte Tabellenspalte) -> Kartonnummer
    col_to_boxnum: dict[int, int] = {}
    for i, col in enumerate(pack.box_cols):
        try:
            col_to_boxnum[col] = int(map_df.iloc[i]["Lager-Kartonnummer"])
        except (KeyError, TypeError, ValueError, IndexError):
            col_to_boxnum[col] = int(start) + i

    shipment = build_shipment_from_state()
    sug = pack_mod.suggest_fill(pack, shipment, col_to_boxnum)

    if sug.unmatched_skus:
        st.warning(
            "Keine Lager-Daten gefunden für SKU(s): " + ", ".join(sug.unmatched_skus)
            + " – diese Zeilen bleiben leer. Prüfe ggf. die Artikelnummern in Schritt 1."
        )
    if sug.missing_box_meta:
        st.warning(
            "Keine Maße/Gewicht für Kartonnummer(n): "
            + ", ".join(str(n) for n in sug.missing_box_meta)
        )

    # --- Stückzahl-Raster prüfen ---
    st.subheader("Stückzahlen je Karton prüfen")
    boxnum_cols = [col_to_boxnum[c] for c in pack.box_cols]
    grid = pd.DataFrame(
        index=[f"{sku}" for (_r, sku, _e) in pack.sku_rows],
        columns=[str(n) for n in boxnum_cols],
        dtype="Int64",
    )
    for (row, sku, _e) in pack.sku_rows:
        for c in pack.box_cols:
            boxnum = col_to_boxnum[c]
            qty = sug.units.get((row, boxnum), 0)
            grid.at[f"{sku}", str(boxnum)] = qty if qty else pd.NA
    grid.insert(0, "Erwartet", [e for (_r, _s, e) in pack.sku_rows])

    st.caption("Spalten = Lager-Kartonnummer. Werte bei Bedarf anpassen.")
    edited_grid = st.data_editor(grid, use_container_width=True, key="ed_grid")

    # Summen-Kontrolle
    sums = edited_grid.drop(columns=["Erwartet"]).fillna(0).sum(axis=1).astype(int)
    check = pd.DataFrame(
        {
            "SKU": [s for (_r, s, _e) in pack.sku_rows],
            "Erwartet": [e for (_r, _s, e) in pack.sku_rows],
            "Verteilt": sums.values,
        }
    )
    check["OK"] = check["Erwartet"] == check["Verteilt"]
    n_bad = int((~check["OK"]).sum())
    if n_bad:
        st.warning(f"{n_bad} SKU(s) mit Abweichung zwischen erwarteter und verteilter Menge.")
        st.dataframe(check[~check["OK"]], use_container_width=True, hide_index=True)
    else:
        st.success("Alle Stückzahlen stimmen mit der erwarteten Menge überein.")

    # --- Maße/Gewicht prüfen ---
    st.subheader("Kartonmaße & Gewicht prüfen")
    meta_rows = []
    for c in pack.box_cols:
        boxnum = col_to_boxnum[c]
        m = sug.boxmeta.get(boxnum)
        meta_rows.append(
            {
                "Kartonnummer": boxnum,
                "Gewicht (kg)": m[0] if m else None,
                "Länge (cm)": m[1] if m else None,
                "Breite (cm)": m[2] if m else None,
                "Höhe (cm)": m[3] if m else None,
            }
        )
    meta_df = st.data_editor(
        pd.DataFrame(meta_rows),
        use_container_width=True,
        hide_index=True,
        key="ed_meta",
    )

    # --- aus den bearbeiteten Tabellen Schreibdaten bauen ---
    units: dict[tuple[int, int], int] = {}
    for ri, (row, sku, _e) in enumerate(pack.sku_rows):
        for c in pack.box_cols:
            boxnum = col_to_boxnum[c]
            val = edited_grid.iloc[ri][str(boxnum)]
            if pd.notna(val) and int(val) != 0:
                units[(row, boxnum)] = int(val)

    boxmeta: dict[int, tuple[float, float, float, float]] = {}
    for _, r in meta_df.iterrows():
        try:
            num = int(r["Kartonnummer"])
        except (TypeError, ValueError):
            continue
        if pd.isna(r["Gewicht (kg)"]):
            continue
        boxmeta[num] = (
            float(r["Gewicht (kg)"]),
            float(r["Länge (cm)"]) if pd.notna(r["Länge (cm)"]) else 0.0,
            float(r["Breite (cm)"]) if pd.notna(r["Breite (cm)"]) else 0.0,
            float(r["Höhe (cm)"]) if pd.notna(r["Höhe (cm)"]) else 0.0,
        )

    st.divider()
    try:
        out = pack_mod.write_packliste(raw, pack, col_to_boxnum, units, boxmeta)
    except Exception as e:  # noqa: BLE001
        st.error(f"Packliste konnte nicht befüllt werden: {e}")
        return

    st.download_button(
        "⬇️ Befüllte Packliste herunterladen",
        data=out,
        file_name=f"Packliste_befuellt_{_safe(up.name.replace('.xlsx',''))}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )
    st.info("Lade die befüllte Datei anschließend im Seller Central als Kartoninhalts-Datei hoch.")


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(s))[:60]


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    if not check_password():
        return

    st.title("📦 Sendungen erstellen")
    st.caption("Amazon FBA – Manifest & Packliste aus Lager-Scans automatisch erzeugen.")

    api_key = get_api_key()
    if not api_key:
        return

    tab1, tab2 = st.tabs(["1 · Manifest erstellen", "2 · Packliste befüllen"])
    with tab1:
        step_manifest(api_key)
    with tab2:
        step_packliste()


if __name__ == "__main__":
    main()
