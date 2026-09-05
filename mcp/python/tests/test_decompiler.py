"""Contract/security tests for the real Ghidra adapter (no mock C generation).

Launcher/result contract tests use a fake result only to inspect boundaries.
Actual decompiler evidence comes from the separate Ghidra integration run.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from melonds_mcp import decompiler, tools_analysis


@pytest.fixture(autouse=True)
def isolated_result_cache(monkeypatch):
    monkeypatch.setattr(decompiler, "_RESULT_CACHE", decompiler._ResultCache())
    monkeypatch.delenv("MELONDS_MCP_ANALYSIS_CACHE", raising=False)


@pytest.fixture
def toolchain(tmp_path, monkeypatch):
    home = tmp_path / "Ghidra & literal installation"
    jdk = tmp_path / "Java ! literal installation"
    for path in (home / "Ghidra/Framework/Utility/lib/Utility.jar",
                 decompiler._native_decompiler(home),
                 jdk / "bin" / ("java.exe" if os.name == "nt" else "java"),
                 jdk / "bin" / ("javac.exe" if os.name == "nt" else "javac")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (home / "Ghidra/application.properties").write_text(
        "application.version=12.1.3\napplication.java.min=21\n", encoding="utf-8")
    language = home / "Ghidra/Processors/ARM/data/languages/ARM.ldefs"
    language.parent.mkdir(parents=True)
    language.write_text('<language id="ARM:LE:32:v5t"/><language id="ARM:LE:32:v4t"/>', encoding="utf-8")
    (jdk / "release").write_text('JAVA_VERSION="21.0.12.1"\nOS_ARCH="x86_64"\n', encoding="utf-8")
    monkeypatch.setenv("GHIDRA_HOME", str(home))
    monkeypatch.setenv("JAVA_HOME", str(jdk))
    return home, jdk


def test_missing_dependencies_reported_without_process(monkeypatch):
    monkeypatch.delenv("GHIDRA_HOME", raising=False)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(decompiler.subprocess, "Popen", lambda *a, **k: pytest.fail("unexpected process"))
    status = decompiler.analysis_status()
    assert status["available"] is False
    assert len(status["missing"]) == 2
    with pytest.raises(decompiler.DecompilerError, match="GHIDRA_UNAVAILABLE"):
        decompiler.decompile_bytes("0700a0e31eff2fe1", 0x02000000, 0x02000000)


def test_toolchain_checks_language_jdk_and_native_binary(toolchain):
    home, jdk = toolchain
    assert decompiler.analysis_status()["available"] is True
    decompiler._native_decompiler(home).unlink()
    assert "native decompiler" in ";".join(decompiler.analysis_status()["missing"])
    (jdk / "release").write_text('JAVA_VERSION="17.0.1"\nOS_ARCH="x86_64"\n', encoding="utf-8")
    assert "JDK 21+" in ";".join(decompiler.analysis_status()["missing"])


@pytest.mark.parametrize("kwargs", [
    {"hex_data": ""}, {"hex_data": "00 "}, {"hex_data": "0"},
    {"hex_data": "00" * 4097}, {"base_address": -1},
    {"base_address": 0xFFFFFFFC, "entry_address": 0xFFFFFFFC},
    {"entry_address": 0x02000001}, {"entry_address": 0x02000008},
    {"entry_address": True}, {"base_address": True}, {"cpu": True},
    {"cpu": 2}, {"timeout_seconds": 0}, {"timeout_seconds": 121},
    {"timeout_seconds": 1.0}, {"thumb": 1},
])
def test_invalid_input_rejected_before_dependency_or_launch(monkeypatch, kwargs):
    monkeypatch.setattr(decompiler, "_toolchain", lambda: pytest.fail("validated too late"))
    parameters = dict(hex_data="0700a0e31eff2fe1", base_address=0x02000000, entry_address=0x02000000)
    parameters.update(kwargs)
    with pytest.raises(ValueError):
        decompiler.decompile_bytes(**parameters)


def test_launcher_is_direct_java_and_request_provenance_is_immutable(toolchain, monkeypatch):
    monkeypatch.setenv("MELONDS_MCP_ANALYSIS_CACHE", "0")
    seen = []

    def run(command, workspace, timeout):
        seen.append(workspace)
        assert command[0].endswith("java.exe" if os.name == "nt" else "java")
        assert not any(value.lower().endswith((".bat", ".cmd")) for value in command)
        assert "ghidra.app.util.headless.AnalyzeHeadless" in command
        assert "ARM:LE:32:v4t" in command
        assert "true" in command
        assert (workspace / "input.bin").read_bytes() == bytes.fromhex("40187047")
        result = {"ok": True, "c_code": "int mcp_entry(int a,int b) { return a+b; }",
                  "analyzer": {"name": "Ghidra", "version": "12.1.3", "language_id": "ARM:LE:32:v4t"},
                  "function": {"entry_address": "0x02000000"}, "cfg": {"nodes": [], "edges": []}}
        (workspace / "result.json").write_text(json.dumps(result), encoding="utf-8")
        return 0, ""

    monkeypatch.setattr(decompiler, "_run", run)
    first = decompiler.decompile_bytes("40187047", 0x02000000, 0x02000000, thumb=True, cpu=1)
    second = decompiler.decompile_bytes("40187047", 0x02000000, 0x02000000, thumb=True, cpu=1)
    assert first["input"]["sha256"] == hashlib.sha256(bytes.fromhex("40187047")).hexdigest()
    assert first["input"]["mode"] == "thumb"
    assert second["kind"] == "decompilation"
    assert seen[0] != seen[1]
    assert all(not directory.exists() for directory in seen)


def test_failure_does_not_forge_a_disassembly_result(toolchain, monkeypatch):
    def fail(command, workspace, timeout):
        (workspace / "result.json").write_text('{"ok":false,"error":"No function"}', encoding="utf-8")
        return 0, "not pseudocode"
    monkeypatch.setattr(decompiler, "_run", fail)
    with pytest.raises(decompiler.DecompilerError, match="GHIDRA_DECOMPILE_FAILED.*No function"):
        decompiler.decompile_bytes("0700a0e31eff2fe1", 0x02000000, 0x02000000)


def test_real_process_output_is_bounded_and_not_sent_to_stdout(tmp_path, capsys):
    code, log = decompiler._run([sys.executable, "-c", "print('a'*100000)"], tmp_path, 5)
    assert code == 0
    assert len(log.encode()) <= decompiler.MAX_LOG_BYTES
    assert capsys.readouterr().out == ""


def test_real_process_timeout_is_bounded(tmp_path):
    start = time.monotonic()
    with pytest.raises(decompiler.DecompilerError, match="GHIDRA_TIMEOUT"):
        decompiler._run([sys.executable, "-c", "import time;time.sleep(30)"], tmp_path, 1)
    assert time.monotonic() - start < 15


@pytest.mark.parametrize("view", [None, "instruction", "data"])
def test_memory_tool_takes_one_debug_peek_and_attaches_frame(monkeypatch, view):
    tools = {}
    class Registry:
        def tool(self):
            def add(function):
                tools[function.__name__] = function
                return function
            return add
    calls = []
    def reader(selected):
        def peek(cpu, address, length):
            calls.append((selected, cpu, address, length))
            return bytes.fromhex("40187047")
        return peek
    emu = SimpleNamespace(lib=SimpleNamespace(get_status=lambda: [0, 17],
                                             peek_block=reader("data"),
                                             code_peek_block=reader("instruction")))
    monkeypatch.setattr(decompiler, "decompile_bytes", lambda *a, **k: {"input": {"sha256": "testhash"}})
    tools_analysis.register(Registry(), emu)
    result = tools["decompile_memory"](0x02000000, 4, 1, True, **({} if view is None else {"view": view}))
    selected = view or "instruction"
    assert calls == [(selected, 1, 0x02000000, 4)]
    assert result["snapshot"]["view"] == selected
    assert result["snapshot"]["access"] == ("debug_peek_data_view" if selected == "data" else "debug_peek_instruction_backing")
    assert result["snapshot"]["frame_number"] == 17
    assert result["snapshot"]["sha256"] == "testhash"


@pytest.fixture
def successful_analyzer(toolchain, tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "McpDecompile.java"
    script.write_bytes((decompiler.SCRIPT_DIR / "McpDecompile.java").read_bytes())
    monkeypatch.setattr(decompiler, "SCRIPT_DIR", scripts)
    calls = []

    def run(command, workspace, timeout):
        calls.append(command)
        # This fixture only tests cache/launcher boundaries, not decompilation.
        chain, _ = decompiler._toolchain()
        result = {"ok": True, "c_code": "int mcp_entry(void) { return 7; }",
                  "analyzer": {"name": "Ghidra", "version": chain.version,
                               "language_id": command[command.index("-processor") + 1]},
                  "function": {"entry_address": command[command.index("-postScript") + 3]},
                  "cfg": {"nodes": [{"start": "0x02000000"}], "edges": []}}
        (workspace / "result.json").write_text(json.dumps(result), encoding="utf-8")
        return 0, ""

    monkeypatch.setattr(decompiler, "_run", run)
    return calls, script


def _cached_request(**changes):
    arguments = dict(hex_data="0700a0e31eff2fe1", base_address=0x02000000, entry_address=0x02000000)
    arguments.update(changes)
    return decompiler.decompile_bytes(**arguments)


def test_successful_cache_hit_does_not_launch_second_java(successful_analyzer):
    calls, _ = successful_analyzer
    first = _cached_request()
    second = _cached_request(timeout_seconds=1)  # A successful fact is independent of time budget.
    assert len(calls) == 1
    assert first["cache"]["hit"] is False
    assert first["cache"]["stored"] is True
    assert second["cache"]["hit"] is True
    assert second["cache"]["key"] == first["cache"]["key"]
    assert second["cache"]["original_analysis_seconds"] == first["elapsed_seconds"]
    assert second["input"] == first["input"]
    status = decompiler.analysis_status()["cache"]
    assert status["enabled"] is True
    assert status["entries"] == 1
    assert 0 < status["json_bytes"] <= status["max_json_bytes"]


@pytest.mark.parametrize("changes", [
    {"hex_data": "0800a0e31eff2fe1"},
    {"entry_address": 0x02000004},
    {"base_address": 0x02001000, "entry_address": 0x02001000},
    {"cpu": 1}, {"thumb": True},
])
def test_input_address_cpu_and_mode_changes_miss(successful_analyzer, changes):
    calls, _ = successful_analyzer
    first = _cached_request()
    second = _cached_request(**changes)
    assert len(calls) == 2
    assert second["cache"]["hit"] is False
    assert first["cache"]["key"] != second["cache"]["key"]


def test_script_content_change_invalidates_even_with_preserved_file_metadata(successful_analyzer):
    calls, script = successful_analyzer
    first = _cached_request()
    info = script.stat()
    data = script.read_bytes()
    script.write_bytes(data.replace(b"Genuine", b"GENUINE", 1))  # Same length.
    os.utime(script, ns=(info.st_atime_ns, info.st_mtime_ns))
    second = _cached_request()
    assert len(calls) == 2
    assert first["cache"]["key"] != second["cache"]["key"]


@pytest.mark.parametrize("target", ["native", "utility", "language", "java", "modules"])
def test_binary_fingerprints_invalidate_same_version_installation(toolchain, successful_analyzer, target):
    home, jdk = toolchain
    paths = {"native": decompiler._native_decompiler(home),
             "utility": home / "Ghidra/Framework/Utility/lib/Utility.jar",
             "language": home / "Ghidra/Processors/ARM/data/languages/ARM5t_le.sla",
             "java": jdk / "bin" / ("java.exe" if os.name == "nt" else "java"),
             "modules": jdk / "lib/modules"}
    path = paths[target]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"old")
    first = _cached_request()
    path.write_bytes(b"changed binary")
    second = _cached_request()
    assert len(successful_analyzer[0]) == 2
    assert first["cache"]["key"] != second["cache"]["key"]


@pytest.mark.parametrize("target", ["ghidra_version", "java_version", "ghidra_path", "java_path"])
def test_toolchain_version_and_absolute_path_changes_miss(toolchain, successful_analyzer,
                                                        monkeypatch, tmp_path, target):
    home, jdk = toolchain
    first = _cached_request()
    if target == "ghidra_version":
        path = home / "Ghidra/application.properties"
        path.write_text(path.read_text().replace("12.1.3", "12.1.4"))
    elif target == "java_version":
        path = jdk / "release"
        path.write_text(path.read_text().replace("21.0.12.1", "21.0.12.2"))
    else:
        source = home if target == "ghidra_path" else jdk
        destination = tmp_path / (target + "_copy")
        shutil.copytree(source, destination)
        monkeypatch.setenv("GHIDRA_HOME" if target == "ghidra_path" else "JAVA_HOME", str(destination))
    second = _cached_request()
    assert len(successful_analyzer[0]) == 2
    assert first["cache"]["key"] != second["cache"]["key"]


def test_return_value_nested_mutations_and_snapshots_never_pollute_cache(successful_analyzer):
    first = _cached_request()
    first["c_code"] = "modified"
    first["cfg"]["nodes"][0]["start"] = "modified"
    first["snapshot"] = {"frame_number": 123}
    second = _cached_request()
    assert second["c_code"] != "modified"
    assert second["cfg"]["nodes"][0]["start"] != "modified"
    assert "snapshot" not in second
    second["input"]["sha256"] = "modified"
    third = _cached_request()
    assert third["input"]["sha256"] != "modified"


@pytest.mark.parametrize("error", ["failure", "timeout"])
def test_failures_and_timeouts_are_never_cached(successful_analyzer, monkeypatch, error):
    original_run = decompiler._run
    def fail(*args):
        if error == "timeout":
            raise decompiler.DecompilerError("GHIDRA_TIMEOUT: fixture")
        return 1, "failure fixture"
    monkeypatch.setattr(decompiler, "_run", fail)
    for _ in range(2):
        with pytest.raises(decompiler.DecompilerError):
            _cached_request()
    assert decompiler.analysis_status()["cache"]["entries"] == 0
    monkeypatch.setattr(decompiler, "_run", original_run)
    assert _cached_request()["cache"]["hit"] is False
    assert len(successful_analyzer[0]) == 1


def test_cache_hit_still_validates_inputs_and_dependencies(toolchain, successful_analyzer, monkeypatch):
    _cached_request()
    with pytest.raises(ValueError):
        _cached_request(timeout_seconds=0)
    monkeypatch.delenv("JAVA_HOME")
    with pytest.raises(decompiler.DecompilerError, match="GHIDRA_UNAVAILABLE"):
        _cached_request()
    assert len(successful_analyzer[0]) == 1


def test_disable_cache_skips_lookup_and_insert(successful_analyzer, monkeypatch):
    _cached_request()
    monkeypatch.setenv("MELONDS_MCP_ANALYSIS_CACHE", "0")
    for _ in range(2):
        result = _cached_request()
        assert result["cache"]["hit"] is False
        assert result["cache"]["stored"] is False
    assert len(successful_analyzer[0]) == 3
    status = decompiler.analysis_status()["cache"]
    assert status["enabled"] is False
    assert status["entries"] == 1


def test_lru_entry_and_byte_limits():
    cache = decompiler._ResultCache(max_entries=2, max_bytes=1000)
    assert cache.put("a", {"ok": True, "value": "a"})
    assert cache.put("b", {"ok": True, "value": "b"})
    assert cache.get("a") is not None  # a is newest; b must be evicted.
    assert cache.put("c", {"ok": True, "value": "c"})
    assert cache.get("b") is None
    assert cache.get("a") is not None
    assert cache.status()["entries"] == 2
    cache = decompiler._ResultCache(max_entries=8, max_bytes=100)
    for key in "abc":
        assert cache.put(key, {"ok": True, "value": key * 20})
    assert cache.status()["entries"] == 2
    assert cache.status()["json_bytes"] <= 100
    before = cache.status()
    assert cache.put("oversized", {"ok": True, "value": "x" * 200}) is False
    assert cache.status() == before
    cache = decompiler._ResultCache(max_result_bytes=32)
    assert cache.put("oversized", {"ok": True, "value": "x" * 33}) is False
    assert cache.status()["entries"] == 0


def test_cache_replacement_counts_json_bytes_once():
    cache = decompiler._ResultCache()
    result = {"ok": True, "value": "a"}
    cache.put("key", result)
    original_size = cache.status()["json_bytes"]
    cache.put("key", result)
    assert cache.status()["json_bytes"] == original_size
    assert cache.status()["entries"] == 1


def test_analyzer_process_does_not_hold_lru_lock(successful_analyzer, monkeypatch):
    original = decompiler._run
    def inspect_lock(*args):
        acquired = threading.Event()
        def access_cache():
            decompiler._RESULT_CACHE.status()
            acquired.set()
        thread = threading.Thread(target=access_cache)
        thread.start()
        assert acquired.wait(1), "LRU lock must not cover external analysis"
        thread.join()
        return original(*args)
    monkeypatch.setattr(decompiler, "_run", inspect_lock)
    _cached_request()


def test_installation_change_during_analysis_does_not_seed_old_key(successful_analyzer, monkeypatch):
    original = decompiler._run
    script = successful_analyzer[1]
    def changed(*args):
        result = original(*args)
        script.write_bytes(script.read_bytes() + b"\n// changed during analysis\n")
        return result
    monkeypatch.setattr(decompiler, "_run", changed)
    result = _cached_request()
    assert result["cache"]["stored"] is False
    assert decompiler.analysis_status()["cache"]["entries"] == 0
