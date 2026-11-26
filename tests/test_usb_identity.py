"""Offline tests for bridge/usb_identity.py using temporary sysfs/dev fixtures.

The live /sys and /dev are never inspected: every call passes tmp_path trees.
"""
import inspect
from pathlib import Path

import pytest

from bridge.usb_identity import find_boot_tty


def device_dir(sysfs, name):
    """The fake USB device directory backing tty `name` (per-name, unique)."""
    return sysfs.parents[2] / "devices" / f"dev-{name}"


def make_tty(sysfs, dev, name, vendor="31b7", product="0002",
             node=True, split=False):
    """Build a fake USB device tree and a ttyACM symlink for `name`.

    Layout: <tmp>/devices/dev-<name>          (idVendor/idProduct)
                       /dev-<name>:1.0/tty/<name>
    sysfs/<name> -> .../tty/<name>; dev/<name> created unless node=False.
    split=True puts idProduct on the interface ancestor instead of the device,
    so no single ancestor holds both attributes.
    """
    device = device_dir(sysfs, name)
    interface = device / f"dev-{name}:1.0"
    tty_dir = interface / "tty" / name
    tty_dir.mkdir(parents=True, exist_ok=True)
    (device / "idVendor").write_text(vendor + "\n")
    if split:
        (interface / "idProduct").write_text(product + "\n")
    else:
        (device / "idProduct").write_text(product + "\n")
    (sysfs / name).symlink_to(tty_dir)
    if node:
        (dev / name).touch()
    return dev / name


@pytest.fixture
def trees(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "tty"
    dev = tmp_path / "dev"
    sysfs.mkdir(parents=True)
    dev.mkdir()
    return sysfs, dev


def test_single_matching_tty_returns_its_dev_path(trees):
    sysfs, dev = trees
    expected = make_tty(sysfs, dev, "ttyACM0")
    assert find_boot_tty(sysfs, dev) == str(expected)


def test_unrelated_first_sorted_tty_is_not_chosen(trees):
    sysfs, dev = trees
    make_tty(sysfs, dev, "ttyACM0", vendor="1234", product="5678")  # sorts first
    correct = make_tty(sysfs, dev, "ttyACM1")
    assert find_boot_tty(sysfs, dev) == str(correct)


def test_wrong_product_or_vendor_fails_closed(trees):
    sysfs, dev = trees
    make_tty(sysfs, dev, "ttyACM0", product="0001")  # service mode, not boot
    assert find_boot_tty(sysfs, dev) is None
    sysfs2, dev2 = trees
    make_tty(sysfs2, dev2, "ttyACM1", vendor="1d6b")  # some other vendor
    assert find_boot_tty(sysfs2, dev2) is None


def test_split_ancestor_attributes_do_not_match(trees):
    sysfs, dev = trees
    make_tty(sysfs, dev, "ttyACM0", split=True)  # idVendor and idProduct on different ancestors
    assert find_boot_tty(sysfs, dev) is None


def test_two_matching_candidates_fail_closed(trees):
    sysfs, dev = trees
    make_tty(sysfs, dev, "ttyACM0")
    make_tty(sysfs, dev, "ttyACM1")
    assert find_boot_tty(sysfs, dev) is None


def test_absent_dev_node_excludes_candidate(trees):
    sysfs, dev = trees
    make_tty(sysfs, dev, "ttyACM0", node=False)  # sysfs entry without /dev node
    assert find_boot_tty(sysfs, dev) is None


def test_missing_or_unreadable_metadata_fails_closed(trees):
    sysfs, dev = trees
    sysfs2, dev2 = trees
    # missing idVendor entirely
    make_tty(sysfs, dev, "ttyACM0")
    (device_dir(sysfs, "ttyACM0") / "idVendor").unlink()
    assert find_boot_tty(sysfs, dev) is None
    # unreadable idVendor: a directory where the hex file should be
    make_tty(sysfs2, dev2, "ttyACM1")
    idv = device_dir(sysfs2, "ttyACM1") / "idVendor"
    idv.unlink()
    idv.mkdir()
    assert find_boot_tty(sysfs2, dev2) is None


def test_dangling_symlink_fails_closed(trees):
    sysfs, dev = trees
    (sysfs / "ttyACM0").symlink_to(sysfs / "nonexistent-target")
    (dev / "ttyACM0").touch()
    assert find_boot_tty(sysfs, dev) is None


def test_no_candidates_returns_none(trees):
    sysfs, dev = trees
    assert find_boot_tty(sysfs, dev) is None
    assert find_boot_tty(sysfs / "does-not-exist", dev) is None


def test_defaults_target_linux_sysfs_and_dev():
    sig = inspect.signature(find_boot_tty)
    assert sig.parameters["sysfs_root"].default == Path("/sys/class/tty")
    assert sig.parameters["dev_root"].default == Path("/dev")
