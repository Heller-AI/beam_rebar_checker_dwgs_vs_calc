import math
import os
import re
import csv
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import pandas as pd
import pypdf


class BeamCheckerApp:

    def __init__(self, root):
        self.root = root
        self.root.title("Prokon Multi-Beam Comparison Tool - Row-by-Row View")
        self.root.geometry("1450x850")
        self.root.minsize(1100, 700)

        self.style = ttk.Style()
        self.style.theme_use("clam")

        self.style.configure(
            "Green.Horizontal.TProgressbar",
            troughcolor="#E0E0E0",
            background="#28a745",
            thickness=18,
        )

        self.excel_path = tk.StringVar()
        self.pdf_path = tk.StringVar()
        self.sheet_name = tk.StringVar()
        self.excel_format = tk.StringVar(value="Format 2")
        self.filter_beam = tk.StringVar()
        self.filter_status = tk.StringVar(value="All")

        self.filter_beam.trace_add("write", lambda *args: self.apply_filter())
        self.filter_status.trace_add("write", lambda *args: self.apply_filter())

        self.all_results = []
        self.pdf_beam_names = set()
        self.excel_beam_names = set()

        self.create_widgets()

    def create_widgets(self):
        main_frame = ttk.Frame(self.root, padding=15)
        main_frame.pack(fill=tk.BOTH, expand=True)

        title_label = ttk.Label(
            main_frame,
            text="MULTI-BEAM REINFORCEMENT CHECKER (PROKON VS BEAM SCHEDULE)",
            font=("Arial", 13, "bold"),
        )
        title_label.pack(anchor="w", pady=(0, 10))

        # 1. File Selection Frame
        file_frame = ttk.LabelFrame(
            main_frame, text=" 1. Select Input Files, Sheet & Format ", padding=10
        )
        file_frame.pack(fill=tk.X, pady=(0, 10))
        file_frame.columnconfigure(1, weight=1)

        # Excel Path
        ttk.Label(file_frame, text="Excel Schedule (.xlsx):", width=22).grid(
            row=0, column=0, sticky="w", pady=4
        )
        ttk.Entry(file_frame, textvariable=self.excel_path).grid(
            row=0, column=1, sticky="ew", padx=5, pady=4
        )
        ttk.Button(file_frame, text="Browse...", command=self.browse_excel, width=12).grid(
            row=0, column=2, padx=5, pady=4
        )

        # Select Sheet Name
        ttk.Label(file_frame, text="Excel Sheet Name:", width=22).grid(
            row=1, column=0, sticky="w", pady=4
        )
        self.sheet_cb = ttk.Combobox(
            file_frame, textvariable=self.sheet_name, state="readonly"
        )
        self.sheet_cb.grid(row=1, column=1, sticky="ew", padx=5, pady=4)

        # Prokon PDF Path
        ttk.Label(file_frame, text="Prokon Report (.pdf):", width=22).grid(
            row=2, column=0, sticky="w", pady=4
        )
        ttk.Entry(file_frame, textvariable=self.pdf_path).grid(
            row=2, column=1, sticky="ew", padx=5, pady=4
        )
        ttk.Button(file_frame, text="Browse...", command=self.browse_pdf, width=12).grid(
            row=2, column=2, padx=5, pady=4
        )

        # Radio options for Format
        radio_frame = ttk.Frame(file_frame)
        radio_frame.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 2))

        ttk.Label(radio_frame, text="Excel Format Option:", font=("Arial", 9, "bold")).pack(
            side=tk.LEFT, padx=(0, 10)
        )

        r1 = ttk.Radiobutton(
            radio_frame,
            text="Type 1 (Mark Col A | Top: C-E, Bot: F-G | Stirrups: K,L,M)",
            variable=self.excel_format,
            value="Format 1",
        )
        r1.pack(side=tk.LEFT, padx=10)

        r2 = ttk.Radiobutton(
            radio_frame,
            text="Type 2 (Mark Col B | Top: E-G, Bot: H-J | Stirrups: L,M,N)",
            variable=self.excel_format,
            value="Format 2",
        )
        r2.pack(side=tk.LEFT, padx=10)

        # Action Frame & Progress Bar
        action_frame = ttk.Frame(main_frame)
        action_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Button(
            action_frame,
            text="Run Comparison All Beams",
            command=self.run_check,
            style="Accent.TButton",
        ).pack(side=tk.LEFT, padx=(0, 5))

        ttk.Button(action_frame, text="Export CSV Report", command=self.export_csv).pack(
            side=tk.LEFT, padx=5
        )

        ttk.Button(action_frame, text="Export Excel Report", command=self.export_excel).pack(
            side=tk.LEFT, padx=5
        )

        ttk.Button(
            action_frame, text="Show Unmatched Beams", command=self.show_unmatched_summary
        ).pack(side=tk.LEFT, padx=5)

        filter_frame = ttk.Frame(action_frame)
        filter_frame.pack(side=tk.RIGHT)

        ttk.Label(filter_frame, text="Search Beam:").pack(side=tk.LEFT, padx=(5, 2))
        ttk.Entry(filter_frame, textvariable=self.filter_beam, width=12).pack(
            side=tk.LEFT, padx=(0, 10)
        )

        ttk.Label(filter_frame, text="Status:").pack(side=tk.LEFT, padx=(5, 2))
        status_cb = ttk.Combobox(
            filter_frame,
            textvariable=self.filter_status,
            values=["All", "FAIL Only", "OK Only"],
            state="readonly",
            width=10,
        )
        status_cb.pack(side=tk.LEFT)

        # Progress Bar Frame
        progress_frame = ttk.Frame(main_frame)
        progress_frame.pack(fill=tk.X, pady=(0, 8))

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            progress_frame,
            variable=self.progress_var,
            maximum=100,
            style="Green.Horizontal.TProgressbar",
        )
        self.progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))

        self.status_lbl = ttk.Label(progress_frame, text="Ready", width=25, anchor="e")
        self.status_lbl.pack(side=tk.RIGHT)

        # 2. Detailed Results Table
        result_frame = ttk.LabelFrame(main_frame, text=" 2. Comparison Results (3 Position Rows per Span) ", padding=10)
        result_frame.pack(fill=tk.BOTH, expand=True)

        columns = (
            "beam", "pos",
            "req_as", "prov_bars", "prov_as", "flex_ratio", "flex_status",
            "req_asv", "prov_stirrup", "prov_asv", "shear_ratio", "shear_status",
            "overall_status", "remark"
        )
        self.tree = ttk.Treeview(
            result_frame, columns=columns, show="headings", selectmode="browse"
        )

        self.tree.heading("beam", text="Beam Mark")
        self.tree.heading("pos", text="Position / Location")

        # Thép dọc
        self.tree.heading("req_as", text="Req. As (mm²)")
        self.tree.heading("prov_bars", text="Provided Bars")
        self.tree.heading("prov_as", text="Prov. As (mm²)")
        self.tree.heading("flex_ratio", text="Ratio (%)")
        self.tree.heading("flex_status", text="Flex. Status")

        # Thép đai
        self.tree.heading("req_asv", text="Req. Asv/sv")
        self.tree.heading("prov_stirrup", text="Prov. Stirrup")
        self.tree.heading("prov_asv", text="Prov. Asv/sv")
        self.tree.heading("shear_ratio", text="Ratio (%)")
        self.tree.heading("shear_status", text="Shear Status")

        self.tree.heading("overall_status", text="Overall")
        self.tree.heading("remark", text="Remark")

        col_widths = {
            "beam": 90, "pos": 150,
            "req_as": 85, "prov_bars": 120, "prov_as": 85, "flex_ratio": 70, "flex_status": 80,
            "req_asv": 85, "prov_stirrup": 100, "prov_asv": 85, "shear_ratio": 70, "shear_status": 80,
            "overall_status": 75, "remark": 100
        }

        for col, width in col_widths.items():
            self.tree.column(col, width=width, anchor="center")

        self.tree.tag_configure("FAIL", background="#FFC7CE", foreground="#9C0006")
        self.tree.tag_configure("BEAM_EVEN", background="#FFFFFF", foreground="#000000")
        self.tree.tag_configure("BEAM_ODD", background="#F8F9F9", foreground="#000000")

        scrollbar_y = ttk.Scrollbar(result_frame, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar_x = ttk.Scrollbar(result_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)

        self.tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        scrollbar_y.pack(side=tk.RIGHT, fill=tk.Y)
        scrollbar_x.pack(side=tk.BOTTOM, fill=tk.X)

    def browse_excel(self):
        filename = filedialog.askopenfilename(
            title="Select Excel Schedule", filetypes=[("Excel Files", "*.xlsx *.xls")]
        )
        if filename:
            self.excel_path.set(filename)
            self.load_excel_sheets(filename)

    def load_excel_sheets(self, filepath):
        try:
            xl = pd.ExcelFile(filepath)
            sheets = xl.sheet_names
            self.sheet_cb["values"] = sheets

            if sheets:
                target_sheet = sheets[0]
                for s in sheets:
                    if "BEAM" in s.upper() or "SCHEDULE" in s.upper():
                        target_sheet = s
                        break
                self.sheet_name.set(target_sheet)
        except Exception as e:
            messagebox.showerror("Error Reading Sheet List", f"Cannot read sheet list: {str(e)}")

    def browse_pdf(self):
        filename = filedialog.askopenfilename(
            title="Select Prokon PDF", filetypes=[("PDF Files", "*.pdf")]
        )
        if filename:
            self.pdf_path.set(filename)

    def is_arrow_symbol(self, val_str):
        if not val_str:
            return False
        clean = str(val_str).strip()
        arrow_chars = ["←", "→", "🡠", "🡢", "!", '"', "-", "—"]
        return clean in arrow_chars

    def parse_bar_notation(self, notation_str):
        if not notation_str or str(notation_str).strip().lower() in ["", "nan", "none", "-", "n.a", "n/a"]:
            return 0.0, "-"

        clean_str = str(notation_str).strip()
        total_area = 0.0
        parts = clean_str.split("+")

        valid_parts = []
        for part in parts:
            part = part.strip()
            if "H" in part.upper():
                try:
                    tokens = part.upper().split("H")
                    num = int(tokens[0]) if tokens[0] != "" else 1
                    dia = float(tokens[1])
                    area = num * (math.pi * (dia ** 2) / 4.0)
                    total_area += area
                    valid_parts.append(f"{num}H{int(dia) if dia.is_integer() else dia}")
                except Exception:
                    pass

        return round(total_area, 1), "+".join(valid_parts) if valid_parts else "-"

    def parse_stirrup_single_str(self, notation_str):
        if not notation_str or str(notation_str).strip().lower() in ["", "nan", "none", "-", "n.a", "n/a"]:
            return 0.0, "-"

        clean_str = str(notation_str).strip().upper()

        try:
            if "-" in clean_str:
                bar_part, spacing_str = clean_str.split("-")[0], clean_str.split("-")[1]
            elif "/" in clean_str:
                bar_part, spacing_str = clean_str.split("/")[0], clean_str.split("/")[1]
            else:
                return 0.0, clean_str

            spacing = float(re.sub(r"[^\d.]", "", spacing_str))
            tokens = bar_part.split("H")

            legs = int(tokens[0]) if (tokens[0] != "" and tokens[0].isdigit()) else 2
            dia = float(tokens[1])

            if spacing > 0:
                asv_single = math.pi * (dia ** 2) / 4.0
                asv_total = legs * asv_single
                asv_sv = asv_total / spacing
                return round(asv_sv, 3), clean_str
        except Exception:
            pass

        return 0.0, clean_str

    def parse_stirrup_list(self, stirrup_str_list):
        total_asv_sv = 0.0
        valid_notations = []

        for s_str in stirrup_str_list:
            asv_sv, not_str = self.parse_stirrup_single_str(s_str)
            if asv_sv > 0:
                total_asv_sv += asv_sv
                valid_notations.append(not_str)

        combined_notation = "+".join(valid_notations) if valid_notations else "-"
        return round(total_asv_sv, 3), combined_notation

    def normalize_str(self, text):
        if not text:
            return ""
        return re.sub(r"[\s\-_.]", "", str(text)).lower()

    def clean_suffix(self, mark):
        """Tách tên dầm gốc và số nhịp (VD: '12TRB10-2' -> ('12TRB10', 2))"""
        if not mark:
            return "", 1
        mark_str = str(mark).strip()
        match = re.match(r"^(.+?)[\-_](\d+)$", mark_str)
        if match:
            return match.group(1).strip(), int(match.group(2))
        return mark_str, 1

    def is_valid_beam_mark(self, name_str):
        """Kiểm tra tên dầm có hợp lệ hay không"""
        if not name_str:
            return False
        clean = str(name_str).strip()
        if clean.isdigit() or len(clean) < 3:
            return False
        return True

    def update_progress(self, val, status_text=""):
        self.progress_var.set(val)
        if status_text:
            self.status_lbl.config(text=status_text)
        self.root.update_idletasks()

    def extract_all_beams_from_pdf(self, pdf_file):
        """Đọc PDF và lưu theo cấu trúc: pdf_beams[BaseName][span_num] = {thép dọc, thép đai}"""
        reader = pypdf.PdfReader(pdf_file)
        num_pages = len(reader.pages)

        beams_raw = {}
        current_beam_base = None
        current_span_num = 1
        in_bending_table = False
        in_shear_table = False

        for idx, page in enumerate(reader.pages):
            prog = int(((idx + 1) / num_pages) * 40)
            self.update_progress(prog, f"Reading PDF Page {idx + 1}/{num_pages}")

            text = page.extract_text()
            if not text:
                continue

            lines = text.split("\n")

            for line in lines:
                line_str = line.strip()
                if not line_str:
                    continue

                # 1. Bắt tên dầm gốc
                m_name = re.search(r"(?:Continuous Beam|Title|Beam)\s*:\s*([\w\-]+)", line_str, re.IGNORECASE)
                if m_name:
                    raw_title = m_name.group(1).strip()
                    base_name, _ = self.clean_suffix(raw_title)
                    if self.is_valid_beam_mark(base_name):
                        current_beam_base = base_name
                        current_span_num = 1
                        if current_beam_base not in beams_raw:
                            beams_raw[current_beam_base] = {}
                    else:
                        current_beam_base = None
                    continue

                # 2. Bắt nhãn SPAN (VD: SPAN 1, SPAN 2...)
                m_span = re.search(r"^\s*SPAN\s+(\d+)", line_str, re.IGNORECASE)
                if m_span:
                    current_span_num = int(m_span.group(1))
                    continue

                # 3. Quản lý trạng thái đọc bảng
                if "BENDING MOMENTS & REINFORCEMENT" in line_str.upper():
                    in_bending_table = True
                    in_shear_table = False
                    continue

                if "SHEAR FORCES & REINFORCEMENT" in line_str.upper():
                    in_shear_table = True
                    in_bending_table = False
                    continue

                if "COLUMN REACTIONS" in line_str.upper() or "DEFLECTION" in line_str.upper():
                    in_bending_table = False
                    in_shear_table = False

                # 4. Trích xuất thép dọc
                if in_bending_table and current_beam_base:
                    cleaned_line = re.sub(r"(\d+)\.\s+(\d+)", r"\1.\2", line_str)
                    parts = cleaned_line.split()
                    if len(parts) >= 5:
                        try:
                            pos = float(parts[0])
                            as_top = float(parts[3])
                            as_bot = float(parts[4])

                            if current_span_num not in beams_raw[current_beam_base]:
                                beams_raw[current_beam_base][current_span_num] = {"points": [], "shear_points": []}

                            beams_raw[current_beam_base][current_span_num]["points"].append(
                                {"pos": pos, "as_top": as_top, "as_bot": as_bot}
                            )
                        except ValueError:
                            pass

                # 5. Trích xuất thép đai
                if in_shear_table and current_beam_base:
                    cleaned_line = re.sub(r"(\d+)\.\s+(\d+)", r"\1.\2", line_str)
                    parts = cleaned_line.split()
                    if len(parts) >= 5:
                        try:
                            pos = float(parts[0])
                            asv_sv = float(parts[4])

                            if current_span_num not in beams_raw[current_beam_base]:
                                beams_raw[current_beam_base][current_span_num] = {"points": [], "shear_points": []}

                            beams_raw[current_beam_base][current_span_num]["shear_points"].append(
                                {"pos": pos, "asv_sv": asv_sv}
                            )
                        except ValueError:
                            pass

        # Tính toán thông số thép cho từng nhịp của dầm
        parsed_beams = {}
        for base_name, spans in beams_raw.items():
            parsed_beams[base_name] = {}
            for s_no, span_data in spans.items():
                points = span_data["points"]
                shear_pts = span_data["shear_points"]

                if not points:
                    continue

                sorted_points = sorted(points, key=lambda x: x["pos"])
                sorted_shear = sorted(shear_pts, key=lambda x: x["pos"]) if shear_pts else []

                as_top_left = sorted_points[0]["as_top"]
                as_top_right = sorted_points[-1]["as_top"]
                as_bot_max = max(p["as_bot"] for p in sorted_points)

                if sorted_shear:
                    asv_left = sorted_shear[0]["asv_sv"]
                    asv_right = sorted_shear[-1]["asv_sv"]
                    asv_mid = max(p["asv_sv"] for p in sorted_shear)
                else:
                    asv_left, asv_mid, asv_right = 0.0, 0.0, 0.0

                parsed_beams[base_name][s_no] = {
                    "req_t1": as_top_left, "req_b2": as_bot_max, "req_t3": as_top_right,
                    "req_asv_l": asv_left, "req_asv_m": asv_mid, "req_asv_r": asv_right,
                }

        return parsed_beams

    def run_check(self):
        exc = self.excel_path.get()
        pdf = self.pdf_path.get()
        sheet = self.sheet_name.get()
        fmt = self.excel_format.get()

        if not exc or not os.path.exists(exc):
            messagebox.showerror("Error", "Please select a valid Excel Schedule file!")
            return
        if not pdf or not os.path.exists(pdf):
            messagebox.showerror("Error", "Please select a valid Prokon PDF file!")
            return

        self.all_results.clear()
        self.pdf_beam_names.clear()
        self.excel_beam_names.clear()

        try:
            self.update_progress(5, "Parsing Prokon PDF...")
            pdf_beams = self.extract_all_beams_from_pdf(pdf)

            if not pdf_beams:
                self.update_progress(0, "Error")
                messagebox.showwarning("Warning", "No beam reinforcement data found in PDF!")
                return

            self.pdf_beam_names = set(pdf_beams.keys())

            self.update_progress(45, f"Reading Sheet: {sheet}...")

            df = pd.read_excel(exc, sheet_name=sheet, header=None) if sheet else pd.read_excel(exc, header=None)

            # GỘP CÁC DÒNG EXCEL THEO TỪNG NHỊP DẦM
            span_groups = []
            curr_mark = None
            curr_rows = []

            for r_idx, row in df.iterrows():
                col_mark_idx = 0 if fmt == "Format 1" else 1
                val_mark = str(row[col_mark_idx]).strip() if pd.notna(row[col_mark_idx]) else ""

                if val_mark and val_mark.upper() not in ["MARK", "BEAM MARK", "PPVC BEAM SCHEDULE", "BEAM SCHEDULE", "NAN"]:
                    if curr_mark and curr_rows:
                        span_groups.append((curr_mark, curr_rows))
                    curr_mark = val_mark
                    curr_rows = [row]
                else:
                    if curr_mark:
                        curr_rows.append(row)

            if curr_mark and curr_rows:
                span_groups.append((curr_mark, curr_rows))

            total_spans = len(span_groups)
            matched_count = 0
            pdf_matched_bases = set()

            for idx, (beam_mark, rows_group) in enumerate(span_groups):
                prog = 45 + int(((idx + 1) / total_spans) * 50)
                self.update_progress(prog, f"Checking Beam Span {idx + 1}/{total_spans}")

                excel_base, excel_span = self.clean_suffix(beam_mark)
                if not self.is_valid_beam_mark(excel_base):
                    continue

                self.excel_beam_names.add(excel_base)

                # Khớp dầm dựa trên Base Mark
                matched_pdf_base = None
                if excel_base in pdf_beams:
                    matched_pdf_base = excel_base
                else:
                    norm_base = self.normalize_str(excel_base)
                    for p_base in pdf_beams.keys():
                        if self.normalize_str(p_base) == norm_base:
                            matched_pdf_base = p_base
                            break

                if matched_pdf_base:
                    matched_count += 1
                    pdf_matched_bases.add(matched_pdf_base)
                    spans_data = pdf_beams[matched_pdf_base]

                    # Tra cứu theo đúng số nhịp Span (Mặc định Span 1 nếu không chỉ định)
                    target_span = excel_span if excel_span in spans_data else 1
                    if target_span not in spans_data and spans_data:
                        target_span = list(spans_data.keys())[0]

                    p_data = spans_data.get(
                        target_span,
                        {"req_t1": 0.0, "req_b2": 0.0, "req_t3": 0.0, "req_asv_l": 0.0, "req_asv_m": 0.0, "req_asv_r": 0.0}
                    )

                    if fmt == "Format 1":
                        t1_idx, t2_idx, t3_idx = 2, 3, 4
                        b1_idx, b2_idx = 5, 6
                        st_l_idx, st_m_idx, st_r_idx = 10, 11, 12
                    else:  # Format 2
                        t1_idx, t2_idx, t3_idx = 4, 5, 6
                        b1_idx, b2_idx = 7, 8
                        st_l_idx, st_m_idx, st_r_idx = 11, 12, 13

                    # 1. ĐỌC THÉP DỌC
                    raw_t1_list, raw_t3_list, raw_b_list = [], [], []
                    raw_stl_list, raw_stm_list, raw_str_list = [], [], []

                    for r in rows_group:
                        v_t1 = str(r[t1_idx]).strip() if pd.notna(r[t1_idx]) else ""
                        v_t2 = str(r[t2_idx]).strip() if pd.notna(r[t2_idx]) else ""
                        v_t3 = str(r[t3_idx]).strip() if pd.notna(r[t3_idx]) else ""
                        v_b1 = str(r[b1_idx]).strip() if pd.notna(r[b1_idx]) else ""
                        v_b2 = str(r[b2_idx]).strip() if pd.notna(r[b2_idx]) else ""

                        v_stl = str(r[st_l_idx]).strip() if pd.notna(r[st_l_idx]) else ""
                        v_stm = str(r[st_m_idx]).strip() if pd.notna(r[st_m_idx]) else ""
                        v_str = str(r[st_r_idx]).strip() if pd.notna(r[st_r_idx]) else ""

                        s_t1 = v_t2 if self.is_arrow_symbol(v_t1) else v_t1
                        s_t3 = v_t2 if self.is_arrow_symbol(v_t3) else v_t3
                        s_b2 = v_b2 if (v_b2 and v_b2 != "nan") else v_b1

                        if s_t1 and s_t1 != "nan": raw_t1_list.append(s_t1)
                        if s_t3 and s_t3 != "nan": raw_t3_list.append(s_t3)
                        if s_b2 and s_b2 != "nan": raw_b_list.append(s_b2)

                        if v_stl and v_stl != "nan": raw_stl_list.append(v_stl)
                        if v_stm and v_stm != "nan": raw_stm_list.append(v_stm)
                        if v_str and v_str != "nan": raw_str_list.append(v_str)

                    comb_t1 = "+".join(raw_t1_list) if raw_t1_list else "-"
                    comb_t3 = "+".join(raw_t3_list) if raw_t3_list else "-"
                    comb_b2 = "+".join(raw_b_list) if raw_b_list else "-"

                    # Xử lý CANTILEVER (-) Thép Dọc
                    if comb_t3 in ["-", "—"] and comb_t1 not in ["-", "—", ""]:
                        comb_t3 = comb_t1
                    elif comb_t1 in ["-", "—"] and comb_t3 not in ["-", "—", ""]:
                        comb_t1 = comb_t3

                    prov_t1, not_t1 = self.parse_bar_notation(comb_t1)
                    prov_b2, not_b2 = self.parse_bar_notation(comb_b2)
                    prov_t3, not_t3 = self.parse_bar_notation(comb_t3)

                    req_t1, req_b2, req_t3 = p_data["req_t1"], p_data["req_b2"], p_data["req_t3"]

                    # 2. ĐỌC THÉP ĐAI
                    prov_asv_l, not_stl = self.parse_stirrup_list(raw_stl_list)
                    prov_asv_m, not_stm = self.parse_stirrup_list(raw_stm_list)
                    prov_asv_r, not_str = self.parse_stirrup_list(raw_str_list)

                    # Xử lý CANTILEVER (-) Thép Đai
                    if not_str in ["-", "—"] or prov_asv_r == 0:
                        if prov_asv_m > 0:
                            prov_asv_r, not_str = prov_asv_m, not_stm
                        elif prov_asv_l > 0:
                            prov_asv_r, not_str = prov_asv_l, not_stl

                    if not_stl in ["-", "—"] or prov_asv_l == 0:
                        if prov_asv_m > 0:
                            prov_asv_l, not_stl = prov_asv_m, not_stm
                        elif prov_asv_r > 0:
                            prov_asv_l, not_stl = prov_asv_r, not_str

                    req_asv_l, req_asv_m, req_asv_r = p_data["req_asv_l"], p_data["req_asv_m"], p_data["req_asv_r"]

                    # PHÂN TÍCH THEO 3 DÒNG VỊ TRÍ (LEFT, MID, RIGHT)
                    positions = [
                        ("Left Support (Pos Start)", req_t1, not_t1, prov_t1, req_asv_l, not_stl, prov_asv_l, "From Col E/L"),
                        ("Mid-Span (Max Bot)", req_b2, not_b2, prov_b2, req_asv_m, not_stm, prov_asv_m, "From Col H/M"),
                        ("Right Support (Pos End)", req_t3, not_t3, prov_t3, req_asv_r, not_str, prov_asv_r, "From Col G/N")
                    ]

                    for pos_name, r_as, p_bars, p_as, r_asv, p_stir, p_asv, rem in positions:
                        # Tỷ lệ & Status thép dọc
                        f_ratio_str = f"{(p_as / r_as * 100):.1f}%" if r_as > 0 else "N/A"
                        f_st = "OK" if p_as >= r_as else "FAIL (Deficit)"

                        # Tỷ lệ & Status thép đai
                        s_ratio_str = f"{(p_asv / r_asv * 100):.1f}%" if r_asv > 0 else "N/A"
                        s_st = "OK" if p_asv >= r_asv else "FAIL (Deficit)"

                        # Status Tổng vị trí
                        pos_overall = "OK" if (f_st == "OK" and s_st == "OK") else "FAIL"

                        row_item = (
                            beam_mark,
                            pos_name,
                            f"{r_as:.1f}", p_bars, f"{p_as:.1f}", f_ratio_str, f_st,
                            f"{r_asv:.3f}", p_stir, f"{p_asv:.3f}", s_ratio_str, s_st,
                            pos_overall, rem
                        )
                        self.all_results.append(row_item)

            self.pdf_matched_bases = pdf_matched_bases
            self.update_progress(100, "Done")
            self.apply_filter()

            if matched_count == 0:
                messagebox.showwarning("No Match Found", f"None of the beams in PDF matched with Sheet '{sheet}'!")
            else:
                messagebox.showinfo("Success", f"Successfully checked {matched_count} beam span(s)!")

            self.show_unmatched_summary()

        except Exception as e:
            self.update_progress(0, "Error")
            messagebox.showerror("Execution Error", f"Error details: {str(e)}")

    def apply_filter(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        search_txt = self.filter_beam.get().strip().lower()
        status_mode = self.filter_status.get()

        color_toggle = False
        last_beam = None

        for item in self.all_results:
            beam_mark = item[0]
            overall = item[12]

            if search_txt and search_txt not in beam_mark.lower():
                continue

            if status_mode == "FAIL Only" and overall != "FAIL":
                continue
            if status_mode == "OK Only" and overall != "OK":
                continue

            if beam_mark != last_beam:
                color_toggle = not color_toggle
                last_beam = beam_mark

            if overall == "FAIL":
                tag = "FAIL"
            else:
                tag = "BEAM_ODD" if color_toggle else "BEAM_EVEN"

            self.tree.insert("", tk.END, values=item, tags=(tag,))

    def show_unmatched_summary(self):
        if not self.pdf_beam_names and not self.excel_beam_names:
            messagebox.showinfo("Notice", "Please run comparison first!")
            return

        pdf_matched = getattr(self, "pdf_matched_bases", set())

        pdf_only = sorted([b for b in self.pdf_beam_names if b not in self.excel_beam_names])
        excel_only = sorted([b for b in self.excel_beam_names if b not in pdf_matched])

        dialog = tk.Toplevel(self.root)
        dialog.title(f"Beam Mismatch Report (Sheet: {self.sheet_name.get()})")
        dialog.geometry("700x500")

        frame_top = ttk.Frame(dialog, padding=10)
        frame_top.pack(fill=tk.BOTH, expand=True)

        f1 = ttk.LabelFrame(frame_top, text=f" In PDF, Missing in Excel ({len(pdf_only)}) ", padding=5)
        f1.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        list_pdf = tk.Listbox(f1)
        for item in pdf_only:
            list_pdf.insert(tk.END, item)
        list_pdf.pack(fill=tk.BOTH, expand=True)

        f2 = ttk.LabelFrame(frame_top, text=f" In Excel, Missing in PDF ({len(excel_only)}) ", padding=5)
        f2.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))

        list_excel = tk.Listbox(f2)
        for item in excel_only:
            list_excel.insert(tk.END, item)
        list_excel.pack(fill=tk.BOTH, expand=True)

    def export_csv(self):
        if not self.all_results:
            messagebox.showwarning("Warning", "No data to export!")
            return

        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV Files", "*.csv")],
            title="Save CSV Report As",
        )
        if file_path:
            with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                headers = [
                    "Beam Mark", "Position / Location",
                    "Req. As (mm²)", "Provided Bars", "Prov. As (mm²)", "Flex Ratio (%)", "Flex Status",
                    "Req. Asv/sv", "Provided Stirrup", "Prov. Asv/sv", "Shear Ratio (%)", "Shear Status",
                    "Overall Status", "Remark"
                ]
                writer.writerow(headers)
                for row_id in self.tree.get_children():
                    writer.writerow(self.tree.item(row_id)["values"])
            messagebox.showinfo("Success", f"CSV report saved to:\n{file_path}")

    def export_excel(self):
        if not self.all_results:
            messagebox.showwarning("Warning", "No data to export!")
            return

        file_path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel Files", "*.xlsx")],
            title="Save Excel Report As",
        )
        if file_path:
            headers = [
                "Beam Mark", "Position / Location",
                "Req. As (mm²)", "Provided Bars", "Prov. As (mm²)", "Flex Ratio (%)", "Flex Status",
                "Req. Asv/sv", "Provided Stirrup", "Prov. Asv/sv", "Shear Ratio (%)", "Shear Status",
                "Overall Status", "Remark"
            ]
            rows = [self.tree.item(row_id)["values"] for row_id in self.tree.get_children()]
            df = pd.DataFrame(rows, columns=headers)

            with pd.ExcelWriter(file_path, engine="openpyxl") as writer:
                df.to_excel(writer, sheet_name="Rebar & Stirrup Detail Check", index=False)

            messagebox.showinfo("Success", f"Excel report saved to:\n{file_path}")


if __name__ == "__main__":
    root = tk.Tk()
    app = BeamCheckerApp(root)
    root.mainloop()