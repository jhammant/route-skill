from route.swarm_channel import _safe_reason


def test_passes_a_mail_error_code_and_message():
    assert _safe_reason("subagent_limit: at most 32 live sub-agents\n") == "subagent_limit: at most 32 live sub-agents"


def test_uses_only_the_last_line():
    assert _safe_reason("warning: slow\ngroup_full: the group is full (33 members)") == "group_full: the group is full (33 members)"


def test_drops_anything_that_looks_like_a_credential():
    assert _safe_reason("invalid_token: am_st_abc123 was rejected") == ""
    assert _safe_reason("redeem_failed: bearer xyz") == ""


def test_drops_free_text_and_empty_output():
    assert _safe_reason("Traceback (most recent call last):") == ""
    assert _safe_reason("") == ""
    assert _safe_reason(None) == ""
