import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sony_icd import usb  # noqa: E402


class FakeLib:
    """Stands in for libusb's descriptor calls; devices are {handle: (vid, pid, bus, ports, address)}."""

    def __init__(self, devs):
        self.devs = devs

    def libusb_get_device_descriptor(self, dev, ref):
        vid, pid = self.devs[dev][:2]
        ref._obj.idVendor, ref._obj.idProduct = vid, pid
        return 0

    def libusb_get_bus_number(self, dev):
        return self.devs[dev][2]

    def libusb_get_port_numbers(self, dev, arr, n):
        ports = self.devs[dev][3]
        for i, p in enumerate(ports):
            arr[i] = p
        return len(ports)

    def libusb_get_device_address(self, dev):
        return self.devs[dev][4]


class EnumTests(unittest.TestCase):
    def test_port_path(self):
        self.assertEqual(usb.port_path(1, [4]), "1-4")
        self.assertEqual(usb.port_path(2, [1, 3, 2]), "2-1.3.2")

    def test_find_matches_vid_pid_only_and_ids_include_address(self):
        lib = FakeLib({11: (0x054C, 0x0103, 1, [4], 7), 12: (0x046D, 0xC52B, 1, [5], 3),
                       13: (0x054C, 0x0103, 2, [1, 3], 12)})
        found = usb._find(lib, [11, 12, 13], 0x054C, 0x0103)
        self.assertEqual(found, [(11, "1-4@7"), (13, "2-1.3@12")])

    def test_replug_in_same_socket_gets_new_id(self):
        before = usb._find(FakeLib({1: (0x054C, 0x0103, 1, [4], 7)}), [1], 0x054C, 0x0103)
        after = usb._find(FakeLib({1: (0x054C, 0x0103, 1, [4], 8)}), [1], 0x054C, 0x0103)
        self.assertNotEqual(before[0][1], after[0][1])

    def test_descriptor_layout_matches_libusb(self):
        self.assertEqual(usb.ctypes.sizeof(usb._DeviceDescriptor), 18)
        self.assertEqual(usb._DeviceDescriptor.idVendor.offset, 8)
        self.assertEqual(usb._DeviceDescriptor.idProduct.offset, 10)

    def test_load_libusb_falls_back_to_vendored_dll_on_windows(self):
        calls = []

        def fake_cdll(path):
            calls.append(path)
            raise OSError("not found")

        with mock.patch.object(usb.sys, "platform", "win32"), \
             mock.patch.object(usb.ctypes, "CDLL", side_effect=fake_cdll), \
             self.assertRaises(usb.UsbError):
            usb._load_libusb()
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(usb.__file__)))
        expected = os.path.join(repo_root, "vendor", "libusb-1.0.30", "libusb-1.0.dll")
        self.assertEqual(calls[-1], expected)
        self.assertGreater(len(calls), 1)   # the vendored DLL is a last resort, not the only candidate


if __name__ == "__main__":
    unittest.main()
