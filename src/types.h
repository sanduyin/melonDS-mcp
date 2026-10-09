/*
    Copyright 2016-2026 melonDS team

    This file is part of melonDS.

    melonDS is free software: you can redistribute it and/or modify it under
    the terms of the GNU General Public License as published by the Free
    Software Foundation, either version 3 of the License, or (at your option)
    any later version.

    melonDS is distributed in the hope that it will be useful, but WITHOUT ANY
    WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
    FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

    You should have received a copy of the GNU General Public License along
    with melonDS. If not, see http://www.gnu.org/licenses/.
*/

#ifndef TYPES_H
#define TYPES_H

#include <stdint.h>
#include <array>

namespace melonDS
{
typedef uint8_t     u8;
typedef uint16_t    u16;
typedef uint32_t    u32;
typedef uint64_t    u64;
typedef int8_t      s8;
typedef int16_t     s16;
typedef int32_t     s32;
typedef int64_t     s64;

// Keep the bit helpers available to every core compiler. melonDS historically
// used GCC/Clang builtins directly in otherwise portable core code, which made
// the interpreter-only build unnecessarily depend on those compilers.
constexpr int CountSetBits(u32 value) noexcept
{
    int count = 0;
    while (value)
    {
        value &= value - 1;
        ++count;
    }
    return count;
}

constexpr int CountTrailingZeroes(u32 value) noexcept
{
    if (!value) return 32;

    int count = 0;
    while (!(value & 1))
    {
        value >>= 1;
        ++count;
    }
    return count;
}

constexpr int CountTrailingZeroes(u64 value) noexcept
{
    if (!value) return 64;

    int count = 0;
    while (!(value & 1))
    {
        value >>= 1;
        ++count;
    }
    return count;
}

constexpr int CountTrailingZeroes(u16 value) noexcept
{
    return CountTrailingZeroes(static_cast<u32>(value));
}

constexpr int CountTrailingZeroes(u8 value) noexcept
{
    return CountTrailingZeroes(static_cast<u32>(value));
}

constexpr int CountLeadingZeroes(u64 value) noexcept
{
    if (!value) return 64;

    int count = 0;
    for (u64 mask = 1ULL << 63; !(value & mask); mask >>= 1)
        ++count;
    return count;
}

template<class T, std::size_t A, std::size_t B>
using array2d = std::array<std::array<T, B>, A>;

#ifdef _MSC_VER
#define strcasecmp _stricmp
#define strncasecmp _strnicmp

typedef ptrdiff_t ssize_t;
#endif
}
#endif // TYPES_H
