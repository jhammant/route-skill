"""pick() selects claude from a codex host on raw pools; its template must not be empty."""
from route.ultra_select import dispatch_template, pick


def test_claude_arm_from_a_codex_host_gets_a_command(pools):
    arm = pick("planner", pools, {}, host="codex")
    assert arm == "claude"
    assert dispatch_template(arm, pools, host="codex") != ""
