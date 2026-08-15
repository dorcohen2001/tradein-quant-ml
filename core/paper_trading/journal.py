"""Excel trade-journal mechanism: upsert a row, matched by trade ID.

The two models' `paper_trading/journal.py` files were identical apart from the
column list and the corresponding row values -- Livne's schema carries the
multi-tier TP1/breakeven fields, Biton's is the older flat shape. Everything
else (open-or-create, find-row-by-ID, append-or-overwrite, save) was duplicated
verbatim.

So the SCHEMA stays with each model, where it belongs, and only the mechanism
lives here. That is why this takes `columns` and `values` rather than a trade
dict: this module deliberately knows nothing about what a trade looks like.

Matching by ID (not appending) is the point of the whole thing -- the sheet
mirrors the JSON store rather than growing a new row every time a status
changes.
"""
from __future__ import annotations

import os

from openpyxl import Workbook, load_workbook


def _open_or_create_workbook(xlsx_path: str, columns: list[str], sheet_title: str):
    if os.path.exists(xlsx_path):
        wb = load_workbook(xlsx_path)
        return wb, wb.active

    os.makedirs(os.path.dirname(xlsx_path), exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.append(columns)
    return wb, ws


def upsert_row(xlsx_path: str, columns: list[str], values: list,
               row_id, sheet_title: str = "Trades") -> None:
    """Insert or update the row whose first column equals `row_id`.

    Note this does NOT rewrite the header of an existing workbook. A journal
    written under an older column set keeps its original header, so historical
    sheets stay readable exactly as before rather than being silently
    reinterpreted under a new schema.
    """
    wb, ws = _open_or_create_workbook(xlsx_path, columns, sheet_title)

    target_row = None
    for (id_cell,) in ws.iter_rows(min_row=2, max_col=1):
        if id_cell.value == row_id:
            target_row = id_cell.row
            break

    if target_row is None:
        ws.append(values)
    else:
        for col_idx, value in enumerate(values, start=1):
            ws.cell(row=target_row, column=col_idx, value=value)

    wb.save(xlsx_path)
