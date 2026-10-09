# melonDS MCP server

This package exposes melonDS' live ARM9/ARM7 debugger through the Model Context
Protocol (MCP). The first milestone intentionally reuses melonDS' built-in GDB
remote stubs. It already supports connection management, core switching,
register and safe RAM/VRAM memory access, execution breakpoints, stepping, and ARM/Thumb
disassembly. A native melonDS control bridge will add emulator lifecycle,
input, savestates, screenshots, and GPU inspection in later milestones.

## Install for development

```powershell
python -m venv .\mcp-server\.venv
.\mcp-server\.venv\Scripts\python.exe -m pip install -e ".\mcp-server[dev]"
.\mcp-server\.venv\Scripts\python.exe -m pytest .\mcp-server\tests
```

Start the stdio server with:

```powershell
.\mcp-server\.venv\Scripts\python.exe -m melonds_mcp
```

Standard output is reserved for MCP traffic. Runtime logs go to standard error.

## Configure melonDS

Build this repository's melonDS fork with `ENABLE_GDBSTUB=ON` (the default), then disable JIT
and enable the GDB stub in **Emulation settings > Debug**. The default ports are
3333 for ARM9 and 3334 for ARM7. Start a game before calling `emulator_attach`.

The transitional client requires the fork's `QStartNoAckMode` reconnect fix and
disconnect cleanup. An unpatched stock melonDS build is not supported: after a
no-ack debugger disconnect it can fail the next handshake and retain execution
breakpoints with no debugger attached.

The upstream GDB implementation runs both DS CPUs on one emulator thread. Only
one core can therefore be stopped in its debugger loop at a time. The server
handles this through an explicit active-core switch: accessing ARM7 while ARM9
is stopped resumes ARM9 first, then stops ARM7. Do not attach a second debugger
to either port while this MCP server is connected.

This backend uses a conservative address allowlist: BIOS/TCM, RAM/WRAM,
palette, VRAM, and OAM. It refuses MMIO, cartridge ROM/SRAM/GPIO, and unaudited
mappings because some reads consume FIFO/card data, advance cartridge state, or
otherwise mutate the emulator. Writes use verification and best-effort
before-image rollback, but are not externally atomic. It also refuses CPSR and
SPSR writes because upstream callbacks can assign CPSR without safely
coordinating CPU mode, banked registers, and the instruction pipeline. These
operations will be added through checked native-bridge APIs, not through the
transitional RSP path. Upstream watchpoint packets are also not advertised: the
current interpreter memory paths do not call the watchpoint checker.

## Example MCP host configuration

Use absolute paths in real host configuration:

```json
{
  "mcpServers": {
    "melonds": {
      "command": "D:\\absolute\\path\\to\\melonDS-MCP\\mcp-server\\.venv\\Scripts\\python.exe",
      "args": ["-m", "melonds_mcp"]
    }
  }
}
```

The package currently uses local stdio transport. The server binds no network
port; only its debugger client connects to the configured melonDS host/ports.
