"""Run the native and real MCP workflows and emit a fresh coverage report.

Run with mcp/.venv/Scripts/python.exe mcp/tests/verify.py --library DLL.
Add --analysis (and GHIDRA_HOME/JAVA_HOME or the corresponding flags) to
include the real decompiler and verify all published tools in this release.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

import psutil
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ("native_smoke", "native_graphics", "native_memory_peek",
          "native_memory_poke", "native_savestate_errors")
WORKFLOWS = ("mcp_e2e", "graphics_e2e", "graphics_maps_e2e", "memory_poke_e2e",
             "debug_workflow_e2e", "control_workflow_e2e")
ANALYSIS_TOOLS = {"analysis_status", "decompile_bytes", "decompile_memory"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_identity() -> dict:
    files = {}
    for directory in (ROOT / "src", ROOT / "mcp/shim", ROOT / "mcp/python/melonds_mcp",
                      ROOT / "mcp/tests", ROOT / "mcp/ghidra_scripts"):
        for path in sorted(directory.rglob("*")):
            if path.suffix in (".py", ".cpp", ".c", ".h", ".java"):
                files[path.relative_to(ROOT).as_posix()] = sha256(path)
    return {"sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
            "files": files}


async def discover(library: Path) -> list[dict]:
    environment = dict(os.environ)
    environment.update(PYTHONPATH=str(ROOT / "mcp/python"), MELONDS_MCP_LIB=str(library))
    environment.pop("MELONDS_MCP_ROM", None)
    server = StdioServerParameters(command=sys.executable, args=["-m", "melonds_mcp"],
                                   env=environment, cwd=str(ROOT))
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            listed = await session.list_tools()
            return [{"name": tool.name, "description": tool.description,
                     "inputSchema": tool.inputSchema} for tool in listed.tools]


def run(name: str, arguments: list[str], destination: Path, timeout: int) -> dict:
    log = destination / f"{name}.log"
    started = time.monotonic()
    print(f"Running {name} ...", flush=True)
    with log.open("wb") as output:
        process = subprocess.Popen([sys.executable, *arguments], cwd=ROOT,
                                   stdout=output, stderr=subprocess.STDOUT)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Terminate only this verifier's owned process tree.
            try:
                owned = psutil.Process(process.pid)
                children = owned.children(recursive=True)
                for child in reversed(children):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                owned.kill()
            except psutil.NoSuchProcess:
                pass
            process.wait(timeout=10)
            code = 124
    text = log.read_text(encoding="utf-8", errors="replace")
    counts = re.findall(r"(?:Ran (\d+) tests|(?<!\d)(\d+) passed)", text)
    count = sum(int(first or second) for first, second in counts)
    result = {"name": name, "exit_code": code, "seconds": round(time.monotonic() - started, 3),
              "test_count": count, "log": str(log)}
    print(f"{'PASS' if code == 0 else 'FAIL'} {name} ({result['seconds']}s)", flush=True)
    if code:
        print(text[-6000:], flush=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, default=ROOT / "build/verification")
    parser.add_argument("--analysis", action="store_true")
    parser.add_argument("--ghidra-home", type=Path)
    parser.add_argument("--java-home", type=Path)
    args = parser.parse_args()
    library = args.library.resolve(strict=True)
    destination = args.artifacts.resolve() / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination.mkdir(parents=True)
    identity = source_identity()
    library_hash = sha256(library)
    manifest = asyncio.run(asyncio.wait_for(discover(library), timeout=30))
    (destination / "tools.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    checks, workflow_results = [], []
    checks.append(run("python", ["-m", "pytest", "mcp/python/tests", "-q"], destination, 120))
    for name in NATIVE:
        checks.append(run(name, [f"mcp/tests/{name}.py", "--library", str(library)], destination, 90))
    for name in (*WORKFLOWS, *(("analysis_e2e",) if args.analysis else ())):
        output = destination / name
        command = [f"mcp/tests/{name}.py", "--library", str(library), "--artifacts", str(output)]
        if name == "analysis_e2e":
            if args.ghidra_home:
                command += ["--ghidra-home", str(args.ghidra_home.resolve())]
            if args.java_home:
                command += ["--java-home", str(args.java_home.resolve())]
        check = run(name, command, destination, 450 if name == "analysis_e2e" else 120)
        checks.append(check)
        report = output / "result.json"
        if check["exit_code"] == 0 and report.is_file():
            workflow_results.append(json.loads(report.read_text(encoding="utf-8")))
        elif check["exit_code"] == 0:
            check["exit_code"] = 1
            check["error"] = "successful workflow did not emit its fresh evidence report"
    available = {entry["name"] for entry in manifest}
    coverage = {tool: [] for tool in sorted(available)}
    for report in workflow_results:
        for tool in report["verified_tools"]:
            coverage.setdefault(tool, []).append(report["backend"])
    missing = sorted(tool for tool in available if not coverage[tool])
    required_missing = set(missing) - (set() if args.analysis else ANALYSIS_TOOLS)
    unchanged = library_hash == sha256(library) and identity == source_identity()
    result = {
        "ok": all(check["exit_code"] == 0 for check in checks) and not required_missing and unchanged,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "scope": {"platform": platform.platform(), "python": platform.python_version(),
                  "console": "DS", "engine": "interpreter", "roms": "synthetic originals"},
        "analysis_included": args.analysis, "library": str(library), "library_sha256": library_hash,
        "source_identity": identity, "source_and_library_unchanged_during_verification": unchanged,
        "checks": checks, "tool_count": len(available), "covered_tool_count": len(available) - len(missing),
        "missing_tools": missing, "all_tools_exercised": not missing,
        "mcp_calls": sum(report["calls"] for report in workflow_results),
        "tool_coverage": coverage,
        "coverage_note": "Tool-level workflow coverage; not exhaustive argument, instruction, ROM or platform coverage.",
    }
    (destination / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("ok", "tool_count", "covered_tool_count", "missing_tools", "mcp_calls")}, indent=2))
    print(f"Report: {destination / 'result.json'}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
