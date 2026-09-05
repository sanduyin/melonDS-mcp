/*
    Copyright 2026 melonDS-MCP contributors

    This file is part of melonDS.

    melonDS is free software: you can redistribute it and/or modify it under
    the terms of the GNU General Public License as published by the Free
    Software Foundation, either version 3 of the License, or (at your option)
    any later version.
*/

#include "DebugCoordinator.h"

#include <algorithm>
#include <atomic>
#include <deque>
#include <limits>
#include <mutex>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

namespace melonDS::MCP
{

namespace detail
{

struct CancellationState
{
    std::atomic<bool> Requested {false};
};

enum class PendingPhase
{
    Queued,
    Executing,
    Completed,
};

struct PendingCommand
{
    CommandSpec Spec;
    CommandHandler Handler;
    std::shared_ptr<CancellationState> Cancellation =
        std::make_shared<CancellationState>();
    std::promise<CommandResult> Promise;
    PendingPhase Phase = PendingPhase::Queued;
};

} // namespace detail

namespace
{

bool IsKnownRunState(RunState state) noexcept
{
    switch (state)
    {
    case RunState::Running:
    case RunState::Paused:
    case RunState::Stopped:
    case RunState::Tainted:
        return true;
    }

    return false;
}

CommandError MakeError(CoordinatorErrorCode code, std::string message)
{
    CommandError error;
    error.Code = code;
    error.Message = std::move(message);
    return error;
}

StateCounter CheckedAdd(StateCounter value, StateCounter increment)
{
    if (increment > std::numeric_limits<StateCounter>::max() - value)
    {
        throw CoordinatorException(
            CoordinatorErrorCode::CounterExhausted,
            "a native bridge state counter was exhausted");
    }

    return value + increment;
}

std::optional<CommandError> CheckRequiredPreconditions(
    const CommandSpec& spec)
{
    if (spec.Required.ServerInstanceId &&
        !spec.Preconditions.ExpectedServerInstanceId.has_value())
    {
        return MakeError(
            CoordinatorErrorCode::MissingPrecondition,
            "expected_server_instance_id is required");
    }

    if (spec.Required.SessionId &&
        !spec.Preconditions.ExpectedSessionId.has_value())
    {
        return MakeError(
            CoordinatorErrorCode::MissingPrecondition,
            "expected_session_id is required");
    }

    if (spec.Required.StateVersion &&
        !spec.Preconditions.ExpectedStateVersion.has_value())
    {
        return MakeError(
            CoordinatorErrorCode::MissingPrecondition,
            "expected_state_version is required");
    }

    if (spec.Required.StopId &&
        !spec.Preconditions.ExpectedStopId.has_value())
    {
        return MakeError(
            CoordinatorErrorCode::MissingPrecondition,
            "expected_stop_id is required");
    }

    return std::nullopt;
}

std::optional<CommandError> CheckExpectedState(
    const CommandSpec& spec,
    const StateSnapshot& snapshot)
{
    const auto& expected = spec.Preconditions;

    if (expected.ExpectedServerInstanceId.has_value() &&
        *expected.ExpectedServerInstanceId != snapshot.ServerInstanceId)
    {
        return MakeError(
            CoordinatorErrorCode::StaleServerInstance,
            "server instance compare-and-swap precondition failed");
    }

    if (expected.ExpectedSessionId.has_value() &&
        *expected.ExpectedSessionId != snapshot.SessionId)
    {
        return MakeError(
            CoordinatorErrorCode::StaleSession,
            "session compare-and-swap precondition failed");
    }

    if (expected.ExpectedStateVersion.has_value() &&
        *expected.ExpectedStateVersion != snapshot.StateVersion)
    {
        return MakeError(
            CoordinatorErrorCode::StaleStateVersion,
            "state version compare-and-swap precondition failed");
    }

    if (expected.ExpectedStopId.has_value() &&
        *expected.ExpectedStopId != snapshot.StopId)
    {
        return MakeError(
            CoordinatorErrorCode::StaleStopId,
            "stop ID compare-and-swap precondition failed");
    }

    return std::nullopt;
}

CommandResult BuildResult(
    RequestId id,
    CommandOutcome outcome,
    StateSnapshot snapshot)
{
    CommandResult result;
    result.Id = id;
    result.Ok = outcome.Ok;
    result.Value = std::move(outcome.Value);
    result.Error = std::move(outcome.Error);
    result.Snapshot = std::move(snapshot);

    if (!result.Ok && result.Error.Code == CoordinatorErrorCode::None)
    {
        result.Error = MakeError(
            CoordinatorErrorCode::InternalError,
            "a failed command did not provide an error code");
    }
    else if (result.Ok)
    {
        result.Error = {};
    }

    return result;
}

void Fulfill(
    const std::shared_ptr<detail::PendingCommand>& pending,
    CommandOutcome outcome,
    StateSnapshot snapshot)
{
    pending->Promise.set_value(BuildResult(
        pending->Spec.Id,
        std::move(outcome),
        std::move(snapshot)));
}

class DrainGuard final
{
public:
    explicit DrainGuard(std::atomic<bool>& active)
        : Active(active)
    {
    }

    ~DrainGuard()
    {
        Active.store(false, std::memory_order_release);
    }

    DrainGuard(const DrainGuard&) = delete;
    DrainGuard& operator=(const DrainGuard&) = delete;

private:
    std::atomic<bool>& Active;
};

} // anonymous namespace

struct DebugCoordinator::Impl
{
    explicit Impl(DebugCoordinatorOptions options)
        : Options(std::move(options))
    {
        State.ServerInstanceId = Options.ServerInstanceId;
        State.SessionId = Options.InitialSessionId;
        State.StateVersion = 1;
        State.StopId = Options.InitialState == RunState::Running ? 0 : 1;
        State.FrameNumber = 0;
        State.State = Options.InitialState;
        if (Options.InitialState == RunState::Tainted)
            State.TaintReason = "coordinator was initialized as tainted";
    }

    DebugCoordinatorOptions Options;

    mutable std::mutex OwnerMutex;
    bool HasOwnerThread = false;
    std::thread::id OwnerThread;

    mutable std::mutex StateMutex;
    StateSnapshot State;

    mutable std::mutex QueueMutex;
    std::deque<std::shared_ptr<detail::PendingCommand>> Queue;
    std::unordered_map<RequestId, std::shared_ptr<detail::PendingCommand>> Outstanding;
    std::shared_ptr<detail::PendingCommand> Executing;
    bool Closed = false;
    RequestId LastRequest = 0;
    std::atomic<bool> DrainActive {false};
    std::atomic<StateCounter> EventSequence {0};
};

const char* RunStateName(RunState state) noexcept
{
    switch (state)
    {
    case RunState::Running:
        return "running";
    case RunState::Paused:
        return "paused";
    case RunState::Stopped:
        return "stopped";
    case RunState::Tainted:
        return "tainted";
    }

    return "unknown";
}

const char* CoordinatorErrorCodeName(CoordinatorErrorCode code) noexcept
{
    switch (code)
    {
    case CoordinatorErrorCode::None:
        return "none";
    case CoordinatorErrorCode::InvalidArgument:
        return "invalid_argument";
    case CoordinatorErrorCode::MissingPrecondition:
        return "missing_precondition";
    case CoordinatorErrorCode::StaleServerInstance:
        return "stale_server_instance";
    case CoordinatorErrorCode::StaleSession:
        return "stale_session";
    case CoordinatorErrorCode::StaleStateVersion:
        return "stale_state_version";
    case CoordinatorErrorCode::StaleStopId:
        return "stale_stop_id";
    case CoordinatorErrorCode::NonMonotonicRequestId:
        return "non_monotonic_request_id";
    case CoordinatorErrorCode::RequestIdExhausted:
        return "request_id_exhausted";
    case CoordinatorErrorCode::CounterExhausted:
        return "counter_exhausted";
    case CoordinatorErrorCode::QueueFull:
        return "queue_full";
    case CoordinatorErrorCode::DeadlineExceeded:
        return "deadline_exceeded";
    case CoordinatorErrorCode::Cancelled:
        return "cancelled";
    case CoordinatorErrorCode::RequestNotFound:
        return "request_not_found";
    case CoordinatorErrorCode::CoordinatorClosed:
        return "coordinator_closed";
    case CoordinatorErrorCode::OwnerThreadNotBound:
        return "owner_thread_not_bound";
    case CoordinatorErrorCode::WrongOwnerThread:
        return "wrong_owner_thread";
    case CoordinatorErrorCode::DrainAlreadyActive:
        return "drain_already_active";
    case CoordinatorErrorCode::InvalidStateTransition:
        return "invalid_state_transition";
    case CoordinatorErrorCode::HandlerFailure:
        return "handler_failure";
    case CoordinatorErrorCode::InternalError:
        return "internal_error";
    }

    return "internal_error";
}

CoordinatorException::CoordinatorException(
    CoordinatorErrorCode code,
    std::string message)
    : std::runtime_error(std::move(message)),
      ErrorCode(code)
{
}

CommandOutcome CommandOutcome::Success(std::any value)
{
    CommandOutcome outcome;
    outcome.Ok = true;
    outcome.Value = std::move(value);
    return outcome;
}

CommandOutcome CommandOutcome::Failure(
    CoordinatorErrorCode code,
    std::string message)
{
    CommandOutcome outcome;
    outcome.Ok = false;
    outcome.Error = MakeError(code, std::move(message));
    return outcome;
}

const char* CancelDispositionName(CancelDisposition disposition) noexcept
{
    switch (disposition)
    {
    case CancelDisposition::CancelledBeforeExecution:
        return "cancelled_before_execution";
    case CancelDisposition::CancellationRequested:
        return "cancellation_requested";
    case CancelDisposition::RequestNotFound:
        return "request_not_found";
    }

    return "request_not_found";
}

CommandContext::CommandContext(
    DebugCoordinator& coordinator,
    RequestId id,
    std::string operation,
    StateSnapshot initialState,
    std::shared_ptr<detail::CancellationState> cancellation,
    std::optional<CoordinatorTimePoint> deadline)
    : Coordinator(coordinator),
      CommandId(id),
      OperationName(std::move(operation)),
      InitialState(std::move(initialState)),
      Cancellation(std::move(cancellation)),
      Deadline(deadline)
{
}

bool CommandContext::IsCancellationRequested() const noexcept
{
    return Cancellation->Requested.load(std::memory_order_acquire);
}

bool CommandContext::IsDeadlineExceeded() const
{
    return Deadline.has_value() && Coordinator.Now() >= *Deadline;
}

void CommandContext::AbortIfRequested() const
{
    if (IsDeadlineExceeded())
    {
        throw CoordinatorException(
            CoordinatorErrorCode::DeadlineExceeded,
            "the command deadline was exceeded during execution");
    }

    if (IsCancellationRequested())
    {
        throw CoordinatorException(
            CoordinatorErrorCode::Cancelled,
            "the command was cancelled during execution");
    }
}

StateSnapshot CommandContext::Snapshot() const
{
    return Coordinator.Snapshot();
}

void CommandContext::BeginSession(std::string sessionId, RunState initialState)
{
    Coordinator.BeginSession(std::move(sessionId), initialState);
}

void CommandContext::TransitionRunState(RunState state)
{
    Coordinator.TransitionRunState(state);
}

void CommandContext::PublishStop(RunState state)
{
    Coordinator.PublishStop(state);
}

void CommandContext::MarkTainted(std::string reason)
{
    Coordinator.MarkTainted(std::move(reason));
}

void CommandContext::RecordMutation()
{
    Coordinator.RecordMutation();
}

void CommandContext::AdvanceFrames(StateCounter count)
{
    Coordinator.AdvanceFrames(count);
}

DebugCoordinator::DebugCoordinator(DebugCoordinatorOptions options)
{
    if (options.ServerInstanceId.empty())
    {
        throw CoordinatorException(
            CoordinatorErrorCode::InvalidArgument,
            "server instance ID must not be empty");
    }

    if (options.InitialSessionId.empty())
    {
        throw CoordinatorException(
            CoordinatorErrorCode::InvalidArgument,
            "initial session ID must not be empty");
    }

    if (!IsKnownRunState(options.InitialState))
    {
        throw CoordinatorException(
            CoordinatorErrorCode::InvalidArgument,
            "initial run state is invalid");
    }

    if (options.MaxQueueDepth == 0)
    {
        throw CoordinatorException(
            CoordinatorErrorCode::InvalidArgument,
            "maximum queue depth must be greater than zero");
    }

    if (!options.Now)
    {
        options.Now = [] {
            return CoordinatorClock::now();
        };
    }

    Internal = std::make_unique<Impl>(std::move(options));
}

DebugCoordinator::~DebugCoordinator()
{
    Close();
}

void DebugCoordinator::BindOwnerThread()
{
    std::lock_guard<std::mutex> lock(Internal->OwnerMutex);
    const auto currentThread = std::this_thread::get_id();

    if (!Internal->HasOwnerThread)
    {
        Internal->OwnerThread = currentThread;
        Internal->HasOwnerThread = true;
        return;
    }

    if (Internal->OwnerThread != currentThread)
    {
        throw CoordinatorException(
            CoordinatorErrorCode::WrongOwnerThread,
            "the coordinator is already bound to another owner thread");
    }
}

bool DebugCoordinator::OwnerThreadBound() const
{
    std::lock_guard<std::mutex> lock(Internal->OwnerMutex);
    return Internal->HasOwnerThread;
}

bool DebugCoordinator::IsOwnerThread() const
{
    std::lock_guard<std::mutex> lock(Internal->OwnerMutex);
    return Internal->HasOwnerThread &&
        Internal->OwnerThread == std::this_thread::get_id();
}

Submission DebugCoordinator::Submit(CommandSpec spec, CommandHandler handler)
{
    auto pending = std::make_shared<detail::PendingCommand>();
    pending->Spec = std::move(spec);
    pending->Handler = std::move(handler);

    Submission submission;
    submission.Completion = pending->Promise.get_future();

    std::optional<CommandError> immediateError;
    if (pending->Spec.Operation.empty())
    {
        immediateError = MakeError(
            CoordinatorErrorCode::InvalidArgument,
            "operation must not be empty");
    }
    else if (!pending->Handler)
    {
        immediateError = MakeError(
            CoordinatorErrorCode::InvalidArgument,
            "command handler must not be empty");
    }
    else
    {
        immediateError = CheckRequiredPreconditions(pending->Spec);
    }

    if (!immediateError.has_value() && pending->Spec.Deadline.has_value())
    {
        try
        {
            if (Now() >= *pending->Spec.Deadline)
            {
                immediateError = MakeError(
                    CoordinatorErrorCode::DeadlineExceeded,
                    "the command deadline had already expired at submission");
            }
        }
        catch (const std::exception& exception)
        {
            immediateError = MakeError(
                CoordinatorErrorCode::InternalError,
                std::string("the coordinator clock failed: ") + exception.what());
        }
        catch (...)
        {
            immediateError = MakeError(
                CoordinatorErrorCode::InternalError,
                "the coordinator clock failed");
        }
    }

    bool queued = false;
    {
        std::lock_guard<std::mutex> lock(Internal->QueueMutex);

        if (pending->Spec.Id == 0)
        {
            if (Internal->LastRequest == std::numeric_limits<RequestId>::max())
            {
                immediateError = MakeError(
                    CoordinatorErrorCode::RequestIdExhausted,
                    "the server-wide request ID space was exhausted");
            }
            else
            {
                Internal->LastRequest++;
                pending->Spec.Id = Internal->LastRequest;
            }
        }
        else if (pending->Spec.Id <= Internal->LastRequest)
        {
            immediateError = MakeError(
                CoordinatorErrorCode::NonMonotonicRequestId,
                "explicit request IDs must be strictly increasing");
        }
        else
        {
            Internal->LastRequest = pending->Spec.Id;
        }

        submission.Id = pending->Spec.Id;

        if (!immediateError.has_value() && Internal->Closed)
        {
            immediateError = MakeError(
                CoordinatorErrorCode::CoordinatorClosed,
                "the native bridge coordinator is closed");
        }

        if (!immediateError.has_value() &&
            Internal->Queue.size() >= Internal->Options.MaxQueueDepth)
        {
            immediateError = MakeError(
                CoordinatorErrorCode::QueueFull,
                "the native bridge command queue is full");
        }

        if (!immediateError.has_value())
        {
            Internal->Queue.push_back(pending);
            Internal->Outstanding.emplace(pending->Spec.Id, pending);
            queued = true;
        }
        else
        {
            pending->Phase = detail::PendingPhase::Completed;
        }
    }

    if (!queued)
    {
        const auto error = immediateError.value_or(MakeError(
            CoordinatorErrorCode::InternalError,
            "submission failed without an error"));
        Fulfill(
            pending,
            CommandOutcome::Failure(error.Code, error.Message),
            Snapshot());
    }
    else if (Internal->Options.NotifyOwner)
    {
        try
        {
            Internal->Options.NotifyOwner();
        }
        catch (...)
        {
            // Advisory wakeups must not invalidate an already queued command.
        }
    }

    return submission;
}

CancelResult DebugCoordinator::Cancel(RequestId targetId)
{
    CancelResult result;
    result.TargetId = targetId;

    std::shared_ptr<detail::PendingCommand> cancelled;
    bool notifyOwner = false;
    {
        std::lock_guard<std::mutex> lock(Internal->QueueMutex);
        const auto found = Internal->Outstanding.find(targetId);
        if (found == Internal->Outstanding.end())
        {
            result.Disposition = CancelDisposition::RequestNotFound;
            result.Error = MakeError(
                CoordinatorErrorCode::RequestNotFound,
                "the target request is not queued or executing");
            return result;
        }

        // Keep our own shared_ptr before erasing the map entry. A reference to
        // found->second would be invalid immediately after erase(found).
        const auto pending = found->second;
        pending->Cancellation->Requested.store(true, std::memory_order_release);

        if (pending->Phase == detail::PendingPhase::Queued)
        {
            const auto queued = std::find(
                Internal->Queue.begin(),
                Internal->Queue.end(),
                pending);
            if (queued != Internal->Queue.end())
                Internal->Queue.erase(queued);

            pending->Phase = detail::PendingPhase::Completed;
            Internal->Outstanding.erase(found);
            cancelled = pending;
            result.Disposition = CancelDisposition::CancelledBeforeExecution;
        }
        else
        {
            result.Disposition = CancelDisposition::CancellationRequested;
            notifyOwner = true;
        }
    }

    if (cancelled)
    {
        Fulfill(
            cancelled,
            CommandOutcome::Failure(
                CoordinatorErrorCode::Cancelled,
                "the command was cancelled before execution"),
            Snapshot());
    }

    if (notifyOwner && Internal->Options.NotifyOwner)
    {
        try
        {
            Internal->Options.NotifyOwner();
        }
        catch (...)
        {
            // Cancellation remains visible through CommandContext.
        }
    }

    return result;
}

DrainSummary DebugCoordinator::Drain(std::size_t maxCommands)
{
    EnsureOwnerThread();

    bool expected = false;
    if (!Internal->DrainActive.compare_exchange_strong(
            expected,
            true,
            std::memory_order_acq_rel,
            std::memory_order_acquire))
    {
        throw CoordinatorException(
            CoordinatorErrorCode::DrainAlreadyActive,
            "the command queue is already being drained");
    }
    DrainGuard drainGuard(Internal->DrainActive);

    DrainSummary summary;
    while (summary.Processed < maxCommands)
    {
        std::shared_ptr<detail::PendingCommand> pending;
        {
            std::lock_guard<std::mutex> lock(Internal->QueueMutex);
            if (Internal->Queue.empty())
                break;

            pending = Internal->Queue.front();
            Internal->Queue.pop_front();
            pending->Phase = detail::PendingPhase::Executing;
            Internal->Executing = pending;
        }

        CommandOutcome outcome;
        const auto initialSnapshot = Snapshot();

        if (pending->Cancellation->Requested.load(std::memory_order_acquire))
        {
            outcome = CommandOutcome::Failure(
                CoordinatorErrorCode::Cancelled,
                "the command was cancelled before execution");
        }
        else
        {
            bool deadlineExceeded = false;
            if (pending->Spec.Deadline.has_value())
            {
                try
                {
                    deadlineExceeded = Now() >= *pending->Spec.Deadline;
                }
                catch (const std::exception& exception)
                {
                    outcome = CommandOutcome::Failure(
                        CoordinatorErrorCode::InternalError,
                        std::string("the coordinator clock failed: ") +
                            exception.what());
                }
                catch (...)
                {
                    outcome = CommandOutcome::Failure(
                        CoordinatorErrorCode::InternalError,
                        "the coordinator clock failed");
                }
            }

            if (outcome.Ok && deadlineExceeded)
            {
                outcome = CommandOutcome::Failure(
                    CoordinatorErrorCode::DeadlineExceeded,
                    "the command deadline expired while it was queued");
            }

            if (outcome.Ok)
            {
                const auto preconditionError =
                    CheckExpectedState(pending->Spec, initialSnapshot);
                if (preconditionError.has_value())
                {
                    outcome = CommandOutcome::Failure(
                        preconditionError->Code,
                        preconditionError->Message);
                }
            }
        }

        if (outcome.Ok)
        {
            CommandContext context(
                *this,
                pending->Spec.Id,
                pending->Spec.Operation,
                initialSnapshot,
                pending->Cancellation,
                pending->Spec.Deadline);

            try
            {
                outcome = pending->Handler(context);
            }
            catch (const CoordinatorException& exception)
            {
                outcome = CommandOutcome::Failure(
                    exception.Code(),
                    exception.what());
            }
            catch (const std::exception& exception)
            {
                outcome = CommandOutcome::Failure(
                    CoordinatorErrorCode::HandlerFailure,
                    exception.what());
            }
            catch (...)
            {
                outcome = CommandOutcome::Failure(
                    CoordinatorErrorCode::HandlerFailure,
                    "the native command handler threw an unknown exception");
            }
        }

        auto result = BuildResult(
            pending->Spec.Id,
            std::move(outcome),
            Snapshot());
        const bool succeeded = result.Ok;

        {
            std::lock_guard<std::mutex> lock(Internal->QueueMutex);
            pending->Phase = detail::PendingPhase::Completed;
            Internal->Outstanding.erase(pending->Spec.Id);
            if (Internal->Executing == pending)
                Internal->Executing.reset();
        }

        pending->Promise.set_value(std::move(result));
        summary.Processed++;
        if (succeeded)
            summary.Succeeded++;
        else
            summary.Failed++;
    }

    summary.Remaining = QueueDepth();
    return summary;
}

void DebugCoordinator::Close()
{
    std::vector<std::shared_ptr<detail::PendingCommand>> rejected;
    bool notifyOwner = false;
    {
        std::lock_guard<std::mutex> lock(Internal->QueueMutex);
        if (Internal->Closed)
            return;

        Internal->Closed = true;
        rejected.reserve(Internal->Queue.size());
        while (!Internal->Queue.empty())
        {
            auto pending = Internal->Queue.front();
            Internal->Queue.pop_front();
            pending->Phase = detail::PendingPhase::Completed;
            Internal->Outstanding.erase(pending->Spec.Id);
            rejected.push_back(std::move(pending));
        }

        if (Internal->Executing)
        {
            Internal->Executing->Cancellation->Requested.store(
                true,
                std::memory_order_release);
            notifyOwner = true;
        }
    }

    for (const auto& pending : rejected)
    {
        Fulfill(
            pending,
            CommandOutcome::Failure(
                CoordinatorErrorCode::CoordinatorClosed,
                "the native bridge coordinator was closed before execution"),
            Snapshot());
    }

    if (notifyOwner && Internal->Options.NotifyOwner)
    {
        try
        {
            Internal->Options.NotifyOwner();
        }
        catch (...)
        {
            // Close is idempotent and cannot be undone by a wakeup failure.
        }
    }
}

bool DebugCoordinator::IsClosed() const
{
    std::lock_guard<std::mutex> lock(Internal->QueueMutex);
    return Internal->Closed;
}

std::size_t DebugCoordinator::QueueDepth() const
{
    std::lock_guard<std::mutex> lock(Internal->QueueMutex);
    return Internal->Queue.size();
}

std::size_t DebugCoordinator::MaxQueueDepth() const noexcept
{
    return Internal->Options.MaxQueueDepth;
}

RequestId DebugCoordinator::LastRequestId() const
{
    std::lock_guard<std::mutex> lock(Internal->QueueMutex);
    return Internal->LastRequest;
}

StateCounter DebugCoordinator::AllocateEventSequence()
{
    auto current = Internal->EventSequence.load(std::memory_order_relaxed);
    while (true)
    {
        if (current == std::numeric_limits<StateCounter>::max())
        {
            throw CoordinatorException(
                CoordinatorErrorCode::CounterExhausted,
                "the server-wide event sequence was exhausted");
        }

        if (Internal->EventSequence.compare_exchange_weak(
                current,
                current + 1,
                std::memory_order_relaxed,
                std::memory_order_relaxed))
        {
            return current + 1;
        }
    }
}

StateSnapshot DebugCoordinator::Snapshot() const
{
    std::lock_guard<std::mutex> lock(Internal->StateMutex);
    return Internal->State;
}

void DebugCoordinator::BeginSession(std::string sessionId, RunState initialState)
{
    EnsureOwnerThread();

    if (sessionId.empty())
    {
        throw CoordinatorException(
            CoordinatorErrorCode::InvalidArgument,
            "session ID must not be empty");
    }

    if (!IsKnownRunState(initialState) || initialState == RunState::Tainted)
    {
        throw CoordinatorException(
            CoordinatorErrorCode::InvalidArgument,
            "a new session must start in running, paused, or stopped state");
    }

    std::lock_guard<std::mutex> lock(Internal->StateMutex);
    if (sessionId == Internal->State.SessionId)
    {
        throw CoordinatorException(
            CoordinatorErrorCode::InvalidArgument,
            "a new session ID must differ from the current session ID");
    }

    const auto nextVersion = CheckedAdd(Internal->State.StateVersion, 1);
    const auto nextStopId = CheckedAdd(Internal->State.StopId, 1);
    Internal->State.SessionId = std::move(sessionId);
    Internal->State.StateVersion = nextVersion;
    Internal->State.StopId = nextStopId;
    Internal->State.FrameNumber = 0;
    Internal->State.State = initialState;
    Internal->State.TaintReason.clear();
}

void DebugCoordinator::TransitionRunState(RunState state)
{
    EnsureOwnerThread();

    if (!IsKnownRunState(state))
    {
        throw CoordinatorException(
            CoordinatorErrorCode::InvalidArgument,
            "run state is invalid");
    }

    if (state == RunState::Tainted)
    {
        throw CoordinatorException(
            CoordinatorErrorCode::InvalidStateTransition,
            "use MarkTainted() to publish a tainted state with a reason");
    }

    std::lock_guard<std::mutex> lock(Internal->StateMutex);
    if (Internal->State.State == RunState::Tainted)
    {
        throw CoordinatorException(
            CoordinatorErrorCode::InvalidStateTransition,
            "a tainted session can only be replaced with BeginSession()");
    }

    if (Internal->State.State == state)
        return;

    const auto nextVersion = CheckedAdd(Internal->State.StateVersion, 1);
    auto nextStopId = Internal->State.StopId;
    if (state != RunState::Running)
        nextStopId = CheckedAdd(nextStopId, 1);

    Internal->State.StateVersion = nextVersion;
    Internal->State.StopId = nextStopId;
    Internal->State.State = state;
    Internal->State.TaintReason.clear();
}

void DebugCoordinator::PublishStop(RunState state)
{
    EnsureOwnerThread();

    if (state != RunState::Paused && state != RunState::Stopped)
    {
        throw CoordinatorException(
            CoordinatorErrorCode::InvalidArgument,
            "PublishStop() accepts only paused or stopped state");
    }

    std::lock_guard<std::mutex> lock(Internal->StateMutex);
    if (Internal->State.State == RunState::Tainted)
    {
        throw CoordinatorException(
            CoordinatorErrorCode::InvalidStateTransition,
            "a tainted session can only be replaced with BeginSession()");
    }

    const auto nextVersion = CheckedAdd(Internal->State.StateVersion, 1);
    const auto nextStopId = CheckedAdd(Internal->State.StopId, 1);
    Internal->State.StateVersion = nextVersion;
    Internal->State.StopId = nextStopId;
    Internal->State.State = state;
    Internal->State.TaintReason.clear();
}

void DebugCoordinator::MarkTainted(std::string reason)
{
    EnsureOwnerThread();

    if (reason.empty())
    {
        throw CoordinatorException(
            CoordinatorErrorCode::InvalidArgument,
            "a tainted state requires a nonempty reason");
    }

    std::lock_guard<std::mutex> lock(Internal->StateMutex);
    const auto nextVersion = CheckedAdd(Internal->State.StateVersion, 1);
    const auto nextStopId = CheckedAdd(Internal->State.StopId, 1);
    Internal->State.StateVersion = nextVersion;
    Internal->State.StopId = nextStopId;
    Internal->State.State = RunState::Tainted;
    Internal->State.TaintReason = std::move(reason);
}

void DebugCoordinator::RecordMutation()
{
    EnsureOwnerThread();
    std::lock_guard<std::mutex> lock(Internal->StateMutex);
    Internal->State.StateVersion = CheckedAdd(
        Internal->State.StateVersion,
        1);
}

void DebugCoordinator::AdvanceFrames(StateCounter count)
{
    EnsureOwnerThread();

    if (count == 0)
    {
        throw CoordinatorException(
            CoordinatorErrorCode::InvalidArgument,
            "frame advance count must be greater than zero");
    }

    std::lock_guard<std::mutex> lock(Internal->StateMutex);
    const auto nextFrame = CheckedAdd(Internal->State.FrameNumber, count);
    const auto nextVersion = CheckedAdd(Internal->State.StateVersion, 1);
    Internal->State.FrameNumber = nextFrame;
    Internal->State.StateVersion = nextVersion;
}

CoordinatorTimePoint DebugCoordinator::Now() const
{
    return Internal->Options.Now();
}

void DebugCoordinator::EnsureOwnerThread() const
{
    std::lock_guard<std::mutex> lock(Internal->OwnerMutex);
    if (!Internal->HasOwnerThread)
    {
        throw CoordinatorException(
            CoordinatorErrorCode::OwnerThreadNotBound,
            "the coordinator owner thread has not been bound");
    }

    if (Internal->OwnerThread != std::this_thread::get_id())
    {
        throw CoordinatorException(
            CoordinatorErrorCode::WrongOwnerThread,
            "the operation must run on the bound coordinator owner thread");
    }
}

} // namespace melonDS::MCP
