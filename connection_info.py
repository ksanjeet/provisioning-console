"""SSH connection details for the hosts provisioned by a console job.

Builds a ready-to-use ~/.ssh/config snippet and per-host ssh commands from the generated job variables and
the discovered instances. A bastion (jump host) is used through ProxyJump, so private keys never leave the
user's machine.
"""

import os

CONTAINER_SSH_DIR = "/mnt/ssh"
DEFAULT_REMOTE_USER = "root"


def _host_path(path):
    """Translate a key path inside the container (/mnt/ssh/...) to the path on the Docker host, when known."""
    if not path:
        return path
    host_ssh_dir = os.environ.get("SAP_SSH_DIR_HOST", "").rstrip("/")
    if host_ssh_dir and (path == CONTAINER_SSH_DIR or path.startswith(CONTAINER_SSH_DIR + "/")):
        return host_ssh_dir + path[len(CONTAINER_SSH_DIR):]
    return path


def _planned_hosts(job_vars):
    platform = job_vars.get("sap_vm_provision_iac_platform", "")
    plan = job_vars.get("sap_vm_provision_host_specification_plan", "")
    host_dict = job_vars.get(f"sap_vm_provision_{platform}_host_specifications_dictionary", {})
    selected = host_dict.get(plan, {}) if isinstance(host_dict, dict) else {}
    hosts = []
    if isinstance(selected, dict):
        for name, spec in selected.items():
            spec = spec if isinstance(spec, dict) else {}
            hosts.append({
                "name": name,
                "sap_host_type": spec.get("sap_host_type", ""),
                "ip": spec.get("cloudstack_vm_ip_address") or spec.get("ovirt_vm_ip") or "",
            })
    return hosts


def build_connection_info(job_vars, instances=None):
    """Return {'hosts': [...], 'ssh_config': str, 'commands': [...], 'notes': [...]}."""
    instances = instances or []
    by_name = {item.get("name"): item for item in instances if item.get("name")}
    hosts = []
    for planned in _planned_hosts(job_vars):
        found = by_name.pop(planned["name"], {})
        hosts.append({**planned, "ip": found.get("private_ip") or planned["ip"], "state": found.get("state", "")})
    for name, found in by_name.items():  # discovered but not in the plan (e.g. scale-out node names)
        hosts.append({"name": name, "sap_host_type": "", "ip": found.get("private_ip", ""), "state": found.get("state", "")})

    host_key = _host_path(job_vars.get("sap_vm_provision_ssh_host_private_key_file_path", ""))
    use_bastion = bool(job_vars.get("sap_vm_provision_bastion_execution")) and bool(job_vars.get("sap_vm_provision_bastion_public_ip"))
    notes = []
    lines = ["# SSH access to the hosts of this deployment - add to ~/.ssh/config", ""]
    commands = []

    if use_bastion:
        bastion_key = _host_path(job_vars.get("sap_vm_provision_ssh_bastion_private_key_file_path", "") or host_key)
        lines += [
            "Host sap-bastion",
            f"    HostName {job_vars.get('sap_vm_provision_bastion_public_ip')}",
            f"    User {job_vars.get('sap_vm_provision_bastion_user') or 'ENTER_BASTION_USER'}",
            f"    Port {job_vars.get('sap_vm_provision_bastion_ssh_port') or 22}",
        ]
        if bastion_key:
            lines.append(f"    IdentityFile {bastion_key}")
        lines += ["    IdentitiesOnly yes", ""]
        commands.append({"name": "bastion", "command": "ssh sap-bastion"})
        notes.append("The hosts have no public IP; they are reached through the bastion with ProxyJump. "
                     "Your private key stays on your machine.")

    for host in hosts:
        lines.append(f"Host {host['name']}")
        lines.append(f"    HostName {host['ip'] or 'ENTER_HOST_IP'}")
        lines.append(f"    User {DEFAULT_REMOTE_USER}")
        if host_key:
            lines.append(f"    IdentityFile {host_key}")
        lines.append("    IdentitiesOnly yes")
        if use_bastion:
            lines.append("    ProxyJump sap-bastion")
        lines.append("")
        commands.append({"name": host["name"], "command": f"ssh {host['name']}"})
        if not host["ip"]:
            notes.append(f"The IP address of {host['name']} is not known yet; replace ENTER_HOST_IP once the host exists.")

    if host_key.startswith(CONTAINER_SSH_DIR + "/"):
        notes.append(f"Key paths are paths inside the console container ({CONTAINER_SSH_DIR}); use the matching file "
                     "of the directory mounted as SAP_SSH_DIR on your machine.")
    if not hosts:
        notes.append("No hosts were found for this job.")

    return {
        "hosts": hosts,
        "ssh_config": "\n".join(lines).rstrip() + "\n",
        "commands": commands,
        "notes": notes,
        "bastion": use_bastion,
    }
