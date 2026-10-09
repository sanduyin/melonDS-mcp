#include "DebugCoordinator.h"

#include <algorithm>
#include <any>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <future>
#include <iostream>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace
{

using melonDS::MCP::CancelDisposition;
using melonDS::MCP::CommandContext;
using melonDS::MCP::CommandOutcome;
using melonDS::MCP::CommandResult;
using melonDS::MCP::CommandSpec;
using melonDS::MCP::CoordinatorErrorCode;
using melonDS::MCP::CoordinatorException;
using melonDS::MCP::CoordinatorTimePoint;
using melonDS::MCP::DebugCoordinator;
using melonDS::MCP::DebugCoordinatorOptions;
using melonDS::MCP::RequestId;
using melonDS::MCP::RunState;
using melonDS::MCP::StateCounter;
using melonDS::MCP::Submission;

int FailureCount = 0;

#define CHECK(condition) Check(static_cast<bool>(condition), #condition, __FILE__, __LINE__)

void Check(bool condition, const char* expression, const char* file, int line)
{
    if (condition)
        return;

    std::cerr << file << ':' << line << ": check failed: " << expression << '\n';
    FailureCount++;
}

DebugCoordinatorOptions Options(
    std::size_t maxQueueDepth = 256,
    std::string server = "server-A",
    std::string session = "session-A")
{
    DebugCoordinatorOptions options;
    options.ServerInstanceId = std::move(server);
    options.InitialSessionId = std::move(session);
    options.InitialState = RunState::Stopped;
    options.MaxQueueDepth = maxQueueDepth;
    return options;
}

CommandSpec Spec(std::string operation)
{
    CommandSpec spec;
    spec.Operation = std::move(operation);
    return spec;
}

CommandOutcome EmptySuccess(CommandContext&)
{
    return CommandOutcome::Success();
}

template<typename Function>
void ExpectExceptionCode(Function&& function, CoordinatorErrorCode expected)
{
    try
    {
        function();
        CHECK(false);
    }
    catch (const CoordinatorException& exception)
    {
        CHECK(exception.Code() == expected);
    }
    catch (...)
    {
        CHECK(false);
    }
}

CommandResult GetReady(std::future<CommandResult>& future)
{
    CHECK(future.wait_for(std::chrono::seconds(0)) == std::future_status::ready);
    return future.get();
}

void TestStateLifecycleAndOwnerThread()
{
    DebugCoordinator coordinator(Options());
    auto initial = coordinator.Snapshot();
    CHECK(initial.ServerInstanceId == "server-A");
    CHECK(initial.SessionId == "session-A");
    CHECK(initial.StateVersion == 1);
    CHECK(initial.StopId == 1);
    CHECK(initial.FrameNumber == 0);
    CHECK(initial.State == RunState::Stopped);
    CHECK(initial.TaintReason.empty());

    ExpectExceptionCode(
        [&coordinator] { coordinator.RecordMutation(); },
        CoordinatorErrorCode::OwnerThreadNotBound);

    coordinator.BindOwnerThread();
    coordinator.BindOwnerThread();
    CHECK(coordinator.OwnerThreadBound());
    CHECK(coordinator.IsOwnerThread());

    coordinator.TransitionRunState(RunState::Running);
    coordinator.AdvanceFrames(3);
    coordinator.PublishStop(RunState::Paused);
    coordinator.RecordMutation();

    auto paused = coordinator.Snapshot();
    CHECK(paused.State == RunState::Paused);
    CHECK(paused.StateVersion == 5);
    CHECK(paused.StopId == 2);
    CHECK(paused.FrameNumber == 3);

    coordinator.MarkTainted("unsafe native callback failed");
    auto tainted = coordinator.Snapshot();
    CHECK(tainted.State == RunState::Tainted);
    CHECK(tainted.StateVersion == 6);
    CHECK(tainted.StopId == 3);
    CHECK(tainted.TaintReason == "unsafe native callback failed");

    ExpectExceptionCode(
        [&coordinator] { coordinator.TransitionRunState(RunState::Running); },
        CoordinatorErrorCode::InvalidStateTransition);

    coordinator.BeginSession("session-B", RunState::Stopped);
    auto replacement = coordinator.Snapshot();
    CHECK(replacement.SessionId == "session-B");
    CHECK(replacement.StateVersion == 7);
    CHECK(replacement.StopId == 4);
    CHECK(replacement.FrameNumber == 0);
    CHECK(replacement.State == RunState::Stopped);
    CHECK(replacement.TaintReason.empty());

    std::atomic<int> otherThreadError {-1};
    std::thread wrongOwner([&] {
        try
        {
            static_cast<void>(coordinator.Drain());
        }
        catch (const CoordinatorException& exception)
        {
            otherThreadError.store(
                static_cast<int>(exception.Code()),
                std::memory_order_release);
        }
    });
    wrongOwner.join();
    CHECK(otherThreadError.load(std::memory_order_acquire) ==
        static_cast<int>(CoordinatorErrorCode::WrongOwnerThread));
}

void TestCompareAndSwapAtExecutionTime()
{
    DebugCoordinator coordinator(Options());
    coordinator.BindOwnerThread();
    const auto initial = coordinator.Snapshot();

    auto exact = Spec("memory.write");
    exact.Required.ServerInstanceId = true;
    exact.Required.SessionId = true;
    exact.Required.StateVersion = true;
    exact.Required.StopId = true;
    exact.Preconditions.ExpectedServerInstanceId = initial.ServerInstanceId;
    exact.Preconditions.ExpectedSessionId = initial.SessionId;
    exact.Preconditions.ExpectedStateVersion = initial.StateVersion;
    exact.Preconditions.ExpectedStopId = initial.StopId;

    auto accepted = coordinator.Submit(
        std::move(exact),
        [](CommandContext& context) {
            CHECK(context.InitialSnapshot().StateVersion == 1);
            context.RecordMutation();
            return CommandOutcome::Success(std::string("written"));
        });
    // This command is accepted while version 1 is current, but must be
    // rejected after the preceding queued mutation advances the version.
    bool staleHandlerRan = false;
    auto stale = Spec("register.write");
    stale.Preconditions.ExpectedStateVersion = initial.StateVersion;
    auto staleSubmission = coordinator.Submit(
        std::move(stale),
        [&staleHandlerRan](CommandContext&) {
            staleHandlerRan = true;
            return CommandOutcome::Success();
        });

    const auto firstDrain = coordinator.Drain();
    CHECK(firstDrain.Processed == 2);
    CHECK(firstDrain.Succeeded == 1);
    CHECK(firstDrain.Failed == 1);
    auto acceptedResult = GetReady(accepted.Completion);
    CHECK(acceptedResult.Ok);
    CHECK(std::any_cast<std::string>(acceptedResult.Value) == "written");
    CHECK(acceptedResult.Snapshot.StateVersion == 2);

    auto staleResult = GetReady(staleSubmission.Completion);
    CHECK(!staleResult.Ok);
    CHECK(staleResult.Error.Code == CoordinatorErrorCode::StaleStateVersion);
    CHECK(!staleHandlerRan);

    auto missing = Spec("breakpoint.set");
    missing.Required.SessionId = true;
    auto missingSubmission = coordinator.Submit(std::move(missing), EmptySuccess);
    CHECK(coordinator.QueueDepth() == 0);
    auto missingResult = GetReady(missingSubmission.Completion);
    CHECK(!missingResult.Ok);
    CHECK(missingResult.Error.Code == CoordinatorErrorCode::MissingPrecondition);

    struct ExpectedFailure
    {
        CommandSpec SpecValue;
        CoordinatorErrorCode Error;
    };

    std::vector<ExpectedFailure> failures;
    auto server = Spec("status.server");
    server.Preconditions.ExpectedServerInstanceId = "wrong-server";
    failures.push_back({std::move(server), CoordinatorErrorCode::StaleServerInstance});

    auto session = Spec("status.session");
    session.Preconditions.ExpectedSessionId = "wrong-session";
    failures.push_back({std::move(session), CoordinatorErrorCode::StaleSession});

    auto stop = Spec("status.stop");
    stop.Preconditions.ExpectedStopId = initial.StopId + 10;
    failures.push_back({std::move(stop), CoordinatorErrorCode::StaleStopId});

    for (auto& failure : failures)
    {
        auto submission = coordinator.Submit(std::move(failure.SpecValue), EmptySuccess);
        coordinator.Drain();
        auto result = GetReady(submission.Completion);
        CHECK(!result.Ok);
        CHECK(result.Error.Code == failure.Error);
    }
}

void TestRequestIdsQueueBoundsAndQueuedCancellation()
{
    DebugCoordinator coordinator(Options(2));
    coordinator.BindOwnerThread();
    std::vector<RequestId> executionOrder;

    auto first = coordinator.Submit(
        Spec("first"),
        [&executionOrder](CommandContext& context) {
            executionOrder.push_back(context.Id());
            return CommandOutcome::Success();
        });
    CHECK(first.Id == 1);

    auto explicitSpec = Spec("explicit-five");
    explicitSpec.Id = 5;
    auto fifth = coordinator.Submit(
        std::move(explicitSpec),
        [&executionOrder](CommandContext& context) {
            executionOrder.push_back(context.Id());
            return CommandOutcome::Success();
        });
    CHECK(fifth.Id == 5);
    CHECK(coordinator.QueueDepth() == 2);

    auto oldSpec = Spec("old-four");
    oldSpec.Id = 4;
    auto old = coordinator.Submit(std::move(oldSpec), EmptySuccess);
    auto oldResult = GetReady(old.Completion);
    CHECK(old.Id == 4);
    CHECK(oldResult.Error.Code == CoordinatorErrorCode::NonMonotonicRequestId);

    auto fullSpec = Spec("full-six");
    fullSpec.Id = 6;
    auto full = coordinator.Submit(std::move(fullSpec), EmptySuccess);
    auto fullResult = GetReady(full.Completion);
    CHECK(fullResult.Error.Code == CoordinatorErrorCode::QueueFull);
    CHECK(coordinator.LastRequestId() == 6);

    const auto cancel = coordinator.Cancel(first.Id);
    CHECK(cancel.Disposition == CancelDisposition::CancelledBeforeExecution);
    auto cancelledResult = GetReady(first.Completion);
    CHECK(cancelledResult.Error.Code == CoordinatorErrorCode::Cancelled);
    CHECK(coordinator.QueueDepth() == 1);

    auto seventhSpec = Spec("seventh");
    seventhSpec.Id = 7;
    auto seventh = coordinator.Submit(
        std::move(seventhSpec),
        [&executionOrder](CommandContext& context) {
            executionOrder.push_back(context.Id());
            return CommandOutcome::Success();
        });

    const auto summary = coordinator.Drain();
    CHECK(summary.Processed == 2);
    CHECK(summary.Succeeded == 2);
    CHECK(summary.Remaining == 0);
    CHECK(executionOrder.size() == 2);
    if (executionOrder.size() == 2)
    {
        CHECK(executionOrder[0] == 5);
        CHECK(executionOrder[1] == 7);
    }
    CHECK(GetReady(fifth.Completion).Ok);
    CHECK(GetReady(seventh.Completion).Ok);

    const auto missingCancel = coordinator.Cancel(999);
    CHECK(!missingCancel);
    CHECK(missingCancel.Error.Code == CoordinatorErrorCode::RequestNotFound);
}

void TestDeterministicDeadlines()
{
    std::atomic<std::int64_t> nowMilliseconds {0};
    auto options = Options();
    options.Now = [&nowMilliseconds] {
        return CoordinatorTimePoint(
            std::chrono::milliseconds(nowMilliseconds.load(std::memory_order_acquire)));
    };
    DebugCoordinator coordinator(std::move(options));
    coordinator.BindOwnerThread();

    bool queuedHandlerRan = false;
    auto queuedSpec = Spec("deadline.queued");
    queuedSpec.Deadline = CoordinatorTimePoint(std::chrono::milliseconds(10));
    auto queued = coordinator.Submit(
        std::move(queuedSpec),
        [&queuedHandlerRan](CommandContext&) {
            queuedHandlerRan = true;
            return CommandOutcome::Success();
        });
    nowMilliseconds.store(10, std::memory_order_release);
    coordinator.Drain();
    auto queuedResult = GetReady(queued.Completion);
    CHECK(queuedResult.Error.Code == CoordinatorErrorCode::DeadlineExceeded);
    CHECK(!queuedHandlerRan);

    auto expiredSpec = Spec("deadline.expired");
    expiredSpec.Deadline = CoordinatorTimePoint(std::chrono::milliseconds(5));
    auto expired = coordinator.Submit(std::move(expiredSpec), EmptySuccess);
    auto expiredResult = GetReady(expired.Completion);
    CHECK(expiredResult.Error.Code == CoordinatorErrorCode::DeadlineExceeded);
    CHECK(coordinator.QueueDepth() == 0);

    auto duringSpec = Spec("deadline.during");
    duringSpec.Deadline = CoordinatorTimePoint(std::chrono::milliseconds(20));
    auto during = coordinator.Submit(
        std::move(duringSpec),
        [&nowMilliseconds](CommandContext& context) {
            nowMilliseconds.store(21, std::memory_order_release);
            context.AbortIfRequested();
            return CommandOutcome::Success();
        });
    coordinator.Drain();
    auto duringResult = GetReady(during.Completion);
    CHECK(duringResult.Error.Code == CoordinatorErrorCode::DeadlineExceeded);
}

void TestExecutingCancellationIsCooperative()
{
    DebugCoordinator coordinator(Options());
    coordinator.BindOwnerThread();

    std::mutex mutex;
    std::condition_variable condition;
    bool handlerStarted = false;
    bool cancellationSent = false;

    auto submission = coordinator.Submit(
        Spec("long-running"),
        [&](CommandContext& context) {
            {
                std::lock_guard<std::mutex> lock(mutex);
                handlerStarted = true;
            }
            condition.notify_all();

            std::unique_lock<std::mutex> lock(mutex);
            condition.wait(lock, [&cancellationSent] { return cancellationSent; });
            lock.unlock();

            context.AbortIfRequested();
            return CommandOutcome::Success();
        });

    CancelDisposition disposition = CancelDisposition::RequestNotFound;
    std::thread canceller([&] {
        std::unique_lock<std::mutex> lock(mutex);
        condition.wait(lock, [&handlerStarted] { return handlerStarted; });
        lock.unlock();

        disposition = coordinator.Cancel(submission.Id).Disposition;
        {
            std::lock_guard<std::mutex> completedLock(mutex);
            cancellationSent = true;
        }
        condition.notify_all();
    });

    const auto summary = coordinator.Drain();
    canceller.join();
    CHECK(disposition == CancelDisposition::CancellationRequested);
    CHECK(summary.Processed == 1);
    CHECK(summary.Failed == 1);
    auto result = GetReady(submission.Completion);
    CHECK(result.Error.Code == CoordinatorErrorCode::Cancelled);
}

void TestCloseAndHandlerFailures()
{
    DebugCoordinator coordinator(Options());
    coordinator.BindOwnerThread();

    auto exception = coordinator.Submit(
        Spec("handler.exception"),
        [](CommandContext&) -> CommandOutcome {
            throw std::runtime_error("deliberate handler failure");
        });
    auto explicitFailure = coordinator.Submit(
        Spec("handler.failure"),
        [](CommandContext&) {
            return CommandOutcome::Failure(
                CoordinatorErrorCode::InvalidArgument,
                "bad native argument");
        });
    auto reentrant = coordinator.Submit(
        Spec("handler.reentrant-drain"),
        [&coordinator](CommandContext&) {
            static_cast<void>(coordinator.Drain(1));
            return CommandOutcome::Success();
        });

    coordinator.Drain();
    CHECK(GetReady(exception.Completion).Error.Code ==
        CoordinatorErrorCode::HandlerFailure);
    CHECK(GetReady(explicitFailure.Completion).Error.Code ==
        CoordinatorErrorCode::InvalidArgument);
    CHECK(GetReady(reentrant.Completion).Error.Code ==
        CoordinatorErrorCode::DrainAlreadyActive);

    auto queuedOne = coordinator.Submit(Spec("close.one"), EmptySuccess);
    auto queuedTwo = coordinator.Submit(Spec("close.two"), EmptySuccess);
    coordinator.Close();
    coordinator.Close();
    CHECK(coordinator.IsClosed());
    CHECK(coordinator.QueueDepth() == 0);
    CHECK(GetReady(queuedOne.Completion).Error.Code ==
        CoordinatorErrorCode::CoordinatorClosed);
    CHECK(GetReady(queuedTwo.Completion).Error.Code ==
        CoordinatorErrorCode::CoordinatorClosed);

    auto afterClose = coordinator.Submit(Spec("close.after"), EmptySuccess);
    CHECK(GetReady(afterClose.Completion).Error.Code ==
        CoordinatorErrorCode::CoordinatorClosed);
}

void TestCloseWhileExecuting()
{
    DebugCoordinator coordinator(Options());
    coordinator.BindOwnerThread();

    std::mutex mutex;
    std::condition_variable condition;
    bool handlerStarted = false;
    bool closeReturned = false;

    auto executing = coordinator.Submit(
        Spec("close.executing"),
        [&](CommandContext& context) {
            {
                std::lock_guard<std::mutex> lock(mutex);
                handlerStarted = true;
            }
            condition.notify_all();

            std::unique_lock<std::mutex> lock(mutex);
            condition.wait(lock, [&closeReturned] { return closeReturned; });
            lock.unlock();
            context.AbortIfRequested();
            return CommandOutcome::Success();
        });
    auto queued = coordinator.Submit(Spec("close.queued"), EmptySuccess);

    std::thread closer([&] {
        std::unique_lock<std::mutex> lock(mutex);
        condition.wait(lock, [&handlerStarted] { return handlerStarted; });
        lock.unlock();

        coordinator.Close();
        {
            std::lock_guard<std::mutex> completedLock(mutex);
            closeReturned = true;
        }
        condition.notify_all();
    });

    const auto summary = coordinator.Drain();
    closer.join();
    CHECK(summary.Processed == 1);
    CHECK(summary.Failed == 1);
    CHECK(GetReady(executing.Completion).Error.Code ==
        CoordinatorErrorCode::Cancelled);
    CHECK(GetReady(queued.Completion).Error.Code ==
        CoordinatorErrorCode::CoordinatorClosed);
}

void TestConcurrentIdAndEventAllocation()
{
    constexpr std::size_t ThreadCount = 16;
    auto options = Options(ThreadCount);
    std::atomic<int> wakeups {0};
    options.NotifyOwner = [&wakeups] {
        wakeups.fetch_add(1, std::memory_order_relaxed);
    };
    DebugCoordinator coordinator(std::move(options));

    std::mutex mutex;
    std::vector<Submission> submissions;
    submissions.reserve(ThreadCount);
    std::vector<std::thread> producers;
    producers.reserve(ThreadCount);
    for (std::size_t index = 0; index < ThreadCount; index++)
    {
        producers.emplace_back([&] {
            auto submission = coordinator.Submit(Spec("concurrent"), EmptySuccess);
            std::lock_guard<std::mutex> lock(mutex);
            submissions.push_back(std::move(submission));
        });
    }
    for (auto& producer : producers)
        producer.join();

    std::vector<RequestId> ids;
    ids.reserve(submissions.size());
    for (const auto& submission : submissions)
        ids.push_back(submission.Id);
    std::sort(ids.begin(), ids.end());
    CHECK(ids.size() == ThreadCount);
    for (std::size_t index = 0; index < ids.size(); index++)
        CHECK(ids[index] == static_cast<RequestId>(index + 1));
    CHECK(wakeups.load(std::memory_order_relaxed) ==
        static_cast<int>(ThreadCount));

    std::vector<StateCounter> eventIds;
    eventIds.reserve(ThreadCount);
    producers.clear();
    for (std::size_t index = 0; index < ThreadCount; index++)
    {
        producers.emplace_back([&] {
            const auto eventId = coordinator.AllocateEventSequence();
            std::lock_guard<std::mutex> lock(mutex);
            eventIds.push_back(eventId);
        });
    }
    for (auto& producer : producers)
        producer.join();
    std::sort(eventIds.begin(), eventIds.end());
    for (std::size_t index = 0; index < eventIds.size(); index++)
        CHECK(eventIds[index] == static_cast<StateCounter>(index + 1));

    coordinator.Close();
    for (auto& submission : submissions)
    {
        CHECK(GetReady(submission.Completion).Error.Code ==
            CoordinatorErrorCode::CoordinatorClosed);
    }
}

void TestRequestIdExhaustionAndNames()
{
    DebugCoordinator coordinator(Options(1));
    auto maximum = Spec("maximum-id");
    maximum.Id = std::numeric_limits<RequestId>::max();
    auto accepted = coordinator.Submit(std::move(maximum), EmptySuccess);
    CHECK(accepted.Id == std::numeric_limits<RequestId>::max());

    auto exhausted = coordinator.Submit(Spec("exhausted"), EmptySuccess);
    CHECK(exhausted.Id == 0);
    CHECK(GetReady(exhausted.Completion).Error.Code ==
        CoordinatorErrorCode::RequestIdExhausted);

    CHECK(std::string(melonDS::MCP::RunStateName(RunState::Paused)) == "paused");
    CHECK(std::string(melonDS::MCP::CoordinatorErrorCodeName(
        CoordinatorErrorCode::StaleStopId)) == "stale_stop_id");
    CHECK(std::string(melonDS::MCP::CancelDispositionName(
        CancelDisposition::CancellationRequested)) == "cancellation_requested");

    coordinator.Close();
    CHECK(GetReady(accepted.Completion).Error.Code ==
        CoordinatorErrorCode::CoordinatorClosed);
}

} // anonymous namespace

int main()
{
    std::cout << "TestStateLifecycleAndOwnerThread\n" << std::flush;
    TestStateLifecycleAndOwnerThread();
    std::cout << "TestCompareAndSwapAtExecutionTime\n" << std::flush;
    TestCompareAndSwapAtExecutionTime();
    std::cout << "TestRequestIdsQueueBoundsAndQueuedCancellation\n" << std::flush;
    TestRequestIdsQueueBoundsAndQueuedCancellation();
    std::cout << "TestDeterministicDeadlines\n" << std::flush;
    TestDeterministicDeadlines();
    std::cout << "TestExecutingCancellationIsCooperative\n" << std::flush;
    TestExecutingCancellationIsCooperative();
    std::cout << "TestCloseAndHandlerFailures\n" << std::flush;
    TestCloseAndHandlerFailures();
    std::cout << "TestCloseWhileExecuting\n" << std::flush;
    TestCloseWhileExecuting();
    std::cout << "TestConcurrentIdAndEventAllocation\n" << std::flush;
    TestConcurrentIdAndEventAllocation();
    std::cout << "TestRequestIdExhaustionAndNames\n" << std::flush;
    TestRequestIdExhaustionAndNames();

    if (FailureCount != 0)
    {
        std::cerr << FailureCount << " DebugCoordinator test(s) failed\n";
        return EXIT_FAILURE;
    }

    std::cout << "DebugCoordinator tests passed\n";
    return EXIT_SUCCESS;
}
