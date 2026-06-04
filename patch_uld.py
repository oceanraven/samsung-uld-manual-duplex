#!/usr/bin/env python3
"""
patch_uld.py
Applies the manual duplex patch to an extracted Samsung ULD (Unified Linux Driver) directory.
"""

import os
import sys
import glob
import shutil


def patch_ppd_file(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

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

    # Ensure file is writable
    if not os.access(filepath, os.W_OK):
        import stat

        os.chmod(filepath, os.stat(filepath).st_mode | stat.S_IWUSR)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    return True


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path_to_uld_directory>")
        sys.exit(1)

    target_dir = sys.argv[1]

    if not os.path.isdir(target_dir):
        print(f"Error: {target_dir} is not a directory")
        sys.exit(1)

    print(f"Patching Samsung ULD in: {target_dir}")

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
    script_dir = os.path.dirname(os.path.realpath(__file__))
    wrapper_path = os.path.join(script_dir, "rastertospl-wrapper.py")

    if not os.path.isfile(wrapper_path):
        print(f"Error: Could not find {wrapper_path}")
        sys.exit(1)

    for arch in ["x86_64", "i386", "arm"]:
        arch_dir = os.path.join(target_dir, arch)
        if not os.path.isdir(arch_dir):
            continue

        if not os.access(arch_dir, os.W_OK):
            import stat

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
    main()

