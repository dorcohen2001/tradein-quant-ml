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
import sys

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
               row_id, sheet_title: str = "Trades") -> bool:
    """Insert or update the row whose first column equals `row_id`.

    Returns True on success, False if the write failed. **Never raises.**

    THIS IS LOAD-BEARING. The journal is a convenience mirror of the JSON trade
    store, but it is an Excel file, and on Windows `wb.save()` raises
    PermissionError whenever the workbook is open in Excel -- something an
    operator reviewing a tenant's trades does routinely.

    Before this was non-raising, that exception propagated out of
    `update_open_positions` AFTER the trade store had already been written but
    BEFORE the caller could send its Telegram alerts. The status transition was
    therefore persisted while the alert was never sent, and because the trade
    was no longer in `changed` on the next cycle, the alert was lost
    permanently. A spreadsheet being open must never cost a tenant an exit
    alert.

    Note this does NOT rewrite the header of an existing workbook. A journal
    written under an older column set keeps its original header, so historical
    sheets stay readable exactly as before rather than being silently
    reinterpreted under a new schema.
    """
    wb = None
    try:
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
        return True
    except PermissionError as exc:
        print(f"[journal] {os.path.basename(xlsx_path)} is locked (open in Excel?): {exc}. "
              f"Row {row_id} was NOT journalled; the trade store and Telegram alerts are "
              f"unaffected. It will be rewritten on this trade's next status change.",
              file=sys.stderr)
        return False
    except Exception as exc:  # noqa: BLE001 -- a journal write must never break a cycle
        print(f"[journal] failed to write {os.path.basename(xlsx_path)} for row {row_id}: "
              f"{type(exc).__name__}: {exc}. Trade store and alerts unaffected.",
              file=sys.stderr)
        return False
    finally:
        # A workbook loaded from disk holds an open ZipFile. If `save` failed we
        # never closed it, and the interpreter later prints a confusing
        # "Exception ignored in ZipFile.__del__ / PermissionError" during GC --
        # noise that looks like a second, unhandled fault in the logs.
        if wb is not None:
            try:
                wb.close()
            except Exception:  # noqa: BLE001 -- closing must not resurrect the failure
                pass
