"""Resource detection must follow the cgroup, not the machine underneath it."""

import oex.system as system


def _fake_chain(tmp_path, files):
    directory = tmp_path / "cg"
    directory.mkdir()
    for name, content in files.items():
        (directory / name).write_text(content)
    return [directory]


def test_cpu_count_follows_cgroup_quota(monkeypatch, tmp_path):
    monkeypatch.setattr(
        system, "_cgroup_chain", lambda: _fake_chain(tmp_path, {"cpu.max": "200000 100000"})
    )
    monkeypatch.setattr(system.psutil, "cpu_count", lambda logical=True: 32)
    monkeypatch.setattr(system.os, "sched_getaffinity", lambda pid: set(range(32)))
    assert system.cpu_count() == 2


def test_cpu_count_uses_host_when_uncapped(monkeypatch, tmp_path):
    monkeypatch.setattr(
        system, "_cgroup_chain", lambda: _fake_chain(tmp_path, {"cpu.max": "max 100000"})
    )
    monkeypatch.setattr(system.psutil, "cpu_count", lambda logical=True: 32)
    monkeypatch.setattr(system.os, "sched_getaffinity", lambda pid: set(range(32)))
    assert system.cpu_count() == 32


def test_cpu_count_never_returns_zero_for_fractional_quota(monkeypatch, tmp_path):
    monkeypatch.setattr(
        system, "_cgroup_chain", lambda: _fake_chain(tmp_path, {"cpu.max": "50000 100000"})
    )
    monkeypatch.setattr(system.psutil, "cpu_count", lambda logical=True: 32)
    monkeypatch.setattr(system.os, "sched_getaffinity", lambda pid: set(range(32)))
    assert system.cpu_count() == 1


def test_memory_follows_cgroup_cap(monkeypatch, tmp_path):
    monkeypatch.delenv("OEX_MEMORY_GB", raising=False)
    monkeypatch.setattr(
        system, "_cgroup_chain", lambda: _fake_chain(tmp_path, {"memory.max": str(8 * 1024**3)})
    )
    monkeypatch.setattr(
        system.psutil, "virtual_memory", lambda: type("m", (), {"total": 64 * 1024**3})()
    )
    assert system.total_memory_gb() == 8.0


def test_memory_uses_host_when_uncapped(monkeypatch, tmp_path):
    monkeypatch.delenv("OEX_MEMORY_GB", raising=False)
    monkeypatch.setattr(
        system, "_cgroup_chain", lambda: _fake_chain(tmp_path, {"memory.max": "max"})
    )
    monkeypatch.setattr(
        system.psutil, "virtual_memory", lambda: type("m", (), {"total": 64 * 1024**3})()
    )
    assert system.total_memory_gb() == 64.0


def test_env_override_wins_over_cgroup(monkeypatch, tmp_path):
    monkeypatch.setenv("OEX_MEMORY_GB", "3")
    monkeypatch.setattr(
        system, "_cgroup_chain", lambda: _fake_chain(tmp_path, {"memory.max": str(8 * 1024**3)})
    )
    assert system.total_memory_gb() == 3.0


def test_tightest_ancestor_limit_wins(monkeypatch, tmp_path):
    """A pod-level cap binds even when the container's own cgroup is uncapped."""
    parent = tmp_path / "parent"
    child = tmp_path / "child"
    parent.mkdir()
    child.mkdir()
    (parent / "memory.max").write_text(str(4 * 1024**3))
    (child / "memory.max").write_text("max")
    monkeypatch.delenv("OEX_MEMORY_GB", raising=False)
    monkeypatch.setattr(system, "_cgroup_chain", lambda: [child, parent])
    monkeypatch.setattr(
        system.psutil, "virtual_memory", lambda: type("m", (), {"total": 64 * 1024**3})()
    )
    assert system.total_memory_gb() == 4.0


def test_missing_cgroup_files_fall_back_to_host(monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.delenv("OEX_MEMORY_GB", raising=False)
    monkeypatch.setattr(system, "_cgroup_chain", lambda: [empty])
    monkeypatch.setattr(system.psutil, "cpu_count", lambda logical=True: 16)
    monkeypatch.setattr(system.os, "sched_getaffinity", lambda pid: set(range(16)))
    monkeypatch.setattr(
        system.psutil, "virtual_memory", lambda: type("m", (), {"total": 32 * 1024**3})()
    )
    assert system.cpu_count() == 16
    assert system.total_memory_gb() == 32.0
