cmake_minimum_required(VERSION 3.16)

if(CMAKE_CURRENT_SOURCE_DIR STREQUAL CMAKE_SOURCE_DIR)
    project(melonds_mcp_protocol LANGUAGES CXX)
    set(MELONDS_MCP_PROTOCOL_STANDALONE ON)
else()
    set(MELONDS_MCP_PROTOCOL_STANDALONE OFF)
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(Threads REQUIRED)

add_library(melonds_mcp_protocol STATIC
    DebugCoordinator.cpp
    DebugCoordinator.h
    FrameCodec.cpp
    FrameCodec.h
    LocalByteStream.cpp
    LocalByteStream.h)

target_include_directories(melonds_mcp_protocol PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}")

target_link_libraries(melonds_mcp_protocol PUBLIC
    Threads::Threads)

option(MELONDS_MCP_PROTOCOL_WARNINGS_AS_ERRORS
    "Treat native bridge protocol warnings as build errors"
    ${MELONDS_MCP_PROTOCOL_STANDALONE})

function(melonds_mcp_configure_warnings target)
    if(MSVC)
        target_compile_options(${target} PRIVATE /W4 /permissive-)
        if(MELONDS_MCP_PROTOCOL_WARNINGS_AS_ERRORS)
            target_compile_options(${target} PRIVATE /WX)
        endif()
    else()
        target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic)
        if(MELONDS_MCP_PROTOCOL_WARNINGS_AS_ERRORS)
            target_compile_options(${target} PRIVATE -Werror)
        endif()
    endif()
endfunction()

melonds_mcp_configure_warnings(melonds_mcp_protocol)

if(WIN32)
    # SetEntriesInAclW, used to grant the named pipe only to the current user.
    target_link_libraries(melonds_mcp_protocol PRIVATE advapi32)
endif()

option(MELONDS_MCP_PROTOCOL_BUILD_TESTS
    "Build the standalone melonDS bridge frame-codec tests"
    ${MELONDS_MCP_PROTOCOL_STANDALONE})

if(MELONDS_MCP_PROTOCOL_BUILD_TESTS)
    enable_testing()

    add_executable(melonds_mcp_protocol_tests
        tests/FrameCodecTests.cpp)
    target_link_libraries(melonds_mcp_protocol_tests PRIVATE
        melonds_mcp_protocol)

    melonds_mcp_configure_warnings(melonds_mcp_protocol_tests)

    add_test(NAME melonds_mcp_protocol_tests
        COMMAND melonds_mcp_protocol_tests)

    add_executable(melonds_mcp_local_stream_tests
        tests/LocalByteStreamTests.cpp)
    target_link_libraries(melonds_mcp_local_stream_tests PRIVATE
        melonds_mcp_protocol
        Threads::Threads)
    melonds_mcp_configure_warnings(melonds_mcp_local_stream_tests)

    add_test(NAME melonds_mcp_local_stream_tests
        COMMAND melonds_mcp_local_stream_tests)
    set_tests_properties(melonds_mcp_local_stream_tests PROPERTIES
        TIMEOUT 15)

    add_executable(melonds_mcp_debug_coordinator_tests
        tests/DebugCoordinatorTests.cpp)
    target_link_libraries(melonds_mcp_debug_coordinator_tests PRIVATE
        melonds_mcp_protocol)
    melonds_mcp_configure_warnings(
        melonds_mcp_debug_coordinator_tests)

    add_test(NAME melonds_mcp_debug_coordinator_tests
        COMMAND melonds_mcp_debug_coordinator_tests)
    set_tests_properties(melonds_mcp_debug_coordinator_tests PROPERTIES
        TIMEOUT 15)
endif()
