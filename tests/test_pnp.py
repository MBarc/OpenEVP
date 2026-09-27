import ctypes
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from openevp import pnp  # noqa: E402
from openevp.pnp import hardware_id, instances_of, needs_setup, present_instances  # noqa: E402

A = r"USB\VID_054C&PID_0103\5&38E97A59&0&7"
B = r"USB\VID_054C&PID_0103\5&38E97A59&0&3"
X = r"USB\VID_1234&PID_00AB\SERIAL-1"          # a second (made-up) WinUSB model
MOUSE = r"USB\VID_046D&PID_C52B\6&2A&0&5"


def windows_cannot_say(instance_id):
    raise pnp.PnpError("CR 0xd")


class FakeCfgmgr32:
    """cfgmgr32 as pnp.driver_of calls it, over a made-up device tree:
    {instance id: (status, problem, service)}; service None = no driver
    (CR_NO_SUCH_VALUE), an int = that CR from the property read."""
    DEVNODE_BASE = 100

    def __init__(self, tree):
        self.tree, self.order = tree, list(tree)

    @staticmethod
    def _set(ref, value):
        ref._obj.value = value

    def CM_Locate_DevNodeW(self, devinst, instance_id, flags):
        if instance_id not in self.tree:
            return 0x0D                                  # CR_NO_SUCH_DEVNODE
        self._set(devinst, self.DEVNODE_BASE + self.order.index(instance_id))
        return 0

    def CM_Get_DevNode_Status(self, status, problem, devinst, flags):
        st, prob, _service = self.tree[self.order[devinst - self.DEVNODE_BASE]]
        self._set(status, st)
        self._set(problem, prob)
        return 0

    def CM_Get_DevNode_Registry_PropertyW(self, devinst, prop, regtype, buf, size, flags):
        assert (prop, regtype) == (pnp.CM_DRP_SERVICE, None)
        service = self.tree[self.order[devinst - self.DEVNODE_BASE]][2]
        if service is None:
            return pnp.CR_NO_SUCH_VALUE
        if isinstance(service, int):
            return service
        ctypes.memmove(buf, ctypes.create_unicode_buffer(service), (len(service) + 1) * 2)
        return 0


class PerInstanceTests(unittest.TestCase):
    """A6: which recorder lacks the driver is read per Windows instance, not guessed from counts."""
    OK = (0x0180200A, 0, "WinUSB")                      # started, no problem
    NO_DRIVER = (0x01802400, pnp.CM_PROB_FAILED_INSTALL, None)

    def tree(self, tree):
        patchers = [mock.patch.object(pnp.sys, "platform", "win32"),
                    mock.patch.object(pnp, "_cfgmgr32", return_value=FakeCfgmgr32(tree))]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_the_recorder_without_the_driver_gets_the_placeholder(self):
        """Recorder A has no driver, B works: counting (2 present, 1 usable) would
        have picked B, the first sorted one."""
        self.tree({A: self.NO_DRIVER, B: self.OK})
        self.assertEqual(sorted([A, B])[0], B)
        self.assertEqual(needs_setup([A, B], 1), ["setup:" + A])
        self.assertEqual(pnp.driver_of(B), ("WinUSB", 0))
        self.assertEqual(pnp.driver_of(A), ("", pnp.CM_PROB_FAILED_INSTALL))

    def test_every_instance_is_judged_on_its_own(self):
        self.tree({A: (0x01802400, 0, "usbccgp"),                          # another driver
                   B: (0x01802400, pnp.CM_PROB_FAILED_INSTALL, "WinUSB"),  # WinUSB half-installed
                   X: (0x0180200A, 0, "winusb")})                          # case does not matter
        self.assertEqual(needs_setup([A, B, X], 0), ["setup:" + B, "setup:" + A])
        self.assertEqual(needs_setup([X], 0), [])
        self.assertEqual(needs_setup([], 0), [])

    def test_every_driver_libusb_can_open_is_usable(self):
        """R11: WinUSB, libusbK and libusb0 (as Windows names their services;
        e.g. bound by Zadig) are all usable, as v0.7.2's count treated them; only
        a recorder without a driver gets the placeholder."""
        L0 = r"USB\VID_054C&PID_0103\5&38E97A59&0&9"
        self.tree({A: (0x0180200A, 0, "WinUSB"), B: (0x0180200A, 0, "libusbK"),
                   L0: (0x0180200A, 0, "libusb0"), X: self.NO_DRIVER})
        self.assertEqual(needs_setup([A, B, L0, X], 3), ["setup:" + X])
        for service in ("WinUSB", "libusbK", "libusb0", "WINUSB", "LIBUSBK", "LibUsb0"):
            self.assertFalse(pnp.lacks_winusb(service, 0), service)
        for service in ("", None, "usbccgp", "HidUsb", "libusb", "libusbK2", "WinUSB "):
            self.assertTrue(pnp.lacks_winusb(service, 0), service)

    def test_any_problem_code_needs_setup(self):
        for service in ("WinUSB", "libusbK", "libusb0"):
            for problem in (1, 10, 28, 43):
                self.assertTrue(pnp.lacks_winusb(service, problem), (service, problem))

    def test_the_problem_code_counts_only_with_dn_has_problem(self):
        self.tree({A: (0x0180200A, pnp.CM_PROB_FAILED_INSTALL, "WinUSB")})
        self.assertEqual(pnp.driver_of(A), ("WinUSB", 0))
        self.assertEqual(needs_setup([A], 0), [])

    def test_an_api_error_falls_back_to_counting(self):
        for tree in ({B: self.OK},                                    # A cannot be located
                     {A: (0x0180200A, 0, 0x1F), B: self.OK}):          # the property read fails
            with self.subTest(tree=tree):
                self.tree(tree)
                self.assertEqual(needs_setup([A, B], 1), ["setup:" + B])   # today's count behaviour
                self.assertEqual(needs_setup([A, B], 2), [])

    def test_off_windows_it_counts(self):
        with mock.patch.object(pnp.sys, "platform", "linux"):
            with self.assertRaises(pnp.PnpError):
                pnp.driver_of(A)
            self.assertEqual(needs_setup([A], 0), ["setup:" + A])


class NeedsSetupTests(unittest.TestCase):
    """The count fallback, when Windows cannot say which instance lacks the driver."""

    def setUp(self):
        patcher = mock.patch.object(pnp, "driver_of", windows_cannot_say)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_all_usable_means_nothing_to_set_up(self):
        self.assertEqual(needs_setup([A], 1), [])
        self.assertEqual(needs_setup([], 0), [])

    def test_recorder_without_driver_is_listed(self):
        self.assertEqual(needs_setup([A], 0), ["setup:" + A])
        self.assertEqual(len(needs_setup([A, B], 1)), 1)


class MatchingTests(unittest.TestCase):
    ST25 = ((0x054C, 0x0103),)
    OTHER = ((0x1234, 0x00AB), (0x1234, 0x00AC))

    def test_hardware_id_is_windows_spelling(self):
        self.assertEqual(hardware_id(0x054C, 0x0103), r"USB\VID_054C&PID_0103")
        self.assertEqual(hardware_id(0x1, 0xabcd), r"USB\VID_0001&PID_ABCD")

    def test_only_the_models_own_devices_match(self):
        present = [A, X, MOUSE, r"USB\VID_054C&PID_0103&MI_00\7&1", r"USB\VID_054C&PID_01031\Z",
                   r"usb\vid_054c&pid_0103\lower"]
        self.assertEqual(instances_of(self.ST25, present), [A, r"usb\vid_054c&pid_0103\lower"])
        self.assertEqual(instances_of(self.OTHER, present), [X])
        self.assertEqual(instances_of((), present), [])

    def test_needs_driver_is_decided_per_model(self):
        """Two models, one PC: the ST25 has its driver (1 usable of 1), the other
        model has none (0 of 1). Each model subtracts only its own usable count,
        so the other model's recorder is reported and the ST25 is not; pooling
        both (2 present - 1 usable) would have reported the wrong one or none."""
        with mock.patch.object(pnp, "_usb_instances", return_value=[A, X, MOUSE]), \
                mock.patch.object(pnp, "driver_of", windows_cannot_say):
            st25 = needs_setup(present_instances(self.ST25), 1)
            other = needs_setup(present_instances(self.OTHER), 0)
        self.assertEqual((st25, other), ([], ["setup:" + X]))

    def test_off_windows_nothing_is_present(self):
        with mock.patch.object(pnp.sys, "platform", "linux"):
            self.assertEqual(present_instances(self.ST25), [])


if __name__ == "__main__":
    unittest.main()
