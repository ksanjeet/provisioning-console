#!/usr/bin/env python3
"""Apply reproducible fixes to pinned Ansible collections."""

import re
import sys
from pathlib import Path
from typing import List

import yaml

SECRET_KEY_LINE = 'secret_key: "{{ sap_vm_provision_aws_secret_access_key }}"'
SESSION_TOKEN_LINE = 'session_token: "{{ sap_vm_provision_aws_session_token | d(omit) }}"'
STORAGE_SIZE_SOURCE = "combine({'disk_size_gb': __size_gb})"
STORAGE_SIZE_TARGET = "combine({'disk_size_gb': (__size_gb | int)})"
HSR_EXECUTABLE_PATH = "{{ __sap_ha_install_hana_hsr_executable_path }}"
HSR_COMMAND_ENVIRONMENT = (
    'cd "{{ __sap_ha_install_hana_hsr_executable_path | regex_replace(\'/exe$\', \'\') }}" && '
    'source ./hdbenv.sh && '
    'cd "${SAP_RETRIEVAL_PATH:?HANA host directory was not initialized}" && '
)
HSR_BACKUP_EXPANSION_SOURCE = (
    "source {{ __sap_ha_install_hana_hsr_home_path }}/.profile &&\n"
    "              echo ${{ __basepath_databackup_expand_var }}"
)
HSR_BACKUP_EXPANSION_TARGET = (
    "if [ \"{{ __basepath_databackup_expand_var }}\" = \"DIR_INSTANCE\" ]; then\n"
    "                printf '%s' \"{{ __sap_ha_install_hana_hsr_executable_path | regex_replace('/exe$', '') }}\";\n"
    "              else\n"
    "                printenv \"{{ __basepath_databackup_expand_var }}\";\n"
    "              fi"
)
HSR_BACKUP_VERIFY_SOURCE = (
    "source {{ __sap_ha_install_hana_hsr_home_path }}/.profile &&\n"
    "      find {{ __sap_ha_install_hana_hsr_fact_backup_path_final }}\n"
    "      -type f -name {{ __sap_ha_install_hana_hsr_fact_backup_prefix }}*\n"
    "      -exec /usr/sap/{{ __sap_ha_install_hana_hsr_sid }}/SYS/exe/hdb/hdbbackupcheck {} \\;"
)
HSR_BACKUP_VERIFY_TARGET = (
    "find {{ __sap_ha_install_hana_hsr_fact_backup_path_final }}\n"
    "      -type f -name {{ __sap_ha_install_hana_hsr_fact_backup_prefix }}*\n"
    "      -exec /usr/sap/{{ __sap_ha_install_hana_hsr_sid }}/SYS/exe/hdb/hdbbackupcheck {} \\;"
)
HSR_BACKUP_VERIFY_WITHOUT_PROFILE_SOURCE = HSR_BACKUP_VERIFY_TARGET
HSR_BACKUP_VERIFY_WITH_LIBRARY_PATH_TARGET = (
    ". \"{{ __sap_ha_install_hana_hsr_executable_path }}/hdbenv.sh\" >/dev/null 2>&1 || true;\n"
    "      export LD_LIBRARY_PATH=\"{{ __sap_ha_install_hana_hsr_executable_path }}:${LD_LIBRARY_PATH:-}\" &&\n"
    "      export SAP_RETRIEVAL_PATH=\"${SAP_RETRIEVAL_PATH:-{{ __sap_ha_install_hana_hsr_executable_path }}}\" &&\n"
    "      export DIR_EXECUTABLE=\"${DIR_EXECUTABLE:-{{ __sap_ha_install_hana_hsr_executable_path }}}\" &&\n"
    "      export SECUDIR=\"${SECUDIR:-{{ __sap_ha_install_hana_hsr_executable_path }}/sec}\" &&\n"
    "      find {{ __sap_ha_install_hana_hsr_fact_backup_path_final }}\n"
    "      -type f -name {{ __sap_ha_install_hana_hsr_fact_backup_prefix }}*\n"
    "      -exec /usr/sap/{{ __sap_ha_install_hana_hsr_sid }}/SYS/exe/hdb/hdbbackupcheck {} \\;"
)
LAUNCHPAD_TIMEOUT_SOURCE = """def _request(url, **kwargs):
    global https_session
    if 'headers' not in kwargs:
"""
LAUNCHPAD_TIMEOUT_TARGET = """DEFAULT_REQUEST_TIMEOUT = (30, 300)


def _request(url, **kwargs):
    global https_session
    if 'timeout' not in kwargs:
        kwargs['timeout'] = DEFAULT_REQUEST_TIMEOUT
    if 'headers' not in kwargs:
"""
SESSION_TOKEN_TARGET_FILES = [
    "ansible_collections/community/sap_infrastructure/roles/sap_vm_provision/tasks/platform_ansible/aws_ec2_vs/execute_main.yml",
    "ansible_collections/community/sap_infrastructure/roles/sap_vm_provision/tasks/platform_ansible/aws_ec2_vs/execute_provision.yml",
    "ansible_collections/community/sap_infrastructure/roles/sap_vm_provision/tasks/platform_ansible/aws_ec2_vs/execute_setup_ha.yml",
    "ansible_collections/community/sap_infrastructure/roles/sap_vm_provision/tasks/platform_ansible/aws_ec2_vs/execute_setup_ha_anydb.yml",
    "ansible_collections/community/sap_infrastructure/roles/sap_vm_provision/tasks/platform_ansible/aws_ec2_vs/execute_setup_ha_ascs_ers.yml",
    "ansible_collections/community/sap_infrastructure/roles/sap_vm_provision/tasks/platform_ansible/aws_ec2_vs/execute_setup_ha_hana.yml",
]
STORAGE_TYPE_TARGET_FILES = [
    "ansible_collections/community/sap_install/roles/sap_storage_setup/tasks/configuration/main.yml",
]
HSR_COMMAND_TARGET_FILES = [
    "ansible_collections/community/sap_install/roles/sap_ha_install_hana_hsr/tasks/pre_tasks/get_replication_status.yml",
    "ansible_collections/community/sap_install/roles/sap_ha_install_hana_hsr/tasks/enable_hsr.yml",
    "ansible_collections/community/sap_install/roles/sap_ha_install_hana_hsr/tasks/disable_hsr.yml",
]
HSR_BACKUP_EXPANSION_TARGET_FILES = [
    "ansible_collections/community/sap_install/roles/sap_ha_install_hana_hsr/tasks/pre_tasks/create_backup.yml",
]
HSR_LOG_MODE_TARGET = (
    "ansible_collections/community/sap_install/roles/sap_ha_install_hana_hsr/tasks/pre_tasks/set_log_mode.yml"
)
HSR_LOG_MODE_REPAIR_NAME = "SAP HSR - Repair legacy HOST log_mode override"
SRHOOK_TARGET = (
    "ansible_collections/community/sap_install/roles/sap_ha_pacemaker_cluster/tasks/post_tasks/post_srhook.yml"
)
SRHOOK_UPDATE_TASK_NAME = "SAP HA Pacemaker srHook - Update srHook providers in global.ini"
SRHOOK_UPDATE_REGISTER = "__sap_ha_pacemaker_cluster_register_srhook_update"
SRHOOK_ACTIVATE_NAME = "SAP HA Pacemaker srHook - Activate HA/DR providers in running SAP HANA"
CLASSIC_PROMOTABLE_DIR = "ansible_collections/community/sap_install/roles/sap_ha_pacemaker_cluster/tasks/prepare_vars"
CLASSIC_PROMOTABLE_MARKER = "# Pacemaker 2.1+: promotable clone instead of the deprecated master resource"
# Pacemaker 2.1 deprecates <master> resources and the Master/Slave role names; crm_verify reports them as warnings with
# a non-zero exit code and the ha_cluster role aborts with "Cluster configuration was invalid" (classic SAPHanaSR on SUSE)
CLASSIC_PROMOTABLE_REPLACEMENTS = {
    "prepare_hana_scaleup.yml": [
        (
            """            - name: maintenance
              value: "true"
      # ms attribute is used to create ms resource instead of clone.
      ms: true""",
            '            - name: maintenance\n'
            '              value: "true"\n'
            '            ' + CLASSIC_PROMOTABLE_MARKER + '\n'
            '            - name: promotable\n'
            '              value: "true"',
        ),
        ("""            - name: role
              value: Slave""", """            - name: role
              value: Unpromoted"""),
        ("""            - name: role
              value: Master""", """            - name: role
              value: Promoted"""),
    ],
    "prepare_vip_constraints_hana.yml": [
        ("role: \"{{ 'master' if ansible_facts['os_family']", "role: \"{{ 'promoted' if ansible_facts['os_family']"),
        ("role: \"{{ 'slave' if ansible_facts['os_family']", "role: \"{{ 'unpromoted' if ansible_facts['os_family']"),
    ],
}
LAUNCHPAD_TIMEOUT_TARGET_FILES = [
    "ansible_collections/community/sap_launchpad/plugins/module_utils/sap_api_common.py",
]


def patch_file(path: Path) -> int:
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    patched_lines = []  # type: List[str]
    insertions = 0

    for index, line in enumerate(lines):
        patched_lines.append(line)
        if SECRET_KEY_LINE not in line:
            continue
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        if SESSION_TOKEN_LINE in next_line:
            continue
        indent = line[: len(line) - len(line.lstrip(" "))]
        patched_lines.append(f"{indent}{SESSION_TOKEN_LINE}")
        insertions += 1

    if insertions:
        path.write_text("\n".join(patched_lines) + "\n", encoding="utf-8")
    return insertions


def replace_text(path: Path, source: str, target: str) -> int:
    original = path.read_text(encoding="utf-8")
    replacements = original.count(source)
    if replacements == 0:
        return 0
    path.write_text(original.replace(source, target), encoding="utf-8")
    return replacements


def validate_yaml_file(path: Path) -> None:
    try:
        yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Patched YAML is invalid in {path}: {exc}") from exc


def patch_hsr_commands(path: Path) -> int:
    original = path.read_text(encoding="utf-8")
    # Repair only the malformed scalar indentation/executable fields emitted by older patches.
    text = re.sub(r"(?m)^  executable: /bin/bash\n", "", original)

    def repair_scalar(match):
        indent = match["indent"] + "  "
        return match["header"] + "".join(
            indent + line.lstrip() + "\n" for line in match["body"].splitlines()
        )

    text = re.sub(
        r"(?m)^(?P<header>(?P<indent> +)cmd: >-?\n)"
        r"(?P<body>(?: +(?:source |\. |export |.*hdbnsutil|-[^\n]*)[^\n]*\n)+)",
        repair_scalar,
        text,
    )
    tasks = yaml.safe_load(text)
    count = 0

    def patch_tasks(items):
        nonlocal count
        for task in items:
            for key in ("block", "rescue", "always"):
                if key in task:
                    patch_tasks(task[key])
            shell = task.get("ansible.builtin.shell")
            if not isinstance(shell, dict):
                continue
            command = shell.get("cmd", "")
            binary = HSR_EXECUTABLE_PATH + "/hdbnsutil"
            if binary not in command:
                continue
            # Discard legacy profile/hdbenv prefixes, preserving the utility and all arguments.
            command = command[command.index(binary):].strip()
            shell["cmd"] = HSR_COMMAND_ENVIRONMENT + command
            shell["executable"] = "/bin/bash"
            if "-sr_enable" in command and task.get("failed_when") is False:
                del task["failed_when"]
            count += 1

    patch_tasks(tasks)
    if not count:
        raise ValueError(f"No HSR commands found in {path}")
    patched = yaml.safe_dump(tasks, sort_keys=False, width=1000)
    yaml.safe_load(patched)
    if patched != original:
        path.write_text(patched, encoding="utf-8")
        return count
    return 0


def patch_hsr_log_mode(path: Path) -> int:
    original = path.read_text(encoding="utf-8")
    tasks = yaml.safe_load(original)
    if any(task.get("name") == HSR_LOG_MODE_REPAIR_NAME for task in tasks):
        return 0
    hdbsql = [
        HSR_EXECUTABLE_PATH + "/hdbsql",
        "-U", "{{ sap_ha_install_hana_hsr_hdbuserstore_system_backup_user }}",
        "-x",
    ]
    # HOST overrides written by older wizard runs take precedence over SYSTEM normal.
    tasks.append({
        "name": HSR_LOG_MODE_REPAIR_NAME,
        "become": True,
        "become_user": "{{ __sap_ha_install_hana_hsr_sid | lower }}adm",
        "environment": {"LD_LIBRARY_PATH": HSR_EXECUTABLE_PATH},
        "block": [
            {
                "name": "SAP HSR - Check for legacy HOST overwrite logging",
                "ansible.builtin.command": {"argv": hdbsql + [
                    "SELECT COUNT(*) FROM SYS.M_INIFILE_CONTENTS "
                    "WHERE FILE_NAME = 'global.ini' AND LAYER_NAME = 'HOST' "
                    "AND HOST = '{{ ansible_facts['hostname'] }}' "
                    "AND SECTION = 'persistence' AND KEY = 'log_mode' AND VALUE = 'overwrite'"
                ]},
                "register": "__sap_ha_install_hana_hsr_register_host_overwrite",
                "changed_when": False,
            },
            {
                "name": "SAP HSR - Remove legacy HOST overwrite logging",
                "ansible.builtin.command": {"argv": hdbsql + [
                    "ALTER SYSTEM ALTER CONFIGURATION "
                    "('global.ini', 'HOST', '{{ ansible_facts['hostname'] }}') "
                    "UNSET ('persistence', 'log_mode') WITH RECONFIGURE"
                ]},
                "when": (
                    "'1' in (__sap_ha_install_hana_hsr_register_host_overwrite.stdout_lines "
                    "| map('trim') | list)"
                ),
                "changed_when": True,
            },
        ],
    })
    patched = yaml.safe_dump(tasks, sort_keys=False, width=1000)
    yaml.safe_load(patched)
    path.write_text(patched, encoding="utf-8")
    return 1


def patch_srhook_activation(path: Path) -> int:
    """The role writes the HA/DR provider hooks to global.ini while SAP HANA is running, but neither reloads the
    providers nor restarts SAP HANA, so the hooks stay inactive (no srHook attribute, no takeover protection).
    Reload the providers on all nodes and restart SAP HANA on the secondary once, while the SAP HANA cluster resources
    are still in maintenance, so the hook on the primary records the system replication state."""
    tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
    if any(task.get("name") == SRHOOK_ACTIVATE_NAME for task in tasks):
        return 0
    update = next((task for task in tasks if task.get("name") == SRHOOK_UPDATE_TASK_NAME), None)
    if update is None:
        raise RuntimeError(f"{path}: task '{SRHOOK_UPDATE_TASK_NAME}' not found")
    update["register"] = SRHOOK_UPDATE_REGISTER
    sidadm = "{{ sap_ha_pacemaker_cluster_hana_sid | lower }}adm"
    instance = "{{ sap_ha_pacemaker_cluster_hana_instance_nr }}"
    tasks.append({
        "name": SRHOOK_ACTIVATE_NAME,
        "when": [f"{SRHOOK_UPDATE_REGISTER} is changed"],
        "block": [
            {
                "name": "SAP HA Pacemaker srHook - Check if SAP HANA is running",
                "ansible.builtin.shell": f"su - {sidadm} -c 'sapcontrol -nr {instance} -function GetProcessList'",
                "register": "__sap_ha_pacemaker_cluster_register_hana_processes",
                "changed_when": False,
                # sapcontrol GetProcessList: 3 = all processes running (GREEN)
                "failed_when": False,
            },
            {
                "name": "SAP HA Pacemaker srHook - Reload HA/DR providers",
                "ansible.builtin.shell": f"su - {sidadm} -c 'hdbnsutil -reloadHADRProviders'",
                "when": ["__sap_ha_pacemaker_cluster_register_hana_processes.rc == 3"],
                "changed_when": True,
            },
            {
                "name": "SAP HA Pacemaker srHook - Get system replication mode",
                "ansible.builtin.shell": f"su - {sidadm} -c 'hdbnsutil -sr_state' | grep -E '^mode:'",
                "register": "__sap_ha_pacemaker_cluster_register_sr_mode",
                "when": ["__sap_ha_pacemaker_cluster_register_hana_processes.rc == 3"],
                "changed_when": False,
                "failed_when": False,
            },
            {
                "name": "SAP HA Pacemaker srHook - Restart SAP HANA secondary to record the system replication state",
                "ansible.builtin.shell": f"su - {sidadm} -c 'HDB stop && HDB start'",
                "when": [
                    "__sap_ha_pacemaker_cluster_register_hana_processes.rc == 3",
                    "__sap_ha_pacemaker_cluster_register_sr_mode.stdout | d('') | length > 0",
                    "'primary' not in __sap_ha_pacemaker_cluster_register_sr_mode.stdout",
                    "'none' not in __sap_ha_pacemaker_cluster_register_sr_mode.stdout",
                ],
                "changed_when": True,
            },
        ],
    })
    patched = yaml.safe_dump(tasks, sort_keys=False, width=1000)
    yaml.safe_load(patched)
    path.write_text(patched, encoding="utf-8")
    return 1


def patch_classic_promotable(directory: Path) -> int:
    """Classic SAPHanaSR resources as promotable clone with Promoted/Unpromoted roles (Pacemaker 2.1+)."""
    replacements = 0
    for name, pairs in CLASSIC_PROMOTABLE_REPLACEMENTS.items():
        path = directory / name
        text = path.read_text(encoding="utf-8")
        original = text
        for source, target in pairs:
            if target in text:
                continue
            if source not in text:
                raise RuntimeError(f"{path}: expected text not found: {source.splitlines()[0].strip()}")
            text = text.replace(source, target)
            replacements += 1
        if text != original:
            yaml.safe_load(text)
            path.write_text(text, encoding="utf-8")
    return replacements


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: apply_collection_patches.py <collections-root>", file=sys.stderr)
        return 2

    collections_root = Path(sys.argv[1]).resolve()
    total_insertions = 0
    total_storage_replacements = 0
    total_hsr_replacements = 0
    total_hsr_log_mode_replacements = 0
    total_hsr_backup_expansion_replacements = 0
    total_hsr_backup_verify_replacements = 0
    total_launchpad_timeout_replacements = 0
    total_srhook_activation_insertions = 0
    total_classic_promotable_replacements = 0
    missing_files = []  # type: List[str]

    for relative_path in SESSION_TOKEN_TARGET_FILES:
        target = collections_root / relative_path
        if not target.exists():
            missing_files.append(relative_path)
            continue
        total_insertions += patch_file(target)

    for relative_path in STORAGE_TYPE_TARGET_FILES:
        target = collections_root / relative_path
        if not target.exists():
            missing_files.append(relative_path)
            continue
        total_storage_replacements += replace_text(target, STORAGE_SIZE_SOURCE, STORAGE_SIZE_TARGET)

    for relative_path in HSR_COMMAND_TARGET_FILES:
        target = collections_root / relative_path
        if not target.exists():
            missing_files.append(relative_path)
            continue
        total_hsr_replacements += patch_hsr_commands(target)
        validate_yaml_file(target)

    log_mode_target = collections_root / HSR_LOG_MODE_TARGET
    if log_mode_target.exists():
        total_hsr_log_mode_replacements += patch_hsr_log_mode(log_mode_target)
    else:
        missing_files.append(HSR_LOG_MODE_TARGET)

    for relative_path in HSR_BACKUP_EXPANSION_TARGET_FILES:
        target = collections_root / relative_path
        if not target.exists():
            missing_files.append(relative_path)
            continue
        total_hsr_backup_expansion_replacements += replace_text(
            target,
            HSR_BACKUP_EXPANSION_SOURCE,
            HSR_BACKUP_EXPANSION_TARGET,
        )
        total_hsr_backup_verify_replacements += replace_text(
            target,
            HSR_BACKUP_VERIFY_SOURCE,
            HSR_BACKUP_VERIFY_WITH_LIBRARY_PATH_TARGET,
        )
        total_hsr_backup_verify_replacements += replace_text(
            target,
            HSR_BACKUP_VERIFY_WITHOUT_PROFILE_SOURCE,
            HSR_BACKUP_VERIFY_WITH_LIBRARY_PATH_TARGET,
        )
        validate_yaml_file(target)

    for relative_path in LAUNCHPAD_TIMEOUT_TARGET_FILES:
        target = collections_root / relative_path
        if not target.exists():
            missing_files.append(relative_path)
            continue
        total_launchpad_timeout_replacements += replace_text(
            target,
            LAUNCHPAD_TIMEOUT_SOURCE,
            LAUNCHPAD_TIMEOUT_TARGET,
        )

    srhook_target = collections_root / SRHOOK_TARGET
    if srhook_target.exists():
        total_srhook_activation_insertions += patch_srhook_activation(srhook_target)
    else:
        missing_files.append(SRHOOK_TARGET)

    classic_dir = collections_root / CLASSIC_PROMOTABLE_DIR
    if all((classic_dir / name).exists() for name in CLASSIC_PROMOTABLE_REPLACEMENTS):
        total_classic_promotable_replacements += patch_classic_promotable(classic_dir)
    else:
        missing_files.extend(f"{CLASSIC_PROMOTABLE_DIR}/{name}" for name in CLASSIC_PROMOTABLE_REPLACEMENTS)

    if missing_files:
        print("missing collection files:", file=sys.stderr)
        for relative_path in missing_files:
            print(f"  - {relative_path}", file=sys.stderr)
        return 1

    print(
        "Applied "
        f"{total_insertions} AWS session token insertion(s) and "
        f"{total_storage_replacements} storage size normalization replacement(s) and "
        f"{total_hsr_replacements} HSR command cleanup replacement(s) and "
        f"{total_hsr_log_mode_replacements} HSR log mode repair insertion(s) and "
        f"{total_hsr_backup_expansion_replacements} HSR backup expansion replacement(s) and "
        f"{total_hsr_backup_verify_replacements} HSR backup verification replacement(s) and "
        f"{total_launchpad_timeout_replacements} Launchpad timeout replacement(s) and "
        f"{total_srhook_activation_insertions} srHook activation insertion(s) and "
        f"{total_classic_promotable_replacements} classic SAPHanaSR promotable replacement(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
