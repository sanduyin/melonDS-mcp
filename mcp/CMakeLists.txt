# mcp/CMakeLists.txt — 构建 libmelonds_mcp（无头模拟器控制/调试共享库）
# Imported from https://github.com/sanduyin/melonDS-mcp (GPL-3.0).
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

project(melonds_mcp LANGUAGES C CXX)

include(CheckSymbolExists)
include(CheckLibraryExists)
include(CMakeDependentOption)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
if (MSVC)
    set(CMAKE_CXX_STANDARD 20)
    add_compile_options(/utf-8)
    add_compile_definitions(_CRT_SECURE_NO_WARNINGS NOMINMAX WIN32_LEAN_AND_MEAN)
endif()
find_package(Threads REQUIRED)

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
detect_architecture("_M_X64" x86_64)
detect_architecture("_M_IX86" x86)
detect_architecture("_M_ARM" ARM)
detect_architecture("_M_ARM64" ARM64)

# ── melonDS 核心选项 ──

cmake_dependent_option(ENABLE_JIT "Enable JIT recompiler" OFF
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

target_link_libraries(melonds_mcp PRIVATE core Threads::Threads ${CMAKE_DL_LIBS})
target_compile_definitions(melonds_mcp PRIVATE MELONDS_MCP_BUILDING_LIBRARY)

set_target_properties(melonds_mcp PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
    ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
)
# Keep the ctypes library in one location with Visual Studio multi-config builds.
foreach(config DEBUG RELEASE RELWITHDEBINFO MINSIZEREL)
    set_target_properties(melonds_mcp PROPERTIES
        LIBRARY_OUTPUT_DIRECTORY_${config} "${CMAKE_BINARY_DIR}"
        RUNTIME_OUTPUT_DIRECTORY_${config} "${CMAKE_BINARY_DIR}"
        ARCHIVE_OUTPUT_DIRECTORY_${config} "${CMAKE_BINARY_DIR}")
endforeach()
