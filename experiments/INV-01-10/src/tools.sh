#!/bin/sh
# INV-01-10 tool-inventory probe. POSIX sh + standard Unix tools only, no
# dependency. Emits one JSON object to stdout per invocation.
#
# Usage: tools.sh [--negative]
#   --negative   Deliberately misreport clang as present with an empty
#                version string, violating the present-implies-nonempty-
#                version invariant check.py enforces. Required negative
#                control; produces no claim.
set -eu

NEGATIVE=0
for a in "$@"; do
    [ "$a" = "--negative" ] && NEGATIVE=1
done

# resolve <tool>: prefer the Xcode/CLT-aware resolver (xcrun --find), since
# that is how a developer directory actually dispatches these tools; fall
# back to a plain PATH lookup for tools xcrun does not register (e.g.
# rustc, brew). Prints the resolved absolute path, or nothing.
resolve() {
    p=$(xcrun --find "$1" 2>/dev/null) && [ -n "$p" ] && { printf '%s' "$p"; return 0; }
    command -v "$1" 2>/dev/null
}

# json_escape <string>: minimal escaping sufficient for --version output
# (backslash, double quote, newline). ponytail: not a general JSON encoder,
# upgrade if a captured string ever contains other control characters.
json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n' ' '
}

# tool_field <name> <path> <version>: prints one JSON object.
tool_field() {
    name=$1; path=$2; version=$3
    if [ -n "$path" ]; then
        printf '{"present": true, "path": "%s", "version": "%s"}' \
            "$(json_escape "$path")" "$(json_escape "$version")"
    else
        printf '{"present": false, "path": null, "version": null}'
    fi
    : "$name" # name only used for readability at call sites
}

presence_field() {
    path=$1
    if [ -n "$path" ]; then
        printf '{"present": true, "path": "%s"}' "$(json_escape "$path")"
    else
        printf '{"present": false, "path": null}'
    fi
}

CLANG_PATH=$(resolve clang || true)
CLANG_VER=$( [ -n "$CLANG_PATH" ] && "$CLANG_PATH" --version 2>&1 | head -1 || true)
if [ "$NEGATIVE" = "1" ]; then CLANG_VER=""; fi

LLDB_PATH=$(resolve lldb || true)
LLDB_VER=$( [ -n "$LLDB_PATH" ] && "$LLDB_PATH" --version 2>&1 | head -1 || true)

OBJDUMP_PATH=$(resolve llvm-objdump || true)
OBJDUMP_VER=$( [ -n "$OBJDUMP_PATH" ] && "$OBJDUMP_PATH" --version 2>&1 | head -1 || true)

PY_PATH=$(resolve python3 || true)
PY_VER=$( [ -n "$PY_PATH" ] && "$PY_PATH" --version 2>&1 | head -1 || true)

RUSTC_PATH=$(resolve rustc || true)
RUSTC_VER=$( [ -n "$RUSTC_PATH" ] && "$RUSTC_PATH" --version 2>&1 | head -1 || true)

GIT_PATH=$(resolve git || true)
GIT_VER=$( [ -n "$GIT_PATH" ] && "$GIT_PATH" --version 2>&1 | head -1 || true)

CODESIGN_PATH=$(resolve codesign || true)
# codesign has no --version option; record that plainly rather than a
# fabricated version string (confirmed: `codesign --version` prints its
# usage banner to stderr and exits nonzero).
CODESIGN_VER=$( [ -n "$CODESIGN_PATH" ] && printf '(no --version option; present via xcrun --find)' || true)

XCODE_SELECT_PATH=$(xcode-select -p 2>/dev/null || true)

XCTRACE_PATH=$(resolve xctrace || true)
XCTRACE_FUNCTIONAL=false
if [ -n "$XCTRACE_PATH" ]; then
    if xctrace version >/tmp/.inv0110_xctrace.$$ 2>&1; then
        XCTRACE_FUNCTIONAL=true
    fi
    rm -f /tmp/.inv0110_xctrace.$$
fi

CMAKE_PATH=$(resolve cmake || true)
NINJA_PATH=$(resolve ninja || true)
QEMU_PATH=$(resolve qemu-system-x86_64 || true)
BREW_PATH=$(resolve brew || true)
BREW_VER=$( [ -n "$BREW_PATH" ] && "$BREW_PATH" --version 2>&1 | head -1 || true)

SDK_DIR="/Library/Developer/CommandLineTools/SDKs"
SDK_LIST=""
if [ -d "$SDK_DIR" ]; then
    first=1
    for entry in "$SDK_DIR"/*; do
        [ -e "$entry" ] || continue
        base=$(basename "$entry")
        [ "$first" = 1 ] || SDK_LIST="$SDK_LIST,"
        SDK_LIST="$SDK_LIST\"$(json_escape "$base")\""
        first=0
    done
fi

printf '{\n'
printf '  "schema_version": 1,\n'
printf '  "negative_control": %s,\n' "$( [ "$NEGATIVE" = "1" ] && echo true || echo false )"
printf '  "tools": {\n'
printf '    "clang": %s,\n' "$(tool_field clang "$CLANG_PATH" "$CLANG_VER")"
printf '    "lldb": %s,\n' "$(tool_field lldb "$LLDB_PATH" "$LLDB_VER")"
printf '    "llvm_objdump": %s,\n' "$(tool_field llvm-objdump "$OBJDUMP_PATH" "$OBJDUMP_VER")"
printf '    "python3": %s,\n' "$(tool_field python3 "$PY_PATH" "$PY_VER")"
printf '    "rustc": %s,\n' "$(tool_field rustc "$RUSTC_PATH" "$RUSTC_VER")"
printf '    "git": %s,\n' "$(tool_field git "$GIT_PATH" "$GIT_VER")"
printf '    "codesign": %s\n' "$(tool_field codesign "$CODESIGN_PATH" "$CODESIGN_VER")"
printf '  },\n'
printf '  "presence": {\n'
printf '    "xcode_select_path": %s,\n' "$(presence_field "$XCODE_SELECT_PATH")"
printf '    "xctrace": {"present": %s, "path": %s, "functional_without_xcode": %s},\n' \
    "$( [ -n "$XCTRACE_PATH" ] && echo true || echo false )" \
    "$( [ -n "$XCTRACE_PATH" ] && printf '"%s"' "$(json_escape "$XCTRACE_PATH")" || echo null )" \
    "$XCTRACE_FUNCTIONAL"
printf '    "cmake": %s,\n' "$(presence_field "$CMAKE_PATH")"
printf '    "ninja": %s,\n' "$(presence_field "$NINJA_PATH")"
printf '    "qemu_system_x86_64": %s,\n' "$(presence_field "$QEMU_PATH")"
printf '    "brew": {"present": %s, "path": %s, "version": %s}\n' \
    "$( [ -n "$BREW_PATH" ] && echo true || echo false )" \
    "$( [ -n "$BREW_PATH" ] && printf '"%s"' "$(json_escape "$BREW_PATH")" || echo null )" \
    "$( [ -n "$BREW_PATH" ] && printf '"%s"' "$(json_escape "$BREW_VER")" || echo null )"
printf '  },\n'
printf '  "sdk_directory": "%s",\n' "$(json_escape "$SDK_DIR")"
printf '  "sdk_directory_listing": [%s]\n' "$SDK_LIST"
printf '}\n'
