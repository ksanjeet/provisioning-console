import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from container.apply_collection_patches import (
    HSR_COMMAND_ENVIRONMENT,
    HSR_EXECUTABLE_PATH,
    patch_hsr_commands,
    patch_hsr_log_mode,
    patch_srhook_activation,
    SRHOOK_ACTIVATE_NAME,
    SRHOOK_UPDATE_REGISTER,
)


class HsrCommandPatchTests(unittest.TestCase):
    def patch(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "enable_hsr.yml"
            path.write_text(text)
            self.assertGreater(patch_hsr_commands(path), 0)
            patched = path.read_text()
            self.assertEqual(patch_hsr_commands(path), 0)
            self.assertEqual(path.read_text(), patched)
            return yaml.safe_load(patched)

    def test_original_and_legacy_commands(self):
        for prefix in (
            "",
            "source {{ __sap_ha_install_hana_hsr_home_path }}/.profile && ",
            '. "{{ __sap_ha_install_hana_hsr_executable_path }}/hdbenv.sh" >/dev/null 2>&1 || true; ',
            'source "{{ __sap_ha_install_hana_hsr_executable_path }}/hdbenv.sh" >/dev/null 2>&1 || true && ',
        ):
            with self.subTest(prefix=prefix):
                tasks = self.patch(
                    "- name: Enable HSR\n"
                    "  ansible.builtin.shell:\n"
                    "    cmd: >-\n"
                    f'      {prefix}{HSR_EXECUTABLE_PATH}/hdbnsutil -sr_enable --name="DC01"\n'
                    "  become: true\n"
                    "  failed_when: false\n"
                )
                self.assertEqual(
                    tasks[0]["ansible.builtin.shell"]["cmd"],
                    HSR_COMMAND_ENVIRONMENT + HSR_EXECUTABLE_PATH + '/hdbnsutil -sr_enable --name="DC01"',
                )
                self.assertNotIn("failed_when", tasks[0])
                self.assertEqual(tasks[0]["ansible.builtin.shell"]["executable"], "/bin/bash")

    def test_repairs_nested_legacy_indentation_and_executable(self):
        tasks = self.patch(
            "- name: Status block\n"
            "  block:\n"
            "    - name: Check status\n"
            "      ansible.builtin.shell:\n"
            "        cmd: >-\n"
            f'                    source "{HSR_EXECUTABLE_PATH}/hdbenv.sh" >/dev/null 2>&1 || true &&\n'
            f"          {HSR_EXECUTABLE_PATH}/hdbnsutil\n"
            "          -sr_state\n"
            "      become: true\n"
            "  executable: /bin/bash\n"
            "      register: srstate\n"
            "      failed_when: false\n"
        )
        task = tasks[0]["block"][0]
        self.assertTrue(task["ansible.builtin.shell"]["cmd"].endswith("/hdbnsutil -sr_state"))
        self.assertFalse(task["failed_when"])
        self.assertEqual(task["register"], "srstate")

    def test_preserves_registration_arguments_and_conditions(self):
        tasks = self.patch(
            "- name: Register\n"
            "  ansible.builtin.shell:\n"
            "    cmd: >-\n"
            f"      {HSR_EXECUTABLE_PATH}/hdbnsutil\n"
            "      -sr_register --name=DC02\n"
            "      --remoteHost=hana-p --remoteInstance=90\n"
            "      --replicationMode=sync --operationMode=logreplay --online\n"
            "  when: is_secondary\n"
            "  retries: 6\n"
        )
        self.assertEqual(tasks[0]["when"], "is_secondary")
        self.assertEqual(tasks[0]["retries"], 6)
        self.assertIn("--operationMode=logreplay --online", tasks[0]["ansible.builtin.shell"]["cmd"])

    def test_shell_environment_and_failure_propagation(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = Path(directory)
            host = instance / "hana-p"
            host.mkdir()
            (instance / "hdbenv.sh").write_text(
                '[[ -n "$BASH_VERSION" ]] || return 12\n'
                f'[[ "$PWD" == "{instance}" ]] || return 13\n'
                f'export DIR_INSTANCE="{instance}"\n'
                'export DIR_EXECUTABLE="$DIR_INSTANCE/exe"\n'
                'export SAP_RETRIEVAL_PATH="$DIR_INSTANCE/hana-p"\n'
                'export SECUDIR="$SAP_RETRIEVAL_PATH/sec"\n'
                'export SAPSYSTEMNAME=H01 TINSTANCE=90\n'
            )
            # Render the instance-path expression without requiring Ansible.
            prefix = HSR_COMMAND_ENVIRONMENT.replace(
                "{{ __sap_ha_install_hana_hsr_executable_path | regex_replace('/exe$', '') }}",
                directory,
            ).replace(HSR_EXECUTABLE_PATH, str(instance / "exe"))
            result = subprocess.run(
                ["/bin/bash", "-c", prefix + (
                    'printf "%s|%s|%s|%s|%s" "$PWD" "$SAP_RETRIEVAL_PATH" '
                    '"$SECUDIR" "$SAPSYSTEMNAME" "$TINSTANCE"'
                )],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, f"{host}|{host}|{host}/sec|H01|90")
            result = subprocess.run(["/bin/bash", "-c", prefix + "exit 17"], capture_output=True)
            self.assertEqual(result.returncode, 17)
            (instance / "hdbenv.sh").write_text("return 23\n")
            result = subprocess.run(
                ["/bin/bash", "-c", prefix + "printf utility-ran"], capture_output=True,
            )
            self.assertEqual(result.returncode, 23)
            self.assertEqual(result.stdout, b"")
            (instance / "hdbenv.sh").unlink()
            result = subprocess.run(
                ["/bin/bash", "-c", prefix + "printf utility-ran"], capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")

    def test_upgrades_explicit_executable_directory_environment(self):
        tasks = self.patch(
            "- name: Enable HSR\n"
            "  ansible.builtin.shell:\n"
            "    cmd: >-\n"
            f'      export SAP_RETRIEVAL_PATH="{HSR_EXECUTABLE_PATH}" &&\n'
            f'      export SECUDIR="{HSR_EXECUTABLE_PATH}/sec" &&\n'
            f'      {HSR_EXECUTABLE_PATH}/hdbnsutil -sr_enable --name=DC01\n'
        )
        command = tasks[0]["ansible.builtin.shell"]["cmd"]
        self.assertNotIn('export SAP_RETRIEVAL_PATH=', command)
        self.assertIn("/hdbenv.sh", command)
        self.assertNotIn("/.profile", command)

    def test_invalid_yaml_does_not_get_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "enable_hsr.yml"
            original = "- invalid: [\n"
            path.write_text(original)
            with self.assertRaises(yaml.YAMLError):
                patch_hsr_commands(path)
            self.assertEqual(path.read_text(), original)

    def test_log_mode_repair_preserves_system_tasks_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "set_log_mode.yml"
            original = [{"name": "Existing SYSTEM logging task", "ansible.builtin.debug": {"msg": "normal"}}]
            path.write_text(yaml.safe_dump(original))
            self.assertEqual(patch_hsr_log_mode(path), 1)
            patched = path.read_text()
            self.assertEqual(patch_hsr_log_mode(path), 0)
            self.assertEqual(path.read_text(), patched)
            tasks = yaml.safe_load(patched)
            self.assertEqual(tasks[0], original[0])
            check, repair = tasks[1]["block"]
            query = check["ansible.builtin.command"]["argv"][-1]
            self.assertIn("LAYER_NAME = 'HOST'", query)
            self.assertIn("VALUE = 'overwrite'", query)
            self.assertIn("ansible_facts['hostname']", query)
            self.assertIn(
                "UNSET ('persistence', 'log_mode') WITH RECONFIGURE",
                repair["ansible.builtin.command"]["argv"][-1],
            )
            self.assertIn("stdout_lines", repair["when"])
            for task in (check, repair):
                self.assertNotIn("ignore_errors", task)
                self.assertNotIn("failed_when", task)


if __name__ == "__main__":
    unittest.main()


class SrHookActivationPatchTests(unittest.TestCase):
    ORIGINAL = [
        {"name": "SAP HA Pacemaker srHook - Get contents of global.ini", "ansible.builtin.command": "cat global.ini"},
        {"name": "SAP HA Pacemaker srHook - Update srHook providers in global.ini",
         "ansible.builtin.blockinfile": {"path": "global.ini", "block": "x"}, "loop": "{{ hooks }}"},
        {"name": "SAP HA Pacemaker srHook - Add srHook sudo entries", "ansible.builtin.template": {"src": "a", "dest": "b"}},
    ]

    def _file(self, tasks):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False)
        yaml.safe_dump(tasks, handle)
        handle.close()
        return Path(handle.name)

    def test_registers_update_and_appends_activation_once(self):
        path = self._file(self.ORIGINAL)
        self.assertEqual(patch_srhook_activation(path), 1)
        self.assertEqual(patch_srhook_activation(path), 0)
        tasks = yaml.safe_load(path.read_text())
        update = tasks[1]
        self.assertEqual(update["register"], SRHOOK_UPDATE_REGISTER)
        activation = tasks[-1]
        self.assertEqual(activation["name"], SRHOOK_ACTIVATE_NAME)
        self.assertEqual(activation["when"], [f"{SRHOOK_UPDATE_REGISTER} is changed"])
        commands = " ".join(task["ansible.builtin.shell"] for task in activation["block"])
        self.assertIn("hdbnsutil -reloadHADRProviders", commands)
        self.assertIn("HDB stop && HDB start", commands)
        restart = activation["block"][-1]
        self.assertIn("'primary' not in __sap_ha_pacemaker_cluster_register_sr_mode.stdout", restart["when"])

    def test_missing_update_task_is_an_error(self):
        path = self._file([self.ORIGINAL[0]])
        with self.assertRaises(RuntimeError):
            patch_srhook_activation(path)
