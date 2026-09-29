from __future__ import annotations

import io
import os
import re
import shutil
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from PIL import Image, ImageOps


FIELD_ALIASES = {
    "invoice_number": [r"invoice\s*(?:number|no\.?|#|id)", r"inv\.?\s*(?:no\.?|#)", r"invoice\s*ref(?:erence)?"],
    "invoice_date": [r"invoice\s*date", r"date\s*of\s*issue", r"issued\s*(?:on|date)", r"issue\s*date"],
    "due_date": [r"payment\s*due\s*(?:date)?", r"due\s*date", r"pay\s*by"],
    "vendor_name": [r"vendor\s*(?:name)?", r"seller\s*(?:name)?", r"bill\s*from", r"supplier\s*(?:name)?"],
    "customer_name": [r"bill\s*to", r"billed\s*to", r"customer\s*(?:name)?", r"client\s*(?:name)?", r"buyer\s*(?:name)?"],
    "subtotal": [r"sub\s*total", r"net\s*subtotal"],
    "discount": [r"discount\s*(?:amount)?", r"less\s*discount"],
    "tax": [r"sales\s*tax", r"tax\s*amount", r"vat\s*(?:amount)?", r"tax"],
    "total": [r"grand\s*total", r"invoice\s*total", r"total\s*amount", r"total"],
}
AMBIGUOUS_TOTAL = [r"amount\s*due", r"total\s*due"]
CREDIT_WORDS = re.compile(r"\b(?:paid|prepaid|deposit|balance\s*due|credit|payment\s*received)\b", re.I)
_AMOUNT = re.compile(r"(?<![\w.])[-+]?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?![\w.])")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _alnum(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", _clean(value).casefold())


def _money(value: Any) -> Decimal | None:
    try:
        text = re.sub(r"(?i)\b(?:USD|SAR|EUR|GBP|AED)\b|[$€£]", "", _clean(value))
        n = Decimal(text.replace(",", "").strip())
        return n if n.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _date(value: Any) -> date | None:
    text = _clean(value)
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        try:
            return date(*(int(x) for x in match.groups()))
        except ValueError:
            return None
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass

    return None


def run_ocr(image_bytes: bytes) -> dict:


    result = {"available": False, "lines": [], "message": ""}
    try:
        import pytesseract
        from pytesseract import Output
    except ImportError:
        result["message"] = "Install pytesseract and Tesseract to enable source-text checks."
        return result

    binary = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")


    if not binary and os.name == "nt":
        windows_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if os.path.isfile(windows_path):
            binary = windows_path
    if not binary or not os.path.isfile(binary):
        result["message"] = (
            "Tesseract executable not found; set TESSERACT_CMD or install "
            "Tesseract and add it to PATH. Arithmetic checks remain available."
        )
        return result
    pytesseract.pytesseract.tesseract_cmd = binary
    try:
        with Image.open(io.BytesIO(image_bytes)) as original:
            image = ImageOps.exif_transpose(original).convert("RGB")

        if max(image.size) > 2500:
            image.thumbnail((2500, 2500))
        data = pytesseract.image_to_data(
            image, config="--psm 11", output_type=Output.DICT, timeout=45
        )
        groups: dict[tuple, list] = defaultdict(list)
        for i, word in enumerate(data.get("text", [])):
            token = _clean(word)
            try:
                confidence = float(data["conf"][i])
            except (ValueError, TypeError, KeyError):
                continue
            if not token or confidence < 25:
                continue
            key = tuple(int(data[k][i]) for k in ("page_num", "block_num", "par_num", "line_num"))
            groups[key].append({
                "text": token,
                "x": int(data["left"][i]),
                "y": int(data["top"][i]),
                "w": int(data["width"][i]),
                "h": int(data["height"][i]),
                "confidence": confidence,
            })
        lines = []
        for words in groups.values():
            words.sort(key=lambda x: x["x"])
            x1, y1 = min(w["x"] for w in words), min(w["y"] for w in words)
            x2 = max(w["x"] + w["w"] for w in words)
            y2 = max(w["y"] + w["h"] for w in words)
            lines.append({
                "text": " ".join(w["text"] for w in words),
                "x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1,
                "confidence": round(sum(w["confidence"] for w in words) / len(words), 1),


                "words": words,
            })
        lines.sort(key=lambda line: (line["y"], line["x"]))
        result.update(available=True, lines=lines, message=f"OCR read {len(lines)} text lines.")
    except Exception as exc:
        result["message"] = f"Optional OCR failed: {type(exc).__name__}: {exc}"
    return result


_LABELS_ANY = re.compile(
    r"\b(?:invoice\s*(?:number|no\.?|date|id)|due\s*date|payment\s*due|"
    r"sub\s*total|net\s*subtotal|grand\s*total|total\s*amount|"
    r"tax\s*rate|sales\s*tax|tax\s*amount|vat|discount|"
    r"bill\s*to|bill\s*from|vendor|supplier|customer|client|buyer)\b",
    re.I,
)
_PERCENT = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*%")
_INVOICE_CODE = re.compile(
    r"(?<![A-Z\d])(?=[A-Z\d/-]*\d)[A-Z]{1,12}[-/][A-Z\d-]{2,}(?![A-Z\d])",
    re.I,
)
_DATE_FRAGMENT = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}|"
    r"[A-Za-z]{3,9}\s+\d{1,2},?\s*\d{4}|"
    r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}"
)


def _line_text(line: dict) -> str:
    return _clean(line.get("text", ""))


def _xywh(line: dict) -> tuple[float, float, float, float]:
    return (float(line.get("x", 0)), float(line.get("y", 0)),
            float(line.get("w", 0)), max(12., float(line.get("h", 16))))


def _label_box(line: dict, start: int, end: int) -> tuple[float, float, float, float]:

    x, y, w, h = _xywh(line)
    words = line.get("words") or []
    if words:
        spans, offset = [], 0
        for word in words:
            token = _clean(word.get("text", ""))
            if not token:
                continue
            spans.append((offset, offset + len(token), word))
            offset += len(token) + 1
        hits = [word for a, b, word in spans if a < end and b > start]
        if hits:
            left = min(float(q["x"]) for q in hits)
            right = max(float(q["x"]) + float(q["w"]) for q in hits)
            top = min(float(q["y"]) for q in hits)
            bottom = max(float(q["y"]) + float(q["h"]) for q in hits)
            return left, top, right - left, max(12., bottom - top)


    length = max(1, len(_line_text(line)))
    return x + w * start / length, y, max(1., w * (end-start) / length), h


def _truncate_at_next_label(text: str) -> str:
    other = _LABELS_ANY.search(text)
    return _clean(text[:other.start()] if other else text).lstrip(" :-#|\t")


def _parse_candidates(key: str, candidate: str) -> list:

    candidate = _clean(candidate)
    if key == "total":
        strict = re.fullmatch(
            r"(?:(?:USD|SAR|EUR|GBP|AED)\s*|[$€£]\s*)?"
            r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}",
            candidate,
            re.I,
        )
        return [_money(candidate)] if strict and _money(candidate) is not None else []
    if key in {"subtotal", "discount", "tax"}:
        candidate = re.sub(r"\d+(?:\.\d+)?\s*%", " ", candidate)
        result = []
        for m in _AMOUNT.finditer(candidate):
            amount = _money(m.group())
            if amount is not None:
                result.append(amount)
        return result
    if key in {"invoice_date", "due_date"}:
        return [val for m in _DATE_FRAGMENT.finditer(candidate)
                if (val := _date(m.group())) is not None]
    if key == "invoice_number":
        return [_alnum(m.group()) for m in _INVOICE_CODE.finditer(candidate)]
    return [_alnum(candidate)] if _alnum(candidate) else []


def _desired(key: str, value: Any):
    if key in {"subtotal", "discount", "tax", "total"}:
        return _money(value)
    if key in {"invoice_date", "due_date"}:
        return _date(value)
    return _alnum(value)


def _equivalent(key: str, expected, observed) -> bool:
    if key in {"subtotal", "discount", "tax", "total"}:
        return (expected is not None and isinstance(observed, Decimal)
                and abs(expected-observed) <= Decimal("0.01"))
    return bool(expected) and expected == observed


def _suitable_value_text(key: str, text: str) -> bool:

    parsed = _parse_candidates(key, text)
    if key in {"vendor_name", "customer_name"}:
        clean = _clean(text)
        return (len(_alnum(clean)) >= 4
                and not _LABELS_ANY.match(clean)
                and not re.match(r"^\d+\s+", clean))

    return len(parsed) == 1


def _below_candidates(key: str, lines: list[dict], label_line: dict,
                      label_box: tuple) -> list[dict]:

    lx, ly, lw, lh = label_box
    label_center = lx + lw / 2
    limit = max(32., lh * 3.8)
    results = []
    for target in lines:
        if target is label_line:
            continue
        tx, ty, tw, th = _xywh(target)
        gap = ty - (ly + lh)
        if gap < -2 or gap > limit:
            continue
        if key in {"vendor_name", "customer_name"}:


            if (abs(tx - lx) <= max(65., 1.25 * lw)
                    and _suitable_value_text(key, _line_text(target))):
                results.append({"text": _line_text(target),
                                "confidence": min(float(label_line.get("confidence", 0)),
                                                  float(target.get("confidence", 0))),
                                "geometry": "below", "label": _line_text(label_line)})
            continue
        words = target.get("words") or []
        if words:


            near = [w for w in words
                    if abs(float(w["x"]) + float(w["w"]) / 2 - label_center)
                    <= max(52., lw * .75)]
            if key in {"subtotal", "discount", "tax", "total"}:
                near = [w for w in near if len(_parse_candidates(key, w["text"])) == 1]
            elif key in {"invoice_date", "due_date", "invoice_number"}:
                near = [w for w in near if _suitable_value_text(key, w["text"])]
            if near:
                near.sort(key=lambda w: abs(float(w["x"]) + float(w["w"])/2-label_center))
                word = near[0]
                results.append({"text": word["text"], "confidence": min(float(word.get("confidence", 0)),
                                float(label_line.get("confidence", 0))),
                                "geometry": "below", "label": _line_text(label_line)})
            elif key in {"vendor_name", "customer_name"} and _suitable_value_text(key, _line_text(target)):


                if abs(tx - lx) <= max(40., lw * .5):
                    results.append({"text": _line_text(target),
                                    "confidence": min(float(label_line.get("confidence", 0)),
                                                      float(target.get("confidence", 0))),
                                    "geometry": "below", "label": _line_text(label_line)})
        else:


            if abs(tx - lx) <= max(65., lw * .55) and _suitable_value_text(key, _line_text(target)):
                results.append({"text": _line_text(target),
                                "confidence": min(float(label_line.get("confidence", 0)),
                                                  float(target.get("confidence", 0))),
                                "geometry": "below", "label": _line_text(label_line)})
    results.sort(key=lambda r: r["confidence"], reverse=True)
    return results[:2]


def _same_row_candidates(key: str, lines: list[dict], label_line: dict,
                         label_box: tuple) -> list[dict]:
    lx, ly, lw, lh = label_box
    results = []
    for target in lines:
        if target is label_line:
            continue
        tx, ty, tw, th = _xywh(target)


        if abs((ty+th/2) - (ly+lh/2)) > max(27., lh * 1.5):
            continue
        if tx < lx + lw - 4:
            continue


        if _suitable_value_text(key, _line_text(target)):
            results.append({"text": _line_text(target),
                            "confidence": min(float(label_line.get("confidence", 0)),
                                              float(target.get("confidence", 0))),
                            "geometry": "same_row", "label": _line_text(label_line)})
    results.sort(key=lambda r: float(r["confidence"]), reverse=True)


    return results if len(results) == 1 else []


def _is_item_total_header(line: dict, lines: list[dict]) -> bool:
    text = _line_text(line)
    if not re.fullmatch(r"\s*total\s*", text, re.I):
        return False
    y = float(line.get("y", 0))
    return any(
        other is not line
        and abs(float(other.get("y", 0)) - y) <= 27
        and re.search(r"\b(?:qty|quantity|unit\s*price|description)\b", _line_text(other), re.I)
        for other in lines
    )


def _footer_total_candidates(lines: list[dict]) -> list[dict]:
    matches = []
    for label in lines:
        text = _line_text(label)
        if _is_item_total_header(label, lines):
            continue
        if not re.match(r"^\s*(?:(?:grand|invoice)\s+)?total\s*(?::|$|\s+(?:USD|SAR|EUR|GBP|AED|[$€£]|[0-9]))", text, re.I):
            continue
        if CREDIT_WORDS.search(text) or re.search(r"\b(?:subtotal|tax|discount|items|quantity)\b", text, re.I):
            continue
        m = re.search(r"\btotal\b", text, re.I)
        if not m:
            continue
        suffix = _clean(text[m.end():]).lstrip(" :-#|\t")
        if suffix and len(_parse_candidates("total", suffix)) == 1:
            matches.append({"text": suffix, "confidence": float(label.get("confidence", 0)),
                            "geometry": "footer_same_line", "label": text,
                            "y": float(label.get("y", 0)), "ambiguous": False})
            continue
        candidates = _same_row_candidates("total", lines, label, _label_box(label, m.start(), m.end()))
        if len(candidates) == 1:
            item = dict(candidates[0])
            item.update(y=float(label.get("y", 0)), ambiguous=False, geometry="footer_same_row")
            matches.append(item)
    if not matches:
        return []
    bottom = max(item["y"] for item in matches)
    return [item for item in matches if bottom - item["y"] <= 25]


def _associated(key: str, lines: list[dict]) -> list[dict]:
    patterns = [(p, False) for p in FIELD_ALIASES.get(key, [])]
    if key == "total":
        patterns += [(p, True) for p in AMBIGUOUS_TOTAL]
    candidates = []
    for line in lines:
        text = _line_text(line)
        handled = False
        for pattern, ambiguous in patterns:
            for match in re.finditer(r"(?<!\w)(?:" + pattern + r")(?!\w)", text, re.I):


                if key == "total" and _is_item_total_header(line, lines):
                    continue
                if key == "total" and re.fullmatch(r"total", text, re.I):
                    y = float(line.get("y", 0))
                    near_headers = {word for other in lines if other is not line
                                    and abs(float(other.get("y", 0)) - y) <= 14
                                    for word in ("qty", "unit price", "description")
                                    if word in _line_text(other).casefold()}
                    if near_headers:
                        continue


                if key == "total" and pattern == r"total" and re.search(
                        r"\btotal\s*due\b", text, re.I):
                    continue
                if key == "total" and (
                    re.search(r"\b(?:qty|description|unit\s*price|total\s*paid|balance\s*due)\b", text, re.I)
                    or re.search(r"\btotal\s+(?:tax|discount|paid|items|quantity)\b", text, re.I)
                ):
                    continue


                if key == "tax" and re.match(r"\s*rate\b", text[match.end():], re.I):
                    continue
                suffix = _truncate_at_next_label(text[match.end():])
                prior_label = _LABELS_ANY.search(text[:match.start()])
                if match.start() > 15 and not line.get("words") and not prior_label and not suffix:
                    continue
                label_box = _label_box(line, match.start(), match.end())
                if suffix and _suitable_value_text(key, suffix):
                    candidates.append({"text": suffix,
                                       "confidence": float(line.get("confidence", 0)),
                                       "geometry": "same_line", "ambiguous": ambiguous,
                                       "label": text})
                else:
                    others = (_same_row_candidates(key, lines, line, label_box)
                              + _below_candidates(key, lines, line, label_box))
                    for candidate in others:
                        candidate["ambiguous"] = ambiguous
                    candidates.extend(others)
                handled = True
                break
            if handled:
                break
    return candidates


def _unlabeled_vendor(key: str, value: Any, lines: list[dict]) -> dict | None:


    if key != "vendor_name":
        return None
    bill_to = [line for line in lines
               if re.search(r"^\s*(?:bill\s*to|billed\s*to)\s*:?[\s]*$", _line_text(line), re.I)]
    if not bill_to:
        return None
    top_bill = min(float(l.get("y", 0)) for l in bill_to)
    matches = [line for line in lines
               if _alnum(_line_text(line)) == _alnum(value)
               and _line_text(line) and float(line.get("confidence", 0)) >= 85
               and (float(line.get("y", 0)) + float(line.get("h", 0))) < top_bill]
    if len(matches) == 1:
        return {"status": "Valid", "message":
                "Exact OCR name in supplier header above the distinct BILL TO block."}
    return None


def extract_ocr_rates(lines: list[dict]) -> dict[str, str | None]:

    out: dict[str, str | None] = {"discount_rate": None, "tax_rate": None}
    patterns = {"discount_rate": r"\bdiscount(?:\s*rate)?\b",
                "tax_rate": r"\b(?:tax|vat|sales\s*tax)\s*rate\b|\b(?:vat|tax)\b"}
    for key, pattern in patterns.items():
        found = set()
        for line in lines:
            text = _line_text(line)
            match = re.search(pattern, text, re.I)
            if not match:
                continue
            if key == "tax_rate" and re.search(r"\btax\s*amount\b", text, re.I):
                continue
            suffix = _truncate_at_next_label(text[match.end():])
            immediate = _PERCENT.search(suffix)
            if immediate and 0 <= float(immediate.group(1)) <= 100:
                found.add(immediate.group(1))
                continue
            box = _label_box(line, match.start(), match.end())


            for target in lines:
                if target is line:
                    continue
                tx, ty, tw, th = _xywh(target)
                lx, ly, lw, lh = box
                gap = ty - (ly + lh)
                if gap < -2 or gap > max(32., lh * 3.8):
                    continue
                words = target.get("words") or []
                if words:
                    matches = [w for w in words if _PERCENT.fullmatch(_clean(w.get("text")))
                               and abs(float(w["x"]) + float(w["w"])/2 - (lx+lw/2))
                               <= max(52., lw * .75)]
                    if len(matches) == 1:
                        pct = _PERCENT.fullmatch(_clean(matches[0]["text"]))
                        if pct and float(pct.group(1)) <= 100:
                            found.add(pct.group(1))
                elif abs(tx-lx) <= max(65., lw * .55):
                    pct = _PERCENT.fullmatch(_line_text(target))
                    if pct and float(pct.group(1)) <= 100:
                        found.add(pct.group(1))
        if len(found) == 1:
            out[key] = next(iter(found))
    return out


_PARTY_ANCHORS = {
    "vendor_name": re.compile(
        r"^\s*(?:bill\s*from|from|vendor(?:\s+name)?|seller(?:\s+name)?|"
        r"supplier(?:\s+name)?)\b\s*:?\s*", re.I),
    "customer_name": re.compile(
        r"^\s*(?:bill\s*to|billed\s*to|customer(?:\s+name)?|"
        r"client(?:\s+name)?|buyer(?:\s+name)?)\b\s*:?\s*", re.I),
}
_PARTY_NONNAME = re.compile(
    r"\b(?:street|st\.?|suite|road|rd\.?|avenue|ave\.?|boulevard|blvd|"
    r"postal|postcode|zip|phone|email|canada|usa|invoice|details|description|"
    r"www\.|https?://|@)\b", re.I,
)
_PARTY_HEADER = re.compile(
    r"^\s*(?:bill\s*to|billed\s*to|bill\s*from|from|"
    r"vendor(?:\s+name)?|supplier(?:\s+name)?|seller(?:\s+name)?|"
    r"customer(?:\s+name)?|client(?:\s+name)?|buyer(?:\s+name)?|"
    r"invoice|details|ship\s*to|description)\s*:?\s*$", re.I,
)


def _party_text_candidate(text: str) -> bool:

    text = _clean(text)
    return (
        len(_alnum(text)) >= 2
        and not re.match(r"^\s*\d", text)
        and not _PARTY_NONNAME.search(text)
        and not _PARTY_HEADER.match(text)
        and not _DATE_FRAGMENT.search(text)
        and not _INVOICE_CODE.search(text)
        and not _AMOUNT.fullmatch(text)
    )


def _name_under_anchor(label: dict, lines: list[dict]) -> tuple[str, float] | None:
    lx, ly, lw, lh = _xywh(label)
    nearby = []
    for line in lines:
        if line is label:
            continue
        tx, ty, tw, th = _xywh(line)
        gap = ty - (ly + lh)
        if not -2 <= gap <= max(75., 5.0 * lh):
            continue
        if _party_text_candidate(_line_text(line)):
            nearby.append((line, tx, ty, tw, th, gap))

    aligned = [part for part in nearby if abs(part[1] - lx) <= max(70., 1.15 * lw)]
    if not aligned:
        return None
    aligned.sort(key=lambda part: (part[5], abs(part[1] - lx)))
    first = aligned[0]
    pieces = [first]
    fx, fy, fw, fh = first[1:5]
    edge = fx + fw

    same_row = sorted(
        (part for part in nearby if part is not first
         and abs(part[2] + part[4] / 2 - (fy + fh / 2)) <= max(9., fh * .65)
         and part[1] >= fx),
        key=lambda part: part[1],
    )
    for part in same_row:
        px, pw = part[1], part[3]
        if px >= edge - 4 and px - edge <= max(45., fh * 2.5):
            pieces.append(part)
            edge = px + pw

    line_end = max(part[2] + part[4] for part in pieces)
    used = {id(part[0]) for part in pieces}
    for _ in range(2):
        next_rows = [
            part for part in aligned
            if id(part[0]) not in used
            and line_end - 2 <= part[2] <= line_end + max(24., fh * 1.7)
            and abs(part[1] - fx) <= max(34., fh * 1.6)
        ]
        if not next_rows:
            break
        next_rows.sort(key=lambda part: (part[2], abs(part[1] - fx)))
        next_part = next_rows[0]
        pieces.append(next_part)
        used.add(id(next_part[0]))
        line_end = next_part[2] + next_part[4]

    observed = _clean(" ".join(_line_text(part[0]) for part in pieces))
    if len(_alnum(observed)) < 4:
        return None
    confidence = min(float(part[0].get("confidence", 0)) for part in pieces)
    return observed, confidence


def _anchored_party(key: str, value: Any, lines: list[dict]) -> dict | None:

    anchors = []
    for line in lines:
        text = _line_text(line)
        m = _PARTY_ANCHORS[key].match(text)
        if m:

            anchors.append((line, _clean(text[m.end():]).lstrip(':-| ')))
    if not anchors:
        return None

    readings = []
    for label, inline in anchors:
        if float(label.get("confidence", 0)) < 45:
            return {"status": "Not Verified", "message":
                    "The party heading was read with low OCR confidence."}
        if inline and _party_text_candidate(inline):
            observed = (inline, float(label.get("confidence", 0)))
        else:
            observed = _name_under_anchor(label, lines)
        if observed is None:
            return {"status": "Not Verified", "message":
                    f"Could not identify a complete name under the {_clean(_line_text(label))} heading."}
        readings.append(observed)

    if any(conf < 65 for _, conf in readings):
        return {"status": "Not Verified", "message":
                "The name near the expected heading has low OCR confidence."}
    distinct = {_alnum(text) for text, _ in readings}
    if len(distinct) != 1:
        return {"status": "Not Verified", "message":
                "Conflicting OCR names found in the same party's labeled blocks."}

    source_name = readings[0][0]
    expected, observed = _alnum(value), _alnum(source_name)
    party = "supplier" if key == "vendor_name" else "customer"
    if expected == observed and expected:
        return {"status": "Valid", "message":
                f"Complete {party} name matches the OCR value in its labeled block."}


    if (expected and observed and
            (expected.startswith(observed) or observed.startswith(expected))):
        return {"status": "Not Verified", "message":
                f"OCR {party} name appears incomplete; review the invoice."}
    if all(conf >= 88 for _, conf in readings):
        return {"status": "Needs Review", "message":
                f"{party.title()} name differs from the readable labeled OCR name: '{source_name}'."}
    return {"status": "Not Verified", "message":
            f"{party.title()} name differs from OCR, but the source reading is uncertain."}


def _anchored_customer(value: Any, lines: list[dict]) -> dict | None:

    return _anchored_party("customer_name", value, lines)


def _anchored_vendor(value: Any, lines: list[dict]) -> dict | None:
    return _anchored_party("vendor_name", value, lines)


def match_source_field(key: str, value: Any, lines: list[dict],
                       arithmetic_consistent: bool = False) -> dict:


    if not lines or key not in FIELD_ALIASES or not _clean(value):
        return {"status": "Not Verified", "message": "No usable source OCR evidence."}
    if key == "customer_name":
        anchored = _anchored_customer(value, lines)
        if anchored is not None:
            return anchored
    if key == "vendor_name":
        anchored = _anchored_vendor(value, lines)
        if anchored is not None:
            return anchored


        branded = _unlabeled_vendor(key, value, lines)
        if branded is not None:
            return branded
    desired = _desired(key, value)
    if desired is None or desired == "":
        return {"status": "Not Verified", "message": "Extracted value cannot be parsed."}
    candidates = _footer_total_candidates(lines) if key == "total" else []
    if not candidates:
        candidates = _associated(key, lines)


    reliable = [c for c in candidates if not c.get("ambiguous")
                and float(c["confidence"]) >= 88]
    observed_strong = [parsed[0] for c in reliable
                       if len(parsed := _parse_candidates(key, c["text"])) == 1]
    distinct = []
    for observed in observed_strong:
        if not any(_equivalent(key, observed, other) for other in distinct):
            distinct.append(observed)
    if len(distinct) > 1:
        return {"status": "Not Verified", "message":
                "Conflicting OCR values found for the same label; inspect image manually."}
    accepted = [c for c in candidates if float(c["confidence"]) >= 65
                and any(_equivalent(key, desired, item)
                        for item in _parse_candidates(key, c["text"]))]
    if accepted and distinct and not _equivalent(key, desired, distinct[0]):
        return {"status": "Not Verified", "message":
                "A high-confidence source value disagrees with weaker matching OCR evidence."}
    if accepted:
        if any(not c.get("ambiguous") for c in accepted):
            return {"status": "Valid", "message":
                    "OCR value matches the associated invoice field label."}
        has_payment = any(CREDIT_WORDS.search(_line_text(line)) for line in lines)
        if arithmetic_consistent and not has_payment:
            return {"status": "Valid", "message":
                    "Amount Due agrees with OCR and with the invoice equation; no payment markers detected."}
        return {"status": "Not Verified", "message":
                "Amount Due may be net balance; cannot independently confirm gross total."}
    firm = [c for c in candidates if not c.get("ambiguous")
            and float(c["confidence"]) >= 88]

    if len(firm) == 1:
        observed = _parse_candidates(key, firm[0]["text"])
        if len(observed) == 1 and not _equivalent(key, desired, observed[0]):
            if key in {"subtotal", "discount", "tax", "total", "invoice_date", "due_date", "invoice_number"}:
                detail = ("OCR date differs from the extracted date"
                          if key in {"invoice_date", "due_date"}
                          else "High-confidence labeled OCR value differs")
                return {"status": "Needs Review", "message":
                        f"{detail}: '{firm[0]['text']}'. Check invoice image."}
    header = _unlabeled_vendor(key, value, lines)
    if header is not None:
        return header
    return {"status": "Not Verified", "message":
            "OCR could not confidently associate a matching label and source value."}
