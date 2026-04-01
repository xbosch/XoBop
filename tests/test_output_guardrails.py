from xobop.output_guardrails import normalize_machine_kv_output


def test_normalizes_compact_keys_to_underscored_keys():
    text = "RUNSTATUS=PASS\nCOUNT=5\nOUTPUTDIR=/tmp/x\nERROR=NONE"
    out = normalize_machine_kv_output(text)
    assert "RUN_STATUS=PASS" in out
    assert "OUTPUT_DIR=/tmp/x" in out


def test_downgrades_pass_when_sha_missing():
    text = "RUN_STATUS=PASS\nSHA256=\nCOUNT=5"
    out = normalize_machine_kv_output(text)
    assert "RUN_STATUS=FAIL" in out
    assert "ERROR=Missing checksum evidence" in out
