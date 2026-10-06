from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import hashlib
import json
import re
import sqlite3

DB_PATH = Path(__file__).resolve().parent / "invosight.db"
APP_MODEL = "Donut"

FIELDS = (
    "invoice_number", "invoice_date", "due_date", "vendor_name",
    "customer_name", "subtotal", "discount", "tax", "total",
)
MONEY_FIELDS = frozenset(("subtotal", "discount", "tax", "total"))
COLAB_COLUMNS = ("id", "image_file", "model", "extracted_at", *FIELDS)
DETAIL_COLUMNS = (
    "original_json", "final_json", "validation_json", "validation_status",
    "modified_json", "image_hash", "updated_at",
)
CENT = Decimal("0.01")

FIELD_NAMES = {
    "invoice_number": "Invoice Number",
    "invoice_date": "Invoice Date",
    "due_date": "Due Date",
    "vendor_name": "Vendor Name",
    "customer_name": "Customer Name",
    "subtotal": "Subtotal",
    "discount": "Discount",
    "tax": "Tax Amount",
    "total": "Total Amount",
}


class InvoiceFilenameConflict(ValueError):
    """A different uploaded image already uses this filename for the model."""


def clean_text(value):
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = None
    return "" if value is None else str(value).strip()


def parse_money(value):
    """Parse an extracted amount; reject nonnumeric or excessive precision."""
    text = clean_text(value)
    if not text:
        return None
    text = text.replace(",", "").replace("$", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1].strip()
    try:
        amount = Decimal(text)
        if not amount.is_finite():
            return None
        rounded = amount.quantize(CENT, rounding=ROUND_HALF_UP)
        return rounded if amount == rounded else None
    except (InvalidOperation, ValueError):
        return None


@contextmanager
def connection():
    """Open the existing local copy of the Colab SQLite file."""
    if not DB_PATH.is_file():
        raise FileNotFoundError(
            f"Database not found: {DB_PATH}. Place the Colab invosight.db "
            "beside Database.py. Do not replace an existing file without a backup."
        )
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema():
    """Keep the original 13 Colab columns and store checks in a side table.

    Older 21-column local copies are accepted. Their original rows/columns are
    left intact, and existing check results are copied to the side table once.
    """
    with connection() as conn:
        existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(invoices)")
        }
        if not existing:
            raise RuntimeError("The selected file has no Colab invoices table.")
        missing = sorted(set(COLAB_COLUMNS) - existing)
        if missing:
            raise RuntimeError(f"Missing required Colab columns: {missing}")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS invoice_verification (
                invoice_id INTEGER PRIMARY KEY,
                original_json TEXT,
                final_json TEXT,
                validation_json TEXT,
                validation_status TEXT,
                modified_json TEXT,
                image_hash TEXT,
                updated_at TEXT,
                FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
            )
        """)
        detail_found = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(invoice_verification)")
        }
        detail_missing = sorted(set(DETAIL_COLUMNS) - detail_found)
        if detail_missing:
            raise RuntimeError(
                f"The verification table is missing columns: {detail_missing}"
            )

        # Existing local copies may have stored these seven values in invoices.
        # Preserve them without altering the original records or side-table data.
        if set(DETAIL_COLUMNS) <= existing:
            names = ", ".join(f'"{name}"' for name in DETAIL_COLUMNS)
            sources = ", ".join(f'i."{name}"' for name in DETAIL_COLUMNS)
            has_old_data = " OR ".join(
                f'i."{name}" IS NOT NULL' for name in DETAIL_COLUMNS
            )
            conn.execute(f"""
                INSERT INTO invoice_verification (invoice_id, {names})
                SELECT i.id, {sources}
                FROM invoices AS i
                WHERE ({has_old_data})
                  AND NOT EXISTS (
                      SELECT 1 FROM invoice_verification AS v
                      WHERE v.invoice_id = i.id
                  )
            """)


def overall_invoice_status(validation, modified):
    if modified or not isinstance(validation, dict):
        return "Needs Review"
    if all(
        isinstance(validation.get(key), dict)
        and validation[key].get("status") == "Valid"
        for key in FIELDS
    ):
        return "Validated"
    return "Needs Review"


def _all_records(conn, where="", params=(), limit=None):
    original = ", ".join(f'i."{name}" AS "{name}"' for name in COLAB_COLUMNS)
    details = ", ".join(f'v."{name}" AS "{name}"' for name in DETAIL_COLUMNS)
    sql = f"""
        SELECT {original}, {details}
        FROM invoices AS i
        LEFT JOIN invoice_verification AS v ON v.invoice_id = i.id
    """ + where + " ORDER BY i.id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params = (*params, max(1, min(int(limit), 1000)))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _save_verification(conn, invoice_id, original, final, validation,
                       modified, status, digest, timestamp):
    conn.execute("""
        INSERT INTO invoice_verification
            (invoice_id, original_json, final_json, validation_json,
             validation_status, modified_json, image_hash, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(invoice_id) DO UPDATE SET
            original_json=excluded.original_json,
            final_json=excluded.final_json,
            validation_json=excluded.validation_json,
            validation_status=excluded.validation_status,
            modified_json=excluded.modified_json,
            image_hash=excluded.image_hash,
            updated_at=excluded.updated_at
    """, (
        invoice_id,
        json.dumps(original, ensure_ascii=False),
        json.dumps(final, ensure_ascii=False),
        json.dumps(validation, ensure_ascii=False, default=str),
        status,
        json.dumps(modified, ensure_ascii=False),
        digest,
        timestamp,
    ))


def save_invoice(*, final_fields, original_fields, validation,
                 modified, image_file, image_bytes, model=APP_MODEL):
    """Write the extracted values to Colab's original invoices table."""
    ensure_schema()
    if not isinstance(final_fields, dict) or not isinstance(original_fields, dict):
        raise ValueError("Original and final fields must be dictionaries.")
    if not isinstance(validation, dict):
        raise ValueError("Validation results must be a dictionary.")
    name = clean_text(image_file)
    model = clean_text(model) or APP_MODEL
    if not name or not isinstance(image_bytes, bytes) or not image_bytes:
        raise ValueError("An uploaded invoice image and filename are required.")
    if not any(clean_text(final_fields.get(key)) for key in FIELDS):
        raise ValueError("Extract the invoice before saving it.")

    digest = hashlib.sha256(image_bytes).hexdigest()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    final = {key: clean_text(final_fields.get(key)) for key in FIELDS}
    original = {key: clean_text(original_fields.get(key)) for key in FIELDS}
    modified = sorted(set(modified or []) & set(FIELDS))
    amounts = {key: parse_money(final[key]) for key in MONEY_FIELDS}
    status = overall_invoice_status(validation, modified)
    if any(final[key] and amounts[key] is None for key in MONEY_FIELDS):
        raise ValueError("One or more extracted amounts cannot be stored as numbers.")

    values = []
    for key in FIELDS:
        if key in MONEY_FIELDS:
            values.append(float(amounts[key]) if amounts[key] is not None else None)
        else:
            values.append(final[key] or None)

    with connection() as conn:
        found = conn.execute("""
            SELECT i.id, v.image_hash
            FROM invoices AS i
            LEFT JOIN invoice_verification AS v ON v.invoice_id = i.id
            WHERE i.image_file=? AND i.model=?
        """, (name, model)).fetchone()
        if found is not None:
            if not found["image_hash"] or found["image_hash"] != digest:
                raise InvoiceFilenameConflict(
                    "This filename already exists for the same model. "
                    "Rename the uploaded image or inspect the existing record."
                )
            assignments = ", ".join(f'"{key}"=?' for key in FIELDS)
            conn.execute(
                f"UPDATE invoices SET {assignments} WHERE id=?",
                [*values, found["id"]],
            )
            invoice_id, operation = found["id"], "updated"
        else:
            columns = ("image_file", "model", "extracted_at", *FIELDS)
            col_sql = ", ".join(f'"{name}"' for name in columns)
            placeholders = ", ".join("?" for _ in columns)
            cursor = conn.execute(
                f"INSERT INTO invoices ({col_sql}) VALUES ({placeholders})",
                [name, model, stamp, *values],
            )
            invoice_id, operation = cursor.lastrowid, "inserted"
        _save_verification(
            conn, invoice_id, original, final, validation,
            modified, status, digest, stamp,
        )
    return {"id": invoice_id, "operation": operation, "status": status}


def dashboard_stats(model=APP_MODEL):
    """Counts only; the original fields do not establish amount units."""
    ensure_schema()
    where = " WHERE i.model=?" if model is not None else ""
    params = (model,) if model is not None else ()
    with connection() as conn:
        rows = _all_records(conn, where, params)
    validated = sum(row["validation_status"] == "Validated" for row in rows)
    review = sum(row["validation_status"] == "Needs Review" for row in rows)
    modified = 0
    for row in rows:
        try:
            modified += bool(json.loads(row["modified_json"] or "[]"))
        except (TypeError, ValueError):
            pass
    return {
        "saved": len(rows),
        "validated": validated,
        "needs_review": review,
        "unclassified": len(rows) - validated - review,
        "modified": modified,
    }


def _backup_before_change(conn):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    backup_path = DB_PATH.with_name(f"invosight-before-change-{stamp}.db")
    backup = sqlite3.connect(str(backup_path))
    try:
        conn.backup(backup)
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    finally:
        backup.close()
    return backup_path


def delete_invoice(invoice_id):
    ensure_schema()
    with connection() as conn:
        found = conn.execute(
            "SELECT id FROM invoices WHERE id=?", (int(invoice_id),)
        ).fetchone()
        if found is None:
            raise ValueError("This invoice no longer exists.")
        _backup_before_change(conn)
        conn.execute("DELETE FROM invoices WHERE id=?", (int(invoice_id),))
    return True


def delete_all_invoices(model=None):
    """Remove all records, or only one model when explicitly requested."""
    ensure_schema()
    clause = " WHERE model=?" if model is not None else ""
    params = (model,) if model is not None else ()
    with connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM invoices" + clause, params).fetchone()[0]
        if count:
            _backup_before_change(conn)
            conn.execute("DELETE FROM invoices" + clause, params)
    return count


def update_invoice_fields(invoice_id, updated_fields):
    """Edit stored values; affected checks must be reviewed again."""
    ensure_schema()
    if not isinstance(updated_fields, dict) or set(updated_fields) != set(FIELDS):
        raise ValueError("All invoice fields are required.")
    cleaned = {key: clean_text(updated_fields[key]) for key in FIELDS}
    if not cleaned["invoice_number"]:
        raise ValueError("Invoice Number cannot be empty.")
    amounts = {key: parse_money(cleaned[key]) for key in MONEY_FIELDS}
    for key in MONEY_FIELDS:
        if cleaned[key] and amounts[key] is None:
            raise ValueError(f"Enter a valid amount for {FIELD_NAMES[key]}.")
        if amounts[key] is not None and amounts[key] < 0:
            raise ValueError("Amounts cannot be negative.")

    with connection() as conn:
        records = _all_records(conn, " WHERE i.id=?", (int(invoice_id),))
        if not records:
            raise ValueError("This invoice no longer exists.")
        current = records[0]
        try:
            before = json.loads(current["final_json"] or "null")
        except (TypeError, ValueError):
            before = None
        if not isinstance(before, dict):
            before = {key: current.get(key) for key in FIELDS}
        before = {key: clean_text(before.get(key)) for key in FIELDS}
        changes = {key for key in FIELDS if before[key] != cleaned[key]}
        if not changes:
            return {"changed": False, "fields": []}

        try:
            original = json.loads(current["original_json"] or "null")
        except (TypeError, ValueError):
            original = None
        if not isinstance(original, dict):
            original = before
        original = {key: clean_text(original.get(key)) for key in FIELDS}
        modified = sorted(key for key in FIELDS if cleaned[key] != original[key])
        try:
            validation = json.loads(current["validation_json"] or "{}")
        except (TypeError, ValueError):
            validation = {}
        if not isinstance(validation, dict):
            validation = {}
        for key in changes:
            validation[key] = {
                "status": "Needs Review", "reason_code": "manual_update",
                "rule": "Manual database edit",
                "message": "Updated manually; check against the source invoice.",
            }
        if changes & MONEY_FIELDS:
            for key in MONEY_FIELDS:
                validation[key] = {
                    "status": "Needs Review", "reason_code": "manual_update",
                    "rule": "Recheck related amounts",
                    "message": "An amount changed; verify the linked amounts again.",
                }
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _backup_before_change(conn)
        values = []
        for key in FIELDS:
            if key in MONEY_FIELDS:
                values.append(float(amounts[key]) if amounts[key] is not None else None)
            else:
                values.append(cleaned[key] or None)
        assignments = ", ".join(f'"{key}"=?' for key in FIELDS)
        conn.execute(
            f"UPDATE invoices SET {assignments} WHERE id=?",
            [*values, int(invoice_id)],
        )
        _save_verification(
            conn, int(invoice_id), original, cleaned, validation,
            modified, "Needs Review", current["image_hash"], stamp,
        )
    return {"changed": True, "fields": sorted(changes)}


def list_invoices(limit=200, search="", status=None, model=None):
    ensure_schema()
    clauses, params = [], []
    if search:
        clauses.append(
            "(i.invoice_number LIKE ? OR i.vendor_name LIKE ? "
            "OR i.customer_name LIKE ? OR i.image_file LIKE ?)"
        )
        params.extend(["%" + search + "%"] * 4)
    if status in ("Unclassified", "Not Verified"):
        clauses.append("v.validation_status IS NULL")
    elif status in ("Validated", "Needs Review"):
        clauses.append("v.validation_status = ?")
        params.append(status)
    if model is not None:
        clauses.append("i.model = ?")
        params.append(model)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connection() as conn:
        return _all_records(conn, where, tuple(params), limit=limit)


def _show(value):
    return "—" if value is None or value == "" else str(value)


def render_database_page():
    """Browse, edit and delete invoices; show only fields and their checks."""
    import streamlit as st
    from html import escape as html_escape

    def _status_badge(value):
        label = str(value or "Not Verified")
        status = label.strip().lower()
        if status in {"valid", "validated", "modified & verified"}:
            background, foreground = "#E7F8F0", "#166D4B"
        elif status in {"needs review", "review", "pending verification"}:
            background, foreground = "#FFF0DB", "#9C5D08"
        else:
            background, foreground = "#EEF2F7", "#64748B"
        return (
            f'<span style="display:inline-block;padding:5px 12px;'
            f'border-radius:999px;background:{background};color:{foreground};'
            f'font-weight:600;white-space:nowrap">{html_escape(label)}</span>'
        )

    def _render_status_table(headers, rows, status_index):
        """Use HTML badges: Streamlit's dataframe Styler may not show cell colors."""
        header_style = (
            "padding:12px 14px;text-align:left;font-weight:500;"
            "background:#F7FAFE;color:#5F7087;border-bottom:1px solid #DFE6F0;"
        )
        cell_style = (
            "padding:11px 14px;text-align:left;color:#334155;"
            "border-bottom:1px solid #E5EAF2;vertical-align:middle;"
        )
        parts = [
            '<div style="width:100%;overflow-x:auto;border:1px solid #DFE6F0;'
            'border-radius:12px;background:#FFFFFF">',
            '<table style="width:100%;border-collapse:collapse;'
            'font-size:14px;line-height:1.45">',
            '<thead><tr>',
        ]
        parts.extend(
            f'<th style="{header_style}">{html_escape(str(header))}</th>'
            for header in headers
        )
        parts.append('</tr></thead><tbody>')
        for row in rows:
            parts.append('<tr>')
            for index, value in enumerate(row):
                content = (
                    _status_badge(value) if index == status_index
                    else html_escape(str(value))
                )
                parts.append(f'<td style="{cell_style}">{content}</td>')
            parts.append('</tr>')
        parts.append('</tbody></table></div>')
        st.markdown(''.join(parts), unsafe_allow_html=True)

    st.title("Database")
    st.caption("Extracted invoice fields and their validation results.")
    if st.session_state.pop("database_updated_notice", False):
        st.success("Invoice changes saved. Updated fields require review.")
    if st.session_state.pop("database_deleted_notice", False):
        st.success("Invoice records deleted successfully.")

    try:
        ensure_schema()
        search_col, status_col = st.columns([3, 1])
        with search_col:
            keyword = st.text_input(
                "Search invoices", placeholder="Invoice number, vendor or customer"
            )
        with status_col:
            status_choice = st.selectbox(
                "Invoice status", ["All", "Validated", "Needs Review", "Not Verified"]
            )
        records = list_invoices(
            limit=1000,
            search=keyword.strip(),
            status=None if status_choice == "All" else status_choice,
            model=APP_MODEL,
        )
    except (FileNotFoundError, RuntimeError, sqlite3.Error, ValueError) as exc:
        st.error(str(exc))
        return

    st.caption(f"{len(records)} invoice(s) found.")
    if not records:
        st.info("No matching invoice records.")
        return

    st.subheader("Saved invoices")
    _render_status_table(
        ("Invoice Number", "Vendor Name", "Customer Name", "Status"),
        [
            (
                _show(row.get("invoice_number")),
                _show(row.get("vendor_name")),
                _show(row.get("customer_name")),
                row.get("validation_status") or "Not Verified",
            )
            for row in records
        ],
        status_index=3,
    )
    options = {
        f"{_show(row.get('invoice_number'))} · #{row['id']}": row
        for row in records
    }
    selected = options[st.selectbox("Select an invoice", list(options))]
    try:
        checks = json.loads(selected.get("validation_json") or "{}")
    except (TypeError, ValueError):
        checks = {}
    if not isinstance(checks, dict):
        checks = {}

    st.subheader("Extracted fields and validation")
    _render_status_table(
        ("Field", "Extracted Value", "Validation Status"),
        [
            (
                FIELD_NAMES[key],
                _show(selected.get(key)),
                (
                    checks.get(key, {}).get("status", "Not Verified")
                    if isinstance(checks.get(key), dict) else "Not Verified"
                ),
            )
            for key in FIELDS
        ],
        status_index=2,
    )

    with st.expander("Edit selected invoice"):
        st.caption("Manual edits are saved and flagged for review.")
        with st.form(key=f"database_edit_form_{selected['id']}"):
            edited = {
                key: st.text_input(
                    FIELD_NAMES[key],
                    value=clean_text(selected.get(key)),
                    key=f"database_edit_{selected['id']}_{key}",
                ) for key in FIELDS
            }
            save_edit = st.form_submit_button(
                "Save Invoice Changes", type="primary", use_container_width=True
            )
        if save_edit:
            try:
                result = update_invoice_fields(selected["id"], edited)
                if result["changed"]:
                    st.session_state.database_updated_notice = True
                    st.rerun()
                else:
                    st.info("No changes were made.")
            except (ValueError, sqlite3.Error) as exc:
                st.error(str(exc))

    with st.expander("Delete invoices"):
        st.warning("A database backup is created before deletion.")
        confirm_one = st.checkbox(
            "I confirm that I want to delete the selected invoice",
            key=f"database_confirm_{selected['id']}",
        )
        if st.button(
            "Delete Selected Invoice", disabled=not confirm_one,
            key="database_delete_selected",
        ):
            try:
                delete_invoice(selected["id"])
                st.session_state.database_deleted_notice = True
                st.rerun()
            except (ValueError, sqlite3.Error) as exc:
                st.error(str(exc))

        confirmation = st.text_input(
            "Type DELETE ALL to delete all saved Donut invoices",
            key="database_delete_all_confirmation",
        )
        if st.button(
            "Delete All Donut Invoices",
            disabled=confirmation.strip() != "DELETE ALL",
            key="database_delete_all",
        ):
            try:
                delete_all_invoices(model=APP_MODEL)
                st.session_state.database_deleted_notice = True
                st.rerun()
            except sqlite3.Error as exc:
                st.error(str(exc))
