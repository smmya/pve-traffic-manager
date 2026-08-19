import unittest
from unittest import mock

import manager


class ManagerTestCase(unittest.TestCase):
    def test_range_can_use_stale_group_records(self):
        records = [
            {"vm_id": 101, "vm_type": "qemu"},
            {"vm_id": 102, "vm_type": "lxc"},
        ]
        with mock.patch("builtins.input", return_value="101-102"):
            selected = manager.input_vm_range("prompt", records)
        self.assertEqual(selected, [(101, "qemu"), (102, "lxc")])

    def test_huge_range_only_iterates_available_vms(self):
        records = [{"vm_id": 100, "vm_type": "qemu"}]
        with mock.patch("builtins.input", return_value="1-1000000000"):
            selected = manager.input_vm_range("prompt", records)
        self.assertEqual(selected, [(100, "qemu")])

    def test_number_rejects_non_finite_values(self):
        with mock.patch("builtins.input", side_effect=["nan", "2.5"]):
            self.assertEqual(manager.input_number("prompt"), 2.5)

    def test_crontab_status_prefers_runnable_entry_over_marker_comment(self):
        output = "# pve-traffic-manager monitor\n*/5 * * * * python monitor.py # pve-traffic-manager monitor"
        with mock.patch.object(manager, "_read_crontab", return_value=(True, output, "")):
            installed, entry = manager._crontab_status()
        self.assertTrue(installed)
        self.assertTrue(entry.startswith("*/5"))

    def test_crontab_marker_comment_alone_is_not_installed(self):
        with mock.patch.object(
            manager, "_read_crontab",
            return_value=(True, "# pve-traffic-manager monitor", "")
        ):
            self.assertEqual(manager._crontab_status(), (False, ""))

    def test_no_existing_crontab_is_not_treated_as_read_error(self):
        result = mock.Mock(returncode=1, stdout="", stderr="no crontab for root")
        with mock.patch.object(manager.subprocess, "run", return_value=result):
            self.assertEqual(manager._read_crontab(), (True, "", ""))


if __name__ == "__main__":
    unittest.main()
