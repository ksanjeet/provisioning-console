import unittest
from unittest.mock import patch

import app as console
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


if __name__ == "__main__":
    unittest.main()
