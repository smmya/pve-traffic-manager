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

    def test_lxc_listing_uses_hostname_field(self):
        payload = '[{"vmid": 646, "hostname": "ct-646", "status": "running"}]'
        with mock.patch.object(pve, "_run_cmd", return_value=(True, payload, "")):
            result = pve.get_all_lxc_vms()
        self.assertEqual(result[0]["name"], "ct-646")

    def test_lxc_listing_falls_back_to_config_hostname(self):
        listing = '[{"vmid": 646, "status": "running"}]'
        config = '{"hostname": "ct-from-config"}'
        with mock.patch.object(
            pve, "_run_cmd",
            side_effect=[(True, listing, ""), (True, config, "")],
        ) as run:
            result = pve.get_all_lxc_vms()
        self.assertEqual(result[0]["name"], "ct-from-config")
        self.assertEqual(
            run.call_args_list[1].args[0],
            [
                "pvesh", "get", f"/nodes/{pve.PVE_NODE}/lxc/646/config",
                "--output-format", "json",
            ],
        )

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

    def test_lxc_ping_runs_inside_container_with_exactly_three_packets(self):
        with mock.patch.object(
            pve, "_run_cmd", return_value=(True, "3 received", "")
        ) as run:
            ok, _ = pve.ping_from_lxc(100, "1.1.1.1")
        self.assertTrue(ok)
        self.assertEqual(
            run.call_args.args[0],
            ["pct", "exec", "100", "--", "ping", "-c", "3", "-W", "3", "1.1.1.1"],
        )

    def test_lxc_ping_rejects_non_ip_target(self):
        with mock.patch.object(pve, "_run_cmd") as run:
            ok, _ = pve.ping_from_lxc(100, "example.com")
        self.assertFalse(ok)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
