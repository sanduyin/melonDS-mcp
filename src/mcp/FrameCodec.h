/*
    Copyright 2026 melonDS-MCP contributors

    This file is part of melonDS.

    melonDS is free software: you can redistribute it and/or modify it under
    the terms of the GNU General Public License as published by the Free
    Software Foundation, either version 3 of the License, or (at your option)
    any later version.
*/

#ifndef MELONDS_MCP_FRAMECODEC_H_
#define MELONDS_MCP_FRAMECODEC_H_

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace melonDS::MCP
{

// Wire format version 1 is a 32-byte fixed header followed by UTF-8 JSON and
// then an optional opaque binary payload. All numeric fields are little-endian.
constexpr std::array<std::uint8_t, 4> FrameMagic {{'M', 'D', 'S', 'B'}};
constexpr std::uint16_t ProtocolVersion = 1;
constexpr std::size_t FrameHeaderSize = 32;

// These are hard protocol limits. FrameLimits may lower, but never raise them.
constexpr std::uint32_t MaxJsonBytes = 1U * 1024U * 1024U;
constexpr std::uint32_t MaxBinaryBytes = 64U * 1024U * 1024U;
constexpr std::uint64_t MaxPayloadBytes =
    static_cast<std::uint64_t>(MaxJsonBytes) + MaxBinaryBytes;

enum class MessageType : std::uint16_t
{
    Request = 1,
    Response = 2,
    Event = 3,
    Cancel = 4,
};

struct Frame
{
    MessageType Type = MessageType::Request;
    std::uint32_t Flags = 0;
    std::uint64_t RequestId = 0;
    std::string Json;
    std::vector<std::uint8_t> Binary;
};

struct FrameLimits
{
    std::uint32_t JsonBytes = MaxJsonBytes;
    std::uint32_t BinaryBytes = MaxBinaryBytes;
    std::uint64_t PayloadBytes = MaxPayloadBytes;
};

enum class ProtocolError
{
    None,
    InvalidArgument,
    BadMagic,
    UnsupportedVersion,
    UnknownMessageType,
    UnsupportedFlags,
    ReservedFieldNonZero,
    JsonTooLarge,
    BinaryTooLarge,
    PayloadTooLarge,
    InvalidJsonUtf8,
    AllocationFailed,
};

const char* ProtocolErrorName(ProtocolError error);

struct EncodeResult
{
    ProtocolError Error = ProtocolError::None;
    std::size_t BytesWritten = 0;

    explicit operator bool() const { return Error == ProtocolError::None; }
};

// On failure, output is left unchanged.
EncodeResult EncodeFrame(
    const Frame& frame,
    std::vector<std::uint8_t>& output,
    const FrameLimits& limits = {});

enum class ParseStatus
{
    NeedMoreData,
    FramesReady,
    Error,
};

struct ParseResult
{
    ParseStatus Status = ParseStatus::NeedMoreData;
    std::size_t FramesProduced = 0;
    ProtocolError Error = ProtocolError::None;
};

// Incremental, transport-neutral decoder. An error is sticky: after malformed
// input, Feed() keeps returning it until Reset() starts a new stream.
class FrameParser
{
public:
    explicit FrameParser(const FrameLimits& limits = {});

    ParseResult Feed(
        const std::uint8_t* data,
        std::size_t length,
        std::vector<Frame>& output);

    ParseResult Feed(
        const std::vector<std::uint8_t>& data,
        std::vector<Frame>& output)
    {
        return Feed(data.data(), data.size(), output);
    }

    void Reset();

    ProtocolError Error() const { return CurrentError; }
    std::size_t HeaderBytesBuffered() const { return HeaderBytes; }
    std::uint64_t PayloadBytesBuffered() const { return PayloadBytes; }

private:
    ParseResult Fail(ProtocolError error, std::size_t framesProduced);
    ProtocolError DecodeHeader();
    ProtocolError AllocatePayload();
    ProtocolError FinishFrame(std::vector<Frame>& output);
    void ResetFrameState();

    FrameLimits Limits;
    ProtocolError CurrentError = ProtocolError::None;
    std::array<std::uint8_t, FrameHeaderSize> Header {};
    std::size_t HeaderBytes = 0;
    bool HasDecodedHeader = false;
    std::uint32_t JsonLength = 0;
    std::uint32_t BinaryLength = 0;
    std::uint64_t PayloadBytes = 0;
    Frame CurrentFrame;
};

} // namespace melonDS::MCP

#endif // MELONDS_MCP_FRAMECODEC_H_
