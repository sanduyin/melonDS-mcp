/*
    Copyright 2026 melonDS-MCP contributors

    This file is part of melonDS.

    melonDS is free software: you can redistribute it and/or modify it under
    the terms of the GNU General Public License as published by the Free
    Software Foundation, either version 3 of the License, or (at your option)
    any later version.
*/

#ifndef MELONDS_MCP_DEBUGCOORDINATOR_H_
#define MELONDS_MCP_DEBUGCOORDINATOR_H_

#include <any>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <future>
#include <functional>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>

namespace melonDS::MCP
{

using RequestId = std::uint64_t;
using StateCounter = std::uint64_t;
using CoordinatorClock = std::chrono::steady_clock;
using CoordinatorTimePoint = CoordinatorClock::time_point;

enum class RunState
{
    Running,
    Paused,
    Stopped,
    Tainted,
};

const char* RunStateName(RunState state) noexcept;

// Stable machine-readable errors. The string returned by ErrorCodeName() is
// suitable for the bridge JSON v1 error.code field.
enum class CoordinatorErrorCode
{
    None,
    InvalidArgument,
    MissingPrecondition,
    StaleServerInstance,
    StaleSession,
    StaleStateVersion,
    StaleStopId,
    NonMonotonicRequestId,
    RequestIdExhausted,
    CounterExhausted,
    QueueFull,
    DeadlineExceeded,
    Cancelled,
    RequestNotFound,
    CoordinatorClosed,
    OwnerThreadNotBound,
    WrongOwnerThread,
    DrainAlreadyActive,
    InvalidStateTransition,
    HandlerFailure,
    InternalError,
};

const char* CoordinatorErrorCodeName(CoordinatorErrorCode code) noexcept;

class CoordinatorException final : public std::runtime_error
{
public:
    CoordinatorException(CoordinatorErrorCode code, std::string message);

    CoordinatorErrorCode Code() const noexcept { return ErrorCode; }

private:
    CoordinatorErrorCode ErrorCode;
};

struct StateSnapshot
{
    std::string ServerInstanceId;
    std::string SessionId;
    StateCounter StateVersion = 0;
    StateCounter StopId = 0;
    StateCounter FrameNumber = 0;
    RunState State = RunState::Stopped;
    std::string TaintReason;
};

struct CommandPreconditions
{
    std::optional<std::string> ExpectedServerInstanceId;
    std::optional<std::string> ExpectedSessionId;
    std::optional<StateCounter> ExpectedStateVersion;
    std::optional<StateCounter> ExpectedStopId;
};

// A caller can require selected compare-and-swap values to be present. This
// lets mutating operations enforce strong consistency without the coordinator
// knowing anything about emulator commands.
struct RequiredPreconditions
{
    bool ServerInstanceId = false;
    bool SessionId = false;
    bool StateVersion = false;
    bool StopId = false;
};

struct CommandSpec
{
    // Zero asks the coordinator to allocate the next server-wide ID. Explicit
    // nonzero IDs must be strictly greater than every prior submitted ID.
    RequestId Id = 0;
    std::string Operation;
    CommandPreconditions Preconditions;
    RequiredPreconditions Required;
    std::optional<CoordinatorTimePoint> Deadline;
};

struct CommandError
{
    CoordinatorErrorCode Code = CoordinatorErrorCode::None;
    std::string Message;
};

// The value is deliberately std::any: the queue neither parses bridge JSON nor
// depends on an emulator result type. A transport adapter may use std::string,
// while an in-process caller can use a native value object.
struct CommandOutcome
{
    bool Ok = true;
    std::any Value;
    CommandError Error;

    static CommandOutcome Success(std::any value = {});
    static CommandOutcome Failure(
        CoordinatorErrorCode code,
        std::string message);
};

struct CommandResult
{
    RequestId Id = 0;
    bool Ok = false;
    std::any Value;
    CommandError Error;
    StateSnapshot Snapshot;
};

namespace detail
{
struct CancellationState;
}

class DebugCoordinator;

// Constructed only by DebugCoordinator on the bound owner/emulator thread.
// Cancellation and deadline checks are cooperative once a handler has begun:
// long-running handlers must call AbortIfRequested() at safe interruption
// points. The coordinator never lies about a completed side effect by changing
// a successful ignored cancellation into a cancelled result after the fact.
class CommandContext final
{
public:
    RequestId Id() const noexcept { return CommandId; }
    const std::string& Operation() const noexcept { return OperationName; }
    const StateSnapshot& InitialSnapshot() const noexcept { return InitialState; }

    bool IsCancellationRequested() const noexcept;
    bool IsDeadlineExceeded() const;
    void AbortIfRequested() const;

    StateSnapshot Snapshot() const;
    void BeginSession(std::string sessionId, RunState initialState = RunState::Stopped);
    void TransitionRunState(RunState state);
    void PublishStop(RunState state);
    void MarkTainted(std::string reason);
    void RecordMutation();
    void AdvanceFrames(StateCounter count = 1);

private:
    friend class DebugCoordinator;

    CommandContext(
        DebugCoordinator& coordinator,
        RequestId id,
        std::string operation,
        StateSnapshot initialState,
        std::shared_ptr<detail::CancellationState> cancellation,
        std::optional<CoordinatorTimePoint> deadline);

    DebugCoordinator& Coordinator;
    RequestId CommandId;
    std::string OperationName;
    StateSnapshot InitialState;
    std::shared_ptr<detail::CancellationState> Cancellation;
    std::optional<CoordinatorTimePoint> Deadline;
};

using CommandHandler = std::function<CommandOutcome(CommandContext&)>;

struct Submission
{
    RequestId Id = 0;
    std::future<CommandResult> Completion;
};

enum class CancelDisposition
{
    CancelledBeforeExecution,
    CancellationRequested,
    RequestNotFound,
};

const char* CancelDispositionName(CancelDisposition disposition) noexcept;

struct CancelResult
{
    RequestId TargetId = 0;
    CancelDisposition Disposition = CancelDisposition::RequestNotFound;
    CommandError Error;

    explicit operator bool() const noexcept
    {
        return Disposition != CancelDisposition::RequestNotFound;
    }
};

struct DrainSummary
{
    std::size_t Processed = 0;
    std::size_t Succeeded = 0;
    std::size_t Failed = 0;
    std::size_t Remaining = 0;
};

struct DebugCoordinatorOptions
{
    std::string ServerInstanceId;
    std::string InitialSessionId;
    RunState InitialState = RunState::Stopped;
    std::size_t MaxQueueDepth = 256;

    // Immutable after construction and callable from any producer thread.
    // NotifyOwner is advisory (for example, signal an event or post a wakeup);
    // exceptions from it are swallowed because the queued command remains valid.
    std::function<CoordinatorTimePoint()> Now;
    std::function<void()> NotifyOwner;
};

// Thread-safe producer / single-owner actor queue. Producers call Submit() and
// Cancel() from transport threads. Exactly one emulator thread binds itself and
// calls Drain(). All state mutations are restricted to that owner thread, so
// precondition checking and command execution form a serializable sequence.
class DebugCoordinator final
{
public:
    explicit DebugCoordinator(DebugCoordinatorOptions options);
    ~DebugCoordinator();

    DebugCoordinator(const DebugCoordinator&) = delete;
    DebugCoordinator& operator=(const DebugCoordinator&) = delete;
    DebugCoordinator(DebugCoordinator&&) = delete;
    DebugCoordinator& operator=(DebugCoordinator&&) = delete;

    void BindOwnerThread();
    bool OwnerThreadBound() const;
    bool IsOwnerThread() const;

    Submission Submit(CommandSpec spec, CommandHandler handler);
    CancelResult Cancel(RequestId targetId);

    // Throws CoordinatorException when called before owner binding or from a
    // different thread. maxCommands == 0 is a valid no-op after that check.
    DrainSummary Drain(std::size_t maxCommands = static_cast<std::size_t>(-1));

    // Idempotent. Queued commands complete with coordinator_closed. A running
    // command receives a cooperative cancellation request.
    void Close();
    bool IsClosed() const;

    std::size_t QueueDepth() const;
    std::size_t MaxQueueDepth() const noexcept;
    RequestId LastRequestId() const;

    // Event sequence IDs are server-instance-wide and independent of request
    // IDs. They start at one and are safe to allocate from any thread.
    StateCounter AllocateEventSequence();

    StateSnapshot Snapshot() const;

    // Owner-thread-only state publication. StateVersion and StopId never reset
    // during a server instance; FrameNumber is reset by BeginSession().
    void BeginSession(std::string sessionId, RunState initialState = RunState::Stopped);
    void TransitionRunState(RunState state);
    void PublishStop(RunState state);
    void MarkTainted(std::string reason);
    void RecordMutation();
    void AdvanceFrames(StateCounter count = 1);

private:
    friend class CommandContext;

    struct Impl;
    std::unique_ptr<Impl> Internal;

    CoordinatorTimePoint Now() const;
    void EnsureOwnerThread() const;
};

} // namespace melonDS::MCP

#endif // MELONDS_MCP_DEBUGCOORDINATOR_H_
