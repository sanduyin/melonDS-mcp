"""Bounded, isolated Ghidra headless decompilation of explicit ARM byte ranges.

Uses the official ghidra.Ghidra launcher/DecompInterface, not a disassembler
presented as a decompiler. No shell or batch file is used on any platform.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from collections import OrderedDict
import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

MAX_INPUT_BYTES = 4096
MAX_RESULT_BYTES = 1024 * 1024
MAX_LOG_BYTES = 32768
CACHE_MAX_ENTRIES = 8
CACHE_MAX_BYTES = 8 * 1024 * 1024
LANGUAGES = {0: "ARM:LE:32:v5t", 1: "ARM:LE:32:v4t"}
SCRIPT_DIR = Path(__file__).resolve().parents[2] / "ghidra_scripts"


class DecompilerError(RuntimeError):
    """Dependency, launcher, timeout, or genuine decompiler failure."""


@dataclass(frozen=True)
class Toolchain:
    ghidra_home: Path
    java: Path
    version: str
    java_version: str


class _ResultCache:
    """LRU bounded by both entry count and encoded JSON bytes (not heap size)."""

    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES,
                 max_bytes: int = CACHE_MAX_BYTES, max_result_bytes: int = MAX_RESULT_BYTES):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_result_bytes = max_result_bytes
        self._entries: OrderedDict[str, tuple[dict, int]] = OrderedDict()
        self._json_bytes = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> dict | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            # Callers may attach a fresh emulator snapshot or mutate nested CFG.
            return copy.deepcopy(entry[0])

    def put(self, key: str, result: dict) -> bool:
        owned = copy.deepcopy(result)
        size = len(json.dumps(owned, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if (not owned.get("ok") or self.max_entries <= 0
                or size > min(self.max_bytes, self.max_result_bytes)):
            return False
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._json_bytes -= previous[1]
            while self._entries and (len(self._entries) >= self.max_entries
                                     or self._json_bytes + size > self.max_bytes):
                _, (_, removed_size) = self._entries.popitem(last=False)
                self._json_bytes -= removed_size
            self._entries[key] = (owned, size)
            self._json_bytes += size
        return True

    def status(self) -> dict:
        with self._lock:
            return {"policy": "process_local_success_only_lru", "entries": len(self._entries),
                    "json_bytes": self._json_bytes, "max_entries": self.max_entries,
                    "max_json_bytes": self.max_bytes, "max_result_json_bytes": self.max_result_bytes}


_RESULT_CACHE = _ResultCache()


def _cache_enabled() -> bool:
    return os.environ.get("MELONDS_MCP_ANALYSIS_CACHE", "1").strip() != "0"


def _file_fingerprint(path: Path) -> dict:
    resolved = path.resolve()
    try:
        info = resolved.stat()
    except FileNotFoundError:
        return {"path": str(resolved), "missing": True}
    return {"path": str(resolved), "size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns}


def _cache_key(chain: Toolchain, data: bytes, base: int, entry: int, thumb: bool, cpu: int) -> str:
    """Bind cached facts to input, adapter, installation, ISA and binary versions.

    Script content is hashed. Installed analyzer/runtime/language files use
    absolute path, size, mtime_ns and ctime_ns to avoid rehashing large JDK/JAR
    binaries on every live-memory read. No cache key depends on wall-clock age.
    """
    home = chain.ghidra_home.resolve()
    java = chain.java.resolve()
    jdk = java.parent.parent
    language_dir = home / "Ghidra/Processors/ARM/data/languages"
    script = (SCRIPT_DIR / "McpDecompile.java").resolve()
    paths = {
        home / "Ghidra/application.properties",
        home / "Ghidra/Framework/Utility/lib/Utility.jar",
        home / "Ghidra/Framework/SoftwareModeling/lib/SoftwareModeling.jar",
        home / "Ghidra/Features/Base/lib/Base.jar",
        home / "Ghidra/Features/Decompiler/lib/Decompiler.jar",
        _native_decompiler(home), java, jdk / "release", jdk / "lib/modules",
        java.parent / ("javac.exe" if os.name == "nt" else "javac"),
    }
    paths.update(path for path in language_dir.iterdir()
                 if path.suffix.lower() in (".sla", ".ldefs", ".pspec", ".cspec"))
    identity = {
        "cache_schema": 1, "input_sha256": hashlib.sha256(data).hexdigest(),
        "input_size": len(data), "base_address": base, "entry_address": entry,
        "cpu": cpu, "thumb": thumb, "language_id": LANGUAGES[cpu], "compiler_spec": "default",
        "ghidra_home": str(home), "ghidra_version": chain.version,
        "java": str(java), "java_version": chain.java_version,
        "script": str(script), "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "files": [_file_fingerprint(path) for path in sorted(paths, key=str)],
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _properties(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    result = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"')
    return result


def _native_decompiler(home: Path) -> Path:
    machine = platform.machine().lower()
    architecture = "arm_64" if machine in ("arm64", "aarch64") else "x86_64"
    # Public Windows ARM installations run the supplied x86-64 native binaries.
    directory = ("win_x86_64" if os.name == "nt" else
                 ("mac_" if sys.platform == "darwin" else "linux_") + architecture)
    return home / "Ghidra/Features/Decompiler/os" / directory / ("decompile.exe" if os.name == "nt" else "decompile")


def _toolchain() -> tuple[Toolchain | None, list[str]]:
    missing: list[str] = []
    ghidra_env, java_env = os.environ.get("GHIDRA_HOME"), os.environ.get("JAVA_HOME")
    if not ghidra_env:
        missing.append("Set GHIDRA_HOME to an extracted official Ghidra distribution")
    if not java_env:
        missing.append("Set JAVA_HOME to a 64-bit JDK 21+ installation")
    if missing:
        return None, missing
    ghidra, jdk = Path(ghidra_env).resolve(), Path(java_env).resolve()
    props = _properties(ghidra / "Ghidra/application.properties")
    utility = ghidra / "Ghidra/Framework/Utility/lib/Utility.jar"
    java = jdk / "bin" / ("java.exe" if os.name == "nt" else "java")
    javac = jdk / "bin" / ("javac.exe" if os.name == "nt" else "javac")
    if not utility.is_file() or not props.get("application.version"):
        missing.append("GHIDRA_HOME is not a built Ghidra distribution (Utility.jar/version missing)")
    if not _native_decompiler(ghidra).is_file():
        missing.append("Ghidra native decompiler for this host platform is missing")
    if not java.is_file() or not javac.is_file():
        missing.append("JAVA_HOME must contain bin/java and bin/javac (a JDK, not just a JRE)")
    java_props = _properties(jdk / "release")
    version = java_props.get("JAVA_VERSION", "")
    match = re.match(r"(\d+)", version)
    minimum = int(props.get("application.java.min", "21"))
    if not match or int(match[1]) < minimum:
        missing.append(f"Ghidra requires JDK {minimum}+; JAVA_HOME reports {version or 'unknown'}")
    if java_props.get("OS_ARCH") not in ("x86_64", "amd64", "aarch64", "arm64"):
        missing.append("A supported 64-bit JDK is required")
    languages = ghidra / "Ghidra/Processors/ARM/data/languages/ARM.ldefs"
    language_text = languages.read_text(encoding="utf-8") if languages.is_file() else ""
    for language in LANGUAGES.values():
        if f'id="{language}"' not in language_text:
            missing.append(f"Ghidra language definition missing: {language}")
    if not (SCRIPT_DIR / "McpDecompile.java").is_file():
        missing.append("Bundled McpDecompile.java script is missing")
    if missing:
        return None, missing
    return Toolchain(ghidra, java, props["application.version"], version), []


def analysis_status() -> dict:
    """Report configured dependencies without launching Java or mutating a project."""
    chain, missing = _toolchain()
    return {"available": chain is not None, "analyzer": "Ghidra",
            "version": chain.version if chain else None,
            "java_version": chain.java_version if chain else None,
            "languages": LANGUAGES, "max_input_bytes": MAX_INPUT_BYTES,
            "timeout_range_seconds": [1, 120], "missing": missing,
            "cache": {"enabled": _cache_enabled(), **_RESULT_CACHE.status()},
            "verification": "configuration_checked; fresh_execution_or_fingerprint_matched_success_cache"}


def _validate(data: bytes, base: int, entry: int, thumb: bool, timeout: int, cpu: int) -> None:
    if type(cpu) is not int or cpu not in LANGUAGES:
        raise ValueError("cpu must be 0 (ARM9) or 1 (ARM7)")
    if type(thumb) is not bool:
        raise ValueError("thumb must be a boolean")
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError("timeout_seconds must be an integer from 1 to 120")
    if not 1 <= len(data) <= MAX_INPUT_BYTES:
        raise ValueError(f"code must contain 1..{MAX_INPUT_BYTES} bytes")
    if type(base) is not int or not 0 <= base <= 0xFFFFFFFF:
        raise ValueError("base_address must be an unsigned 32-bit integer")
    if base + len(data) > 0x100000000:
        raise ValueError("input range exceeds the 32-bit address space")
    width = 2 if thumb else 4
    if type(entry) is not int or not base <= entry <= base + len(data) - width:
        raise ValueError("entry_address must point to a complete instruction inside the supplied bytes")
    if entry % width:
        raise ValueError(f"entry_address must be {width}-byte aligned (do not set Thumb bit 0)")


def _command(chain: Toolchain, workspace: Path, base: int, entry: int,
             thumb: bool, timeout: int, cpu: int) -> list[str]:
    utility = chain.ghidra_home / "Ghidra/Framework/Utility/lib/Utility.jar"
    return [str(chain.java), "-Xmx512M", "-Xshare:off", "-Djava.awt.headless=true",
            "-Djava.system.class.loader=ghidra.GhidraClassLoader", "-Dfile.encoding=UTF-8",
            "-Duser.language=en", "-Duser.country=US", "-Dcpu.core.override=1",
            "-Dlog4j.skipJansi=true", "--enable-native-access=ALL-UNNAMED",
            f"-Duser.home={workspace / 'home'}", f"-Djava.io.tmpdir={workspace / 'tmp'}",
            f"-Dapplication.settingsdir={workspace / 'settings'}",
            f"-Dapplication.cachedir={workspace / 'cache'}",
            f"-Dapplication.tempdir={workspace / 'tmp'}",
            "-Djavax.xml.accessExternalDTD=", "-Djavax.xml.accessExternalSchema=",
            "-Djavax.xml.accessExternalStylesheet=", "-cp", str(utility),
            "ghidra.Ghidra", "ghidra.app.util.headless.AnalyzeHeadless",
            str(workspace), "mcp_project", "-import", str(workspace / "input.bin"),
            "-loader", "BinaryLoader", "-loader-baseAddr", f"0x{base:08x}",
            "-processor", LANGUAGES[cpu], "-cspec", "default", "-noanalysis",
            "-deleteProject", "-max-cpu", "1", "-scriptPath", str(SCRIPT_DIR),
            "-postScript", "McpDecompile.java", str(workspace / "result.json"),
            f"0x{entry:08x}", "true" if thumb else "false", str(timeout)]


def _kill_tree(process: subprocess.Popen) -> None:
    """Kill only our owned analyzer and its native decompiler subprocesses."""
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/taskkill.exe"
        subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, check=False,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def _run(command: list[str], workspace: Path, timeout: int) -> tuple[int, str]:
    environment = os.environ.copy()
    for key in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "CLASSPATH"):
        environment.pop(key, None)
    output = bytearray()
    kwargs = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
              else {"start_new_session": True})
    process = subprocess.Popen(command, cwd=workspace, env=environment, shell=False,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, **kwargs)

    def drain() -> None:
        assert process.stdout is not None
        while chunk := process.stdout.read(4096):
            output.extend(chunk)
            if len(output) > MAX_LOG_BYTES:
                del output[:-MAX_LOG_BYTES]

    reader = threading.Thread(target=drain, daemon=True, name="ghidra-log-drain")
    reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_tree(process)
        raise DecompilerError(f"GHIDRA_TIMEOUT: analysis exceeded {timeout} seconds; process tree terminated") from exc
    except BaseException:
        _kill_tree(process)
        raise
    finally:
        reader.join(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
    return process.returncode, output.decode("utf-8", errors="replace")


def decompile_bytes(hex_data: str, base_address: int, entry_address: int,
                    thumb: bool = False, timeout_seconds: int = 60, cpu: int = 0) -> dict:
    """Return real C pseudocode/CFG for one explicit entry in a bounded byte copy."""
    start = time.monotonic()
    if type(hex_data) is not str or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,4096}", hex_data):
        raise ValueError("hex_data must be 1..4096 bytes encoded as uninterrupted hex pairs")
    data = bytes.fromhex(hex_data)
    _validate(data, base_address, entry_address, thumb, timeout_seconds, cpu)
    chain, missing = _toolchain()
    if chain is None:
        raise DecompilerError("GHIDRA_UNAVAILABLE: " + "; ".join(missing))
    key = _cache_key(chain, data, base_address, entry_address, thumb, cpu)
    cache_enabled = _cache_enabled()
    cached = _RESULT_CACHE.get(key) if cache_enabled else None
    if cached is not None:
        original_seconds = cached["elapsed_seconds"]
        cached["cache"] = {"enabled": True, "hit": True, "key": key,
                           "original_analysis_seconds": original_seconds}
        cached["elapsed_seconds"] = round(time.monotonic() - start, 3)
        return cached
    with tempfile.TemporaryDirectory(prefix="melonds-ghidra-") as directory:
        workspace = Path(directory)
        for child in ("home", "tmp", "settings", "cache"):
            (workspace / child).mkdir()
        (workspace / "input.bin").write_bytes(data)
        command = _command(chain, workspace, base_address, entry_address, thumb, timeout_seconds, cpu)
        returncode, log = _run(command, workspace, timeout_seconds)
        result_path = workspace / "result.json"
        if returncode != 0 or not result_path.is_file():
            raise DecompilerError(f"GHIDRA_FAILED: exit={returncode}; " + log[-8000:])
        if result_path.stat().st_size > MAX_RESULT_BYTES:
            raise DecompilerError("GHIDRA_OUTPUT_LIMIT: analyzer result exceeded 1 MiB")
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise DecompilerError("GHIDRA_BAD_RESULT: result JSON is unreadable") from exc
        if not isinstance(result, dict) or not result.get("ok"):
            message = result.get("error", "invalid result") if isinstance(result, dict) else "invalid result"
            raise DecompilerError("GHIDRA_DECOMPILE_FAILED: " + str(message)[:8000])
        if not isinstance(result.get("c_code"), str) or not result["c_code"].strip():
            raise DecompilerError("GHIDRA_BAD_RESULT: analyzer returned no C pseudocode")
        analyzer = result.get("analyzer", {})
        if (analyzer.get("name") != "Ghidra" or analyzer.get("version") != chain.version
                or analyzer.get("language_id") != LANGUAGES[cpu]):
            raise DecompilerError("GHIDRA_BAD_RESULT: analyzer identity/ISA does not match the request")
        if result.get("function", {}).get("entry_address") != f"0x{entry_address:08x}":
            raise DecompilerError("GHIDRA_BAD_RESULT: analyzed function entry does not match the request")
    result["kind"] = "decompilation"
    result["input"] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                       "base_address": f"0x{base_address:08x}", "entry_address": f"0x{entry_address:08x}",
                       "cpu": cpu, "isa": "ARMv5TE" if cpu == 0 else "ARMv4T",
                       "mode": "thumb" if thumb else "arm", "endian": "little"}
    result["elapsed_seconds"] = round(time.monotonic() - start, 3)
    result["limitations"] = ["Heuristic C pseudocode, not original source or a proof of semantics.",
                              "Only supplied bytes are mapped; external code/data and symbols are unavailable.",
                              "Entry address and initial ARM/Thumb mode are explicitly supplied by the caller."]
    # Recheck the installation/script fingerprint after the slow analysis. An
    # on-disk update during execution must not seed the prior installation's key.
    unchanged = key == _cache_key(chain, data, base_address, entry_address, thumb, cpu)
    stored = cache_enabled and unchanged and _RESULT_CACHE.put(key, result)
    result["cache"] = {"enabled": cache_enabled, "hit": False, "key": key,
                       "stored": stored, "original_analysis_seconds": result["elapsed_seconds"]}
    return result
