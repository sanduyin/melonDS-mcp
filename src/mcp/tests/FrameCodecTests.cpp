#include "FrameCodec.h"

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <string>
#include <utility>
#include <vector>

namespace
{

using melonDS::MCP::EncodeFrame;
using melonDS::MCP::Frame;
using melonDS::MCP::FrameHeaderSize;
using melonDS::MCP::FrameLimits;
using melonDS::MCP::FrameParser;
using melonDS::MCP::MessageType;
using melonDS::MCP::ParseStatus;
using melonDS::MCP::ProtocolError;

int FailureCount = 0;

#define CHECK(condition) Check(static_cast<bool>(condition), #condition, __FILE__, __LINE__)

void Check(bool condition, const char* expression, const char* file, int line)
{
    if (condition)
        return;

    std::cerr << file << ':' << line << ": check failed: " << expression << '\n';
    FailureCount++;
}

std::vector<std::uint8_t> Encode(const Frame& frame)
{
    std::vector<std::uint8_t> bytes;
    const auto result = EncodeFrame(frame, bytes);
    CHECK(result);
    CHECK(result.BytesWritten == bytes.size());
    return bytes;
}

void TestGoldenHeaderAndPayload()
{
    Frame frame;
    frame.Type = MessageType::Response;
    frame.RequestId = 0x0102030405060708ULL;
    frame.Json = "{}";
    frame.Binary = {0xAA, 0x55, 0x00};

    const auto bytes = Encode(frame);
    const std::vector<std::uint8_t> expected {
        'M', 'D', 'S', 'B',
        0x01, 0x00,             // version (u16 LE)
        0x02, 0x00,             // response (u16 LE)
        0x00, 0x00, 0x00, 0x00, // flags (u32 LE)
        0x08, 0x07, 0x06, 0x05, 0x04, 0x03, 0x02, 0x01, // request_id
        0x02, 0x00, 0x00, 0x00, // JSON length
        0x03, 0x00, 0x00, 0x00, // binary length
        0x00, 0x00, 0x00, 0x00, // reserved
        '{', '}', 0xAA, 0x55, 0x00,
    };

    CHECK(FrameHeaderSize == 32);
    CHECK(bytes == expected);
}

void TestOneByteAtATime()
{
    Frame input;
    input.Type = MessageType::Event;
    input.RequestId = 42;
    input.Json = u8"{\"event\":\"\u5E27\u5B8C\u6210\"}";
    input.Binary = {0x00, 0x01, 0xFE, 0xFF};
    const auto bytes = Encode(input);

    FrameParser parser;
    std::vector<Frame> output;
    for (std::size_t i = 0; i < bytes.size(); i++)
    {
        const auto result = parser.Feed(&bytes[i], 1, output);
        if (i + 1 == bytes.size())
        {
            CHECK(result.Status == ParseStatus::FramesReady);
            CHECK(result.FramesProduced == 1);
        }
        else
        {
            CHECK(result.Status == ParseStatus::NeedMoreData);
            CHECK(result.FramesProduced == 0);
        }
    }

    CHECK(output.size() == 1);
    if (output.size() == 1)
    {
        CHECK(output[0].Type == input.Type);
        CHECK(output[0].RequestId == input.RequestId);
        CHECK(output[0].Json == input.Json);
        CHECK(output[0].Binary == input.Binary);
    }
    CHECK(parser.HeaderBytesBuffered() == 0);
    CHECK(parser.PayloadBytesBuffered() == 0);
}

void TestManyFramesInOneRead()
{
    Frame request;
    request.Type = MessageType::Request;
    request.RequestId = 100;
    request.Json = "{\"method\":\"status\"}";

    Frame cancel;
    cancel.Type = MessageType::Cancel;
    cancel.RequestId = 100;
    cancel.Json = "{}";

    auto bytes = Encode(request);
    const auto second = Encode(cancel);
    bytes.insert(bytes.end(), second.begin(), second.end());

    FrameParser parser;
    std::vector<Frame> output;
    const auto result = parser.Feed(bytes, output);

    CHECK(result.Status == ParseStatus::FramesReady);
    CHECK(result.FramesProduced == 2);
    CHECK(output.size() == 2);
    if (output.size() == 2)
    {
        CHECK(output[0].Type == MessageType::Request);
        CHECK(output[1].Type == MessageType::Cancel);
        CHECK(output[0].RequestId == output[1].RequestId);
    }
}

void TestEmptyFrameCompletesAtHeaderBoundary()
{
    Frame frame;
    frame.Type = MessageType::Cancel;
    frame.RequestId = 7;
    const auto bytes = Encode(frame);
    CHECK(bytes.size() == FrameHeaderSize);

    FrameParser parser;
    std::vector<Frame> output;
    const auto result = parser.Feed(bytes, output);

    CHECK(result.Status == ParseStatus::FramesReady);
    CHECK(result.FramesProduced == 1);
    CHECK(output.size() == 1);
    if (output.size() == 1)
    {
        CHECK(output[0].Json.empty());
        CHECK(output[0].Binary.empty());
    }
}

std::vector<std::uint8_t> HeaderOnly()
{
    Frame frame;
    const auto encoded = Encode(frame);
    return {encoded.begin(), encoded.begin() + FrameHeaderSize};
}

void ExpectHeaderError(
    std::vector<std::uint8_t> header,
    ProtocolError expectedError)
{
    FrameParser parser;
    std::vector<Frame> output;
    const auto result = parser.Feed(header, output);
    CHECK(result.Status == ParseStatus::Error);
    CHECK(result.Error == expectedError);
    CHECK(parser.Error() == expectedError);

    // Errors are sticky, and the malformed stream is not silently resynced.
    const auto sticky = parser.Feed(header, output);
    CHECK(sticky.Status == ParseStatus::Error);
    CHECK(sticky.Error == expectedError);

    parser.Reset();
    CHECK(parser.Error() == ProtocolError::None);
    const auto recovered = parser.Feed(HeaderOnly(), output);
    CHECK(recovered.Status == ParseStatus::FramesReady);
}

void TestMalformedHeaders()
{
    auto badMagic = HeaderOnly();
    badMagic[0] = 'X';
    ExpectHeaderError(std::move(badMagic), ProtocolError::BadMagic);

    auto badVersion = HeaderOnly();
    badVersion[4] = 2;
    ExpectHeaderError(std::move(badVersion), ProtocolError::UnsupportedVersion);

    auto badType = HeaderOnly();
    badType[6] = 5;
    ExpectHeaderError(std::move(badType), ProtocolError::UnknownMessageType);

    auto badFlags = HeaderOnly();
    badFlags[8] = 1;
    ExpectHeaderError(std::move(badFlags), ProtocolError::UnsupportedFlags);

    auto badReserved = HeaderOnly();
    badReserved[28] = 1;
    ExpectHeaderError(std::move(badReserved), ProtocolError::ReservedFieldNonZero);
}

void TestValidFrameBeforeMalformedFrameIsReported()
{
    Frame frame;
    frame.Type = MessageType::Event;
    frame.Json = "{}";
    auto bytes = Encode(frame);
    auto malformed = HeaderOnly();
    malformed[0] = 'X';
    bytes.insert(bytes.end(), malformed.begin(), malformed.end());

    FrameParser parser;
    std::vector<Frame> output;
    const auto result = parser.Feed(bytes, output);

    CHECK(result.Status == ParseStatus::Error);
    CHECK(result.Error == ProtocolError::BadMagic);
    CHECK(result.FramesProduced == 1);
    CHECK(output.size() == 1);
}

void TestLimitsRejectBeforePayloadAllocation()
{
    Frame frame;
    auto jsonTooLarge = HeaderOnly();
    jsonTooLarge[20] = 0x01;
    jsonTooLarge[22] = 0x10; // 0x00100001: one byte over 1 MiB
    ExpectHeaderError(std::move(jsonTooLarge), ProtocolError::JsonTooLarge);

    auto binaryTooLarge = HeaderOnly();
    binaryTooLarge[24] = 0x01;
    binaryTooLarge[27] = 0x04; // 0x04000001: one byte over 64 MiB
    ExpectHeaderError(std::move(binaryTooLarge), ProtocolError::BinaryTooLarge);

    FrameLimits smallLimits;
    smallLimits.JsonBytes = 4;
    smallLimits.BinaryBytes = 4;
    smallLimits.PayloadBytes = 6;
    FrameParser parser(smallLimits);
    std::vector<Frame> output;

    auto combinedTooLarge = HeaderOnly();
    combinedTooLarge[20] = 4;
    combinedTooLarge[24] = 4;
    const auto result = parser.Feed(combinedTooLarge, output);
    CHECK(result.Status == ParseStatus::Error);
    CHECK(result.Error == ProtocolError::PayloadTooLarge);

    frame.Json = "12345";
    std::vector<std::uint8_t> unchanged {9, 8, 7};
    const auto encodeResult = EncodeFrame(frame, unchanged, smallLimits);
    CHECK(!encodeResult);
    CHECK(encodeResult.Error == ProtocolError::JsonTooLarge);
    CHECK((unchanged == std::vector<std::uint8_t> {9, 8, 7}));
}

void TestUtf8Validation()
{
    Frame frame;
    frame.Json.assign("\xF0\x9F\x8D\x89", 4); // U+1F349 WATERMELON
    std::vector<std::uint8_t> validOutput;
    CHECK(EncodeFrame(frame, validOutput));

    frame.Json.assign("\xED\xA0\x80", 3); // UTF-8 encoding of a surrogate
    std::vector<std::uint8_t> output;
    const auto encodeResult = EncodeFrame(frame, output);
    CHECK(!encodeResult);
    CHECK(encodeResult.Error == ProtocolError::InvalidJsonUtf8);

    Frame valid;
    valid.Json = "abc";
    auto bytes = Encode(valid);
    bytes[FrameHeaderSize] = 0xC0; // overlong two-byte lead
    bytes[FrameHeaderSize + 1] = 0x80;

    FrameParser parser;
    std::vector<Frame> frames;
    const auto parseResult = parser.Feed(bytes, frames);
    CHECK(parseResult.Status == ParseStatus::Error);
    CHECK(parseResult.Error == ProtocolError::InvalidJsonUtf8);
    CHECK(frames.empty());
}

void TestNullInputContract()
{
    FrameParser parser;
    std::vector<Frame> output;
    const auto empty = parser.Feed(nullptr, 0, output);
    CHECK(empty.Status == ParseStatus::NeedMoreData);

    const auto invalid = parser.Feed(nullptr, 1, output);
    CHECK(invalid.Status == ParseStatus::Error);
    CHECK(invalid.Error == ProtocolError::InvalidArgument);
}

} // anonymous namespace

int main()
{
    TestGoldenHeaderAndPayload();
    TestOneByteAtATime();
    TestManyFramesInOneRead();
    TestEmptyFrameCompletesAtHeaderBoundary();
    TestMalformedHeaders();
    TestValidFrameBeforeMalformedFrameIsReported();
    TestLimitsRejectBeforePayloadAllocation();
    TestUtf8Validation();
    TestNullInputContract();

    if (FailureCount != 0)
    {
        std::cerr << FailureCount << " protocol checks failed\n";
        return EXIT_FAILURE;
    }

    std::cout << "All frame codec checks passed\n";
    return EXIT_SUCCESS;
}
