/*
    Copyright 2026 melonDS-MCP contributors

    This file is part of melonDS.

    melonDS is free software: you can redistribute it and/or modify it under
    the terms of the GNU General Public License as published by the Free
    Software Foundation, either version 3 of the License, or (at your option)
    any later version.
*/

#ifndef MELONDS_MCP_LOCALBYTESTREAM_H_
#define MELONDS_MCP_LOCALBYTESTREAM_H_

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>

namespace melonDS::MCP
{

// A logical endpoint name, rather than an operating-system path. Restricting
// names prevents callers from escaping the private endpoint namespace.
struct LocalByteStreamConfig
{
    std::string Name;
};

enum class StreamError
{
    None,
    InvalidArgument,
    AlreadyStarted,
    NotStarted,
    AlreadyConnected,
    NotConnected,
    Stopped,
    Timeout,
    Disconnected,
    PermissionDenied,
    AddressInUse,
    ResourceExhausted,
    IoError,
};

const char* StreamErrorName(StreamError error);

struct StreamResult
{
    StreamError Error = StreamError::None;
    std::size_t BytesTransferred = 0;

    // GetLastError() on Windows or errno on POSIX. It is diagnostic only and
    // is zero for validation, lifecycle, timeout, and explicit-stop errors.
    std::uint32_t NativeError = 0;

    explicit operator bool() const { return Error == StreamError::None; }
};

// A single-client local byte-stream server. Windows uses a named pipe with a
// current-user-only DACL and PIPE_REJECT_REMOTE_CLIENTS. POSIX uses an AF_UNIX
// socket inside a mode-0700, current-user-owned runtime directory and verifies
// peer credentials after accept.
//
// Accept(), ReadExact(), and WriteExact() are serialized. Stop() is the one
// operation designed to run concurrently: it wakes or cancels an infinite
// wait promptly. Once stopped, an instance cannot be restarted.
class LocalByteStreamServer
{
public:
    using Timeout = std::chrono::milliseconds;
    static constexpr Timeout InfiniteTimeout = Timeout::max();

    explicit LocalByteStreamServer(LocalByteStreamConfig config);
    ~LocalByteStreamServer();

    LocalByteStreamServer(const LocalByteStreamServer&) = delete;
    LocalByteStreamServer& operator=(const LocalByteStreamServer&) = delete;
    LocalByteStreamServer(LocalByteStreamServer&&) = delete;
    LocalByteStreamServer& operator=(LocalByteStreamServer&&) = delete;

    // Creates the private listening endpoint. Endpoint() is non-empty after a
    // successful call. No client is accepted implicitly.
    StreamResult Start();

    // Waits for exactly one local client. A disconnected server can Accept()
    // another client, but concurrent/multiple connected clients are rejected.
    StreamResult Accept(Timeout timeout);

    // Transfer exactly length bytes or return an explicit short-transfer
    // result. Timeout is a total deadline for the entire call, not per chunk.
    StreamResult ReadExact(
        std::uint8_t* output,
        std::size_t length,
        Timeout timeout);
    StreamResult WriteExact(
        const std::uint8_t* input,
        std::size_t length,
        Timeout timeout);

    // Ends the current client session while keeping the listening endpoint
    // available for a future Accept(). It is a no-op when no client exists.
    void DisconnectClient();

    // Thread-safe, idempotent, and terminal. Wakes pending infinite waits.
    void Stop();

    bool IsStarted() const;
    bool HasClient() const;
    bool IsStopped() const;
    std::string Endpoint() const;

private:
    class Impl;
    std::unique_ptr<Impl> Implementation;
};

} // namespace melonDS::MCP

#endif // MELONDS_MCP_LOCALBYTESTREAM_H_
