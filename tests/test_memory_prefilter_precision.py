from xobop.core_memory_queue_runtime import memory_promotion_prefilter_reason, _looks_like_instruction_blob


def test_instruction_blob_still_caught_step_keywords():
    assert _looks_like_instruction_blob("Step 1: create folder. Step 2: run python script. return exactly this output.")


def test_instruction_blob_still_caught_shell_operators():
    assert _looks_like_instruction_blob("Step 1 create folder agent-test && chmod +x run.sh")


def test_instruction_blob_still_caught_numbered_list():
    assert _looks_like_instruction_blob("1. Install dependencies\n2. Run the server\n3. Open browser")


def test_instruction_blob_still_caught_code_fence_with_shell():
    assert _looks_like_instruction_blob("Run this:\n```\nbash setup.sh && chmod +x run.sh\n```")


def test_legitimate_preference_not_filtered():
    assert not _looks_like_instruction_blob("Remember to always check logs before deploying to production.")


def test_legitimate_note_with_run_not_filtered():
    assert not _looks_like_instruction_blob("I always run status checks before big changes.")


def test_legitimate_note_with_do_not_alone_not_filtered():
    # Single imperative hit should not trigger (threshold is >= 2)
    assert not _looks_like_instruction_blob("Do not forget to review the PR.")


def test_legitimate_note_with_create_not_filtered():
    assert not _looks_like_instruction_blob("We need to create a better onboarding flow.")


def test_prefilter_routes_legitimate_preference_correctly():
    reason = memory_promotion_prefilter_reason("I always prefer concise answers over verbose ones.")
    assert reason == "candidate_preference"


def test_prefilter_routes_legitimate_todo_correctly():
    reason = memory_promotion_prefilter_reason("Remember to schedule a meeting with the team about the deadline.")
    assert reason in ("candidate_preference", "candidate_observation")
