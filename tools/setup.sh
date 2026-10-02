#!/usr/bin/env bash
# One-time, idempotent toolchain setup for the C64 harness.
#   - KickAssembler 5.25          -> tools/kickass/KickAss.jar
#   - VICE 3.10 x64sc (headless)  -> tools/vice/bin/x64sc  (+ ROMs/palettes in tools/vice/share/vice)
# Nothing is installed system-wide.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOLS="$ROOT/tools"
SRC="$TOOLS/src"
PREFIX="$TOOLS/vice"
JOBS="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"

VICE_VER=3.10
VICE_TGZ="vice-$VICE_VER.tar.gz"
VICE_URL="https://downloads.sourceforge.net/project/vice-emu/releases/$VICE_TGZ"
VICE_SHA256=8e5bac18cbcb9f192380ad3ef881f8790f5b75c41d7b3da65d831985d864d6d1
KICKASS_URL="http://theweb.dk/KickAssembler/KickAssembler.zip"

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

# --- KickAssembler -----------------------------------------------------------
if [ ! -f "$TOOLS/kickass/KickAss.jar" ]; then
    say "Downloading KickAssembler"
    mkdir -p "$TOOLS/kickass"
    curl -fsSL -o "$TOOLS/kickass/KickAssembler.zip" "$KICKASS_URL"
    (cd "$TOOLS/kickass" && unzip -o -q KickAssembler.zip KickAss.jar KickAss.cfg KickAssembler.pdf && rm KickAssembler.zip)
fi
KA_BANNER="$(java -jar "$TOOLS/kickass/KickAss.jar" 2>&1 || true)"   # exits non-zero without a source file
grep -m1 "Kick Assembler v" <<< "$KA_BANNER" || { echo "KickAssembler failed to run" >&2; exit 1; }

# --- VICE (headless x64sc) ---------------------------------------------------
if [ ! -x "$PREFIX/bin/x64sc" ]; then
    mkdir -p "$SRC"
    cd "$SRC"
    if [ ! -f "$VICE_TGZ" ]; then
        say "Downloading VICE $VICE_VER"
        curl -fsSL -o "$VICE_TGZ" "$VICE_URL"
    fi
    echo "$VICE_SHA256  $VICE_TGZ" | shasum -a 256 -c -
    rm -rf "vice-$VICE_VER"
    tar xzf "$VICE_TGZ"          # plain tar keeps timestamps -> no autotools/bison regeneration
    cd "vice-$VICE_VER"

    say "Applying upstream fixes (r46032, r46240, r46020)"
    # r46032: _NSGetExecutablePath needs <mach-o/dyld.h> on newer macOS SDKs.
    perl -0pi -e 's|#include <limits.h>\n|#include <limits.h>\n#include <mach-o/dyld.h>\n|' src/arch/shared/macOS-launcher.c
    # r46240 (bug #2259): segfault when stdout is not a terminal and file logging is off.
    perl -pi -e 's/if \(\(log_to_file\) \|\| \(!log_colorize\)\) \{/if ((log_to_file) || (archdep_default_logger_is_terminal() == 0) || (!log_colorize)) {/' src/log.c
    # r46020: binary monitor Display Get under-reports its length by 4 (drops 4 pixels, writes past buffer).
    perl -pi -e 's/response_length = 4 \+ info_length \+ buffer_length;/response_length = (4 + 4) + info_length + buffer_length;/' src/monitor/monitor_binary.c
    grep -q 'mach-o/dyld.h' src/arch/shared/macOS-launcher.c
    grep -q 'is_terminal() == 0) ||' src/log.c
    grep -q '(4 + 4) + info_length' src/monitor/monitor_binary.c

    say "Configuring (headless UI)"
    # makeinfo/dos2unix/xa are only presence-checked; the tarball ships everything they would generate.
    MAKEINFO=true DOS2UNIX=true XA=true ./configure --prefix="$PREFIX" \
        --enable-headlessui --disable-html-docs --disable-pdf-docs \
        --disable-realdevice --without-libcurl --disable-openmp > "$SRC/configure.log" 2>&1 \
        || { tail -30 "$SRC/configure.log"; exit 1; }

    say "Building x64sc ($JOBS jobs)"
    # The x64sc target does not order the bundled libs (linenoise-ng, ...) before linking; build them first.
    (make -j"$JOBS" -C src/lib > "$SRC/build.log" 2>&1 && make -j"$JOBS" x64sc >> "$SRC/build.log" 2>&1) || {
        say "Parallel build failed, retrying serially"
        (make -C src/lib && make x64sc) >> "$SRC/build.log" 2>&1 || { tail -40 "$SRC/build.log"; exit 1; }
    }

    say "Installing into $PREFIX"
    make -C data install > "$SRC/install.log" 2>&1 || { tail -30 "$SRC/install.log"; exit 1; }
    install -d "$PREFIX/bin"
    install -m 755 src/x64sc "$PREFIX/bin/x64sc"
    cd "$SRC" && rm -rf "vice-$VICE_VER"     # ~140 MB of objects; a rebuild re-extracts the tarball anyway
fi

# --- smoke test ----------------------------------------------------------------
say "Smoke test"
SMOKE="$(mktemp -d)"
set +e
"$PREFIX/bin/x64sc" -default -sounddev dummy +sound +logcolorize -logfile "$SMOKE/vice.log" \
    -drive8type 0 -limitcycles 3000000 -exitscreenshot "$SMOKE/boot.png" > /dev/null 2>&1
rc=$?
set -e
if [ "$rc" -ne 1 ] || [ ! -s "$SMOKE/boot.png" ]; then
    echo "x64sc smoke test failed (exit $rc); log: $SMOKE/vice.log" >&2
    exit 1
fi
rm -rf "$SMOKE"
echo "x64sc OK: $PREFIX/bin/x64sc"
say "Setup complete"
