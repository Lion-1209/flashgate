# Build-time firmware identity for flashgate.
# Invoked via: cmake -DSOURCE_DIR=... -DOUT_FILE=... -P firmware_identity.cmake
# Writes fw_identity.h only when content changed, so incremental builds stay quiet.
# APP_GIT_SHA carries a -dirty suffix when the working tree differs from HEAD,
# so the banner can prove "the board runs exactly this tree state".

execute_process(
    COMMAND git rev-parse --short=7 HEAD
    WORKING_DIRECTORY "${SOURCE_DIR}"
    OUTPUT_VARIABLE sha
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE git_result
)
if(NOT git_result EQUAL 0)
    set(sha "unknown")
endif()

execute_process(
    COMMAND git status --porcelain
    WORKING_DIRECTORY "${SOURCE_DIR}"
    OUTPUT_VARIABLE status_out
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE status_result
)
if(status_result EQUAL 0 AND NOT status_out STREQUAL "")
    string(APPEND sha "-dirty")
endif()

string(TIMESTAMP iso "%Y-%m-%dT%H:%M:%SZ" UTC)

set(content "/* flashgate build identity - generated at build time, do not edit. */\n#define APP_GIT_SHA \"${sha}\"\n#define APP_BUILD_ISO \"${iso}\"\n")

if(EXISTS "${OUT_FILE}")
    file(READ "${OUT_FILE}" old)
else()
    set(old "")
endif()

if(NOT content STREQUAL old)
    file(WRITE "${OUT_FILE}" "${content}")
endif()
