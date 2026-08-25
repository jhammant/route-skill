"""SPEC ``pools``: shared registry plus optional machine-local overlay."""

from __future__ import annotations

from route.pools import load_pools


def test_local_overlay_is_last_wins_per_pool_and_base_only_survives(tmp_path):
    base = tmp_path / "pools.toml"
    local = tmp_path / "pools.local.toml"
    base.write_text(
        """
[pools.shared]
shapes = ["batch:classify"]
cost = "local"
strength = 2
dispatch = "base-dispatch {task}"

[pools.base-only]
shapes = ["writing"]
cost = "free"
probe = "base-probe {task}"
""",
        encoding="utf-8",
    )
    local.write_text(
        """
[pools.shared]
shapes = ["coding:test"]
cost = "paid"
strength = 9
probe = "local-probe {task}"

[pools.local-only]
shapes = ["research"]
dispatch = "local-dispatch {task}"
""",
        encoding="utf-8",
    )

    pools = load_pools(path=base, local_path=local)

    shared = pools["shared"]
    assert shared.shapes == ("coding:test",)
    assert shared.cost == "paid"
    assert shared.strength == 9
    assert shared.probe == "local-probe {task}"
    assert shared.dispatch == ""  # the local table replaces, not patches

    base_only = pools["base-only"]
    assert base_only.shapes == ("writing",)
    assert base_only.cost == "free"
    assert base_only.probe == "base-probe {task}"

    assert pools["local-only"].dispatch == "local-dispatch {task}"


def test_missing_local_overlay_is_ignored(tmp_path):
    base = tmp_path / "pools.toml"
    local = tmp_path / "missing-pools.local.toml"
    base.write_text(
        '[pools.base-only]\nshapes = ["writing"]\nprobe = "base {task}"\n',
        encoding="utf-8",
    )

    pools = load_pools(path=base, local_path=local)

    assert pools["base-only"].probe == "base {task}"


def test_dangling_local_overlay_symlink_is_ignored(tmp_path):
    base = tmp_path / "pools.toml"
    local = tmp_path / "pools.local.toml"
    base.write_text(
        '[pools.base-only]\nshapes = ["writing"]\nprobe = "base {task}"\n',
        encoding="utf-8",
    )
    local.symlink_to(tmp_path / "removed-pools.local.toml")

    pools = load_pools(path=base, local_path=local)

    assert pools["base-only"].probe == "base {task}"
