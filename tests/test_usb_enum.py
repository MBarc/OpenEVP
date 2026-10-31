import contextlib
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


class OpenLib:
    """Stands in for libusb's open/claim calls, failing them with the given codes."""

    NAMES = {-1: b"LIBUSB_ERROR_IO", -3: b"LIBUSB_ERROR_ACCESS", -6: b"LIBUSB_ERROR_BUSY",
             -12: b"LIBUSB_ERROR_NOT_SUPPORTED"}

    def __init__(self, open_rc=0, claim_rc=0):
        self.open_rc, self.claim_rc = open_rc, claim_rc
        self.opened, self.exited = [], False

    def libusb_init(self, ref):
        return 0

    def libusb_exit(self, ctx):
        self.exited = True

    def libusb_open(self, dev, ref):
        self.opened.append(dev)
        return self.open_rc

    def libusb_error_name(self, rc):
        return self.NAMES[rc]

    def libusb_set_auto_detach_kernel_driver(self, h, on):
        pass

    def libusb_claim_interface(self, h, interface):
        return self.claim_rc

    def libusb_release_interface(self, h, interface):
        pass

    def libusb_close(self, h):
        pass


class OpenTests(unittest.TestCase):
    """Why an open failed reaches the caller: a recorder another program (often a
    second OpenEVP window) has open is DeviceBusy, not a bare LIBUSB_ERROR_ACCESS."""

    def device(self, lib, device_id="1-4@7", platform="win32", attached=((21, "1-4@7"),)):
        @contextlib.contextmanager
        def device_list(_lib, _ctx):
            yield [dev for dev, _ in attached]

        with mock.patch.object(usb, "_libusb", return_value=lib), \
             mock.patch.object(usb, "_device_list", device_list), \
             mock.patch.object(usb, "_find", return_value=list(attached)), \
             mock.patch.object(usb.sys, "platform", platform):
            return usb.Device(0x054C, 0x0103, device_id)

    def test_access_denied_on_windows_is_busy(self):
        lib = OpenLib(open_rc=-3)
        with self.assertRaises(usb.DeviceBusy) as caught:
            self.device(lib)
        self.assertIn("port 1-4", str(caught.exception))
        self.assertIn("another OpenEVP window", str(caught.exception))
        self.assertIn("LIBUSB_ERROR_ACCESS", str(caught.exception))
        self.assertTrue(lib.exited)

    def test_access_denied_elsewhere_is_a_plain_error(self):
        with self.assertRaises(usb.UsbError) as caught:
            self.device(OpenLib(open_rc=-3), platform="linux")
        self.assertNotIsInstance(caught.exception, usb.DeviceBusy)   # permissions, not another program

    def test_busy_open_or_claim_is_busy(self):
        for lib in (OpenLib(open_rc=-6), OpenLib(claim_rc=-6), OpenLib(claim_rc=-3)):
            with self.subTest(open_rc=lib.open_rc, claim_rc=lib.claim_rc), self.assertRaises(usb.DeviceBusy):
                self.device(lib)

    def test_other_failures_are_unchanged(self):
        with self.assertRaises(usb.DriverMissing):
            self.device(OpenLib(open_rc=-12))
        with self.assertRaises(usb.UsbError) as caught:
            self.device(OpenLib(open_rc=-1))
        self.assertNotIsInstance(caught.exception, usb.DeviceBusy)

    def test_no_id_opens_the_first_attached_recorder_by_id(self):
        lib = OpenLib(open_rc=-3)
        with self.assertRaises(usb.DeviceBusy) as caught:            # the reason is not lost
            self.device(lib, device_id=None, attached=((21, "1-4@7"), (22, "2-1@3")))
        self.assertEqual(lib.opened, [21])
        self.assertIn("port 1-4", str(caught.exception))

    def test_no_id_and_none_attached(self):
        lib = OpenLib()
        with self.assertRaises(usb.UsbError) as caught:
            self.device(lib, device_id=None, attached=())
        self.assertIn("not found", str(caught.exception))
        self.assertEqual(lib.opened, [])
        self.assertTrue(lib.exited)


if __name__ == "__main__":
    unittest.main()
