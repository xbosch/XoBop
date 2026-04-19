from xobop.handlers_runtime import (
    _has_pipeline_kv_response,
    _looks_like_pipeline_contract_request,
    _pipeline_contract_fail_response,
)


def test_pipeline_contract_request_detection():
    req = """
Final output ONLY (exact keys):
RUN_STATUS=<PASS|WARN|FAIL>
RAW_ROWS=<n>
CLEAN_ROWS=<n>
REJECTED_ROWS=<n>
OUTLIER_ROWS=<n>
SCHEMA_VALID=<0|1>
CONSISTENCY=<CONSISTENT|DRIFT>
RECOVERY_OK=<0|1>
PROOF_FILES_OK=<0|1>
OUTPUT_DIR=~/agent-test/stress-lab
ERROR=<NONE or short reason>
"""
    assert _looks_like_pipeline_contract_request(req) is True


def test_pipeline_fail_response_is_strict_kv():
    out = _pipeline_contract_fail_response()
    assert "RUN_STATUS=FAIL" in out
    assert "ERROR=execution_contract_not_satisfied" in out
    assert _has_pipeline_kv_response(out) is True


def test_non_kv_template_response_is_detected_as_invalid():
    templ = "If you want this executed, provide context and run again."
    assert _has_pipeline_kv_response(templ) is False
