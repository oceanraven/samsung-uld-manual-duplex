#!/usr/bin/env python3
"""
patch_uld.py
Applies the manual duplex patch to an extracted Samsung ULD (Unified Linux Driver) directory.
"""

import glob
import os
from pathlib import Path
import re
import shutil
import stat
import sys


def write_ppd_file(filepath, content):
    """Preserve legacy PPD encodings and allow patching read-only archives."""
    path = Path(filepath)
    if not os.access(path, os.W_OK):
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
    path.write_text(content, encoding="latin-1")


def patch_ppd_file(filepath):
    with open(filepath, "r", encoding="latin-1") as f:
        content = f.read()

    if not re.search(r'^\*cupsFilter:.*\brastertospl"?\s*$', content, re.MULTILINE):
        return False

    # Upgrade our previous long-edge-only UI; preserve native duplex PPDs.
    long_option = '*Duplex DuplexNoTumble/Long Edge (Standard): ""'
    short_option = '*Duplex DuplexTumble/Short Edge (Flip): ""'
    if long_option in content and short_option not in content:
        content = content.replace(long_option, long_option + "\n" + short_option)
        write_ppd_file(filepath, content)
        return True

    if "*OpenUI *Duplex" in content or "*OpenUI *sides" in content:
        # Already has duplex options, or was already patched
        return False

    # Standard PPD additions for manual duplex
    duplex_options = """
*OpenUI *Duplex/Double-Sided Printing: PickOne
*OrderDependency: 50 AnySetup *Duplex
*DefaultDuplex: None
*Duplex None/Off (One-Sided): ""
*Duplex DuplexNoTumble/Long Edge (Standard): ""
*Duplex DuplexTumble/Short Edge (Flip): ""
*CloseUI: *Duplex
"""

    # Insert after *LanguageVersion
    if "*LanguageVersion" in content:
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("*LanguageVersion"):
                lines.insert(i + 1, duplex_options.strip("\n"))
                break
        content = "\n".join(lines)
    else:
        # Fallback if no LanguageVersion
        content += "\n" + duplex_options

    write_ppd_file(filepath, content)

    return True


def patch_installer(target_dir):
    """Make the vendor installer copy and track the original filter too."""
    path = Path(target_dir) / "noarch" / "printer.pkg"
    content = path.read_text()
    if re.search(r'^\s*install_(?:lns_)?p .*?/rastertospl-real"', content, re.MULTILINE):
        return
    pattern = (
        r'^(?P<indent>[ \t]*)install_lns_p '
        r'"\$\{DIST_DIR\}/\$\{ARCH_SUBDIR\}/rastertospl" '
        r'"\$INSTDIR_COMMON_PRINTER_BIN" "\$INSTDIR_CUPS_FILTERS"[ \t]*$'
    )
    match = re.search(pattern, content, re.MULTILINE)
    if not match:
        raise ValueError(f"Unsupported installer layout: {path}")
    line = (
        match.group('indent')
        + 'install_p "${DIST_DIR}/${ARCH_SUBDIR}/rastertospl-real" '
        '"$INSTDIR_COMMON_PRINTER_BIN"\n'
    )
    path.write_text(content[:match.start()] + line + content[match.start():])


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path_to_uld_directory>")
        sys.exit(1)

    target_dir = sys.argv[1]

    if not os.path.isdir(target_dir):
        print(f"Error: {target_dir} is not a directory")
        sys.exit(1)

    print(f"Patching Samsung ULD in: {target_dir}")

    script_dir = os.path.dirname(os.path.realpath(__file__))
    wrapper_path = os.path.join(script_dir, "rastertospl-wrapper.py")
    if not os.path.isfile(wrapper_path):
        raise ValueError(f"Missing wrapper: {wrapper_path}")
    binaries = [Path(target_dir) / arch / name
                for arch in ("x86_64", "i386", "arm")
                for name in ("rastertospl", "rastertospl-real")]
    if not any(p.is_file() for p in binaries):
        raise ValueError("No supported rastertospl binaries found")
    patch_installer(target_dir)

    # 1. Patch PPDs
    ppd_dir = os.path.join(target_dir, "noarch", "share", "ppd")
    if not os.path.isdir(ppd_dir):
        print(f"Warning: PPD directory not found at {ppd_dir}")
    else:
        ppds = glob.glob(os.path.join(ppd_dir, "*.ppd"))
        patched_count = 0
        for ppd in ppds:
            if patch_ppd_file(ppd):
                patched_count += 1
        print(f"Patched {patched_count} / {len(ppds)} PPD files with Duplex options.")

    # 2. Patch binaries
    for arch in ["x86_64", "i386", "arm"]:
        arch_dir = os.path.join(target_dir, arch)
        if not os.path.isdir(arch_dir):
            continue

        if not os.access(arch_dir, os.W_OK):
            os.chmod(arch_dir, os.stat(arch_dir).st_mode | stat.S_IWUSR)

        raster_bin = os.path.join(arch_dir, "rastertospl")
        raster_real = os.path.join(arch_dir, "rastertospl-real")

        if os.path.isfile(raster_bin) and not os.path.isfile(raster_real):
            # Rename original binary
            os.rename(raster_bin, raster_real)
            print(f"Renamed original binary to rastertospl-real in {arch}/")

        if os.path.isfile(raster_real):
            # Install wrapper
            shutil.copy2(wrapper_path, raster_bin)
            os.chmod(raster_bin, 0o755)
            print(f"Installed manual duplex wrapper in {arch}/")

    print("Patching complete! You can now install the driver using:")
    print(f"sudo {os.path.join(target_dir, 'install.sh')}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

