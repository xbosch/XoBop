from __future__ import annotations

import re

_CANONICAL_KV_KEYS = {
    "RUN_STATUS", "COUNT", "SUM", "AVG", "MAX", "SHA256", "ERROR", "OUTPUT_DIR",
    "BUILD_OK", "TEST_OK", "PACKAGE_OK", "DETERMINISTIC_OK", "FINAL",
    "TEST_SUM_OK", "TEST_STATS_OK", "ARTIFACT", "SIZE_BYTES",
    "PAGES_COLLECTED", "ROWS_NORMALIZED", "PROOF_FILES_OK",
    "EVIDENCE_WC_RAW", "EVIDENCE_WC_NORM", "EVIDENCE_SHA_MAIN", "EVIDENCE_WC_RESULT", "EVIDENCE_SHA_RESULT",
    "RUN1_PAGES", "RUN2_PAGES", "OVERLAP_PERCENT", "SCHEMA_DRIFT", "CONSISTENCY_VERDICT",
    "RUN1_FAILURES", "RUN2_FAILURES", "RECOVERY_RATE", "RESILIENCE_VERDICT", "ROLLBACK_OK",
    "ADVERSARIAL_CASES_TOTAL", "REJECTED_TOTAL", "REJECTION_PRECISION",
    "SOURCES_COLLECTED", "ITEMS_NORMALIZED", "DEDUP_REMOVED", "TOOLS_NORMALIZED", "BREAKING_CHANGE_TOOLS",
    "APIS_AUDITED", "AVG_COVERAGE_SCORE", "ADVISORIES_NORMALIZED", "HIGH_SEVERITY_COUNT",
    "ROWS_REJECTED", "RAW_ROWS", "CLEAN_ROWS", "REJECTED_ROWS", "OUTLIER_ROWS", "SCHEMA_VALID", "CONSISTENCY",
}


def canonicalize_kv_key(key: str) -> str:
    k = key.strip().upper()
    if k in _CANONICAL_KV_KEYS:
        return k
    compact = re.sub(r"[^A-Z0-9]", "", k)
    matches = [c for c in _CANONICAL_KV_KEYS if re.sub(r"[^A-Z0-9]", "", c) == compact]
    if len(matches) == 1:
        return matches[0]
    return k


def normalize_machine_kv_output(text: str) -> str:
    lines = [ln for ln in text.splitlines()]
    parsed: list[tuple[str, str, int]] = []
    for idx, ln in enumerate(lines):
        m = re.match(r"^([A-Za-z][A-Za-z0-9_]*)=(.*)$", ln.strip())
        if not m:
            continue
        parsed.append((m.group(1), m.group(2), idx))

    if len(parsed) < 3:
        return text

    normalized = lines[:]
    kv: dict[str, str] = {}
    for raw_k, v, idx in parsed:
        nk = canonicalize_kv_key(raw_k)
        normalized[idx] = f"{nk}={v}"
        kv[nk] = v.strip()

    run_status = kv.get("RUN_STATUS", "").upper()
    if run_status == "PASS":
        if "SHA256" in kv and kv.get("SHA256", "").strip() in {"", "0", "NA", "N/A"}:
            kv["RUN_STATUS"] = "FAIL"
            kv.setdefault("ERROR", "Missing checksum evidence")
        if kv.get("PROOF_FILES_OK") == "0":
            kv["RUN_STATUS"] = "WARN"
            kv.setdefault("ERROR", "Proof artifacts missing")

    for i, ln in enumerate(normalized):
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", ln.strip())
        if not m:
            continue
        k = m.group(1)
        if k in kv:
            normalized[i] = f"{k}={kv[k]}"

    if "RUN_STATUS" in kv and not any(x.startswith("ERROR=") for x in normalized):
        normalized.append(f"ERROR={kv.get('ERROR', 'NONE')}")

    return "\n".join(normalized).strip()
