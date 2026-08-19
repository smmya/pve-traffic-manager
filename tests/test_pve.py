import unittest
from unittest import mock

import pve


class PveTestCase(unittest.TestCase):
    def test_boot_time_is_derived_from_uptime(self):
        self.assertEqual(pve.get_vm_boot_time({"uptime": 40}, now=100), 60)

    def test_invalid_network_counter_fails_closed(self):
        with mock.patch.object(
            pve, "get_vm_status",
            return_value={"status": "running", "netin": "bad", "netout": 2, "uptime": 1},
        ):
            netin, netout, status, _ = pve.get_vm_network_snapshot(100, "qemu")
        self.assertIsNone(netin)
        self.assertIsNone(netout)
        self.assertIsNone(status)

    def test_missing_network_counter_fails_closed(self):
        with mock.patch.object(
            pve, "get_vm_status",
            return_value={"status": "running", "netout": 2, "uptime": 1},
        ):
            netin, netout, status, _ = pve.get_vm_network_snapshot(100, "qemu")
        self.assertIsNone(netin)
        self.assertIsNone(netout)
        self.assertIsNone(status)

    def test_vm_listing_skips_malformed_ids(self):
        payload = '[{"vmid": 100, "name": "ok"}, {"name": "bad"}]'
        with mock.patch.object(pve, "_run_cmd", return_value=(True, payload, "")):
            self.assertEqual([vm["vmid"] for vm in pve.get_all_qemu_vms()], [100])

    def test_shutdown_timeout_allows_pve_command_to_finish(self):
        with mock.patch.object(pve, "_run_cmd", return_value=(True, "", "")) as run:
            pve.shutdown_lxc(100)
        self.assertEqual(
            run.call_args.args[0],
            ["pct", "shutdown", "100", "--timeout", "60"],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 75)

    def test_non_object_status_is_rejected(self):
        with mock.patch.object(pve, "_run_cmd", return_value=(True, "[]", "")):
            self.assertIsNone(pve.get_qemu_status(100))


if __name__ == "__main__":
    unittest.main()
