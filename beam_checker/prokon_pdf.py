"""Extract required reinforcement per beam/span from a Prokon continuous-beam PDF report."""

import re

import pypdf

from .parsers import clean_suffix, is_valid_beam_mark


def _float_or_none(parts, i):
    try:
        return float(parts[i])
    except (IndexError, ValueError):
        return None


def extract_all_beams_from_pdf(pdf_file, progress=None):
    """Read the Prokon PDF.

    `pdf_file` can be a path or a file-like object (e.g. a Streamlit upload).
    `progress(fraction, text)` is called once per page if provided.

    Returns {base_name: {span_no: {req_t1, req_b2, req_t3, req_asv_l, req_asv_m, req_asv_r,
                                   nom_asv_l, nom_asv_m, nom_asv_r}}}
    The nominal Asv/sv (next column of the shear table, None if absent) is for display only; the checker
    compares against the required Asv/sv.
    """
    reader = pypdf.PdfReader(pdf_file)
    num_pages = len(reader.pages)

    beams_raw = {}
    current_beam_base = None
    current_span_num = 1
    in_bending_table = False
    in_shear_table = False

    for idx, page in enumerate(reader.pages):
        if progress:
            progress((idx + 1) / num_pages, f"Reading PDF page {idx + 1}/{num_pages}")

        text = page.extract_text()
        if not text:
            continue

        for line in text.split("\n"):
            line_str = line.strip()
            if not line_str:
                continue

            # 1. Beam title
            m_name = re.search(r"(?:Continuous Beam|Title|Beam)\s*:\s*([\w\-]+)", line_str, re.IGNORECASE)
            if m_name:
                base_name, _ = clean_suffix(m_name.group(1).strip())
                if is_valid_beam_mark(base_name):
                    current_beam_base = base_name
                    current_span_num = 1
                    beams_raw.setdefault(current_beam_base, {})
                else:
                    current_beam_base = None
                continue

            # 2. Span label (SPAN 1, SPAN 2, ...)
            m_span = re.search(r"^\s*SPAN\s+(\d+)", line_str, re.IGNORECASE)
            if m_span:
                current_span_num = int(m_span.group(1))
                continue

            # 3. Table state
            upper = line_str.upper()
            if "BENDING MOMENTS & REINFORCEMENT" in upper:
                in_bending_table, in_shear_table = True, False
                continue
            if "SHEAR FORCES & REINFORCEMENT" in upper:
                in_bending_table, in_shear_table = False, True
                continue
            if "COLUMN REACTIONS" in upper or "DEFLECTION" in upper:
                in_bending_table, in_shear_table = False, False

            if not current_beam_base:
                continue

            # 4. Longitudinal steel (pos, ..., As top, As bot)
            if in_bending_table:
                parts = re.sub(r"(\d+)\.\s+(\d+)", r"\1.\2", line_str).split()
                if len(parts) >= 5:
                    try:
                        pos, as_top, as_bot = float(parts[0]), float(parts[3]), float(parts[4])
                        span = beams_raw[current_beam_base].setdefault(
                            current_span_num, {"points": [], "shear_points": []}
                        )
                        span["points"].append({"pos": pos, "as_top": as_top, "as_bot": as_bot})
                    except ValueError:
                        pass

            # 5. Shear steel (pos, ..., Asv/sv)
            if in_shear_table:
                parts = re.sub(r"(\d+)\.\s+(\d+)", r"\1.\2", line_str).split()
                if len(parts) >= 5:
                    try:
                        pos, asv_sv = float(parts[0]), float(parts[4])
                        span = beams_raw[current_beam_base].setdefault(
                            current_span_num, {"points": [], "shear_points": []}
                        )
                        span["shear_points"].append({"pos": pos, "asv_sv": asv_sv, "nom": _float_or_none(parts, 5)})
                    except ValueError:
                        pass

    # Reduce each span to left / mid / right requirements
    parsed_beams = {}
    for base_name, spans in beams_raw.items():
        parsed_beams[base_name] = {}
        for s_no, span_data in spans.items():
            points = span_data["points"]
            if not points:
                continue

            sorted_points = sorted(points, key=lambda x: x["pos"])
            sorted_shear = sorted(span_data["shear_points"], key=lambda x: x["pos"])

            if sorted_shear:
                asv_left = sorted_shear[0]["asv_sv"]
                asv_right = sorted_shear[-1]["asv_sv"]
                asv_mid = max(p["asv_sv"] for p in sorted_shear)
                noms = [p["nom"] for p in sorted_shear if p["nom"] is not None]
                nom_l, nom_r = sorted_shear[0]["nom"], sorted_shear[-1]["nom"]
                nom_m = max(noms) if noms else None
            else:
                asv_left, asv_mid, asv_right = 0.0, 0.0, 0.0
                nom_l = nom_m = nom_r = None

            parsed_beams[base_name][s_no] = {
                "req_t1": sorted_points[0]["as_top"],
                "req_b2": max(p["as_bot"] for p in sorted_points),
                "req_t3": sorted_points[-1]["as_top"],
                "req_asv_l": asv_left,
                "req_asv_m": asv_mid,
                "req_asv_r": asv_right,
                "nom_asv_l": nom_l,
                "nom_asv_m": nom_m,
                "nom_asv_r": nom_r,
            }

    return parsed_beams
