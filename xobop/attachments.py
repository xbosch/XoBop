from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


def extract_docx_text(path: Path, max_chars: int = 4000) -> str:
    chunks: list[str] = []
    with zipfile.ZipFile(path, "r") as zf:
        names = {n for n in zf.namelist()}
        if "word/document.xml" not in names:
            return ""
        xml = zf.read("word/document.xml")
        root = ET.fromstring(xml)
        for node in root.iter():
            if node.tag.endswith("}t") and node.text:
                chunks.append(node.text)
    text = " ".join(chunks).strip()
    return text[:max_chars]


def extract_xlsx_text(path: Path, max_chars: int = 4000) -> str:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            sroot = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in sroot.findall("m:si", ns):
                parts = [t.text or "" for t in si.findall(".//m:t", ns)]
                shared.append("".join(parts))

        rows_out: list[str] = []
        sheet_names = [n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
        for sname in sorted(sheet_names)[:4]:
            root = ET.fromstring(zf.read(sname))
            for row in root.findall(".//m:row", ns)[:120]:
                vals: list[str] = []
                for c in row.findall("m:c", ns):
                    typ = c.attrib.get("t", "")
                    v = c.find("m:v", ns)
                    if v is None or v.text is None:
                        continue
                    raw = v.text
                    if typ == "s" and raw.isdigit():
                        idx = int(raw)
                        if 0 <= idx < len(shared):
                            vals.append(shared[idx])
                        else:
                            vals.append(raw)
                    else:
                        vals.append(raw)
                if vals:
                    rows_out.append(" | ".join(vals))
                if sum(len(x) for x in rows_out) >= max_chars:
                    break
            if sum(len(x) for x in rows_out) >= max_chars:
                break
    return "\n".join(rows_out)[:max_chars]


def summarize_zip(path: Path, max_entries: int = 40) -> str:
    with zipfile.ZipFile(path, "r") as zf:
        infos = zf.infolist()
        lines = []
        for i in infos[:max_entries]:
            lines.append(f"{i.filename} ({i.file_size} bytes)")
        more = "" if len(infos) <= max_entries else f"\n... +{len(infos)-max_entries} more entries"
        return "\n".join(lines) + more
