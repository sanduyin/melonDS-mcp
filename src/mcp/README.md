# melonDS native agent bridge primitives

This directory contains the C++17, Qt-free foundation for the in-process side
of the melonDS agent bridge. It is deliberately split into three layers:

- `FrameCodec` defines and incrementally parses the bounded binary wire frame.
- `LocalByteStreamServer` supplies a private, single-client local byte stream.
- `DebugCoordinator` serializes transport-thread requests onto the emulator
  owner thread and guards mutations with session/state/stop preconditions.

None of these classes parses command JSON or touches emulator state directly.
The eventual bridge service composes them and provides the melonDS-specific
command handlers.

## Local transport security and lifecycle

Callers provide a *logical* endpoint name containing only ASCII letters,
digits, `.`, `_`, and `-` (maximum 128 bytes). They cannot inject an operating
system path.

On Windows, the server creates
`\\.\pipe\melonDS-MCP-<name>` with all of these properties:

- a DACL containing only the current process-token user SID;
- `PIPE_REJECT_REMOTE_CLIENTS`, so SMB/remote named-pipe clients are rejected;
- `FILE_FLAG_FIRST_PIPE_INSTANCE` and a maximum of one pipe instance;
- byte-stream mode and overlapped I/O for deadlines and cancellation.

On POSIX, the server creates an `AF_UNIX` stream socket in a mode-0700 runtime
directory owned by the effective user. A suitable absolute `XDG_RUNTIME_DIR`
is preferred; otherwise `/tmp/melonds-mcp-<uid>` is created and verified. The
socket is mode 0600, and every accepted peer is checked against the effective
UID with `SO_PEERCRED` (Linux) or `getpeereid` (macOS/BSD). There is no TCP
fallback.

`Start()` creates the endpoint but does not accept implicitly. `Accept()`,
`ReadExact()`, and `WriteExact()` are serialized and use one total deadline per
call. Every short operation reports a stable `StreamError`, the exact number of
bytes already transferred, and the native OS error when one exists. A timed-out
accept can be retried. `DisconnectClient()` keeps the listener reusable.
`Stop()` is terminal, idempotent, thread-safe, and promptly wakes/cancels a
pending infinite accept/read/write. Construct a new server to restart.

## Version 1 frame format

Every numeric field is unsigned and little-endian. The fixed header is exactly
32 bytes:

| Offset | Size | Field |
| ---: | ---: | --- |
| 0 | 4 | ASCII magic `MDSB` |
| 4 | 2 | protocol version (`1`) |
| 6 | 2 | message type (`1` request, `2` response, `3` event, `4` cancel) |
| 8 | 4 | flags (must be zero in version 1) |
| 12 | 8 | request ID |
| 20 | 4 | UTF-8 JSON byte length |
| 24 | 4 | opaque binary byte length |
| 28 | 4 | reserved (must be zero) |

The header is followed by JSON bytes and then opaque binary bytes. The codec
validates strict UTF-8 but leaves JSON syntax to the command layer. Hard limits
are 1 MiB of JSON and 64 MiB of binary per frame; callers may lower them.
Malformed input puts `FrameParser` into a sticky error state until `Reset()` so
corrupt input cannot trigger an accidental stream resynchronization.

## Owner-thread coordination

Transport producers submit opaque commands to `DebugCoordinator`. Exactly one
emulator thread binds as owner and drains the queue. The coordinator provides:

- monotonic request IDs and a bounded queue;
- deadlines and cooperative cancellation;
- `server_instance_id`, `session_id`, `state_version`, `stop_id`, and
  `frame_number` snapshots;
- compare-and-swap preconditions for mutating commands;
- explicit run-state transitions and taint propagation;
- futures that always carry the post-command snapshot.

This keeps emulator state changes off IPC threads and makes stale Agent actions
fail deterministically instead of mutating a newer pause/session.

## Standalone Release build and tests

The standalone build enables tests and warnings-as-errors by default. From a
compiler-enabled shell (for Windows, an x64 Visual Studio developer prompt):

```text
cmake -S src/mcp -B build/mcp-native -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DMELONDS_MCP_PROTOCOL_BUILD_TESTS=ON \
  -DMELONDS_MCP_PROTOCOL_WARNINGS_AS_ERRORS=ON
cmake --build build/mcp-native --config Release
ctest --test-dir build/mcp-native -C Release --output-on-failure
```

The test executables cover golden/malformed framing, fragmented and batched
parsing, a real FrameCodec-over-local-stream round trip, accept/read deadlines,
listener reuse, single-instance conflicts, cancellation of infinite waits, and
the coordinator's concurrency/precondition/state-transition invariants. The
Windows transport suite is a real named-pipe test, not a mocked transport.
