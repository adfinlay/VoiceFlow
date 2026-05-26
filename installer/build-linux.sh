#!/bin/bash
# Build Linux release artifacts (tarball + AppImage)
# Run from project root AFTER: pnpm run build
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VERSION=$(node -p "require('$PROJECT_ROOT/package.json').version")
APP_NAME="VoiceFlow"
DIST_DIR="$PROJECT_ROOT/dist"
PYINSTALLER_OUT="$DIST_DIR/$APP_NAME"
OUTPUT_DIR="$DIST_DIR/installer"
ARCH="x86_64"

echo "=== Building Linux release artifacts for $APP_NAME v$VERSION ==="

# Verify PyInstaller output exists
if [ ! -d "$PYINSTALLER_OUT" ]; then
    echo "ERROR: PyInstaller output not found at $PYINSTALLER_OUT"
    echo "Run 'pnpm run build' first."
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

# ============================================================================
# Remove bundled PortAudio and ALSA so the AppImage uses the host's libraries.
# Mirrors .github/workflows/release.yml — keep both in sync.
# ============================================================================
echo ""
echo "--- Removing bundled PortAudio + ALSA ---"
# Ubuntu-bundled PortAudio is ALSA-only (no PipeWire/PulseAudio backend), so
# virtual devices disappear. The system libportaudio knows about both.
rm -f "$PYINSTALLER_OUT/_internal/libportaudio"*
# Bundled libasound has its plugin search path hardcoded to the build host's
# layout (Ubuntu: /usr/lib/x86_64-linux-gnu/alsa-lib; Arch: /usr/lib/alsa-lib).
# Removing it lets the dynamic linker fall through to the user's system
# libasound which knows where its own plugins live. Do NOT touch
# av.libs/libasound-*.so.* — PyAV's libavdevice has that exact filename in
# its NEEDED list (different SONAME, no conflict).
rm -f "$PYINSTALLER_OUT/_internal/libasound"*
echo "Removed bundled PortAudio + ALSA (host libraries will be used at runtime)"

# ============================================================================
# Strip executable-stack marker from bundled .so files.
# python-build-standalone ships libpython with GNU_STACK=RWE, which glibc
# 2.41+ / Linux 6.8+ refuse to load via dlopen — without this step the
# AppImage fails at startup with "cannot enable executable stack as shared
# object requires: Invalid argument". The upstream CI workflow uses the
# `execstack` tool, but Debian 13+ dropped that package, so we use
# `patchelf --clear-execstack` (functionally identical, widely available).
# ============================================================================
echo ""
echo "--- Clearing executable-stack flags ---"
if command -v patchelf >/dev/null 2>&1; then
    CLEAR_CMD='patchelf --clear-execstack'
elif command -v execstack >/dev/null 2>&1; then
    CLEAR_CMD='execstack -c'
else
    echo "ERROR: neither patchelf nor execstack is installed. Install one with:"
    echo "    sudo apt install patchelf     # Debian/Ubuntu/etc."
    echo "    sudo pacman -S patchelf       # Arch"
    echo "    sudo dnf install patchelf     # Fedora"
    exit 1
fi
echo "Using: $CLEAR_CMD"
find "$PYINSTALLER_OUT" -type f -name '*.so*' -exec $CLEAR_CMD {} +
# Verify no RWE markers remain — fail loudly if any slipped through.
BAD=$(find "$PYINSTALLER_OUT" -type f -name '*.so*' -exec sh -c \
    'readelf -l "$1" 2>/dev/null | grep -A1 GNU_STACK | grep -q RWE && echo "$1"' _ {} \;)
if [ -n "$BAD" ]; then
    echo "ERROR: files still have executable stack:"
    echo "$BAD"
    exit 1
fi
echo "All .so files cleared of executable-stack marker"

# ============================================================================
# 1. Tarball (.tar.gz)
# ============================================================================
echo ""
echo "--- Building tarball ---"

TARBALL_NAME="$APP_NAME-$VERSION-linux-$ARCH"
TARBALL_PATH="$OUTPUT_DIR/$TARBALL_NAME.tar.gz"

# Create a staging directory with a nice top-level folder name
STAGING="$DIST_DIR/$TARBALL_NAME"
rm -rf "$STAGING"
cp -a "$PYINSTALLER_OUT" "$STAGING"

# Ensure the main binary is executable
chmod +x "$STAGING/$APP_NAME"

# Create the tarball
(cd "$DIST_DIR" && tar czf "$OUTPUT_DIR/$TARBALL_NAME.tar.gz" "$TARBALL_NAME")
rm -rf "$STAGING"

TARBALL_SIZE=$(du -h "$TARBALL_PATH" | cut -f1)
echo "Created: $TARBALL_PATH ($TARBALL_SIZE)"

# ============================================================================
# 2. AppImage
# ============================================================================
echo ""
echo "--- Building AppImage ---"

APPIMAGE_NAME="$APP_NAME-$VERSION-$ARCH.AppImage"
APPIMAGE_PATH="$OUTPUT_DIR/$APPIMAGE_NAME"

# Check for appimagetool
APPIMAGETOOL=""
if command -v appimagetool &>/dev/null; then
    APPIMAGETOOL="appimagetool"
elif [ -x "$DIST_DIR/appimagetool" ]; then
    APPIMAGETOOL="$DIST_DIR/appimagetool"
else
    echo "appimagetool not found. Downloading..."
    TOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
    curl -fSL "$TOOL_URL" -o "$DIST_DIR/appimagetool"
    chmod +x "$DIST_DIR/appimagetool"
    APPIMAGETOOL="$DIST_DIR/appimagetool"
    echo "Downloaded appimagetool"
fi

# Build AppDir structure
APPDIR="$DIST_DIR/$APP_NAME.AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"

# Copy PyInstaller output into AppDir root
cp -a "$PYINSTALLER_OUT/." "$APPDIR/"

# Copy AppRun and .desktop
cp "$SCRIPT_DIR/AppRun" "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"
cp "$SCRIPT_DIR/VoiceFlow.desktop" "$APPDIR/VoiceFlow.desktop"

# Copy icon (AppImage needs it at root and in hicolor)
cp "$PROJECT_ROOT/src-pyloid/icons/icon.png" "$APPDIR/voiceflow.png"
cp "$PROJECT_ROOT/src-pyloid/icons/icon.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/voiceflow.png"

# Ensure main binary is executable
chmod +x "$APPDIR/$APP_NAME"

# Build the AppImage
ARCH="$ARCH" "$APPIMAGETOOL" "$APPDIR" "$APPIMAGE_PATH"
rm -rf "$APPDIR"

APPIMAGE_SIZE=$(du -h "$APPIMAGE_PATH" | cut -f1)
echo "Created: $APPIMAGE_PATH ($APPIMAGE_SIZE)"

# ============================================================================
# Summary
# ============================================================================
echo ""
echo "=== Linux build complete ==="
echo "  Tarball:  $TARBALL_PATH ($TARBALL_SIZE)"
echo "  AppImage: $APPIMAGE_PATH ($APPIMAGE_SIZE)"
echo ""
echo "To test the AppImage:"
echo "  chmod +x $APPIMAGE_PATH && $APPIMAGE_PATH"
