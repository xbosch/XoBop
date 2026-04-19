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


def test_alias_and_unknown_key_annotation():
    text = "RUNSTATUS=PASS\nOUTPUTDIR=/tmp/x\nMYSTERY_FLAG=1"
    out = normalize_machine_kv_output(text)
    assert "RUN_STATUS=PASS" in out
    assert "OUTPUT_DIR=/tmp/x" in out
    assert "UNKNOWN_KEYS=MYSTERY_FLAG" in out


def test_mixed_separators_and_single_unknown_keys_line():
    text = (
        "RUNSTATUS=PASS\n"
        "PYFILECREATED=1\n"
        "SHFILECREATED=1\n"
        "SHEXECUTABLE=1\n"
        "OUTPUT=plot.svg generated successfully\n"
        "ERROR=NONE\n"
        "FOO=bar\n"
        "baz:qux\n"
        "UNKNOWNKEYS=foo,baz\n"
    )
    out = normalize_machine_kv_output(text)
    # canonical known keys should not be marked unknown
    assert "PYFILECREATED=1" in out
    assert "SHFILECREATED=1" in out
    assert "SHEXECUTABLE=1" in out
    assert "OUTPUT=plot.svg generated successfully" in out
    # unknown list is emitted exactly once and excludes meta key itself
    assert out.count("UNKNOWN_KEYS=") == 1
    assert "UNKNOWN_KEYS=BAZ,FOO" in out


def test_shell_transcript_passthrough_not_kv_normalized():
    text = "Kind: unknown\nCWD: /tmp\n[step 1] $ echo hi\nSTDOUT:\nRUNSTATUS=PASS\nExit: 0"
    out = normalize_machine_kv_output(text)
    assert out == text


def test_recoveryok_and_prooffilesok_aliases_are_canonicalized():
    text = "RUNSTATUS=PASS\nRECOVERYOK=1\nPROOFFILESOK=1\nERROR=NONE"
    out = normalize_machine_kv_output(text)
    assert "RUN_STATUS=PASS" in out
    assert "RECOVERY_OK=1" in out
    assert "PROOF_FILES_OK=1" in out
    assert "UNKNOWN_KEYS" not in out


def test_escape_underscores_md1_basic():
    from xobop.core_telegram_io import _escape_underscores_md1
    out = _escape_underscores_md1("op_events: 5\nruntime_snapshot: ok")
    assert "op\\_events" in out
    assert "runtime\\_snapshot" in out


def test_escape_underscores_md1_preserves_code_span():
    from xobop.core_telegram_io import _escape_underscores_md1
    out = _escape_underscores_md1("See `op_events` table and `runtime_snapshot` key.")
    # underscores inside backtick spans must NOT be escaped
    assert "`op_events`" in out
    assert "`runtime_snapshot`" in out
    # underscores outside code spans in surrounding text (none here besides the span)
    assert "op\\_events" not in out.replace("`op_events`", "")


def test_escape_underscores_md1_preserves_triple_backtick():
    from xobop.core_telegram_io import _escape_underscores_md1
    code = "```\nSELECT * FROM op_events\nWHERE runtime_snapshot = 1\n```"
    out = _escape_underscores_md1(code)
    assert "op_events" in out
    assert "runtime_snapshot" in out


def test_escape_underscores_md1_plain_text_no_underscores():
    from xobop.core_telegram_io import _escape_underscores_md1
    text = "Hello world. No underscores here."
    assert _escape_underscores_md1(text) == text


def test_canonical_kv_includes_llm_keys():
    from xobop.output_guardrails import normalize_machine_kv_output
    text = "RUN_STATUS=PASS\nCAUSE=route friction\nCHANGE=add retry\nTOTAL=10\nFAILED=2\nPASSED=8"
    out = normalize_machine_kv_output(text)
    assert "UNKNOWN_KEYS" not in out


def test_result_aliased_to_run_status():
    from xobop.output_guardrails import normalize_machine_kv_output
    text = "RESULT=PASS\nCAUSE=latency\nTOTAL=5\nFAILED=1"
    out = normalize_machine_kv_output(text)
    assert "RUN_STATUS=PASS" in out
    assert "UNKNOWN_KEYS" not in out


def test_shell_output_with_accidental_kv_not_mangled():
    # env-like output with no canonical keys must pass through unchanged
    text = "HOME=/home/xavi\nUSER=xavi\nSHELL=/bin/bash\nLANG=en_US.UTF-8"
    out = normalize_machine_kv_output(text)
    assert out == text


def test_config_file_like_output_not_mangled():
    # ini-style output with no canonical keys must pass through unchanged
    text = "host=localhost\nport=5432\ndbname=mydb\nuser=admin"
    out = normalize_machine_kv_output(text)
    assert out == text
