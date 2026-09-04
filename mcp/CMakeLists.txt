# mcp/CMakeLists.txt — 构建 libmelonds_mcp（无头模拟器控制/调试共享库）
#
# 将 melonDS 核心（含 MCPDebug 调试钩子）编译为静态库，
# 再与 C shim 链接为共享库，供 Python ctypes 加载。
#
# 用法：
#   cmake -B build -S mcp -DCMAKE_BUILD_TYPE=Release
#   cmake --build build -j

cmake_minimum_required(VERSION 3.16)

cmake_policy(VERSION 3.15)
if (POLICY CMP0076)
    cmake_policy(SET CMP0076 NEW)
endif()

set(CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/../cmake" ${CMAKE_MODULE_PATH})

project(melonds_mcp LANGUAGES C CXX ASM)

include(CheckSymbolExists)
include(CheckLibraryExists)
include(CMakeDependentOption)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# ── 架构检测（melonDS 核心必需）──

function(detect_architecture symbol arch)
    if (NOT DEFINED ARCHITECTURE)
        set(CMAKE_REQUIRED_QUIET 1)
        check_symbol_exists("${symbol}" "" ARCHITECTURE_${arch})
        unset(CMAKE_REQUIRED_QUIET)
        if (ARCHITECTURE_${arch})
            set(ARCHITECTURE "${arch}" PARENT_SCOPE)
            set(ARCHITECTURE_${arch} 1 PARENT_SCOPE)
            add_definitions(-DARCHITECTURE_${arch}=1)
        endif()
    endif()
endfunction()

detect_architecture("__x86_64__" x86_64)
detect_architecture("__i386__" x86)
detect_architecture("__arm__" ARM)
detect_architecture("__aarch64__" ARM64)

# ── melonDS 核心选项 ──

cmake_dependent_option(ENABLE_JIT "Enable JIT recompiler" ON
    "ARCHITECTURE STREQUAL x86_64 OR ARCHITECTURE STREQUAL ARM64" OFF)
option(ENABLE_OGLRENDERER "Enable OpenGL renderer" OFF)
option(ENABLE_GDBSTUB "Enable GDB stub" OFF)

if (ENABLE_GDBSTUB)
    add_definitions(-DGDBSTUB_ENABLED)
endif()

# ── 编译 melonDS 核心（含 MCPDebug）为静态库 ──

add_subdirectory(
    "${CMAKE_CURRENT_SOURCE_DIR}/../src"
    "${CMAKE_CURRENT_BINARY_DIR}/core_build"
)

# ── 编译 shim 为共享库 ──

add_library(melonds_mcp SHARED
    shim/mcp_shim.cpp
    shim/platform_stubs.cpp
)

target_include_directories(melonds_mcp PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../src"
    "${CMAKE_CURRENT_BINARY_DIR}/core_build"  # version.h
)

if (NOT APPLE)
    target_link_libraries(melonds_mcp PRIVATE core dl)
else()
    # macOS: dlopen/dlsym 位于 libSystem，无需单独链接
    target_link_libraries(melonds_mcp PRIVATE core)
endif()

set_target_properties(melonds_mcp PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
)
