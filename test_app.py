import json
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch

import os
import tempfile

# Keep test runs out of the repository: state (e.g. the generated Flask secret key) goes to a temp directory
os.environ.setdefault("SAP_WIZARD_STATE_DIR", tempfile.mkdtemp(prefix="sap-wizard-test-"))

import app as console  # noqa: E402
from app import sanitize_overrides


class WizardProfileSectionTests(unittest.TestCase):
    def assert_wizard_controls(self, response):
        html = response.get_data(as_text=True)
        for removed in (
            "Saved platform profiles",
            "saved-profile-box",
            "saved-profile-select",
            "load-saved-profile",
            "forget-saved-profile",
            "savedProfileNames",
            "refreshSavedProfileOptions",
            "applySavedCredentials",
        ):
            self.assertNotIn(removed, html)
        for retained in (
            'id="provider-config-form"',
            'id="provider-config-save"',
            'id="provider-config-clear"',
            'id="wizard-json-file"',
            "async function saveProviderConfiguration",
            "async function loadWizardJsonFile",
            "wizardJsonFile.addEventListener('change', loadWizardJsonFile)",
        ):
            self.assertIn(retained, html)

    def test_landing_page_keeps_provider_configuration_and_json_import(self):
        with (
            patch.object(console, "check_dependencies", return_value=[]),
            patch.object(console, "list_recent_jobs", return_value=[]),
        ):
            response = console.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assert_wizard_controls(response)

    def test_validation_error_keeps_provider_configuration_and_json_import(self):
        with (
            patch.object(console, "validate_config", return_value=({}, ["Test validation error"])),
            patch.object(console, "ensure_ansible_collection_patches", return_value=[]),
            patch.object(console, "check_dependencies", return_value=[]),
            patch.object(console, "list_recent_jobs", return_value=[]),
        ):
            response = console.app.test_client().post("/run", data={"sid": "S01"})
        self.assertEqual(response.status_code, 400)
        self.assert_wizard_controls(response)
        self.assertIn("Test validation error", response.get_data(as_text=True))

    def test_provider_configuration_can_still_be_saved_and_loaded(self):
        profile = {"aws_region": "eu-central-1"}
        client = console.app.test_client()
        with patch.object(console, "save_provider_profile", return_value=profile) as save:
            response = client.post("/api/credentials/aws_ec2_vs", json=profile)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")
        save.assert_called_once_with("aws_ec2_vs", profile)
        with patch.object(console, "discover_saved_credentials", return_value={"aws_ec2_vs": profile}):
            response = client.get("/api/credentials/aws_ec2_vs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), profile)


class WizardInputTemplateTests(unittest.TestCase):
    def test_account_and_deployment_values_are_placeholders(self):
        path = console.BASE_DIR / "sample-inputs" / "sap_hana_ha_aws_test_32gb.template.json"
        template = json.loads(path.read_text())
        for key in (
            "sap_id_user", "sap_id_user_password", "master_password",
            "dns_root_domain", "sid", "hana_sid", "os_image",
            "ha_primary_ip", "ha_secondary_ip", "ha_vip_hana_primary",
            "aws_region", "aws_vpc_availability_zone", "aws_vpc_subnet_id",
            "aws_vpc_sg_names", "aws_host_os_image",
            "aws_key_pair_name_ssh_host_public_key", "bastion_public_ip",
        ):
            with self.subTest(key=key):
                self.assertTrue(template[key].startswith("ENTER_"))
        self.assertEqual(template["scenario"], "sap_hana_ha")
        self.assertEqual(template["platform"], "aws_ec2_vs")
        self.assertIs(template["use_single_master_password"], True)


class HanaLogModeTests(unittest.TestCase):
    def test_ha_requires_normal_logging_and_preserves_non_ha_defaults(self):
        for scenario, expected in (
            ("sap_hana_ha", "normal"),
            ("sap_s4hana_distributed_ha", "normal"),
            ("sap_s4hana_distributed_ha_maintplan", "normal"),
            ("sap_hana", "overwrite"),
            ("sap_hana_scaleout", "overwrite"),
            ("sap_s4hana_sandbox", "overwrite"),
        ):
            with self.subTest(scenario=scenario):
                config = {
                    "scenario": scenario,
                    "media_dir": "/software",
                    "iac_type": "existing_hosts",
                    "platform": "existing_hosts",
                    "host_plan": "test",
                    "sid": "S01",
                    "hana_sid": "H01",
                    "product": "HANA",
                    "sap_id_user": "test",
                }
                self.assertEqual(
                    sanitize_overrides(config)["sap_hana_install_log_mode"], expected
                )


class CloudStackPlatformTests(unittest.TestCase):
    base_config = {
        "scenario": "sap_hana_ha",
        "media_dir": "/software",
        "iac_type": "ansible",
        "platform": "cloudstack_vm",
        "host_plan": "xsmall_30gb",
        "sid": "H01",
        "hana_sid": "H01",
        "product": "sap_hana_2_sps08_install",
        "sap_id_user": "test",
        "cloudstack_api_url": "http://cloudstack.example.com:8080/client/api",
        "cloudstack_api_key": "key",
        "cloudstack_api_secret": "secret",
        "cloudstack_zone": "zone1",
        "cloudstack_network_name": "guestnet1",
        "cloudstack_template_name": "sles-sap-15-sp7",
        "cloudstack_kvm_host_ssh_user": "root",
        "dns_root_domain": "example.com",
    }

    def test_platform_is_registered(self):
        self.assertIn("cloudstack_vm", console.ALLOWED_PLATFORMS)
        self.assertIn("cloudstack_vm", console.CONFIGURABLE_PROVIDERS)
        self.assertEqual(console.PLATFORM_LABELS["cloudstack_vm"], "Apache CloudStack")
        self.assertEqual(
            console.get_scenario_host_plans("sap_hana_ha", "cloudstack_vm"), ["xsmall_30gb"]
        )

    def test_overrides_map_cloudstack_values(self):
        overrides = sanitize_overrides(dict(self.base_config))
        self.assertEqual(overrides["sap_vm_provision_iac_platform"], "cloudstack_vm")
        self.assertEqual(overrides["sap_vm_provision_cloudstack_api_url"], self.base_config["cloudstack_api_url"])
        self.assertEqual(overrides["sap_vm_provision_cloudstack_zone"], "zone1")
        self.assertEqual(overrides["sap_vm_provision_cloudstack_network_name"], "guestnet1")
        self.assertEqual(overrides["sap_vm_provision_cloudstack_vm_host_os_image"], "webui_selected")
        self.assertEqual(
            overrides["sap_vm_provision_cloudstack_vm_host_os_image_dictionary"],
            {"webui_selected": "sles-sap-15-sp7"},
        )
        self.assertEqual(overrides["sap_vm_provision_cloudstack_kvm_host_ssh_user"], "root")
        self.assertNotIn("sap_vm_provision_cloudstack_kvm_host_ssh_private_key_file_path", overrides)

    def test_validation_requires_ansible_and_cloudstack_fields(self):
        config = {**self.base_config, "iac_type": "ansible_to_terraform", "cloudstack_network_name": ""}
        with (
            patch.object(console.cloudstack_support, "_ini_credentials", return_value={}),
            patch.dict(console.os.environ, {}, clear=True),
            patch.object(console, "discover_saved_credentials", return_value={}),
        ):
            _, errors = console.validate_config(config)
        self.assertTrue(any("only available with 'Provision hosts with Ansible'" in e for e in errors))
        self.assertTrue(any("CloudStack guest network is required" in e for e in errors))

    def test_credentials_ignore_redacted_profile_and_fall_back_to_environment(self):
        profile = {
            "cloudstack_api_url": "http://cs/client/api",
            "cloudstack_api_key": "***redacted***",
            "cloudstack_api_secret": "***redacted***",
        }
        environment = {
            "CLOUDSTACK_ENDPOINT": "http://env/client/api",
            "CLOUDSTACK_KEY": "env-key",
            "CLOUDSTACK_SECRET": "env-secret",
        }
        with (
            patch.object(console.cloudstack_support, "_ini_credentials", return_value={}),
            patch.dict(console.os.environ, environment, clear=True),
        ):
            credentials = console.cloudstack_support.resolve_credentials(profile)
        self.assertEqual(credentials["cloudstack_api_url"], "http://env/client/api")
        self.assertEqual(credentials["cloudstack_api_key"], "env-key")

    def test_sbd_resources_are_matched_by_owner_hosts(self):
        definitions = [
            {"name": "sbd-hanahost1", "props": {"Aux/sap_vm_provision": "hanahost1,hanahost2"}},
            {"name": "sbd-other1", "props": {"Aux/sap_vm_provision": "other1,other2"}},
            {"name": "cs-1234", "props": {}},
        ]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(definitions).encode()

        with patch.object(console.cloudstack_support.urllib_request, "urlopen", return_value=Response()):
            result = console.cloudstack_support._linstor_sbd_resources({}, ["hanahost1", "hanahost2"], "http://linstor:3370")
        self.assertEqual(result, ["sbd-hanahost1"])

    def test_ha_hostnames_and_ips_are_applied_to_host_plan(self):
        config = {
            **self.base_config,
            "ha_primary_hostname": "hanahost1",
            "ha_secondary_hostname": "hanahost2",
            "ha_primary_ip": "192.0.2.16",
            "ha_secondary_ip": "192.0.2.17",
            "ha_vip_hana_primary": "192.0.2.18/24",
        }
        document = console.build_generated_document(config)
        hosts = document["sap_vm_provision_cloudstack_vm_host_specifications_dictionary"]["xsmall_30gb"]
        self.assertEqual(sorted(hosts), ["hanahost1", "hanahost2"])
        self.assertEqual(hosts["hanahost1"]["cloudstack_vm_ip_address"], "192.0.2.16")
        self.assertEqual(hosts["hanahost2"]["sap_host_type"], "hana_secondary")
        self.assertEqual(document["sap_vm_provision_ha_vip_hana_primary"], "192.0.2.18/24")

    def test_fencing_method_and_hypervisor_are_mapped(self):
        config = {
            **self.base_config,
            "cloudstack_hypervisor": "XenServer",
            "cloudstack_ha_fencing_method": "sbd_devices",
            "cloudstack_ha_sbd_devices": "/dev/disk/by-id/scsi-1, /dev/disk/by-id/scsi-2",
        }
        overrides = sanitize_overrides(config)
        self.assertEqual(overrides["sap_vm_provision_cloudstack_vm_hypervisor"], "XenServer")
        self.assertEqual(overrides["sap_vm_provision_cloudstack_ha_fencing_method"], "sbd_devices")
        self.assertEqual(
            overrides["sap_vm_provision_cloudstack_ha_sbd_devices"],
            ["/dev/disk/by-id/scsi-1", "/dev/disk/by-id/scsi-2"],
        )
        self.assertNotIn("sap_vm_provision_cloudstack_vm_hypervisor", sanitize_overrides(dict(self.base_config)))

    def test_ha_with_sbd_devices_requires_device_paths(self):
        config = {
            **self.base_config,
            "cloudstack_ha_fencing_method": "sbd_devices",
            "ha_primary_hostname": "hana1", "ha_secondary_hostname": "hana2",
            "ha_primary_ip": "192.168.10.11", "ha_secondary_ip": "192.168.10.12",
            "ha_vip_hana_primary": "192.168.10.20/24",
        }
        with patch.object(console, "discover_saved_credentials", return_value={}):
            _, errors = console.validate_config(config)
        self.assertTrue(any("fencing method 'sbd_devices'" in e for e in errors))
        with patch.object(console, "discover_saved_credentials", return_value={}):
            _, errors = console.validate_config({**config, "cloudstack_ha_fencing_method": "none"})
        self.assertFalse(any("fencing method" in e for e in errors))

    def test_dashboard_delete_refuses_vms_not_provisioned_by_console(self):
        class Client:
            destroyed = []

            def listVirtualMachines(self, **params):
                return [{"id": "vm-1", "name": "rmt", "tags": []}]

            def destroyVirtualMachine(self, **params):
                self.destroyed.append(params)

        client = Client()
        with (
            patch.object(console.cloudstack_support, "build_client", return_value=client),
            patch.object(console.cloudstack_support, "resolve_credentials", return_value={"x": "y"}),
        ):
            with self.assertRaises(RuntimeError):
                console.cloudstack_support.delete_dashboard_resource({"instance_id": "vm-1"}, {})
        self.assertEqual(client.destroyed, [])

    def test_dashboard_delete_expunges_vm_with_data_volumes(self):
        class Client:
            destroyed = []

            def listVirtualMachines(self, **params):
                return [{"id": "vm-2", "name": "hanahost1", "tags": [{"key": "sap_vm_provision", "value": "plan"}]}]

            def listVolumes(self, **params):
                return [{"id": "vol-a"}, {"id": "vol-b"}]

            def destroyVirtualMachine(self, **params):
                self.destroyed.append(params)

        client = Client()
        with (
            patch.object(console.cloudstack_support, "build_client", return_value=client),
            patch.object(console.cloudstack_support, "resolve_credentials", return_value={"x": "y"}),
        ):
            result = console.cloudstack_support.delete_dashboard_resource({"instance_id": "vm-2"}, {})
        self.assertEqual(client.destroyed[0]["volumeids"], "vol-a,vol-b")
        self.assertTrue(client.destroyed[0]["expunge"])
        self.assertIn("hanahost1", result["message"])

    def test_local_kvm_provider_is_replaced_by_cloudstack(self):
        self.assertNotIn("local_kvm", console.CONFIGURABLE_PROVIDERS)
        self.assertIn("cloudstack_vm", [item["key"] for item in console.DASHBOARD_PROVIDERS])
        self.assertNotIn("local_kvm", [item["key"] for item in console.DASHBOARD_PROVIDERS])

    def test_preflight_uses_service_offering_from_yaml_override(self):
        override = (
            "sap_vm_provision_cloudstack_vm_host_specifications_dictionary:\n"
            "  xsmall_30gb:\n"
            "    hanahost1: {sap_host_type: hana_primary, cloudstack_vm_service_offering: hana-offering}\n"
        )
        config = console.build_target_environment({**self.base_config, "yaml_override": override})
        captured = {}

        def fake_preflight(cfg, build_check):
            captured.update(cfg["_cloudstack_selected_hosts"])
            return []

        with patch.object(console.cloudstack_support, "run_preflight_check", side_effect=fake_preflight):
            console.run_cloudstack_preflight_check(config)
        self.assertEqual(captured["hanahost1"]["cloudstack_vm_service_offering"], "hana-offering")

    def test_platform_extravars_take_precedence_over_scenario_extravars(self):
        document = console.build_generated_document(dict(self.base_config))
        # 'false' in ansible_extravars.yml, 'true' in ansible_extravars_cloudstack_vm.yml
        self.assertIs(document["sap_software_download_on_execution_node"], True)
        override = console.build_generated_document(
            console.build_target_environment({**self.base_config, "yaml_override": "sap_software_download_on_execution_node: false\n"}))
        self.assertIs(override["sap_software_download_on_execution_node"], False)

    def test_dashboard_delete_removes_sbd_disk_after_last_cluster_vm(self):
        remaining_vms = {"hanahost2": [{"id": "vm-3", "name": "hanahost2"}]}

        class Client:
            def listVirtualMachines(self, **params):
                if params.get("id") == "vm-2":
                    return [{"id": "vm-2", "name": "hanahost1", "tags": [{"key": "sap_vm_provision", "value": "plan"}]}]
                return remaining_vms.get(params.get("name"), [])

            def listVolumes(self, **params):
                return []

            def destroyVirtualMachine(self, **params):
                pass

        definitions = [{"name": "sbd-hanahost1", "props": {"Aux/sap_vm_provision": "hanahost1,hanahost2"}}]
        deleted = []

        class Response:
            def __init__(self, body=b"[]"):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self.body

        def fake_urlopen(req, timeout=None):
            if getattr(req, "method", None) == "DELETE":
                deleted.append(req.full_url)
                return Response()
            return Response(json.dumps(definitions).encode())

        sup = console.cloudstack_support
        with (
            patch.object(sup, "build_client", return_value=Client()),
            patch.object(sup, "resolve_credentials", return_value={"x": "y"}),
            patch.object(sup, "_linstor_url", return_value="http://linstor:3370"),
            patch.object(sup.urllib_request, "urlopen", side_effect=fake_urlopen),
        ):
            sup.delete_dashboard_resource({"instance_id": "vm-2"}, {})
            self.assertEqual(deleted, [])  # hanahost2 still exists
            remaining_vms.clear()
            result = sup.delete_dashboard_resource({"instance_id": "vm-2"}, {})
        self.assertEqual(deleted, ["http://linstor:3370/v1/resource-definitions/sbd-hanahost1"])
        self.assertIn("sbd-hanahost1", result["message"])

    def test_landing_page_offers_cloudstack(self):
        with (
            patch.object(console, "check_dependencies", return_value=[]),
            patch.object(console, "list_recent_jobs", return_value=[]),
        ):
            html = console.app.test_client().get("/").get_data(as_text=True)
        self.assertIn('<option value="cloudstack_vm"', html)
        self.assertIn('data-provider-fields="cloudstack_vm"', html)
        self.assertIn('id="cloudstack-fields"', html)


class WizardJsonSaveTests(unittest.TestCase):
    """Executes the save functions of templates/index.html with Node.js against a fake form."""

    @staticmethod
    def _extract_js():
        html = (console.BASE_DIR / "templates" / "index.html").read_text()
        start = html.index("      const WIZARD_SECRET_NAME_PATTERN")
        end = html.index("      function saveWizardJsonFile()")
        return html[start:end]

    def test_buttons_present(self):
        with (
            patch.object(console, "check_dependencies", return_value=[]),
            patch.object(console, "list_recent_jobs", return_value=[]),
        ):
            html = console.app.test_client().get("/").get_data(as_text=True)
        self.assertGreaterEqual(html.count("data-save-wizard-json"), 2)

    def test_saved_json_excludes_secrets_and_reloads(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = self._extract_js() + r"""
const el = (name, value, type = 'text', extra = {}) => ({ name, value, type, disabled: false, ...extra });
const wizardForm = { elements: [
  el('scenario', 'sap_hana_ha', 'select-one'),
  el('platform', 'cloudstack_vm', 'select-one'),
  el('sid', 'H01'),
  el('sap_id_user', 'S000000'),
  el('sap_id_user_password', 'TopSecret1', 'password'),
  el('master_password', 'TopSecret2', 'password'),
  el('cloudstack_api_secret', 'abc', 'text'),
  el('use_single_master_password', 'on', 'checkbox', { checked: true }),
  el('yaml_override', ["sap_vm_provision_os_registration_script_command: >-", "  curl x", "sap_hana_install_master_password: 'Secret3'", "my_api_key: k123", "sap_vm_provision_dns_root_domain: example.com"].join(String.fromCharCode(10)), 'textarea'),
  el('', 'ignored', 'button'),
] };
const result = collectWizardJsonInput();
console.log(JSON.stringify(result));
"""
        out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout
        result = json.loads(out)
        form = result["form"]
        self.assertEqual(form["scenario"], "sap_hana_ha")
        self.assertIs(form["use_single_master_password"], True)
        for secret_field in ("sap_id_user_password", "master_password", "cloudstack_api_secret"):
            self.assertNotIn(secret_field, form)
        self.assertNotIn("Secret", out)
        self.assertNotIn("k123", out)
        self.assertIn('sap_hana_install_master_password: "ENTER_STRING_VALUE_HERE"', form["yaml_override"])
        self.assertIn("sap_vm_provision_dns_root_domain: example.com", form["yaml_override"])
        self.assertEqual(result["redacted"], 2)
        # The saved file is accepted by the console like a wizard submission
        config = console.build_target_environment(form)
        self.assertEqual(config["platform"], "cloudstack_vm")
        self.assertEqual(config["_yaml_override_values"]["sap_hana_install_master_password"], "ENTER_STRING_VALUE_HERE")


class ConsoleSecurityTests(unittest.TestCase):
    def test_basic_auth_enforced_only_when_configured(self):
        client = console.app.test_client()
        with (
            patch.object(console, "check_dependencies", return_value=[]),
            patch.object(console, "list_recent_jobs", return_value=[]),
        ):
            with patch.object(console, "AUTH_USER", ""), patch.object(console, "AUTH_PASSWORD", ""):
                self.assertEqual(client.get("/").status_code, 200)
            with patch.object(console, "AUTH_USER", "admin"), patch.object(console, "AUTH_PASSWORD", "s3cret"):
                self.assertEqual(client.get("/").status_code, 401)
                self.assertEqual(client.get("/", headers={"Authorization": "Basic YWRtaW46d3Jvbmc="}).status_code, 401)
                self.assertEqual(client.get("/", headers={"Authorization": "Basic YWRtaW46czNjcmV0"}).status_code, 200)

    def test_secret_key_is_generated_and_persisted_when_not_configured(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, patch.object(console, "STATE_DIR", console.Path(tmp)):
            with patch.dict(console.os.environ, {"FLASK_SECRET_KEY": "change-me"}):
                first = console._load_or_create_secret_key()
                second = console._load_or_create_secret_key()
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 64)
            self.assertNotIn(first, ("change-me", "sap-ui-dev-secret"))
            with patch.dict(console.os.environ, {"FLASK_SECRET_KEY": "configured-key"}):
                self.assertEqual(console._load_or_create_secret_key(), "configured-key")


class ConnectionInfoTests(unittest.TestCase):
    def test_direct_access_without_bastion(self):
        job_vars = {
            "sap_vm_provision_iac_platform": "cloudstack_vm",
            "sap_vm_provision_host_specification_plan": "plan1",
            "sap_vm_provision_cloudstack_vm_host_specifications_dictionary": {"plan1": {
                "hanahost1": {"sap_host_type": "hana_primary", "cloudstack_vm_ip_address": "192.0.2.16"},
                "hanahost2": {"sap_host_type": "hana_secondary"}}},
            "sap_vm_provision_ssh_host_private_key_file_path": "/mnt/ssh/vm_key",
            "sap_vm_provision_bastion_execution": False,
        }
        with patch.dict(console.os.environ, {"SAP_SSH_DIR_HOST": "/home/user/.ssh"}):
            info = console.connection_info.build_connection_info(
                job_vars, [{"name": "hanahost2", "private_ip": "192.0.2.17", "state": "running"}])
        self.assertIn("Host hanahost1\n    HostName 192.0.2.16\n    User root\n    IdentityFile /home/user/.ssh/vm_key", info["ssh_config"])
        self.assertIn("HostName 192.0.2.17", info["ssh_config"])
        self.assertNotIn("ProxyJump", info["ssh_config"])
        self.assertEqual([c["command"] for c in info["commands"]], ["ssh hanahost1", "ssh hanahost2"])

    def test_bastion_uses_proxyjump_and_unknown_ip_placeholder(self):
        job_vars = {
            "sap_vm_provision_iac_platform": "msazure_vm",
            "sap_vm_provision_host_specification_plan": "plan1",
            "sap_vm_provision_msazure_vm_host_specifications_dictionary": {"plan1": {
                "hanaaz1": {"sap_host_type": "hana_primary"}, "hanaaz2": {"sap_host_type": "hana_secondary"}}},
            "sap_vm_provision_ssh_host_private_key_file_path": "/mnt/ssh/azure_key",
            "sap_vm_provision_bastion_execution": True,
            "sap_vm_provision_bastion_public_ip": "198.51.100.7",
            "sap_vm_provision_bastion_user": "azureuser",
            "sap_vm_provision_bastion_ssh_port": "22",
            "sap_vm_provision_ssh_bastion_private_key_file_path": "/mnt/ssh/azure_key",
        }
        with patch.dict(console.os.environ, {}, clear=True):
            info = console.connection_info.build_connection_info(job_vars, [{"name": "hanaaz1", "private_ip": "10.1.0.8"}])
        self.assertIn("Host sap-bastion\n    HostName 198.51.100.7\n    User azureuser\n    Port 22", info["ssh_config"])
        self.assertEqual(info["ssh_config"].count("ProxyJump sap-bastion"), 2)
        self.assertIn("HostName ENTER_HOST_IP", info["ssh_config"])
        self.assertTrue(any("hanaaz2" in note for note in info["notes"]))
        self.assertTrue(any("/mnt/ssh" in note for note in info["notes"]))
        self.assertEqual(info["commands"][0]["command"], "ssh sap-bastion")

    def test_connection_route_returns_config(self):
        state = {"generated_file": "/nonexistent.yml", "platform": "cloudstack_vm"}
        with (
            patch.object(console, "load_job_state", return_value=state),
            patch.object(console, "load_generated_vars", return_value={}),
            patch.object(console, "list_job_resources", return_value={"instances": []}),
        ):
            client = console.app.test_client()
            data = client.get("/api/job/abc/connection").get_json()
            download = client.get("/api/job/abc/ssh_config")
        self.assertEqual(data["status"], "ok")
        self.assertIn("No hosts were found for this job.", data["notes"])
        self.assertIn("attachment", download.headers["Content-Disposition"])


if __name__ == "__main__":
    unittest.main()
