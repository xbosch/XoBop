from __future__ import annotations

import re

_CANONICAL_KV_KEYS = {
    "RUN_STATUS", "COUNT", "SUM", "AVG", "MAX", "SHA256", "ERROR", "OUTPUT_DIR", "OUTPUT",
    "BUILD_OK", "TEST_OK", "PACKAGE_OK", "DETERMINISTIC_OK", "FINAL",
    "TEST_SUM_OK", "TEST_STATS_OK", "ARTIFACT", "SIZE_BYTES",
    "PAGES_COLLECTED", "ROWS_NORMALIZED", "PROOF_FILES_OK",
    "EVIDENCE_WC_RAW", "EVIDENCE_WC_NORM", "EVIDENCE_SHA_MAIN", "EVIDENCE_WC_RESULT", "EVIDENCE_SHA_RESULT",
    "RUN1_PAGES", "RUN2_PAGES", "OVERLAP_PERCENT", "SCHEMA_DRIFT", "CONSISTENCY_VERDICT",
    "RUN1_FAILURES", "RUN2_FAILURES", "RECOVERY_RATE", "RECOVERY_OK", "RESILIENCE_VERDICT", "ROLLBACK_OK",
    "ADVERSARIAL_CASES_TOTAL", "REJECTED_TOTAL", "REJECTION_PRECISION",
    "SOURCES_COLLECTED", "ITEMS_NORMALIZED", "DEDUP_REMOVED", "TOOLS_NORMALIZED", "BREAKING_CHANGE_TOOLS",
    "APIS_AUDITED", "AVG_COVERAGE_SCORE", "ADVISORIES_NORMALIZED", "HIGH_SEVERITY_COUNT",
    "ROWS_REJECTED", "RAW_ROWS", "CLEAN_ROWS", "REJECTED_ROWS", "OUTLIER_ROWS", "SCHEMA_VALID", "CONSISTENCY",
    "PYFILECREATED", "SHFILECREATED", "SHEXECUTABLE", "UNKNOWN_KEYS",
    "CAUSE", "CHANGE", "TOTAL", "FAILED", "PASSED", "SKIPPED",
    "PROPOSAL_ID", "ARTIFACT_ID", "ARTIFACT_TYPE",
}

# Common aliases from model output variants.
_KV_KEY_ALIASES = {
    "RUNSTATUS": "RUN_STATUS",
    "RUN-STATUS": "RUN_STATUS",
    "OUTPUTDIR": "OUTPUT_DIR",
    "OUTPUTPATH": "OUTPUT_DIR",
    "STATUS": "RUN_STATUS",
    "FILES_OK": "PROOF_FILES_OK",
    "RECOVERYOK": "RECOVERY_OK",
    "PROOFFILESOK": "PROOF_FILES_OK",
    "UNKNOWNKEYS": "UNKNOWN_KEYS",
    "RESULT": "RUN_STATUS",
}

_CANONICAL_BY_COMPACT = {re.sub(r"[^A-Z0-9]", "", c): c for c in _CANONICAL_KV_KEYS}


def canonicalize_kv_key(key: str) -> str:
    k = key.strip().upper()
    if k in _CANONICAL_KV_KEYS:
        return k
    if k in _KV_KEY_ALIASES:
        return _KV_KEY_ALIASES[k]
    compact = re.sub(r"[^A-Z0-9]", "", k)
    alias_hit = _KV_KEY_ALIASES.get(compact)
    if alias_hit:
        return alias_hit
    mapped = _CANONICAL_BY_COMPACT.get(compact)
    if mapped:
        return mapped
    return k


def normalize_machine_kv_output(text: str) -> str:
    # Keep shell transcript formatting intact; this normalizer is for machine KV blocks.
    if re.search(r"(?mi)^\[step\s+\d+\]\s*\$", text) or re.search(r"(?mi)^Kind:\s", text):
        return text

    lines = [ln for ln in text.splitlines()]
    parsed: list[tuple[str, str, int, str]] = []
    for idx, ln in enumerate(lines):
        s = ln.strip()
        m = re.match(r"^([A-Za-z][A-Za-z0-9_]*)\s*([=:])\s*(.*)$", s)
        if not m:
            continue
        parsed.append((m.group(1), m.group(3), idx, m.group(2)))

    if len(parsed) < 3:
        return text

    # Skip normalization if no key resolves to a known canonical key — this is
    # not machine protocol output (e.g. env dumps, config files, shell output
    # that happens to contain KEY=value lines).
    if not any(canonicalize_kv_key(raw_k) in _CANONICAL_KV_KEYS for raw_k, *_ in parsed):
        return text

    normalized = lines[:]
    kv: dict[str, str] = {}
    unknown_keys: set[str] = set()
    for raw_k, v, idx, _sep in parsed:
        nk = canonicalize_kv_key(raw_k)
        normalized[idx] = f"{nk}={v}"
        kv[nk] = v.strip()
        if nk not in _CANONICAL_KV_KEYS and nk != "ERROR":
            unknown_keys.add(nk)

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

    # emit UNKNOWN_KEYS exactly once (drop any pre-existing/aliased lines)
    normalized = [ln for ln in normalized if not re.match(r"^UNKNOWN_KEYS\s*=", ln.strip())]
    if unknown_keys:
        normalized.append("UNKNOWN_KEYS=" + ",".join(sorted(unknown_keys)))

    return "\n".join(normalized).strip()
