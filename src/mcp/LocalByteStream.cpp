/*
    Copyright 2026 melonDS-MCP contributors

    This file is part of melonDS.

    melonDS is free software: you can redistribute it and/or modify it under
    the terms of the GNU General Public License as published by the Free
    Software Foundation, either version 3 of the License, or (at your option)
    any later version.
*/

#include "LocalByteStream.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <limits>
#include <mutex>
#include <utility>

#ifdef _WIN32

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <aclapi.h>

#include <vector>

#else

#include <cerrno>
#include <climits>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

#endif

namespace melonDS::MCP
{

namespace
{

bool IsValidEndpointName(const std::string& name)
{
    if (name.empty() || name.size() > 128)
        return false;

    for (const unsigned char character : name)
    {
        const bool valid =
            (character >= 'a' && character <= 'z') ||
            (character >= 'A' && character <= 'Z') ||
            (character >= '0' && character <= '9') ||
            character == '-' || character == '_' || character == '.';
        if (!valid)
            return false;
    }
    return true;
}

bool IsValidTimeout(LocalByteStreamServer::Timeout timeout)
{
    return timeout == LocalByteStreamServer::InfiniteTimeout ||
        timeout.count() >= 0;
}

using Clock = std::chrono::steady_clock;

class Deadline
{
public:
    explicit Deadline(LocalByteStreamServer::Timeout timeout)
        : Infinite(timeout == LocalByteStreamServer::InfiniteTimeout)
    {
        if (!Infinite)
        {
            const auto now = Clock::now();
            const auto maximumDelay = std::chrono::duration_cast<
                LocalByteStreamServer::Timeout>(Clock::time_point::max() - now);
            End = timeout >= maximumDelay ? Clock::time_point::max() :
                now + std::chrono::duration_cast<Clock::duration>(timeout);
        }
    }

#ifdef _WIN32
    DWORD RemainingMilliseconds() const
    {
        if (Infinite)
            return INFINITE;

        const auto now = Clock::now();
        if (now >= End)
            return 0;

        // Round up so a positive sub-millisecond remainder cannot become a
        // premature zero-time poll. Clamp to the finite Windows range.
        const auto remaining = End - now;
        auto milliseconds = std::chrono::duration_cast<
            std::chrono::milliseconds>(remaining);
        if (milliseconds < remaining)
            milliseconds += std::chrono::milliseconds(1);

        const auto maximum = static_cast<std::int64_t>(INFINITE - 1U);
        return static_cast<DWORD>(std::min(milliseconds.count(), maximum));
    }
#else
    int RemainingMilliseconds() const
    {
        if (Infinite)
            return -1;

        const auto now = Clock::now();
        if (now >= End)
            return 0;

        const auto remaining = End - now;
        auto milliseconds = std::chrono::duration_cast<
            std::chrono::milliseconds>(remaining);
        if (milliseconds < remaining)
            milliseconds += std::chrono::milliseconds(1);
        return static_cast<int>(std::min<std::int64_t>(
            milliseconds.count(), INT_MAX));
    }
#endif

private:
    bool Infinite = false;
    Clock::time_point End {};
};

#ifdef _WIN32

StreamError MapWindowsError(DWORD error)
{
    switch (error)
    {
    case ERROR_ACCESS_DENIED:
        return StreamError::PermissionDenied;
    case ERROR_PIPE_BUSY:
    case ERROR_ALREADY_EXISTS:
        return StreamError::AddressInUse;
    case ERROR_NOT_ENOUGH_MEMORY:
    case ERROR_OUTOFMEMORY:
    case ERROR_NO_SYSTEM_RESOURCES:
        return StreamError::ResourceExhausted;
    case ERROR_BROKEN_PIPE:
    case ERROR_NO_DATA:
    case ERROR_PIPE_NOT_CONNECTED:
        return StreamError::Disconnected;
    case ERROR_OPERATION_ABORTED:
        return StreamError::Stopped;
    default:
        return StreamError::IoError;
    }
}

class ScopedHandle
{
public:
    ScopedHandle() = default;
    explicit ScopedHandle(HANDLE handle) : Value(handle) {}
    ~ScopedHandle() { Reset(); }

    ScopedHandle(const ScopedHandle&) = delete;
    ScopedHandle& operator=(const ScopedHandle&) = delete;

    ScopedHandle(ScopedHandle&& other) noexcept
        : Value(other.Release())
    {
    }

    ScopedHandle& operator=(ScopedHandle&& other) noexcept
    {
        if (this != &other)
            Reset(other.Release());
        return *this;
    }

    HANDLE Get() const { return Value; }
    explicit operator bool() const
    {
        return Value != nullptr && Value != INVALID_HANDLE_VALUE;
    }

    HANDLE Release()
    {
        const HANDLE result = Value;
        Value = nullptr;
        return result;
    }

    void Reset(HANDLE handle = nullptr)
    {
        if (*this)
            CloseHandle(Value);
        Value = handle;
    }

private:
    HANDLE Value = nullptr;
};

class CurrentUserSecurity
{
public:
    StreamResult Initialize()
    {
        ScopedHandle token;
        HANDLE rawToken = nullptr;
        if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &rawToken))
        {
            const DWORD error = GetLastError();
            return {MapWindowsError(error), 0, error};
        }
        token.Reset(rawToken);

        DWORD tokenBytes = 0;
        if (GetTokenInformation(token.Get(), TokenUser, nullptr, 0, &tokenBytes) ||
            GetLastError() != ERROR_INSUFFICIENT_BUFFER)
        {
            const DWORD error = GetLastError();
            return {MapWindowsError(error), 0, error};
        }

        try
        {
            TokenBuffer.resize(tokenBytes);
        }
        catch (const std::bad_alloc&)
        {
            return {StreamError::ResourceExhausted, 0, ERROR_OUTOFMEMORY};
        }

        if (!GetTokenInformation(token.Get(), TokenUser,
                TokenBuffer.data(), tokenBytes, &tokenBytes))
        {
            const DWORD error = GetLastError();
            return {MapWindowsError(error), 0, error};
        }

        const auto* tokenUser = reinterpret_cast<const TOKEN_USER*>(
            TokenBuffer.data());
        EXPLICIT_ACCESSW access {};
        access.grfAccessPermissions = GENERIC_ALL;
        access.grfAccessMode = SET_ACCESS;
        access.grfInheritance = NO_INHERITANCE;
        access.Trustee.TrusteeForm = TRUSTEE_IS_SID;
        access.Trustee.TrusteeType = TRUSTEE_IS_USER;
        access.Trustee.ptstrName = static_cast<wchar_t*>(tokenUser->User.Sid);

        PACL rawAcl = nullptr;
        const DWORD aclError = SetEntriesInAclW(1, &access, nullptr, &rawAcl);
        if (aclError != ERROR_SUCCESS)
            return {MapWindowsError(aclError), 0, aclError};
        Acl = rawAcl;

        if (!InitializeSecurityDescriptor(&Descriptor,
                SECURITY_DESCRIPTOR_REVISION))
        {
            const DWORD error = GetLastError();
            return {MapWindowsError(error), 0, error};
        }
        if (!SetSecurityDescriptorDacl(&Descriptor, TRUE, Acl, FALSE))
        {
            const DWORD error = GetLastError();
            return {MapWindowsError(error), 0, error};
        }

        Attributes.nLength = sizeof(Attributes);
        Attributes.lpSecurityDescriptor = &Descriptor;
        Attributes.bInheritHandle = FALSE;
        return {};
    }

    ~CurrentUserSecurity()
    {
        if (Acl != nullptr)
            LocalFree(Acl);
    }

    SECURITY_ATTRIBUTES* Get() { return &Attributes; }

private:
    std::vector<std::uint8_t> TokenBuffer;
    PACL Acl = nullptr;
    SECURITY_DESCRIPTOR Descriptor {};
    SECURITY_ATTRIBUTES Attributes {};
};

#else

StreamError MapPosixError(int error)
{
    switch (error)
    {
    case EACCES:
    case EPERM:
        return StreamError::PermissionDenied;
    case EADDRINUSE:
        return StreamError::AddressInUse;
    case EMFILE:
    case ENFILE:
    case ENOBUFS:
    case ENOMEM:
        return StreamError::ResourceExhausted;
    case ECONNRESET:
    case EPIPE:
    case ENOTCONN:
        return StreamError::Disconnected;
    default:
        return StreamError::IoError;
    }
}

void CloseDescriptor(int& descriptor)
{
    if (descriptor >= 0)
        close(descriptor);
    descriptor = -1;
}

bool SetCloseOnExecAndNonBlocking(int descriptor)
{
    const int descriptorFlags = fcntl(descriptor, F_GETFD, 0);
    if (descriptorFlags < 0 ||
        fcntl(descriptor, F_SETFD, descriptorFlags | FD_CLOEXEC) < 0)
        return false;

    const int statusFlags = fcntl(descriptor, F_GETFL, 0);
    return statusFlags >= 0 &&
        fcntl(descriptor, F_SETFL, statusFlags | O_NONBLOCK) >= 0;
}

bool IsPrivateDirectory(const std::string& path, uid_t user)
{
    struct stat status {};
    if (lstat(path.c_str(), &status) != 0)
        return false;
    return S_ISDIR(status.st_mode) && status.st_uid == user &&
        (status.st_mode & 0077) == 0;
}

StreamResult EnsurePrivateDirectory(
    const std::string& path,
    uid_t user)
{
    if (mkdir(path.c_str(), 0700) == 0)
        return {};

    const int error = errno;
    if (error == EEXIST && IsPrivateDirectory(path, user))
        return {};
    return {MapPosixError(error), 0, static_cast<std::uint32_t>(error)};
}

bool VerifyPeerUser(int descriptor, uid_t expectedUser)
{
#if defined(__linux__)
    // Layout mandated by Linux SO_PEERCRED. Defining the small POD locally
    // avoids requiring _GNU_SOURCE merely to expose struct ucred.
    struct PeerCredentials
    {
        pid_t Process;
        uid_t User;
        gid_t Group;
    };
    PeerCredentials credentials {};
    socklen_t length = sizeof(credentials);
    return getsockopt(descriptor, SOL_SOCKET, SO_PEERCRED,
               &credentials, &length) == 0 &&
        length == sizeof(credentials) && credentials.User == expectedUser;
#elif defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || \
    defined(__NetBSD__)
    uid_t user = static_cast<uid_t>(-1);
    gid_t group = static_cast<gid_t>(-1);
    return getpeereid(descriptor, &user, &group) == 0 && user == expectedUser;
#else
    (void)descriptor;
    (void)expectedUser;
    return false;
#endif
}

#endif

} // anonymous namespace

const char* StreamErrorName(StreamError error)
{
    switch (error)
    {
    case StreamError::None: return "none";
    case StreamError::InvalidArgument: return "invalid_argument";
    case StreamError::AlreadyStarted: return "already_started";
    case StreamError::NotStarted: return "not_started";
    case StreamError::AlreadyConnected: return "already_connected";
    case StreamError::NotConnected: return "not_connected";
    case StreamError::Stopped: return "stopped";
    case StreamError::Timeout: return "timeout";
    case StreamError::Disconnected: return "disconnected";
    case StreamError::PermissionDenied: return "permission_denied";
    case StreamError::AddressInUse: return "address_in_use";
    case StreamError::ResourceExhausted: return "resource_exhausted";
    case StreamError::IoError: return "io_error";
    }
    return "unknown_error";
}

class LocalByteStreamServer::Impl
{
public:
    explicit Impl(LocalByteStreamConfig config)
        : Config(std::move(config))
    {
    }

    ~Impl()
    {
        Stop();
        std::lock_guard<std::mutex> operationLock(OperationMutex);
#ifdef _WIN32
        Pipe.Reset();
        StopEvent.Reset();
#else
        CloseDescriptor(Client);
        CloseDescriptor(Listener);
        CloseDescriptor(StopRead);
        CloseDescriptor(StopWrite);
        if (OwnsSocketPath && !SocketPath.empty())
            unlink(SocketPath.c_str());
#endif
    }

    StreamResult Start();
    StreamResult Accept(Timeout timeout);
    StreamResult ReadExact(std::uint8_t* output, std::size_t length,
        Timeout timeout);
    StreamResult WriteExact(const std::uint8_t* input, std::size_t length,
        Timeout timeout);
    void DisconnectClient();
    void Stop();

    bool IsStarted() const { return Started.load(); }
    bool HasClient() const { return Connected.load(); }
    bool IsStopped() const { return Stopping.load(); }
    std::string Endpoint() const
    {
        std::lock_guard<std::mutex> stateLock(StateMutex);
        return EndpointName;
    }

private:
#ifdef _WIN32
    enum class PendingResult
    {
        Complete,
        Timeout,
        Stopped,
        Error,
    };

    PendingResult WaitOverlapped(
        OVERLAPPED& operation,
        Deadline& deadline,
        DWORD& bytes,
        DWORD& error);
    StreamResult TransferWindows(
        bool write,
        std::uint8_t* buffer,
        std::size_t length,
        Timeout timeout);
#else
    StreamResult WaitPosix(int descriptor, short events, Deadline& deadline);
    StreamResult TransferPosix(
        bool write,
        std::uint8_t* buffer,
        std::size_t length,
        Timeout timeout);
#endif

    LocalByteStreamConfig Config;
    mutable std::mutex StateMutex;
    std::mutex OperationMutex;
    std::atomic<bool> Started {false};
    std::atomic<bool> Connected {false};
    std::atomic<bool> Stopping {false};
    std::string EndpointName;

#ifdef _WIN32
    ScopedHandle StopEvent;
    ScopedHandle Pipe;
#else
    int Listener = -1;
    int Client = -1;
    int StopRead = -1;
    int StopWrite = -1;
    std::string SocketPath;
    bool OwnsSocketPath = false;
#endif
};

#ifdef _WIN32

StreamResult LocalByteStreamServer::Impl::Start()
{
    std::lock_guard<std::mutex> operationLock(OperationMutex);
    if (Stopping.load())
        return {StreamError::Stopped, 0, 0};
    if (Started.load())
        return {StreamError::AlreadyStarted, 0, 0};
    if (!IsValidEndpointName(Config.Name))
        return {StreamError::InvalidArgument, 0, 0};

    CurrentUserSecurity security;
    StreamResult securityResult = security.Initialize();
    if (!securityResult)
        return securityResult;

    ScopedHandle stopEvent(CreateEventW(nullptr, TRUE, FALSE, nullptr));
    if (!stopEvent)
    {
        const DWORD error = GetLastError();
        return {MapWindowsError(error), 0, error};
    }

    const std::string endpoint = "\\\\.\\pipe\\melonDS-MCP-" + Config.Name;
    const std::wstring pipePath(endpoint.begin(), endpoint.end());
    ScopedHandle pipe(CreateNamedPipeW(
        pipePath.c_str(),
        PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED |
            FILE_FLAG_FIRST_PIPE_INSTANCE,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT |
            PIPE_REJECT_REMOTE_CLIENTS,
        1,
        64U * 1024U,
        64U * 1024U,
        0,
        security.Get()));
    if (!pipe)
    {
        const DWORD error = GetLastError();
        if (error == ERROR_ACCESS_DENIED)
            return {StreamError::AddressInUse, 0, error};
        return {MapWindowsError(error), 0, error};
    }

    {
        std::lock_guard<std::mutex> stateLock(StateMutex);
        EndpointName = endpoint;
    }
    StopEvent = std::move(stopEvent);
    Pipe = std::move(pipe);
    Started.store(true);
    return {};
}

LocalByteStreamServer::Impl::PendingResult
LocalByteStreamServer::Impl::WaitOverlapped(
    OVERLAPPED& operation,
    Deadline& deadline,
    DWORD& bytes,
    DWORD& error)
{
    const HANDLE events[] {operation.hEvent, StopEvent.Get()};
    const DWORD waitResult = WaitForMultipleObjects(
        2, events, FALSE, deadline.RemainingMilliseconds());

    if (waitResult == WAIT_OBJECT_0)
    {
        if (GetOverlappedResult(Pipe.Get(), &operation, &bytes, FALSE))
            return PendingResult::Complete;
        error = GetLastError();
        if (Stopping.load() && error == ERROR_OPERATION_ABORTED)
            return PendingResult::Stopped;
        return PendingResult::Error;
    }

    const bool stopped = waitResult == WAIT_OBJECT_0 + 1 || Stopping.load();
    if (waitResult != WAIT_TIMEOUT && !stopped)
        error = GetLastError();

    // OVERLAPPED lives on our stack. Always cancel and reap the operation
    // before returning so the kernel can no longer reference it.
    if (!CancelIoEx(Pipe.Get(), &operation))
    {
        const DWORD cancelError = GetLastError();
        if (cancelError != ERROR_NOT_FOUND)
            error = cancelError;
    }
    DWORD completedBytes = 0;
    if (GetOverlappedResult(Pipe.Get(), &operation, &completedBytes, TRUE))
    {
        // Completion can race a zero-time wait. Report the completed I/O
        // instead of claiming a timeout after bytes or a connection committed.
        bytes = completedBytes;
        return PendingResult::Complete;
    }
    else
    {
        const DWORD completionError = GetLastError();
        if (completionError != ERROR_OPERATION_ABORTED && error == 0)
            error = completionError;
    }

    if (stopped)
        return PendingResult::Stopped;
    if (waitResult == WAIT_TIMEOUT)
        return PendingResult::Timeout;
    return PendingResult::Error;
}

StreamResult LocalByteStreamServer::Impl::Accept(Timeout timeout)
{
    if (!IsValidTimeout(timeout))
        return {StreamError::InvalidArgument, 0, 0};

    std::lock_guard<std::mutex> operationLock(OperationMutex);
    if (Stopping.load())
        return {StreamError::Stopped, 0, 0};
    if (!Started.load())
        return {StreamError::NotStarted, 0, 0};
    if (Connected.load())
        return {StreamError::AlreadyConnected, 0, 0};

    ScopedHandle event(CreateEventW(nullptr, TRUE, FALSE, nullptr));
    if (!event)
    {
        const DWORD error = GetLastError();
        return {MapWindowsError(error), 0, error};
    }
    OVERLAPPED operation {};
    operation.hEvent = event.Get();

    if (ConnectNamedPipe(Pipe.Get(), &operation))
    {
        Connected.store(true);
        return {};
    }

    DWORD error = GetLastError();
    if (error == ERROR_PIPE_CONNECTED)
    {
        Connected.store(true);
        return {};
    }
    if (error != ERROR_IO_PENDING)
        return {MapWindowsError(error), 0, error};

    Deadline deadline(timeout);
    DWORD bytes = 0;
    switch (WaitOverlapped(operation, deadline, bytes, error))
    {
    case PendingResult::Complete:
        if (Stopping.load())
            return {StreamError::Stopped, 0, 0};
        Connected.store(true);
        return {};
    case PendingResult::Timeout:
        return {StreamError::Timeout, 0, 0};
    case PendingResult::Stopped:
        return {StreamError::Stopped, 0, 0};
    case PendingResult::Error:
        return {MapWindowsError(error), 0, error};
    }
    return {StreamError::IoError, 0, 0};
}

StreamResult LocalByteStreamServer::Impl::TransferWindows(
    bool write,
    std::uint8_t* buffer,
    std::size_t length,
    Timeout timeout)
{
    if (!IsValidTimeout(timeout) || (buffer == nullptr && length != 0))
        return {StreamError::InvalidArgument, 0, 0};
    if (Stopping.load())
        return {StreamError::Stopped, 0, 0};
    if (!Started.load())
        return {StreamError::NotStarted, 0, 0};
    if (!Connected.load())
        return {StreamError::NotConnected, 0, 0};
    if (length == 0)
        return {};

    Deadline deadline(timeout);
    std::size_t transferred = 0;
    while (transferred < length)
    {
        if (Stopping.load())
            return {StreamError::Stopped, transferred, 0};

        ScopedHandle event(CreateEventW(nullptr, TRUE, FALSE, nullptr));
        if (!event)
        {
            const DWORD error = GetLastError();
            return {MapWindowsError(error), transferred, error};
        }
        OVERLAPPED operation {};
        operation.hEvent = event.Get();

        const auto remaining = length - transferred;
        const DWORD chunk = static_cast<DWORD>(std::min<std::size_t>(
            remaining, std::numeric_limits<DWORD>::max()));
        BOOL started = FALSE;
        if (write)
        {
            started = WriteFile(Pipe.Get(), buffer + transferred, chunk,
                nullptr, &operation);
        }
        else
        {
            started = ReadFile(Pipe.Get(), buffer + transferred, chunk,
                nullptr, &operation);
        }

        DWORD bytes = 0;
        DWORD error = ERROR_SUCCESS;
        if (started)
        {
            if (!GetOverlappedResult(Pipe.Get(), &operation, &bytes, TRUE))
                error = GetLastError();
        }
        else
        {
            error = GetLastError();
            if (error == ERROR_IO_PENDING)
            {
                switch (WaitOverlapped(operation, deadline, bytes, error))
                {
                case PendingResult::Complete:
                    error = ERROR_SUCCESS;
                    break;
                case PendingResult::Timeout:
                    return {StreamError::Timeout, transferred, 0};
                case PendingResult::Stopped:
                    return {StreamError::Stopped, transferred, 0};
                case PendingResult::Error:
                    break;
                }
            }
        }

        if (error != ERROR_SUCCESS)
        {
            const StreamError mapped = MapWindowsError(error);
            if (mapped == StreamError::Disconnected)
                Connected.store(false);
            return {mapped, transferred, error};
        }
        if (bytes == 0)
        {
            Connected.store(false);
            return {StreamError::Disconnected, transferred, 0};
        }
        transferred += bytes;
    }

    return {StreamError::None, transferred, 0};
}

StreamResult LocalByteStreamServer::Impl::ReadExact(
    std::uint8_t* output,
    std::size_t length,
    Timeout timeout)
{
    std::lock_guard<std::mutex> operationLock(OperationMutex);
    return TransferWindows(false, output, length, timeout);
}

StreamResult LocalByteStreamServer::Impl::WriteExact(
    const std::uint8_t* input,
    std::size_t length,
    Timeout timeout)
{
    std::lock_guard<std::mutex> operationLock(OperationMutex);
    return TransferWindows(true, const_cast<std::uint8_t*>(input), length,
        timeout);
}

void LocalByteStreamServer::Impl::DisconnectClient()
{
    std::lock_guard<std::mutex> operationLock(OperationMutex);
    if (!Started.load() || !Connected.exchange(false))
        return;
    DisconnectNamedPipe(Pipe.Get());
}

void LocalByteStreamServer::Impl::Stop()
{
    if (Stopping.exchange(true))
        return;
    if (StopEvent)
        SetEvent(StopEvent.Get());
    if (Pipe)
        CancelIoEx(Pipe.Get(), nullptr);
}

#else

StreamResult LocalByteStreamServer::Impl::Start()
{
    std::lock_guard<std::mutex> operationLock(OperationMutex);
    if (Stopping.load())
        return {StreamError::Stopped, 0, 0};
    if (Started.load())
        return {StreamError::AlreadyStarted, 0, 0};
    if (!IsValidEndpointName(Config.Name))
        return {StreamError::InvalidArgument, 0, 0};

    const uid_t user = geteuid();
    std::string directory;
    const char* runtimeDirectory = std::getenv("XDG_RUNTIME_DIR");
    if (runtimeDirectory != nullptr && runtimeDirectory[0] == '/' &&
        IsPrivateDirectory(runtimeDirectory, user))
    {
        directory = runtimeDirectory;
    }
    else
    {
        directory = "/tmp/melonds-mcp-" +
            std::to_string(static_cast<unsigned long long>(user));
        const StreamResult directoryResult = EnsurePrivateDirectory(
            directory, user);
        if (!directoryResult)
            return directoryResult;
    }

    SocketPath = directory + "/" + Config.Name + ".sock";
    if (SocketPath.size() >= sizeof(sockaddr_un::sun_path))
        return {StreamError::InvalidArgument, 0, 0};

    int stopDescriptors[2] {-1, -1};
    if (pipe(stopDescriptors) != 0)
    {
        const int error = errno;
        return {MapPosixError(error), 0, static_cast<std::uint32_t>(error)};
    }
    StopRead = stopDescriptors[0];
    StopWrite = stopDescriptors[1];
    if (!SetCloseOnExecAndNonBlocking(StopRead) ||
        !SetCloseOnExecAndNonBlocking(StopWrite))
    {
        const int error = errno;
        CloseDescriptor(StopRead);
        CloseDescriptor(StopWrite);
        return {MapPosixError(error), 0, static_cast<std::uint32_t>(error)};
    }

    Listener = socket(AF_UNIX, SOCK_STREAM, 0);
    if (Listener < 0)
    {
        const int error = errno;
        CloseDescriptor(StopRead);
        CloseDescriptor(StopWrite);
        return {MapPosixError(error), 0, static_cast<std::uint32_t>(error)};
    }
    if (!SetCloseOnExecAndNonBlocking(Listener))
    {
        const int error = errno;
        CloseDescriptor(Listener);
        CloseDescriptor(StopRead);
        CloseDescriptor(StopWrite);
        return {MapPosixError(error), 0, static_cast<std::uint32_t>(error)};
    }

    sockaddr_un address {};
    address.sun_family = AF_UNIX;
    std::memcpy(address.sun_path, SocketPath.c_str(), SocketPath.size() + 1);
    if (bind(Listener, reinterpret_cast<const sockaddr*>(&address),
            sizeof(address)) != 0)
    {
        const int error = errno;
        CloseDescriptor(Listener);
        CloseDescriptor(StopRead);
        CloseDescriptor(StopWrite);
        return {MapPosixError(error), 0, static_cast<std::uint32_t>(error)};
    }
    OwnsSocketPath = true;
    if (chmod(SocketPath.c_str(), 0600) != 0 || listen(Listener, 1) != 0)
    {
        const int error = errno;
        CloseDescriptor(Listener);
        CloseDescriptor(StopRead);
        CloseDescriptor(StopWrite);
        unlink(SocketPath.c_str());
        OwnsSocketPath = false;
        return {MapPosixError(error), 0, static_cast<std::uint32_t>(error)};
    }

    {
        std::lock_guard<std::mutex> stateLock(StateMutex);
        EndpointName = SocketPath;
    }
    Started.store(true);
    return {};
}

StreamResult LocalByteStreamServer::Impl::WaitPosix(
    int descriptor,
    short events,
    Deadline& deadline)
{
    pollfd descriptors[2] {
        {descriptor, events, 0},
        {StopRead, POLLIN, 0},
    };

    while (true)
    {
        const int result = poll(descriptors, 2, deadline.RemainingMilliseconds());
        if (result > 0)
        {
            if ((descriptors[1].revents & POLLIN) != 0 || Stopping.load())
                return {StreamError::Stopped, 0, 0};
            if ((descriptors[0].revents & events) != 0)
                return {};
            if ((descriptors[0].revents & (POLLERR | POLLHUP | POLLNVAL)) != 0)
                return {StreamError::Disconnected, 0, 0};
            continue;
        }
        if (result == 0)
            return {StreamError::Timeout, 0, 0};
        if (errno == EINTR)
            continue;
        const int error = errno;
        return {MapPosixError(error), 0, static_cast<std::uint32_t>(error)};
    }
}

StreamResult LocalByteStreamServer::Impl::Accept(Timeout timeout)
{
    if (!IsValidTimeout(timeout))
        return {StreamError::InvalidArgument, 0, 0};

    std::lock_guard<std::mutex> operationLock(OperationMutex);
    if (Stopping.load())
        return {StreamError::Stopped, 0, 0};
    if (!Started.load())
        return {StreamError::NotStarted, 0, 0};
    if (Connected.load())
        return {StreamError::AlreadyConnected, 0, 0};

    Deadline deadline(timeout);
    while (true)
    {
        StreamResult wait = WaitPosix(Listener, POLLIN, deadline);
        if (!wait)
            return wait;

        const int client = accept(Listener, nullptr, nullptr);
        if (client < 0)
        {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
                continue;
            const int error = errno;
            return {MapPosixError(error), 0,
                static_cast<std::uint32_t>(error)};
        }

        if (!SetCloseOnExecAndNonBlocking(client))
        {
            const int error = errno;
            close(client);
            return {MapPosixError(error), 0,
                static_cast<std::uint32_t>(error)};
        }
        if (!VerifyPeerUser(client, geteuid()))
        {
            close(client);
            return {StreamError::PermissionDenied, 0, 0};
        }

        Client = client;
        Connected.store(true);
        return {};
    }
}

StreamResult LocalByteStreamServer::Impl::TransferPosix(
    bool write,
    std::uint8_t* buffer,
    std::size_t length,
    Timeout timeout)
{
    if (!IsValidTimeout(timeout) || (buffer == nullptr && length != 0))
        return {StreamError::InvalidArgument, 0, 0};
    if (Stopping.load())
        return {StreamError::Stopped, 0, 0};
    if (!Started.load())
        return {StreamError::NotStarted, 0, 0};
    if (!Connected.load())
        return {StreamError::NotConnected, 0, 0};
    if (length == 0)
        return {};

    Deadline deadline(timeout);
    std::size_t transferred = 0;
    while (transferred < length)
    {
        StreamResult wait = WaitPosix(Client, write ? POLLOUT : POLLIN, deadline);
        if (!wait)
        {
            wait.BytesTransferred = transferred;
            if (wait.Error == StreamError::Disconnected)
                Connected.store(false);
            return wait;
        }

        const std::size_t remaining = std::min<std::size_t>(
            length - transferred,
            static_cast<std::size_t>(std::numeric_limits<ssize_t>::max()));
        ssize_t count = 0;
        if (write)
        {
#ifdef MSG_NOSIGNAL
            count = send(Client, buffer + transferred, remaining, MSG_NOSIGNAL);
#else
            count = send(Client, buffer + transferred, remaining, 0);
#endif
        }
        else
        {
            count = recv(Client, buffer + transferred, remaining, 0);
        }

        if (count > 0)
        {
            transferred += static_cast<std::size_t>(count);
            continue;
        }
        if (count == 0)
        {
            Connected.store(false);
            return {StreamError::Disconnected, transferred, 0};
        }
        if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
            continue;

        const int error = errno;
        const StreamError mapped = MapPosixError(error);
        if (mapped == StreamError::Disconnected)
            Connected.store(false);
        return {mapped, transferred, static_cast<std::uint32_t>(error)};
    }

    return {StreamError::None, transferred, 0};
}

StreamResult LocalByteStreamServer::Impl::ReadExact(
    std::uint8_t* output,
    std::size_t length,
    Timeout timeout)
{
    std::lock_guard<std::mutex> operationLock(OperationMutex);
    return TransferPosix(false, output, length, timeout);
}

StreamResult LocalByteStreamServer::Impl::WriteExact(
    const std::uint8_t* input,
    std::size_t length,
    Timeout timeout)
{
    std::lock_guard<std::mutex> operationLock(OperationMutex);
    return TransferPosix(true, const_cast<std::uint8_t*>(input), length,
        timeout);
}

void LocalByteStreamServer::Impl::DisconnectClient()
{
    std::lock_guard<std::mutex> operationLock(OperationMutex);
    Connected.store(false);
    CloseDescriptor(Client);
}

void LocalByteStreamServer::Impl::Stop()
{
    if (Stopping.exchange(true))
        return;

    if (StopWrite >= 0)
    {
        const std::uint8_t byte = 1;
        const ssize_t ignored = write(StopWrite, &byte, sizeof(byte));
        (void)ignored;
    }
}

#endif

LocalByteStreamServer::LocalByteStreamServer(LocalByteStreamConfig config)
    : Implementation(std::make_unique<Impl>(std::move(config)))
{
}

LocalByteStreamServer::~LocalByteStreamServer() = default;

StreamResult LocalByteStreamServer::Start()
{
    return Implementation->Start();
}

StreamResult LocalByteStreamServer::Accept(Timeout timeout)
{
    return Implementation->Accept(timeout);
}

StreamResult LocalByteStreamServer::ReadExact(
    std::uint8_t* output,
    std::size_t length,
    Timeout timeout)
{
    return Implementation->ReadExact(output, length, timeout);
}

StreamResult LocalByteStreamServer::WriteExact(
    const std::uint8_t* input,
    std::size_t length,
    Timeout timeout)
{
    return Implementation->WriteExact(input, length, timeout);
}

void LocalByteStreamServer::DisconnectClient()
{
    Implementation->DisconnectClient();
}

void LocalByteStreamServer::Stop()
{
    Implementation->Stop();
}

bool LocalByteStreamServer::IsStarted() const
{
    return Implementation->IsStarted();
}

bool LocalByteStreamServer::HasClient() const
{
    return Implementation->HasClient();
}

bool LocalByteStreamServer::IsStopped() const
{
    return Implementation->IsStopped();
}

std::string LocalByteStreamServer::Endpoint() const
{
    return Implementation->Endpoint();
}

} // namespace melonDS::MCP
