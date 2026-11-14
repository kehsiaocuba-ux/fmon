#!/usr/bin/env bash
# Builds the Linux executables (PyInstaller) and a tar.gz from this checkout.
#
#   ./packaging/linux/build.sh
#
# Needs: python3 (3.9+) with the venv module, python3-tk (for the GUI) and
# objdump (binutils). On Debian/Ubuntu: sudo apt install python3-venv python3-tk binutils
#
# Produces dist/fmon-<version>-linux-<arch>.tar.gz. PyInstaller cannot
# cross-compile, so this must run on Linux, on the oldest system you want
# the result to run on (the glibc version of the build machine is the minimum).
set -euo pipefail

if [ "$(uname -s)" != "Linux" ]; then
    echo "This script builds the Linux package and must run on Linux." >&2
    exit 1
fi

cd "$(dirname "$0")/../.."

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -e . pyinstaller

version=$(python -c "import sys; sys.path.insert(0, 'src'); import fmon; print(fmon.__version__)")
arch=$(uname -m)
name="fmon-${version}-linux-${arch}"

rm -rf build dist
python -m PyInstaller --noconfirm --clean packaging/windows/fmon.spec

echo "Smoke test:"
./dist/fmon/fmon --version

glibc=$( (for f in dist/fmon/fmon dist/fmon/fmon-gui $(find dist/fmon/_internal -type f \( -name '*.so' -o -name '*.so.*' \)); do
              objdump -T "$f" 2>/dev/null | grep -o 'GLIBC_[0-9.]*' || true
          done) | sed 's/GLIBC_//' | sort -V -u | tail -1 || true)
[ -n "$glibc" ] || glibc="unknown"

stage="dist/${name}"
mkdir -p "$stage"
cp -a dist/fmon/. "$stage"/
cp LICENSE fmon.ini.example packaging/linux/fmon.desktop packaging/linux/fmon-watch.service "$stage"/

cat > "$stage/README.txt" <<'EOF'
fmon @VERSION@ for Linux (@ARCH@)

Run it from this folder, nothing to install:

    ./fmon --version
    ./fmon            # the complete manual
    ./fmon-gui        # the graphical application (needs a desktop session)

Keep fmon, fmon-gui and the _internal folder together.

Optional per-user install:

    mkdir -p ~/.local/opt ~/.local/bin ~/.local/share/applications
    cp -r . ~/.local/opt/fmon
    ln -sf ~/.local/opt/fmon/fmon ~/.local/bin/fmon
    ln -sf ~/.local/opt/fmon/fmon-gui ~/.local/bin/fmon-gui
    cp fmon.desktop ~/.local/share/applications/

To remove it, delete those links and files and the folder ~/.local/opt/fmon.

Compatibility: the newest system library this build needs is glibc @GLIBC@,
so it runs on Linux systems with glibc @GLIBC@ or newer (for example Ubuntu
24.04). On older systems run fmon from source instead: clone the repository
and run pip install . (Python 3.9+).

License: MIT, see LICENSE. Source: https://github.com/kehsiaocuba-ux/fmon
Verify the download against checksums.txt: sha256sum -c checksums.txt
EOF
sed -i -e "s/@VERSION@/${version}/g" -e "s/@ARCH@/${arch}/g" -e "s/@GLIBC@/${glibc}/g" "$stage/README.txt"

tar -C dist --owner=0 --group=0 -czf "dist/${name}.tar.gz" "$name"

echo "Package: dist/${name}.tar.gz (needs glibc ${glibc} or newer)"
sha256sum "dist/${name}.tar.gz"
