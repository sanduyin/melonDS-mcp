// GPL-3.0-or-later. Regression coverage for optional MCP continuation state.
#include "Savestate.h"
#include "Platform.h"

#include <cstring>
#include <iostream>
#include <stdexcept>
#include <vector>

namespace melonDS::Platform
{
void Log(LogLevel, const char*, ...) {}
}

static void Require(bool value, const char* message)
{
    if (!value) throw std::runtime_error(message);
}

int main()
{
    using melonDS::Savestate;
    try
    {
        Savestate saved(1024);
        saved.Section("TEST");
        melonDS::u32 first = 11, second = 22;
        saved.Var32(&first);
        saved.Var32(&second);
        saved.Finish();
        Require(!saved.Error, "save failed");

        std::vector<melonDS::u8> bytes(saved.Length());
        std::memcpy(bytes.data(), saved.Buffer(), bytes.size());
        Savestate loaded(bytes.data(), static_cast<melonDS::u32>(bytes.size()), false);
        loaded.Section("TEST");
        first = second = 0;
        loaded.Var32(&first);
        Require(loaded.HasSection("TEST"), "present section not found");
        Require(!loaded.HasSection("MCPR"), "optional section unexpectedly found");
        loaded.Var32(&second);
        Require(!loaded.Error && first == 11 && second == 22,
                "HasSection changed cursor or set an error");

        // Corrupt the section length, not the outer MELN header. Queries must
        // return promptly and never walk outside the provided byte buffer.
        for (melonDS::u32 length : {0u, 1u, 15u, 0xFFFFFFFFu})
        {
            std::memcpy(bytes.data() + 0x14, &length, sizeof(length));
            Savestate malformed(bytes.data(), static_cast<melonDS::u32>(bytes.size()), false);
            Require(!malformed.HasSection("TEST"), "malformed section accepted");
            Require(!malformed.HasSection("MCPR"), "malformed optional scan accepted");
        }
        std::cout << "Savestate optional-section tests passed\n";
        return 0;
    }
    catch (const std::exception& error)
    {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
