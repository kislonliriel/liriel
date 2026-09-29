"""
One-time import of the original reference dataset from
MatrixObjectsValence_Rev000.xlsx (the architecture's own worked example:
Liriel's self-image, Veronica, Fábio, Adriana, and the marital-crisis
scenario) into whatever database `.env` points at.

Usage:
    python scripts/import_reference_mov.py /path/to/MatrixObjectsValence_Rev000.xlsx

The workbook has three sheets, each one MOV (MS §6.8's specular-recursion
nesting, one level per sheet):

    MOV_0000  -> Liriel's own focus. Imported as `settings.default_mov_id`
                 (the MOV the terminal chat actually runs on).
    MOV_0002  -> Fábio's focus, as Liriel models it (nested under VOV_0002
                 in MOV_0000). Imported as its own mov_id "MOV_0002".
    MOV_0000B -> Liriel-as-Fábio-imagines-her's own focus — third mirror
                 level, nested under VOV_0000B in MOV_0002. Imported as
                 "MOV_0000B".

One deliberate deviation from the spreadsheet's literal text: the
MOV_0000B sheet's own self-row reuses the ID "VOV_0000B" (the same ID as
the row *in MOV_0002* that points at this sheet as its nested MOV). Our
schema's vov_id is a single global primary key — not scoped per MOV — so
storing both would collide. That self-row is imported as "VOV_0000B_L3"
instead (third mirror level); every other ID is preserved exactly as
written.

Column layout (rows 1-3 are section/subsection/field headers, row 4 is a
decorative musical-note row, data starts at row 5): columns 1-13 are the
fixed VOV fields (Priority, VOV ID, ObjectType, ...); from column 14
onward every named column is immediately followed by a confidence ("C")
column, covering the 14 Feelings, then Ordinances (Instincts/Archetypes/
PersistentLongings), then Modulating Schemas (Culture/Character/
Personality/Intelligence/MindVices/Disorders/BodyFeatures) — see MS §3-§5.
"""
from __future__ import annotations

import sys
from pathlib import Path

import openpyxl
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from database import get_database  # noqa: E402
from models import VectorObjectValence  # noqa: E402

FIELD_COLUMNS = 13  # columns 1-13: Priority .. RelevantRemarks
FORCES_START = 14
ORDINANCES_START = 42
SCHEMAS_START = 78

# The 14 Feeling axes (MS §3) as spelled in the spreadsheet -> the
# axis_key spelling the database/models.py actually use.
FEELING_AXIS_RENAME = {
    "BodySensations: PleasurePain": "BodySensationsPleasurePain",
    "BodySensations": "BodySensationsOther",
}

SHEET_TO_MOV = {
    # filled in at runtime once we know settings.default_mov_id
}

PLACEHOLDER_TOKENS = {"na", "n/a", "none", "null", "-", ""}


def _clean(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return None if v.lower() in PLACEHOLDER_TOKENS else v
    return v


def _clean_str(v):
    """Like _clean, but for fields typed as Optional[str] (perceived_age,
    male_female) that openpyxl may hand back as a float (e.g. 26.0 for a
    numeric-looking cell) — stringify, dropping a spurious ".0"."""
    v = _clean(v)
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _header_pairs(ws, start_col: int, end_col: int) -> list[tuple[str, int, int]]:
    """(name, value_col, confidence_col) for every named column in range,
    assuming a confidence column immediately follows each value column."""
    pairs = []
    c = start_col
    while c <= end_col:
        raw = ws.cell(row=3, column=c).value
        name = str(raw).split("\n")[0].strip() if raw else None
        if name:
            pairs.append((name, c, c + 1))
            c += 2
        else:
            c += 1
    return pairs


def _parse_sheet(ws, mov_id: str, id_overrides: dict) -> list[dict]:
    last_col = ws.max_column
    feeling_pairs = _header_pairs(ws, FORCES_START, ORDINANCES_START - 1)
    ordinance_pairs = _header_pairs(ws, ORDINANCES_START, SCHEMAS_START - 1)
    schema_pairs = _header_pairs(ws, SCHEMAS_START, last_col)

    rows = []
    for r in range(5, ws.max_row + 1):
        vov_id = _clean(ws.cell(row=r, column=3).value)
        if not vov_id:
            continue

        priority = _clean(ws.cell(row=r, column=1).value)
        raw = {
            "vov_id": id_overrides.get(vov_id, vov_id),
            "priority": int(priority) if priority is not None else None,
            "update_datetime": _clean(ws.cell(row=r, column=2).value),
            "object_type": _clean(ws.cell(row=r, column=4).value) or "real",
            "object_nature": _clean(ws.cell(row=r, column=5).value),
            "valence_regime": _clean(ws.cell(row=r, column=6).value) or "State",
            "nested_mov": _clean(ws.cell(row=r, column=7).value),
            "perceived_age": _clean_str(ws.cell(row=r, column=8).value),
            "male_female": _clean_str(ws.cell(row=r, column=9).value),
            "brief_description": _clean(ws.cell(row=r, column=10).value),
            "relevant_relations": [
                s.strip() for s in str(ws.cell(row=r, column=11).value or "").split(",") if s.strip()
            ],
            "relevant_remarks": _clean(ws.cell(row=r, column=13).value),
            "feelings": {},
            "ordinances": {},
            "schemas": {},
        }

        for name, vcol, ccol in feeling_pairs:
            val = _clean(ws.cell(row=r, column=vcol).value)
            if val is None:
                continue
            axis_key = FEELING_AXIS_RENAME.get(name, name)
            conf = _clean(ws.cell(row=r, column=ccol).value)
            raw["feelings"][axis_key] = {"v": float(val), "c": int(float(conf)) if conf is not None else None}

        for name, vcol, ccol in ordinance_pairs:
            val = _clean(ws.cell(row=r, column=vcol).value)
            if val is None:
                continue
            conf = _clean(ws.cell(row=r, column=ccol).value)
            raw["ordinances"][name] = {"v": float(val), "c": int(float(conf)) if conf is not None else None}

        for name, vcol, ccol in schema_pairs:
            val = _clean(ws.cell(row=r, column=vcol).value)
            if val is None:
                continue
            conf = _clean(ws.cell(row=r, column=ccol).value)
            entry_val = val if isinstance(val, str) else float(val)
            raw["schemas"][name] = {"v": entry_val, "c": int(float(conf)) if conf is not None else None}

        rows.append(raw)
    return rows


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python scripts/import_reference_mov.py <path to MatrixObjectsValence_Rev000.xlsx>")
        sys.exit(1)

    xlsx_path = Path(sys.argv[1])
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)

    sheet_to_mov = {
        "MOV_0000": settings.default_mov_id,
        "MOV_0002": "MOV_0002",
        "MOV_0000B": "MOV_0000B",
    }
    # MOV_0000B sheet's own self-row reuses "VOV_0000B" — collides with the
    # row of that name in MOV_0002 (see module docstring). Only that sheet
    # needs the override.
    id_overrides_by_sheet = {
        "MOV_0000": {},
        "MOV_0002": {},
        "MOV_0000B": {"VOV_0000B": "VOV_0000B_L3"},
    }

    db = get_database()
    labels = {
        settings.default_mov_id: "Liriel's own focus (reference spreadsheet, MOV_0000)",
        "MOV_0002": "Fábio's focus, as Liriel models it (reference spreadsheet)",
        "MOV_0000B": "Liriel-as-Fábio-imagines-her's own focus (reference spreadsheet, 3rd mirror level)",
    }
    for mov_id, label in labels.items():
        db.ensure_mov(mov_id, label)

    total = 0
    for sheet_name, mov_id in sheet_to_mov.items():
        if sheet_name not in wb.sheetnames:
            print(f"[skip] sheet {sheet_name!r} not found in workbook")
            continue
        ws = wb[sheet_name]
        rows = _parse_sheet(ws, mov_id, id_overrides_by_sheet[sheet_name])
        for raw in rows:
            try:
                vov = VectorObjectValence.model_validate(raw)
            except ValidationError as exc:
                print(f"[warning] skipped {raw.get('vov_id')!r} from {sheet_name}: {exc}")
                continue
            db.replace_object(mov_id, vov)
            total += 1
            print(f"  {sheet_name} -> mov_id={mov_id}: upserted {vov.vov_id} ({vov.object_nature})")

    db.close()
    print(f"\nDone. {total} VOV(s) imported across {len(sheet_to_mov)} MOV(s).")


if __name__ == "__main__":
    main()
