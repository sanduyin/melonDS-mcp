#include "FrameCodec.h"
#include "LocalByteStream.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <cerrno>
#include <cstring>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#endif

namespace
{

using namespace std::chrono_literals;
using melonDS::MCP::EncodeFrame;
using melonDS::MCP::Frame;
using melonDS::MCP::FrameParser;
using melonDS::MCP::LocalByteStreamConfig;
using melonDS::MCP::LocalByteStreamServer;
using melonDS::MCP::MessageType;
using melonDS::MCP::ParseStatus;
using melonDS::MCP::StreamError;

int FailureCount = 0;

#define CHECK(condition) Check(static_cast<bool>(condition), #condition, __FILE__, __LINE__)

void Check(bool condition, const char* expression, const char* file, int line)
{
    if (condition)
        return;
    std::cerr << file << ':' << line << ": check failed: " << expression << '\n';
    FailureCount++;
}

std::string UniqueName(const char* suffix)
{
#ifdef _WIN32
    const auto process = static_cast<unsigned long long>(GetCurrentProcessId());
#else
    const auto process = static_cast<unsigned long long>(getpid());
#endif
    const auto ticks = static_cast<unsigned long long>(
        std::chrono::steady_clock::now().time_since_epoch().count());
    return "test-" + std::to_string(process) + "-" +
        std::to_string(ticks) + "-" + suffix;
}

class TestClient
{
public:
    ~TestClient() { Close(); }

    bool Connect(const std::string& endpoint)
    {
#ifdef _WIN32
        const std::wstring path(endpoint.begin(), endpoint.end());
        const auto deadline = std::chrono::steady_clock::now() + 2s;
        while (std::chrono::steady_clock::now() < deadline)
        {
            Handle = CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE,
                0, nullptr, OPEN_EXISTING, 0, nullptr);
            if (Handle != INVALID_HANDLE_VALUE)
                return true;

            const DWORD error = GetLastError();
            if (error != ERROR_PIPE_BUSY && error != ERROR_FILE_NOT_FOUND)
                return false;
            WaitNamedPipeW(path.c_str(), 20);
        }
        return false;
#else
        Descriptor = socket(AF_UNIX, SOCK_STREAM, 0);
        if (Descriptor < 0)
            return false;

        sockaddr_un address {};
        address.sun_family = AF_UNIX;
        if (endpoint.size() >= sizeof(address.sun_path))
            return false;
        std::memcpy(address.sun_path, endpoint.c_str(), endpoint.size() + 1);
        if (connect(Descriptor, reinterpret_cast<const sockaddr*>(&address),
                sizeof(address)) == 0)
            return true;
        return false;
#endif
    }

    bool ReadExact(std::uint8_t* output, std::size_t length)
    {
        std::size_t transferred = 0;
        while (transferred < length)
        {
#ifdef _WIN32
            const DWORD chunk = static_cast<DWORD>(std::min<std::size_t>(
                length - transferred, static_cast<std::size_t>(MAXDWORD)));
            DWORD bytes = 0;
            if (!ReadFile(Handle, output + transferred, chunk, &bytes, nullptr))
                return false;
            const auto count = static_cast<std::size_t>(bytes);
#else
            const ssize_t result = recv(Descriptor, output + transferred,
                length - transferred, 0);
            if (result < 0 && errno == EINTR)
                continue;
            if (result <= 0)
                return false;
            const auto count = static_cast<std::size_t>(result);
#endif
            if (count == 0)
                return false;
            transferred += count;
        }
        return true;
    }

    bool WriteAll(const std::uint8_t* input, std::size_t length)
    {
        std::size_t transferred = 0;
        while (transferred < length)
        {
#ifdef _WIN32
            const DWORD chunk = static_cast<DWORD>(std::min<std::size_t>(
                length - transferred, static_cast<std::size_t>(MAXDWORD)));
            DWORD bytes = 0;
            if (!WriteFile(Handle, input + transferred, chunk, &bytes, nullptr))
                return false;
            const auto count = static_cast<std::size_t>(bytes);
#else
            const ssize_t result = send(Descriptor, input + transferred,
                length - transferred, 0);
            if (result < 0 && errno == EINTR)
                continue;
            if (result <= 0)
                return false;
            const auto count = static_cast<std::size_t>(result);
#endif
            if (count == 0)
                return false;
            transferred += count;
        }
        return true;
    }

    bool WriteFragments(const std::vector<std::uint8_t>& bytes)
    {
        const std::size_t first = std::min<std::size_t>(3, bytes.size());
        const std::size_t second = std::min<std::size_t>(17, bytes.size());
        return WriteAll(bytes.data(), first) &&
            WriteAll(bytes.data() + first, second - first) &&
            WriteAll(bytes.data() + second, bytes.size() - second);
    }

    void Close()
    {
#ifdef _WIN32
        if (Handle != INVALID_HANDLE_VALUE)
            CloseHandle(Handle);
        Handle = INVALID_HANDLE_VALUE;
#else
        if (Descriptor >= 0)
            close(Descriptor);
        Descriptor = -1;
#endif
    }

private:
#ifdef _WIN32
    HANDLE Handle = INVALID_HANDLE_VALUE;
#else
    int Descriptor = -1;
#endif
};

std::vector<std::uint8_t> Encode(const Frame& frame)
{
    std::vector<std::uint8_t> result;
    CHECK(EncodeFrame(frame, result));
    return result;
}

void TestLifecycleAndValidation()
{
    LocalByteStreamServer invalid({"../escape"});
    CHECK(invalid.Start().Error == StreamError::InvalidArgument);

    const std::string endpointName = UniqueName("lifecycle");
    LocalByteStreamServer server({endpointName});
    CHECK(server.Accept(0ms).Error == StreamError::NotStarted);
    CHECK(server.Start());
    CHECK(server.IsStarted());
    CHECK(!server.Endpoint().empty());
#ifdef _WIN32
    CHECK(server.Endpoint().find("\\\\.\\pipe\\melonDS-MCP-") == 0);
#else
    CHECK(server.Endpoint().find(".sock") != std::string::npos);
#endif
    CHECK(server.Start().Error == StreamError::AlreadyStarted);
    CHECK(server.Accept(25ms).Error == StreamError::Timeout);

    // A cancelled timed accept must leave the listening instance reusable.
    std::atomic<bool> connected {false};
    std::thread client([&]() {
        TestClient connection;
        connected.store(connection.Connect(server.Endpoint()));
    });
    CHECK(server.Accept(2s));
    client.join();
    CHECK(connected.load());
    CHECK(server.HasClient());
    server.DisconnectClient();
    CHECK(!server.HasClient());

    // The endpoint namespace is single-instance and reports the collision.
    LocalByteStreamServer collision({endpointName});
    CHECK(collision.Start().Error == StreamError::AddressInUse);
    server.Stop();
    CHECK(server.IsStopped());
    CHECK(server.Accept(1s).Error == StreamError::Stopped);
    CHECK(server.ReadExact(nullptr, 0, 1s).Error == StreamError::Stopped);
}

void TestFrameCodecRoundTrip()
{
    LocalByteStreamServer server({UniqueName("roundtrip")});
    CHECK(server.Start());

    Frame request;
    request.Type = MessageType::Request;
    request.RequestId = 0x1122334455667788ULL;
    request.Json = u8"{\"method\":\"frame.capture\",\"screen\":\"\u4E0A\"}";
    request.Binary.resize(32U * 1024U);
    for (std::size_t i = 0; i < request.Binary.size(); i++)
        request.Binary[i] = static_cast<std::uint8_t>(i * 37U);
    const auto requestBytes = Encode(request);

    Frame response;
    response.Type = MessageType::Response;
    response.RequestId = request.RequestId;
    response.Json = "{\"ok\":true}";
    response.Binary = {0x89, 0x50, 0x4E, 0x47};
    const auto responseBytes = Encode(response);

    std::atomic<bool> clientConnected {false};
    std::atomic<bool> clientWrote {false};
    std::atomic<bool> clientRead {false};
    std::vector<std::uint8_t> clientResponse(responseBytes.size());
    std::thread client([&]() {
        TestClient connection;
        clientConnected.store(connection.Connect(server.Endpoint()));
        if (!clientConnected.load())
            return;
        clientWrote.store(connection.WriteFragments(requestBytes));
        if (!clientWrote.load())
            return;
        clientRead.store(connection.ReadExact(
            clientResponse.data(), clientResponse.size()));
    });

    CHECK(server.Accept(2s));
    CHECK(server.HasClient());
    CHECK(server.Accept(0ms).Error == StreamError::AlreadyConnected);

    std::vector<std::uint8_t> received(requestBytes.size());
    const auto read = server.ReadExact(received.data(), received.size(), 2s);
    CHECK(read);
    CHECK(read.BytesTransferred == received.size());

    FrameParser parser;
    std::vector<Frame> parsed;
    const auto parse = parser.Feed(received, parsed);
    CHECK(parse.Status == ParseStatus::FramesReady);
    CHECK(parse.FramesProduced == 1);
    CHECK(parsed.size() == 1);
    if (parsed.size() == 1)
    {
        CHECK(parsed[0].RequestId == request.RequestId);
        CHECK(parsed[0].Json == request.Json);
        CHECK(parsed[0].Binary == request.Binary);
    }

    const auto write = server.WriteExact(
        responseBytes.data(), responseBytes.size(), 2s);
    CHECK(write);
    CHECK(write.BytesTransferred == responseBytes.size());
    client.join();

    CHECK(clientConnected.load());
    CHECK(clientWrote.load());
    CHECK(clientRead.load());
    CHECK(clientResponse == responseBytes);

    FrameParser responseParser;
    std::vector<Frame> parsedResponse;
    const auto responseParse = responseParser.Feed(
        clientResponse, parsedResponse);
    CHECK(responseParse.Status == ParseStatus::FramesReady);
    CHECK(parsedResponse.size() == 1);
    if (parsedResponse.size() == 1)
    {
        CHECK(parsedResponse[0].Type == MessageType::Response);
        CHECK(parsedResponse[0].RequestId == request.RequestId);
        CHECK(parsedResponse[0].Binary == response.Binary);
    }
}

void TestReadTimeoutIsExplicit()
{
    LocalByteStreamServer server({UniqueName("read-timeout")});
    CHECK(server.Start());

    std::atomic<bool> connected {false};
    std::atomic<bool> mayClose {false};
    std::thread client([&]() {
        TestClient connection;
        connected.store(connection.Connect(server.Endpoint()));
        while (!mayClose.load())
            std::this_thread::sleep_for(1ms);
    });

    CHECK(server.Accept(2s));
    std::uint8_t byte = 0;
    const auto result = server.ReadExact(&byte, 1, 35ms);
    CHECK(result.Error == StreamError::Timeout);
    CHECK(result.BytesTransferred == 0);
    CHECK(result.NativeError == 0);
    CHECK(connected.load());

    mayClose.store(true);
    client.join();
}

void TestStopUnblocksAccept()
{
    LocalByteStreamServer server({UniqueName("stop-accept")});
    CHECK(server.Start());

    melonDS::MCP::StreamResult result;
    std::thread waiter([&]() {
        result = server.Accept(LocalByteStreamServer::InfiniteTimeout);
    });
    std::this_thread::sleep_for(25ms);
    const auto start = std::chrono::steady_clock::now();
    server.Stop();
    waiter.join();
    const auto elapsed = std::chrono::steady_clock::now() - start;

    CHECK(result.Error == StreamError::Stopped);
    CHECK(elapsed < 1s);
}

void TestStopUnblocksRead()
{
    LocalByteStreamServer server({UniqueName("stop-read")});
    CHECK(server.Start());

    std::atomic<bool> connected {false};
    std::atomic<bool> mayClose {false};
    std::thread client([&]() {
        TestClient connection;
        connected.store(connection.Connect(server.Endpoint()));
        while (!mayClose.load())
            std::this_thread::sleep_for(1ms);
    });
    CHECK(server.Accept(2s));
    const auto connectedDeadline = std::chrono::steady_clock::now() + 1s;
    while (!connected.load() &&
        std::chrono::steady_clock::now() < connectedDeadline)
    {
        std::this_thread::sleep_for(1ms);
    }
    CHECK(connected.load());

    melonDS::MCP::StreamResult result;
    std::uint8_t byte = 0;
    std::thread reader([&]() {
        result = server.ReadExact(
            &byte, 1, LocalByteStreamServer::InfiniteTimeout);
    });
    std::this_thread::sleep_for(25ms);
    const auto start = std::chrono::steady_clock::now();
    server.Stop();
    reader.join();
    const auto elapsed = std::chrono::steady_clock::now() - start;

    CHECK(result.Error == StreamError::Stopped);
    CHECK(result.BytesTransferred == 0);
    CHECK(elapsed < 1s);
    mayClose.store(true);
    client.join();
}

} // anonymous namespace

int main()
{
    TestLifecycleAndValidation();
    TestFrameCodecRoundTrip();
    TestReadTimeoutIsExplicit();
    TestStopUnblocksAccept();
    TestStopUnblocksRead();

    if (FailureCount != 0)
    {
        std::cerr << FailureCount << " local byte-stream checks failed\n";
        return EXIT_FAILURE;
    }

    std::cout << "All local byte-stream checks passed\n";
    return EXIT_SUCCESS;
}
