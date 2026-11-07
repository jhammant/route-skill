"""`route ultra --pool ARM` pins planning and building; reviews stay cross-family."""
import pytest

from route import cli, ultra_select
from route.pools import Model, Pool
from route.ultra_select import pick


def _pools():
    return {
        "claude": Pool(name="claude", shapes=(), model=Model(name="claude-opus-5-5"), cost="included"),
        "claude-work": Pool(name="claude-work", shapes=(), model=Model(name="claude-opus-5-5"), cost="included",
                            quota_provider="claude-work"),
        "claude-work-fable": Pool(name="claude-work-fable", shapes=(), model=Model(name="claude-fable-5-1"), cost="included"),
        "codex": Pool(name="codex", shapes=(), model=Model(name="gpt-6-astra"), cost="included"),
    }


@pytest.fixture(autouse=True)
def _clear_pin():
    ultra_select.set_pin(())
    yield
    ultra_select.set_pin(())


def test_pin_holds_planner_worker_fixer_and_integrator_to_the_pool():
    # Arrange
    pools = _pools()
    ultra_select.set_pin(["claude-work"])
    # Act / Assert
    for role in ("planner", "integrator"):
        assert pick(role, pools, {}) == "claude-work"
    assert pick("worker", pools, {}, tier="hard") == "claude-work"
    assert pick("fixer", pools, {}, author_arm="claude-work") == "claude-work"


def test_without_a_pin_the_strongest_arm_still_plans():
    # Arrange / Act
    chosen = pick("planner", _pools(), {})
    # Assert: unpinned behaviour is unchanged (Fable ranks above Opus)
    assert chosen == "claude-work-fable"


def test_reviewer_ignores_the_pin_and_stays_cross_family():
    # Arrange
    ultra_select.set_pin(["claude-work"])
    # Act
    reviewer = pick("reviewer", _pools(), {}, author_arm="claude-work")
    # Assert
    assert reviewer == "codex"


def test_a_pinned_pool_out_of_quota_holds_instead_of_falling_back():
    # Arrange
    ultra_select.set_pin(["claude-work"])
    # Act
    chosen = pick("worker", _pools(), {"claude-work": "critical"}, tier="hard")
    # Assert
    assert chosen is None


def test_cli_parses_repeatable_pool_and_refuses_it_with_team(capsys):
    # Arrange
    parser = cli._parser("route")
    # Act
    args = parser.parse_args(["ultra", "--pool", "claude-work", "--pool", "codex", "build it"])
    # Assert
    assert args.pin == ["claude-work", "codex"]
    with pytest.raises(ValueError):
        args = parser.parse_args(["ultra", "--pool", "claude-work", "--team", "website-audit", "q"])
        args.func(args)
