/* melonDS MCP C ABI visibility. GPL-3.0-or-later, same as melonDS. */
#pragma once

#if defined(_WIN32)
# if defined(MELONDS_MCP_BUILDING_LIBRARY)
#  define MELONDS_MCP_API __declspec(dllexport)
# else
#  define MELONDS_MCP_API __declspec(dllimport)
# endif
#else
# define MELONDS_MCP_API __attribute__((visibility("default")))
#endif
