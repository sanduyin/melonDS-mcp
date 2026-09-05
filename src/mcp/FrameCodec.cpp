/*
    Copyright 2026 melonDS-MCP contributors

    This file is part of melonDS.

    melonDS is free software: you can redistribute it and/or modify it under
    the terms of the GNU General Public License as published by the Free
    Software Foundation, either version 3 of the License, or (at your option)
    any later version.
*/

#include "FrameCodec.h"

#include <algorithm>
#include <cstring>
#include <limits>
#include <new>
#include <utility>

namespace melonDS::MCP
{

namespace
{

constexpr std::size_t MagicOffset = 0;
constexpr std::size_t VersionOffset = 4;
constexpr std::size_t MessageTypeOffset = 6;
constexpr std::size_t FlagsOffset = 8;
constexpr std::size_t RequestIdOffset = 12;
constexpr std::size_t JsonLengthOffset = 20;
constexpr std::size_t BinaryLengthOffset = 24;
constexpr std::size_t ReservedOffset = 28;

static_assert(ReservedOffset + sizeof(std::uint32_t) == FrameHeaderSize,
    "version 1 bridge header must remain exactly 32 bytes");

FrameLimits NormalizeLimits(const FrameLimits& limits)
{
    return {
        std::min(limits.JsonBytes, MaxJsonBytes),
        std::min(limits.BinaryBytes, MaxBinaryBytes),
        std::min(limits.PayloadBytes, MaxPayloadBytes),
    };
}

bool IsKnownMessageType(MessageType type)
{
    switch (type)
    {
    case MessageType::Request:
    case MessageType::Response:
    case MessageType::Event:
    case MessageType::Cancel:
        return true;
    }

    return false;
}

std::uint16_t ReadU16LE(const std::uint8_t* data)
{
    return static_cast<std::uint16_t>(data[0]) |
        static_cast<std::uint16_t>(data[1]) << 8;
}

std::uint32_t ReadU32LE(const std::uint8_t* data)
{
    return static_cast<std::uint32_t>(data[0]) |
        static_cast<std::uint32_t>(data[1]) << 8 |
        static_cast<std::uint32_t>(data[2]) << 16 |
        static_cast<std::uint32_t>(data[3]) << 24;
}

std::uint64_t ReadU64LE(const std::uint8_t* data)
{
    std::uint64_t value = 0;
    for (std::size_t i = 0; i < 8; i++)
        value |= static_cast<std::uint64_t>(data[i]) << (i * 8);
    return value;
}

void WriteU16LE(std::uint8_t* data, std::uint16_t value)
{
    data[0] = static_cast<std::uint8_t>(value);
    data[1] = static_cast<std::uint8_t>(value >> 8);
}

void WriteU32LE(std::uint8_t* data, std::uint32_t value)
{
    for (std::size_t i = 0; i < 4; i++)
        data[i] = static_cast<std::uint8_t>(value >> (i * 8));
}

void WriteU64LE(std::uint8_t* data, std::uint64_t value)
{
    for (std::size_t i = 0; i < 8; i++)
        data[i] = static_cast<std::uint8_t>(value >> (i * 8));
}

bool IsContinuationByte(std::uint8_t byte)
{
    return (byte & 0xC0U) == 0x80U;
}

// Strict UTF-8 validation rejects overlong encodings, surrogate code points,
// and code points above U+10FFFF. JSON syntax is intentionally left to the
// command layer so this codec stays independent of a JSON library.
bool IsValidUtf8(const std::string& text)
{
    const auto* bytes = reinterpret_cast<const std::uint8_t*>(text.data());
    const std::size_t length = text.size();

    for (std::size_t i = 0; i < length;)
    {
        const std::uint8_t first = bytes[i];
        if (first <= 0x7FU)
        {
            i++;
            continue;
        }

        if (first >= 0xC2U && first <= 0xDFU)
        {
            if (i + 1 >= length || !IsContinuationByte(bytes[i + 1]))
                return false;
            i += 2;
            continue;
        }

        if (first >= 0xE0U && first <= 0xEFU)
        {
            if (i + 2 >= length ||
                !IsContinuationByte(bytes[i + 1]) ||
                !IsContinuationByte(bytes[i + 2]))
                return false;

            if ((first == 0xE0U && bytes[i + 1] < 0xA0U) ||
                (first == 0xEDU && bytes[i + 1] >= 0xA0U))
                return false;

            i += 3;
            continue;
        }

        if (first >= 0xF0U && first <= 0xF4U)
        {
            if (i + 3 >= length ||
                !IsContinuationByte(bytes[i + 1]) ||
                !IsContinuationByte(bytes[i + 2]) ||
                !IsContinuationByte(bytes[i + 3]))
                return false;

            if ((first == 0xF0U && bytes[i + 1] < 0x90U) ||
                (first == 0xF4U && bytes[i + 1] >= 0x90U))
                return false;

            i += 4;
            continue;
        }

        return false;
    }

    return true;
}

ProtocolError ValidateLengths(
    std::uint64_t jsonLength,
    std::uint64_t binaryLength,
    const FrameLimits& limits)
{
    if (jsonLength > limits.JsonBytes)
        return ProtocolError::JsonTooLarge;
    if (binaryLength > limits.BinaryBytes)
        return ProtocolError::BinaryTooLarge;

    // Both wire lengths are u32, but keep this addition in u64 so the check
    // remains correct if the field widths or configured limits change later.
    if (jsonLength + binaryLength > limits.PayloadBytes)
        return ProtocolError::PayloadTooLarge;

    return ProtocolError::None;
}

} // anonymous namespace

const char* ProtocolErrorName(ProtocolError error)
{
    switch (error)
    {
    case ProtocolError::None: return "none";
    case ProtocolError::InvalidArgument: return "invalid_argument";
    case ProtocolError::BadMagic: return "bad_magic";
    case ProtocolError::UnsupportedVersion: return "unsupported_version";
    case ProtocolError::UnknownMessageType: return "unknown_message_type";
    case ProtocolError::UnsupportedFlags: return "unsupported_flags";
    case ProtocolError::ReservedFieldNonZero: return "reserved_field_non_zero";
    case ProtocolError::JsonTooLarge: return "json_too_large";
    case ProtocolError::BinaryTooLarge: return "binary_too_large";
    case ProtocolError::PayloadTooLarge: return "payload_too_large";
    case ProtocolError::InvalidJsonUtf8: return "invalid_json_utf8";
    case ProtocolError::AllocationFailed: return "allocation_failed";
    }

    return "unknown_error";
}

EncodeResult EncodeFrame(
    const Frame& frame,
    std::vector<std::uint8_t>& output,
    const FrameLimits& requestedLimits)
{
    if (!IsKnownMessageType(frame.Type))
        return {ProtocolError::UnknownMessageType, 0};
    if (frame.Flags != 0)
        return {ProtocolError::UnsupportedFlags, 0};

    const FrameLimits limits = NormalizeLimits(requestedLimits);
    const ProtocolError lengthError = ValidateLengths(
        frame.Json.size(), frame.Binary.size(), limits);
    if (lengthError != ProtocolError::None)
        return {lengthError, 0};
    if (!IsValidUtf8(frame.Json))
        return {ProtocolError::InvalidJsonUtf8, 0};

    const std::uint64_t totalLength = FrameHeaderSize +
        static_cast<std::uint64_t>(frame.Json.size()) + frame.Binary.size();
    if (totalLength > std::numeric_limits<std::size_t>::max())
        return {ProtocolError::PayloadTooLarge, 0};

    std::vector<std::uint8_t> encoded;
    try
    {
        encoded.resize(static_cast<std::size_t>(totalLength));
    }
    catch (const std::bad_alloc&)
    {
        return {ProtocolError::AllocationFailed, 0};
    }

    std::copy(FrameMagic.begin(), FrameMagic.end(), encoded.begin() + MagicOffset);
    WriteU16LE(encoded.data() + VersionOffset, ProtocolVersion);
    WriteU16LE(encoded.data() + MessageTypeOffset,
        static_cast<std::uint16_t>(frame.Type));
    WriteU32LE(encoded.data() + FlagsOffset, frame.Flags);
    WriteU64LE(encoded.data() + RequestIdOffset, frame.RequestId);
    WriteU32LE(encoded.data() + JsonLengthOffset,
        static_cast<std::uint32_t>(frame.Json.size()));
    WriteU32LE(encoded.data() + BinaryLengthOffset,
        static_cast<std::uint32_t>(frame.Binary.size()));
    WriteU32LE(encoded.data() + ReservedOffset, 0);

    if (!frame.Json.empty())
    {
        std::memcpy(encoded.data() + FrameHeaderSize,
            frame.Json.data(), frame.Json.size());
    }
    if (!frame.Binary.empty())
    {
        std::memcpy(encoded.data() + FrameHeaderSize + frame.Json.size(),
            frame.Binary.data(), frame.Binary.size());
    }

    output.swap(encoded);
    return {ProtocolError::None, output.size()};
}

FrameParser::FrameParser(const FrameLimits& limits)
    : Limits(NormalizeLimits(limits))
{
}

ParseResult FrameParser::Fail(ProtocolError error, std::size_t framesProduced)
{
    CurrentError = error;
    return {ParseStatus::Error, framesProduced, error};
}

ProtocolError FrameParser::DecodeHeader()
{
    if (!std::equal(FrameMagic.begin(), FrameMagic.end(), Header.begin() + MagicOffset))
        return ProtocolError::BadMagic;
    if (ReadU16LE(Header.data() + VersionOffset) != ProtocolVersion)
        return ProtocolError::UnsupportedVersion;

    CurrentFrame.Type = static_cast<MessageType>(
        ReadU16LE(Header.data() + MessageTypeOffset));
    if (!IsKnownMessageType(CurrentFrame.Type))
        return ProtocolError::UnknownMessageType;

    CurrentFrame.Flags = ReadU32LE(Header.data() + FlagsOffset);
    if (CurrentFrame.Flags != 0)
        return ProtocolError::UnsupportedFlags;

    if (ReadU32LE(Header.data() + ReservedOffset) != 0)
        return ProtocolError::ReservedFieldNonZero;

    CurrentFrame.RequestId = ReadU64LE(Header.data() + RequestIdOffset);
    JsonLength = ReadU32LE(Header.data() + JsonLengthOffset);
    BinaryLength = ReadU32LE(Header.data() + BinaryLengthOffset);

    return ValidateLengths(JsonLength, BinaryLength, Limits);
}

ProtocolError FrameParser::AllocatePayload()
{
    try
    {
        CurrentFrame.Json.resize(JsonLength);
        CurrentFrame.Binary.resize(BinaryLength);
    }
    catch (const std::bad_alloc&)
    {
        return ProtocolError::AllocationFailed;
    }

    return ProtocolError::None;
}

ProtocolError FrameParser::FinishFrame(std::vector<Frame>& output)
{
    if (!IsValidUtf8(CurrentFrame.Json))
        return ProtocolError::InvalidJsonUtf8;

    try
    {
        output.emplace_back(std::move(CurrentFrame));
    }
    catch (const std::bad_alloc&)
    {
        return ProtocolError::AllocationFailed;
    }
    ResetFrameState();
    return ProtocolError::None;
}

void FrameParser::ResetFrameState()
{
    Header.fill(0);
    HeaderBytes = 0;
    HasDecodedHeader = false;
    JsonLength = 0;
    BinaryLength = 0;
    PayloadBytes = 0;
    CurrentFrame = {};
}

void FrameParser::Reset()
{
    CurrentError = ProtocolError::None;
    ResetFrameState();
}

ParseResult FrameParser::Feed(
    const std::uint8_t* data,
    std::size_t length,
    std::vector<Frame>& output)
{
    if (CurrentError != ProtocolError::None)
        return {ParseStatus::Error, 0, CurrentError};
    if (data == nullptr && length != 0)
        return Fail(ProtocolError::InvalidArgument, 0);

    const std::size_t initialOutputSize = output.size();
    std::size_t consumed = 0;

    // This is intentionally an unconditional state-machine loop. In
    // particular, a frame must be emitted when its final payload byte is the
    // final byte in this Feed() call; using "consumed < length" as the loop
    // condition would leave that complete frame pending until another read.
    while (true)
    {
        if (HeaderBytes < FrameHeaderSize)
        {
            const std::size_t count = std::min(
                FrameHeaderSize - HeaderBytes, length - consumed);
            if (count == 0)
                break;

            std::memcpy(Header.data() + HeaderBytes, data + consumed, count);
            HeaderBytes += count;
            consumed += count;

            if (HeaderBytes < FrameHeaderSize)
                continue;
        }

        if (!HasDecodedHeader)
        {
            const ProtocolError headerError = DecodeHeader();
            if (headerError != ProtocolError::None)
                return Fail(headerError, output.size() - initialOutputSize);

            const ProtocolError allocationError = AllocatePayload();
            if (allocationError != ProtocolError::None)
                return Fail(allocationError, output.size() - initialOutputSize);
            HasDecodedHeader = true;
        }

        const std::uint64_t totalPayload =
            static_cast<std::uint64_t>(JsonLength) + BinaryLength;
        if (PayloadBytes == totalPayload)
        {
            const ProtocolError finishError = FinishFrame(output);
            if (finishError != ProtocolError::None)
                return Fail(finishError, output.size() - initialOutputSize);
            continue;
        }

        if (consumed == length)
            break;

        if (PayloadBytes < JsonLength)
        {
            const std::size_t jsonOffset = static_cast<std::size_t>(PayloadBytes);
            const std::size_t count = std::min<std::size_t>(
                JsonLength - jsonOffset, length - consumed);
            std::memcpy(CurrentFrame.Json.data() + jsonOffset, data + consumed, count);
            PayloadBytes += count;
            consumed += count;
        }
        else
        {
            const std::size_t binaryOffset = static_cast<std::size_t>(
                PayloadBytes - JsonLength);
            const std::size_t count = std::min<std::size_t>(
                BinaryLength - binaryOffset, length - consumed);
            std::memcpy(CurrentFrame.Binary.data() + binaryOffset,
                data + consumed, count);
            PayloadBytes += count;
            consumed += count;
        }
    }

    const std::size_t framesProduced = output.size() - initialOutputSize;
    return {
        framesProduced == 0 ? ParseStatus::NeedMoreData : ParseStatus::FramesReady,
        framesProduced,
        ProtocolError::None,
    };
}

} // namespace melonDS::MCP
