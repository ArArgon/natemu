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

# provenance <path>: classifies where a resolved tool actually comes from —
# CLT / OS / Homebrew / user-local / other — since "a tool exists on this
# host" and "a tool is publicly available on the supported hosts" (what
# KA-09/`sustainable` asks) are different claims (REV-053 M2). A tool
# resolving from a developer's own home directory is not evidence of public
# distribution.
provenance() {
    case "$1" in
        "") printf 'none' ;;
        /Library/Developer/CommandLineTools/*) printf 'CLT' ;;
        /opt/homebrew/*|/usr/local/Cellar/*|/usr/local/Homebrew/*) printf 'Homebrew' ;;
        "$HOME"/*) printf 'user-local' ;;
        /usr/bin/*|/usr/sbin/*|/bin/*|/sbin/*) printf 'OS' ;;
        *) printf 'other' ;;
    esac
}

# json_escape: minimal escaping sufficient for --version output (backslash,
# double quote, newline escaped as the two-character sequence \n so a
# multi-line value is preserved rather than silently flattened — REV-054
# C7). Reads stdin, writes stdout. awk, not sed: BSD sed's `N` at end of
# input drops the pattern space instead of printing it, which silently
# emptied every field when this was first written with the classic sed
# multi-line-join idiom (caught by smoke-testing before commit). ponytail:
# not a general JSON encoder; upgrade if a captured string ever contains
# other control characters.
json_escape() {
    awk 'BEGIN{ORS=""} {gsub(/\\/,"\\\\"); gsub(/"/,"\\\"")} NR>1{printf "\\n"} {printf "%s", $0}'
}

# tool_field <path> <version>: prints one JSON object, including the
# resolved tool's provenance classification.
tool_field() {
    path=$1; version=$2
    if [ -n "$path" ]; then
        printf '{"present": true, "path": "%s", "version": "%s", "provenance": "%s"}' \
            "$(printf '%s' "$path" | json_escape)" "$(printf '%s' "$version" | json_escape)" \
            "$(provenance "$path")"
    else
        printf '{"present": false, "path": null, "version": null, "provenance": null}'
    fi
}

presence_field() {
    path=$1
    if [ -n "$path" ]; then
        printf '{"present": true, "path": "%s", "provenance": "%s"}' \
            "$(printf '%s' "$path" | json_escape)" "$(provenance "$path")"
    else
        printf '{"present": false, "path": null, "provenance": null}'
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
# codesign has no --version option (confirmed: `codesign --version` prints
# its usage banner to stderr and exits nonzero). Report version as null with
# a separate reason field instead of a fabricated/placeholder version
# string a reader could mistake for real (REV-054 C6); check.py exempts
# codesign from the non-empty-version rule accordingly.
CODESIGN_VER=""
CODESIGN_VER_UNAVAILABLE_REASON=""
if [ -n "$CODESIGN_PATH" ]; then
    CODESIGN_VER_UNAVAILABLE_REASON="no --version option"
fi

XCODE_SELECT_PATH=$(xcode-select -p 2>/dev/null || true)
CLT_BUILD=$(pkgutil --pkg-info=com.apple.pkg.CLTools_Executables 2>/dev/null | \
    awk -F': ' '/^version:/{print $2}' || true)

# xctrace: test the RESOLVED binary, not a fresh bare-`xctrace` PATH lookup
# (REV-054 C2 / REV-053 M4) — on hosts where `xcrun --find xctrace` and
# `command -v xctrace` disagree, a bare invocation can attribute a PATH miss
# to Xcode. Capture and report the failure message itself so the record's
# quoted reason is verifiable from the artifact, not asserted in prose.
XCTRACE_PATH=$(resolve xctrace || true)
XCTRACE_FUNCTIONAL=false
XCTRACE_MESSAGE=""
if [ -n "$XCTRACE_PATH" ]; then
    XCTRACE_OUT=$(mktemp)
    if "$XCTRACE_PATH" version >"$XCTRACE_OUT" 2>&1; then
        XCTRACE_FUNCTIONAL=true
    fi
    XCTRACE_MESSAGE=$(head -1 "$XCTRACE_OUT" 2>/dev/null || true)
    rm -f "$XCTRACE_OUT"
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
        SDK_LIST="$SDK_LIST\"$(printf '%s' "$base" | json_escape)\""
        first=0
    done
fi

printf '{\n'
printf '  "schema_version": 2,\n'
printf '  "negative_control": %s,\n' "$( [ "$NEGATIVE" = "1" ] && echo true || echo false )"
printf '  "tools": {\n'
printf '    "clang": %s,\n' "$(tool_field "$CLANG_PATH" "$CLANG_VER")"
printf '    "lldb": %s,\n' "$(tool_field "$LLDB_PATH" "$LLDB_VER")"
printf '    "llvm_objdump": %s,\n' "$(tool_field "$OBJDUMP_PATH" "$OBJDUMP_VER")"
printf '    "python3": %s,\n' "$(tool_field "$PY_PATH" "$PY_VER")"
printf '    "rustc": %s,\n' "$(tool_field "$RUSTC_PATH" "$RUSTC_VER")"
printf '    "git": %s,\n' "$(tool_field "$GIT_PATH" "$GIT_VER")"
if [ -n "$CODESIGN_PATH" ]; then
    printf '    "codesign": {"present": true, "path": "%s", "version": null, "version_unavailable_reason": "%s", "provenance": "%s"}\n' \
        "$(printf '%s' "$CODESIGN_PATH" | json_escape)" \
        "$(printf '%s' "$CODESIGN_VER_UNAVAILABLE_REASON" | json_escape)" \
        "$(provenance "$CODESIGN_PATH")"
else
    printf '    "codesign": {"present": false, "path": null, "version": null, "version_unavailable_reason": null, "provenance": null}\n'
fi
printf '  },\n'
printf '  "presence": {\n'
printf '    "xcode_select_path": %s,\n' "$(presence_field "$XCODE_SELECT_PATH")"
printf '    "xctrace": {"present": %s, "path": %s, "provenance": %s, "functional_without_xcode": %s, "message": %s},\n' \
    "$( [ -n "$XCTRACE_PATH" ] && echo true || echo false )" \
    "$( [ -n "$XCTRACE_PATH" ] && printf '"%s"' "$(printf '%s' "$XCTRACE_PATH" | json_escape)" || echo null )" \
    "$( [ -n "$XCTRACE_PATH" ] && printf '"%s"' "$(provenance "$XCTRACE_PATH")" || echo null )" \
    "$XCTRACE_FUNCTIONAL" \
    "$( [ -n "$XCTRACE_MESSAGE" ] && printf '"%s"' "$(printf '%s' "$XCTRACE_MESSAGE" | json_escape)" || echo null )"
printf '    "cmake": %s,\n' "$(presence_field "$CMAKE_PATH")"
printf '    "ninja": %s,\n' "$(presence_field "$NINJA_PATH")"
printf '    "qemu_system_x86_64": %s,\n' "$(presence_field "$QEMU_PATH")"
printf '    "brew": {"present": %s, "path": %s, "provenance": %s, "version": %s}\n' \
    "$( [ -n "$BREW_PATH" ] && echo true || echo false )" \
    "$( [ -n "$BREW_PATH" ] && printf '"%s"' "$(printf '%s' "$BREW_PATH" | json_escape)" || echo null )" \
    "$( [ -n "$BREW_PATH" ] && printf '"%s"' "$(provenance "$BREW_PATH")" || echo null )" \
    "$( [ -n "$BREW_PATH" ] && printf '"%s"' "$(printf '%s' "$BREW_VER" | json_escape)" || echo null )"
printf '  },\n'
printf '  "sdk_directory": "%s",\n' "$(printf '%s' "$SDK_DIR" | json_escape)"
printf '  "sdk_directory_listing": [%s],\n' "$SDK_LIST"
printf '  "clt_build": %s,\n' "$( [ -n "$CLT_BUILD" ] && printf '"%s"' "$(printf '%s' "$CLT_BUILD" | json_escape)" || echo null )"
printf '  "path": "%s"\n' "$(printf '%s' "$PATH" | json_escape)"
printf '}\n'
