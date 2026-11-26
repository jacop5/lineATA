"""Identity-checked Linux boot-tty discovery for the Keenetic Linear dongle.

Dependency-free discovery: production defaults read Linux /sys and /dev.
Tests inject temporary trees and never inspect host devices. This module
never issues the boot command or opens a USB handle.
"""
from pathlib import Path

VENDOR_ID = "31b7"      # Keenetic Linear
PRODUCT_BOOT = "0002"   # bootloader/CDC mode that accepts the 'b' boot byte


def _read_hex(directory, name):
    """Return the lowercase hex content of directory/name, or None on any
    read failure (missing file, unreadable, dangling path): fail closed."""
    try:
        return (directory / name).read_text().strip().lower()
    except OSError:
        return None


def _identity_matches(directory):
    """True only if THIS directory contains both idVendor=31b7 and idProduct=0002."""
    vendor = _read_hex(directory, "idVendor")
    product = _read_hex(directory, "idProduct")
    if vendor is None or product is None:
        return False
    return vendor == VENDOR_ID and product == PRODUCT_BOOT


def find_boot_tty(sysfs_root=Path("/sys/class/tty"), dev_root=Path("/dev")):
    """Return the dev node path of the single ttyACM* whose USB ancestor carries
    idVendor=31b7 AND idProduct=0002, or None when zero or multiple candidates
    match (fail closed). The matching attributes must live on the SAME ancestor.
    """
    try:
        entries = sorted(p for p in sysfs_root.iterdir() if p.name.startswith("ttyACM"))
    except OSError:
        return None
    matches = []
    for entry in entries:
        try:
            if not (dev_root / entry.name).exists():
                continue
            resolved = entry.resolve(strict=True)  # dangling symlink -> OSError -> skip
        except OSError:
            continue
        try:
            if any(_identity_matches(ancestor)
                   for ancestor in (resolved, *resolved.parents)):
                matches.append(str(dev_root / entry.name))
        except OSError:
            continue  # metadata disappeared mid-scan: fail closed for this candidate
    if len(matches) == 1:
        return matches[0]
    return None
