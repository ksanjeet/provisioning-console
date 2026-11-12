#!/usr/bin/env python3
"""Provisioning Console web UI for the SAP automation project."""

import configparser
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from urllib.parse import quote

import yaml
import cloudstack_support
import connection_info
import gcp_support
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from container.apply_collection_patches import (
    HSR_COMMAND_ENVIRONMENT,
    HSR_EXECUTABLE_PATH,
    HSR_LOG_MODE_REPAIR_NAME,
    HSR_LOG_MODE_TARGET,
)

try:
    import keyring
except ImportError:  # pragma: no cover - optional security dependency
    keyring = None

BASE_DIR = Path(__file__).resolve().parent
SCENARIO_DIR = BASE_DIR / "deploy_scenarios"
STATE_DIR = Path(os.environ.get("SAP_WIZARD_STATE_DIR", str(BASE_DIR))).expanduser()
GENERATED_DIR = Path(os.environ.get("SAP_WIZARD_GENERATED_DIR", str(STATE_DIR / "generated"))).expanduser()
RUNS_DIR = GENERATED_DIR / "runs"
CREDENTIALS_FILE = Path(
    os.environ.get("SAP_WIZARD_CREDENTIALS_FILE", str(STATE_DIR / ".sap_wizard_credentials.json"))
).expanduser()
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
RUNS_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_IAC_TYPES = ["ansible", "ansible_to_terraform", "existing_hosts"]
ALLOWED_PLATFORMS = [
    "aws_ec2_vs",
    "gcp_ce_vm",
    "msazure_vm",
    "ibmcloud_powervs",
    "ibmcloud_vs",
    "ibmpowervm_vm",
    "kubevirt_vm",
    "ovirt_vm",
    "vmware_vm",
    "cloudstack_vm",
    "existing_hosts",
]
CONFIGURABLE_PROVIDERS = [
    "aws_ec2_vs",
    "gcp_ce_vm",
    "msazure_vm",
    "cloudstack_vm",
]

JOB_STATE = {}
SCENARIO_CATALOG_CACHE = {}
PROVIDER_DASHBOARD_CACHE = {"timestamp": 0, "value": None}
KEYRING_SERVICE = "sap-playbook-wizard"
COLLECTION_PATCH_SCRIPT = BASE_DIR / "container" / "apply_collection_patches.py"
COLLECTION_PATCH_REQUIREMENTS = [
    {
        "target": Path(HSR_LOG_MODE_TARGET),
        "needle": HSR_LOG_MODE_REPAIR_NAME,
    },
    {
        "target": Path(
            "ansible_collections/community/sap_infrastructure/roles/sap_vm_provision/tasks/platform_ansible/aws_ec2_vs/execute_main.yml"
        ),
        "needle": 'session_token: "{{ sap_vm_provision_aws_session_token | d(omit) }}"',
    },
    {
        "target": Path("ansible_collections/community/sap_install/roles/sap_storage_setup/tasks/configuration/main.yml"),
        "needle": "combine({'disk_size_gb': (__size_gb | int)})",
    },
    {
        "target": Path(
            "ansible_collections/community/sap_install/roles/sap_ha_install_hana_hsr/tasks/pre_tasks/get_replication_status.yml"
        ),
        "needle": HSR_COMMAND_ENVIRONMENT + HSR_EXECUTABLE_PATH + "/hdbnsutil",
    },
    {
        "target": Path("ansible_collections/community/sap_install/roles/sap_ha_install_hana_hsr/tasks/enable_hsr.yml"),
        "needle": HSR_COMMAND_ENVIRONMENT + HSR_EXECUTABLE_PATH + "/hdbnsutil",
    },
    {
        "target": Path("ansible_collections/community/sap_install/roles/sap_ha_install_hana_hsr/tasks/disable_hsr.yml"),
        "needle": HSR_COMMAND_ENVIRONMENT + HSR_EXECUTABLE_PATH + "/hdbnsutil",
    },
    {
        "target": Path("ansible_collections/community/sap_launchpad/plugins/module_utils/sap_api_common.py"),
        "needle": "DEFAULT_REQUEST_TIMEOUT = (30, 300)",
    },
    {
        "target": Path(
            "ansible_collections/community/sap_install/roles/sap_ha_install_hana_hsr/tasks/pre_tasks/create_backup.yml"
        ),
        "needle": "printf '%s' \"{{ __sap_ha_install_hana_hsr_executable_path | regex_replace('/exe$', '') }}\"",
    },
    {
        "target": Path(
            "ansible_collections/community/sap_install/roles/sap_ha_install_hana_hsr/tasks/pre_tasks/create_backup.yml"
        ),
        "needle": "find {{ __sap_ha_install_hana_hsr_fact_backup_path_final }}\n      -type f -name {{ __sap_ha_install_hana_hsr_fact_backup_prefix }}*\n      -exec /usr/sap/{{ __sap_ha_install_hana_hsr_sid }}/SYS/exe/hdb/hdbbackupcheck {} \\;",
    },
    {
        "target": Path(
            "ansible_collections/community/sap_install/roles/sap_ha_install_hana_hsr/tasks/pre_tasks/create_backup.yml"
        ),
        "needle": ". \"{{ __sap_ha_install_hana_hsr_executable_path }}/hdbenv.sh\" >/dev/null 2>&1 || true;\n      export LD_LIBRARY_PATH=\"{{ __sap_ha_install_hana_hsr_executable_path }}:${LD_LIBRARY_PATH:-}\" &&\n      export SAP_RETRIEVAL_PATH=\"${SAP_RETRIEVAL_PATH:-{{ __sap_ha_install_hana_hsr_executable_path }}}\" &&\n      export DIR_EXECUTABLE=\"${DIR_EXECUTABLE:-{{ __sap_ha_install_hana_hsr_executable_path }}}\" &&\n      export SECUDIR=\"${SECUDIR:-{{ __sap_ha_install_hana_hsr_executable_path }}/sec}\" &&",
    },
]
PLATFORM_LABELS = {
    "aws_ec2_vs": "AWS EC2",
    "gcp_ce_vm": "GCP Compute Engine",
    "msazure_vm": "Azure VM",
    "ibmcloud_vs": "IBM Cloud Virtual Server",
    "ibmcloud_powervs": "IBM Cloud PowerVS",
    "ibmpowervm_vm": "IBM PowerVM",
    "kubevirt_vm": "KubeVirt",
    "ovirt_vm": "oVirt/KVM",
    "vmware_vm": "VMware vSphere",
    "cloudstack_vm": "Apache CloudStack",
    "existing_hosts": "Existing hosts",
}
DEFAULT_SSH_HOST_ARGS = (
    "-o ConnectTimeout=180 "
    "-o ControlMaster=auto "
    "-o ControlPersist=3600s "
    "-o StrictHostKeyChecking=no "
    "-o UserKnownHostsFile=/dev/null "
    "-o ForwardX11=no"
)
DEFAULT_SSH_BASTION_ARGS = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
PROVIDER_DASHBOARD_CACHE_TTL_SECONDS = 30
DASHBOARD_PROVIDERS = [
    {"key": "aws_ec2_vs", "label": "AWS", "accent": "aws"},
    {"key": "gcp_ce_vm", "label": "GCP", "accent": "gcp"},
    {"key": "msazure_vm", "label": "Azure", "accent": "azure"},
    {"key": "cloudstack_vm", "label": "CloudStack", "accent": "cloudstack"},
]
ACTIVE_INSTANCE_STATES = ["pending", "running", "stopping", "stopped", "shutting-down"]
PROVIDER_PROFILE_FIELDS = {
    "aws_ec2_vs": ["aws_access_key", "aws_secret_access_key", "aws_session_token", "aws_region"],
    "gcp_ce_vm": ["gcp_credentials_json", "gcp_project"],
    "msazure_vm": ["azure_subscription_id", "azure_tenant_id", "azure_client_id", "azure_client_secret", "azure_resource_group_name"],
    "cloudstack_vm": cloudstack_support.PROFILE_FIELDS,
}
PROVIDER_PROFILE_REQUIRED_FIELDS = {
    "aws_ec2_vs": ["aws_access_key", "aws_secret_access_key", "aws_region"],
    "gcp_ce_vm": ["gcp_credentials_json", "gcp_project"],
    "msazure_vm": ["azure_subscription_id", "azure_tenant_id", "azure_client_id", "azure_client_secret", "azure_resource_group_name"],
    "cloudstack_vm": cloudstack_support.PROFILE_REQUIRED_FIELDS,
}

def _load_or_create_secret_key():
    """FLASK_SECRET_KEY if set, otherwise a random key generated once and kept in the state directory."""
    configured = os.environ.get("FLASK_SECRET_KEY", "")
    if configured and configured not in ("change-me", "sap-ui-dev-secret"):
        return configured
    key_file = STATE_DIR / ".flask_secret_key"
    try:
        if key_file.exists():
            existing = key_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        generated = secrets.token_hex(32)
        key_file.write_text(generated, encoding="utf-8")
        os.chmod(key_file, 0o600)
        return generated
    except OSError:
        return secrets.token_hex(32)


app = Flask(__name__)
app.config["SECRET_KEY"] = _load_or_create_secret_key()

# Optional HTTP basic authentication, recommended when the console is reachable from other machines:
# set SAP_WIZARD_AUTH_USER and SAP_WIZARD_AUTH_PASSWORD (e.g. in .env for Docker Compose).
AUTH_USER = os.environ.get("SAP_WIZARD_AUTH_USER", "")
AUTH_PASSWORD = os.environ.get("SAP_WIZARD_AUTH_PASSWORD", "")


@app.before_request
def _require_basic_auth():
    if not (AUTH_USER and AUTH_PASSWORD):
        return None
    auth = request.authorization
    if (
        auth is not None
        and hmac.compare_digest(auth.username or "", AUTH_USER)
        and hmac.compare_digest(auth.password or "", AUTH_PASSWORD)
    ):
        return None
    return app.response_class(
        "Authentication required.", 401, {"WWW-Authenticate": 'Basic realm="SAP provisioning console"'}
    )


def discover_scenarios():
    scenarios = []
    if not SCENARIO_DIR.exists():
        return scenarios

    for item in sorted(SCENARIO_DIR.iterdir()):
        if item.is_dir() and (item / "ansible_playbook.yml").exists():
            label = item.name.replace("_", " ").title()
            label = re.sub(r"\bSap\b", "SAP", label)
            label = re.sub(r"\bBw4Hana\b", "BW/4HANA", label)
            label = re.sub(r"\bS4Hana\b", "S/4HANA", label)
            label = re.sub(r"\bEcc\b", "ECC", label)
            label = re.sub(r"\bHana\b", "HANA", label)
            label = re.sub(r"\bHa\b", "HA", label)
            scenarios.append({"name": item.name, "label": label})
    return scenarios


def _load_yaml_document(path):
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    return loaded if isinstance(loaded, dict) else {}


def get_host_specification_variable_name(platform):
    if not platform or platform == "existing_hosts":
        return None
    return f"sap_vm_provision_{platform}_host_specifications_dictionary"


def get_scenario_platform_document(scenario_name, platform):
    if not scenario_name or not platform or platform == "existing_hosts":
        return {}

    scenario_path = SCENARIO_DIR / scenario_name
    if not scenario_path.exists():
        return {}

    merged = _load_yaml_document(scenario_path / "ansible_extravars.yml")
    platform_template = scenario_path / f"ansible_extravars_{platform}.yml"
    if platform_template.exists():
        merged = {**merged, **_load_yaml_document(platform_template)}
    return merged


def get_scenario_host_specifications(scenario_name, platform):
    merged = get_scenario_platform_document(scenario_name, platform)
    host_spec_var = get_host_specification_variable_name(platform)
    host_specs = merged.get(host_spec_var, {}) if host_spec_var else {}
    if not isinstance(host_specs, dict):
        return {}
    return host_specs


def get_scenario_host_plans(scenario_name, platform):
    host_specs = get_scenario_host_specifications(scenario_name, platform)
    if not isinstance(host_specs, dict):
        return []
    return sorted(host_specs.keys())


def build_host_plan_catalog():
    catalog = {}
    for scenario in discover_scenarios():
        scenario_plans = {}
        for platform in ALLOWED_PLATFORMS:
            plans = get_scenario_host_plans(scenario["name"], platform)
            if plans:
                scenario_plans[platform] = plans
        catalog[scenario["name"]] = scenario_plans
    return catalog


def build_instance_profile_catalog():
    catalog = {}
    for scenario in discover_scenarios():
        scenario_profiles = {}
        for platform in ALLOWED_PLATFORMS:
            host_specs = get_scenario_host_specifications(scenario["name"], platform)
            if not host_specs:
                continue

            plan_profiles = {}
            all_profiles = set()
            for plan_name, host_map in host_specs.items():
                if not isinstance(host_map, dict):
                    continue
                host_profiles = []
                for host_name, host_config in host_map.items():
                    if not isinstance(host_config, dict):
                        continue
                    profile = host_config.get("virtual_machine_profile")
                    if not profile:
                        continue
                    host_profiles.append({"host": host_name, "profile": profile})
                    all_profiles.add(profile)
                if host_profiles:
                    plan_profiles[plan_name] = host_profiles

            if plan_profiles:
                scenario_profiles[platform] = {
                    "plans": plan_profiles,
                    "all_profiles": sorted(all_profiles),
                }
        catalog[scenario["name"]] = scenario_profiles
    return catalog


def build_scenario_catalog_bundle():
    host_plan_catalog = {}
    instance_profile_catalog = {}
    scenarios = discover_scenarios()
    for scenario in scenarios:
        scenario_name = scenario["name"]
        scenario_plans = {}
        scenario_profiles = {}
        for platform in ALLOWED_PLATFORMS:
            host_specs = get_scenario_host_specifications(scenario_name, platform)
            if not host_specs:
                continue

            scenario_plans[platform] = sorted(host_specs.keys())

            plan_profiles = {}
            all_profiles = set()
            for plan_name, host_map in host_specs.items():
                if not isinstance(host_map, dict):
                    continue
                host_profiles = []
                for host_name, host_config in host_map.items():
                    if not isinstance(host_config, dict):
                        continue
                    profile = host_config.get("virtual_machine_profile")
                    if not profile:
                        continue
                    host_profiles.append({"host": host_name, "profile": profile})
                    all_profiles.add(profile)
                if host_profiles:
                    plan_profiles[plan_name] = host_profiles

            if plan_profiles:
                scenario_profiles[platform] = {
                    "plans": plan_profiles,
                    "all_profiles": sorted(all_profiles),
                }

        host_plan_catalog[scenario_name] = scenario_plans
        instance_profile_catalog[scenario_name] = scenario_profiles

    return {
        "host_plan_catalog": host_plan_catalog,
        "instance_profile_catalog": instance_profile_catalog,
    }


def _scenario_catalog_stamp():
    scenario_files = []
    scenario_files.extend(SCENARIO_DIR.glob("*/ansible_extravars.yml"))
    scenario_files.extend(SCENARIO_DIR.glob("*/ansible_extravars_*.yml"))
    return max((path.stat().st_mtime for path in scenario_files), default=0)


def get_cached_scenario_catalog(cache_key, builder):
    stamp = _scenario_catalog_stamp()
    cached = SCENARIO_CATALOG_CACHE.get(cache_key)
    if cached and cached.get("stamp") == stamp:
        return cached["value"]

    value = builder()
    SCENARIO_CATALOG_CACHE[cache_key] = {"stamp": stamp, "value": value}
    return value


def get_host_plan_catalog():
    return get_cached_scenario_catalog("scenario_catalog_bundle", build_scenario_catalog_bundle)["host_plan_catalog"]


def get_instance_profile_catalog():
    return get_cached_scenario_catalog("scenario_catalog_bundle", build_scenario_catalog_bundle)["instance_profile_catalog"]


def check_dependencies():
    checks = []

    python_ok = sys.version_info >= (3, 11)
    checks.append({
        "name": "Python 3.11+",
        "ok": python_ok,
        "message": f"Detected {sys.version.split()[0]}",
        "hint": "Use Python 3.11 or newer to match the repository requirements."
    })

    ansible_binary = shutil.which("ansible-playbook")
    ansible_ok = bool(ansible_binary)
    ansible_version = "not found"
    if ansible_ok:
        try:
            version = subprocess.run(
                [ansible_binary, "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                text=True,
                timeout=20,
            )
            ansible_version = version.stdout.splitlines()[0] if version.stdout else "unknown"
            ansible_ok = version.returncode == 0
        except Exception:
            ansible_ok = False
    checks.append({
        "name": "Ansible",
        "ok": ansible_ok,
        "message": ansible_version,
        "hint": "Install Ansible 9+ / ansible-core 2.16+ before executing a playbook."
    })

    required_collections = ["community.sap_infrastructure", "community.sap_install"]
    collections_ok = True
    collection_message = "not checked"
    if ansible_binary:
        try:
            listing = subprocess.run(
                ["ansible-galaxy", "collection", "list"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                text=True,
                timeout=20,
            )
            installed = listing.stdout.lower()
            missing = [name for name in required_collections if name.lower() not in installed]
            collections_ok = not missing
            collection_message = "all required collections installed" if collections_ok else ", ".join(missing)
        except Exception:
            collections_ok = False
            collection_message = "unable to determine"
    checks.append({
        "name": "Ansible collections",
        "ok": collections_ok,
        "message": collection_message,
        "hint": "Install the SAP collections required by this repository before running production deployments."
    })

    collection_patch_results = ensure_ansible_collection_patches()
    if collection_patch_results:
        checks.append({
            "name": "Ansible collection patches",
            "ok": all(item["ok"] for item in collection_patch_results),
            "message": "; ".join(item["message"] for item in collection_patch_results),
            "hint": "The installed SAP collections must include the local compatibility patches used by this UI.",
        })

    git_ok = shutil.which("git") is not None
    checks.append({
        "name": "Git",
        "ok": git_ok,
        "message": "git" if git_ok else "not found",
        "hint": "Git is useful for versioning and troubleshooting the execution directory."
    })

    ssh_ok = shutil.which("ssh") is not None
    checks.append({
        "name": "SSH client",
        "ok": ssh_ok,
        "message": "ssh" if ssh_ok else "not found",
        "hint": "Required for remote host management and existing-host deployments."
    })

    checks.append({
        "name": "Repository layout",
        "ok": SCENARIO_DIR.exists(),
        "message": "deploy_scenarios" if SCENARIO_DIR.exists() else "missing",
        "hint": "The wizard expects the repository to include deploy_scenarios/."
    })

    # Check for AWS SDKs used by the terraform/ansible provisioning role.
    aws_sdk_ok = True
    aws_sdk_message = "boto3 and botocore available"
    try:
        import boto3  # noqa: F401
        import botocore  # noqa: F401
    except Exception:
        aws_sdk_ok = False
        aws_sdk_message = "missing boto3/botocore"
    checks.append({
        "name": "AWS SDK for provisioning",
        "ok": aws_sdk_ok,
        "message": aws_sdk_message,
        "hint": "Install boto3 and botocore in the same Python environment that Ansible uses for AWS provisioning."
    })

    return checks


def parse_yaml_override(raw_value):
    text = (raw_value or "").strip()
    if not text:
        return {}
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML override: {exc}") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError("YAML override must be a dictionary/object.")
    return parsed


def _read_json_file(path):
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json_file(path, payload):
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.chmod(path, 0o600)
    except OSError:
        pass


def reset_provider_dashboard_cache():
    PROVIDER_DASHBOARD_CACHE["timestamp"] = 0
    PROVIDER_DASHBOARD_CACHE["value"] = None


def _iter_ansible_collection_roots():
    seen = set()
    configured_paths = [item for item in os.environ.get("ANSIBLE_COLLECTIONS_PATH", "").split(os.pathsep) if item]
    configured_paths.extend([
        str(Path.home() / ".ansible" / "collections"),
        "/usr/share/ansible/collections",
        "/opt/ansible/collections",
    ])

    for raw_path in configured_paths:
        candidate = Path(raw_path).expanduser()
        resolved = str(candidate.resolve()) if candidate.exists() else str(candidate)
        if resolved in seen or not candidate.exists():
            continue
        seen.add(resolved)
        yield candidate


def ensure_ansible_collection_patches():
    results = []
    if not COLLECTION_PATCH_SCRIPT.exists():
        return [{
            "ok": False,
            "message": f"Missing collection patch helper: {COLLECTION_PATCH_SCRIPT}",
        }]

    for collections_root in _iter_ansible_collection_roots():
        missing_targets = [
            requirement["target"]
            for requirement in COLLECTION_PATCH_REQUIREMENTS
            if not (collections_root / requirement["target"]).exists()
        ]
        if missing_targets:
            continue

        patch_missing = False
        for requirement in COLLECTION_PATCH_REQUIREMENTS:
            target = collections_root / requirement["target"]
            target_text = target.read_text(encoding="utf-8")
            if requirement["needle"] not in target_text:
                patch_missing = True
                break

        if not patch_missing:
            results.append({
                "ok": True,
                "message": f"Required Ansible collection patches already present in {collections_root}.",
            })
            continue

        completed = subprocess.run(
            [sys.executable, str(COLLECTION_PATCH_SCRIPT), str(collections_root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            results.append({
                "ok": False,
                "message": (
                    f"Failed to patch installed Ansible collections under {collections_root}: "
                    f"{completed.stdout.strip() or 'unknown error'}"
                ),
            })
            continue

        unpatched = [
            str(requirement["target"])
            for requirement in COLLECTION_PATCH_REQUIREMENTS
            if requirement["needle"] not in (collections_root / requirement["target"]).read_text(encoding="utf-8")
        ]
        if unpatched:
            results.append({
                "ok": False,
                "message": f"Collection patches remain incomplete under {collections_root}: {', '.join(unpatched)}",
            })
            continue

        results.append({
            "ok": True,
            "message": f"{completed.stdout.strip() or 'Applied required Ansible collection patches.'} ({collections_root})",
        })

    return results


def _read_aws_cli_profile(profile_name="default"):
    candidates = []
    if profile_name and profile_name != "default":
        candidates.append(profile_name)
    candidates.extend(["default"])

    credentials = {}
    credentials_path = Path.home() / ".aws" / "credentials"
    if credentials_path.exists():
        try:
            parser = configparser.ConfigParser()
            parser.read(credentials_path)
        except Exception:
            parser = None
        if parser is not None:
            for candidate in candidates:
                if not parser.has_section(candidate):
                    continue
                for key in ["aws_access_key_id", "aws_secret_access_key", "aws_session_token"]:
                    value = parser.get(candidate, key, fallback=None)
                    if value and value.strip():
                        credentials[key] = value.strip()
                break

    config_path = Path.home() / ".aws" / "config"
    if config_path.exists():
        try:
            parser = configparser.ConfigParser()
            parser.read(config_path)
        except Exception:
            parser = None
        if parser is not None:
            for candidate in candidates:
                if not parser.has_section(candidate):
                    continue
                value = parser.get(candidate, "region", fallback=None)
                if value and value.strip():
                    credentials["aws_region"] = value.strip()
                break

    return credentials


CONTAINER_SSH_DIR = "/mnt/ssh"
# Wizard and provider profile fields holding paths of key or credential files
PATH_FIELDS = (
    "gcp_credentials_json",
    "host_ssh_private_key_file",
    "bastion_private_key_file",
    "cloudstack_kvm_host_ssh_private_key_file",
)


def to_container_path(path):
    """Map a path of the Docker host SSH directory (SAP_SSH_DIR, e.g. /home/user/.ssh/key or ~/.ssh/key)
    to its mount point in the container (/mnt/ssh/key). Other paths are returned unchanged."""
    if not isinstance(path, str) or not path.strip():
        return path
    value = path.strip()
    if not Path(CONTAINER_SSH_DIR).is_dir():
        return value  # not running in the container
    host_ssh_dir = os.environ.get("SAP_SSH_DIR_HOST", "").rstrip("/")
    for prefix in filter(None, [host_ssh_dir, "~/.ssh", "$HOME/.ssh"]):
        if value == prefix or value.startswith(prefix + "/"):
            return CONTAINER_SSH_DIR + value[len(prefix):]
    return value


def _normalize_path_fields(values):
    for key in PATH_FIELDS:
        if key in values:
            values[key] = to_container_path(values[key])
    return values


def discover_saved_credentials():
    merged = {}

    if _has_persistent_keyring():
        for platform in ALLOWED_PLATFORMS:
            try:
                stored = keyring.get_password(KEYRING_SERVICE, platform)
            except Exception:
                stored = None
            if not stored:
                continue
            try:
                parsed = json.loads(stored)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                merged[platform] = parsed

    file_data = _read_json_file(CREDENTIALS_FILE)
    for platform, values in file_data.items():
        if isinstance(values, dict) and platform not in merged:
            merged[platform] = values

    for values in merged.values():
        _normalize_path_fields(values)
    return merged


def _mask_credential_value(key, value):
    if value in (None, ""):
        return value
    lowered = key.lower()
    if any(token in lowered for token in ["secret", "password", "token", "key", "json", "credential"]):
        return "***redacted***"
    return value


def _has_persistent_keyring():
    if keyring is None:
        return False
    try:
        backend = keyring.get_keyring()
    except Exception:
        return False
    backend_name = backend.__class__.__name__
    backend_module = backend.__class__.__module__
    if backend_name == "NullKeyring" or backend_module == "keyring.backends.null":
        return False
    return True


def _persist_platform_profile(platform, platform_map):
    if _has_persistent_keyring():
        try:
            keyring.set_password(KEYRING_SERVICE, platform, json.dumps(platform_map, sort_keys=True))
            _write_json_file(CREDENTIALS_FILE, {platform: {k: _mask_credential_value(k, v) for k, v in platform_map.items()}})
            reset_provider_dashboard_cache()
            return
        except Exception:
            pass

    saved = discover_saved_credentials()
    fallback = {**saved}
    fallback[platform] = platform_map
    _write_json_file(CREDENTIALS_FILE, fallback)
    reset_provider_dashboard_cache()


def save_credentials_for_platform(config):
    platform = config.get("platform")
    if not platform:
        return
    saved = discover_saved_credentials()
    platform_map = saved.setdefault(platform, {})
    wizard_profile_fields = {
        "aws_ec2_vs": [
            "aws_access_key",
            "aws_secret_access_key",
            "aws_session_token",
            "aws_region",
            "aws_vpc_availability_zone",
            "aws_vpc_subnet_id",
            "aws_vpc_sg_names",
            "aws_key_pair_name_ssh_host_public_key",
        ],
        "gcp_ce_vm": [
            "gcp_credentials_json",
            "gcp_project",
            "gcp_region_zone",
            "gcp_region",
            "gcp_vpc_name",
            "gcp_vpc_subnet_name",
        ],
        "msazure_vm": [
            "azure_subscription_id",
            "azure_tenant_id",
            "azure_client_id",
            "azure_client_secret",
            "azure_resource_group_name",
            "azure_region",
            "azure_vnet_name",
            "azure_vnet_subnet_name",
        ],
        "cloudstack_vm": cloudstack_support.PROFILE_FIELDS + cloudstack_support.DEPLOYMENT_FIELDS,
    }
    for key in wizard_profile_fields.get(platform, []):
        value = config.get(key)
        if value:
            platform_map[key] = value

    if platform in {"aws_ec2_vs", "gcp_ce_vm", "msazure_vm", "cloudstack_vm"}:
        dns_value = config.get("dns_root_domain")
        if dns_value:
            platform_map["dns_root_domain"] = dns_value
    _persist_platform_profile(platform, platform_map)


def save_provider_profile(platform, payload):
    if platform not in CONFIGURABLE_PROVIDERS:
        raise ValueError("Unsupported provider.")

    existing = discover_saved_credentials().get(platform, {}).copy()
    fields = PROVIDER_PROFILE_FIELDS.get(platform, [])
    required_fields = PROVIDER_PROFILE_REQUIRED_FIELDS.get(platform, [])

    for key in fields:
        if key not in payload:
            continue
        value = payload.get(key)
        if isinstance(value, str):
            value = value.strip()
        if value:
            existing[key] = value
        elif key in existing:
            existing.pop(key, None)

    _normalize_path_fields(existing)
    missing = [key for key in required_fields if not existing.get(key)]
    if missing:
        raise ValueError("Missing required field(s): {}.".format(", ".join(missing)))

    _persist_platform_profile(platform, existing)
    return existing


def forget_saved_credentials(platform):
    if not platform:
        return

    if keyring is not None:
        try:
            keyring.delete_password(KEYRING_SERVICE, platform)
        except Exception:
            pass

    saved = _read_json_file(CREDENTIALS_FILE)
    if platform in saved:
        del saved[platform]
        _write_json_file(CREDENTIALS_FILE, saved)
    reset_provider_dashboard_cache()


def is_masked_value(value):
    return isinstance(value, str) and value.strip() == "***redacted***"


def _build_dashboard_resource(provider, accent, resource_id, name, state, summary, **extra):
    resource = {
        "provider": provider,
        "accent": accent,
        "resource_id": resource_id,
        "name": name,
        "state": state,
        "summary": summary,
        "deletable": False,
    }
    resource.update(extra)
    return resource


def _build_provider_status(provider, accent, status, summary, details=None, resources=None):
    return {
        "provider": provider,
        "accent": accent,
        "status": status,
        "summary": summary,
        "details": details or [],
        "resources": resources or [],
    }


def _list_missing_or_masked(profile, required_keys):
    missing = []
    masked = []
    for key in required_keys:
        value = (profile or {}).get(key)
        if value in (None, ""):
            missing.append(key)
        elif is_masked_value(value):
            masked.append(key)
    return missing, masked


def _http_json_request(url, headers=None, method="GET", body=None, timeout=20):
    request = urllib_request.Request(url, headers=headers or {}, method=method)
    if body is not None:
        request.data = body
    with urllib_request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload) if payload else {}


def _azure_token_request(tenant_id, client_id, client_secret):
    body = urllib_parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": "https://management.azure.com/.default",
    }).encode("utf-8")
    payload = _http_json_request(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
        body=body,
    )
    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError("Azure token endpoint did not return an access token.")
    return access_token


def _gcp_access_token(credentials_path):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError("google-auth and requests are required for GCP connectivity checks.") from exc

    credentials = service_account.Credentials.from_service_account_file(
        str(credentials_path),
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    credentials.refresh(Request())
    if not credentials.token:
        raise RuntimeError("GCP service account refresh did not yield an access token.")
    return credentials


def _parse_azure_power_state(statuses):
    for status in statuses or []:
        code = status.get("code") or ""
        if code.startswith("PowerState/"):
            return code.split("/", 1)[1]
    return "unknown"


def _extract_azure_resource_group(resource_id):
    parts = [part for part in (resource_id or "").split("/") if part]
    for index, part in enumerate(parts):
        if part.lower() == "resourcegroups" and index + 1 < len(parts):
            return parts[index + 1]
    return None



def inspect_aws_dashboard(saved_credentials):
    provider_key = "aws_ec2_vs"
    profile = saved_credentials.get(provider_key, {})
    if not profile:
        return _build_provider_status(
            provider_key,
            "aws",
            "unknown",
            "No saved AWS profile yet.",
            ["Save an AWS profile in the wizard to enable live connectivity checks and inventory."],
        )

    missing, masked = _list_missing_or_masked(profile, ["aws_access_key", "aws_secret_access_key", "aws_region"])
    if missing or masked:
        detail = []
        if missing:
            detail.append(f"Missing fields: {', '.join(missing)}.")
        if masked:
            detail.append(f"Masked-only fields: {', '.join(masked)}. Re-enter them in the wizard on this host.")
        return _build_provider_status(provider_key, "aws", "degraded", "AWS profile is incomplete for live checks.", detail)

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        return _build_provider_status(
            provider_key,
            "aws",
            "degraded",
            "AWS SDK is not available.",
            ["Install boto3 and botocore to enable AWS connectivity checks."],
        )

    try:
        session = boto3.session.Session(
            aws_access_key_id=profile.get("aws_access_key"),
            aws_secret_access_key=profile.get("aws_secret_access_key"),
            aws_session_token=profile.get("aws_session_token") or None,
            region_name=profile.get("aws_region"),
        )
        sts_client = session.client("sts")
        identity = sts_client.get_caller_identity()
        ec2_client = session.client("ec2", region_name=profile.get("aws_region"))
        response = ec2_client.describe_instances(
            Filters=[{"Name": "instance-state-name", "Values": ACTIVE_INSTANCE_STATES}]
        )
    except ClientError as exc:
        error = exc.response.get("Error", {})
        return _build_provider_status(
            provider_key,
            "aws",
            "error",
            "AWS connectivity failed.",
            [f"{error.get('Code', 'Unknown')}: {error.get('Message', str(exc))}"],
        )
    except BotoCoreError as exc:
        return _build_provider_status(provider_key, "aws", "error", "AWS connectivity failed.", [str(exc)])

    resources = []
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            name = next(
                (tag.get("Value") for tag in instance.get("Tags", []) if tag.get("Key") == "Name"),
                instance.get("InstanceId"),
            )
            state = (instance.get("State") or {}).get("Name", "unknown")
            instance_id = instance.get("InstanceId")
            resources.append(
                _build_dashboard_resource(
                    provider_key,
                    "aws",
                    instance_id,
                    name,
                    state,
                    "{} | {} | {}".format(
                        instance.get("InstanceType", "unknown"),
                        instance.get("Placement", {}).get("AvailabilityZone", "unknown-zone"),
                        profile.get("aws_region"),
                    ),
                    private_ip=instance.get("PrivateIpAddress"),
                    public_ip=instance.get("PublicIpAddress"),
                    metadata=[
                        f"Account: {identity.get('Account', 'unknown')}",
                        f"VPC subnet: {instance.get('SubnetId') or 'n/a'}",
                    ],
                    deletable=True,
                    delete_payload={"instance_id": instance_id},
                )
            )

    return _build_provider_status(
        provider_key,
        "aws",
        "connected",
        f"Connected as {identity.get('Arn', 'unknown-principal')}. {len(resources)} active EC2 instance(s) visible in {profile.get('aws_region')}.",
        [
            f"Region: {profile.get('aws_region')}",
            f"Account: {identity.get('Account', 'unknown')}",
        ],
        sorted(resources, key=lambda item: item["name"].lower()),
    )


def inspect_gcp_dashboard(saved_credentials):
    provider_key = "gcp_ce_vm"
    profile = saved_credentials.get(provider_key, {})
    if not profile:
        return _build_provider_status(
            provider_key,
            "gcp",
            "unknown",
            "No saved GCP profile yet.",
            ["Save a GCP service account path and project in the wizard to enable live checks."],
        )

    missing, masked = _list_missing_or_masked(profile, ["gcp_credentials_json", "gcp_project"])
    if missing or masked:
        detail = []
        if missing:
            detail.append(f"Missing fields: {', '.join(missing)}.")
        if masked:
            detail.append(f"Masked-only fields: {', '.join(masked)}. Re-enter them in the wizard on this host.")
        return _build_provider_status(provider_key, "gcp", "degraded", "GCP profile is incomplete for live checks.", detail)

    credentials_path = Path(profile.get("gcp_credentials_json")).expanduser()
    if not credentials_path.exists():
        return _build_provider_status(
            provider_key,
            "gcp",
            "error",
            "GCP credentials file is missing.",
            [f"Expected file: {credentials_path}",
             f"The console runs in a container: put the key into the directory mounted as SAP_SSH_DIR and use "
             f"{CONTAINER_SSH_DIR}/<file name> (host paths of that directory are translated automatically)."],
        )

    try:
        credentials = _gcp_access_token(credentials_path)
        project = profile.get("gcp_project")
        base_url = f"https://compute.googleapis.com/compute/v1/projects/{project}/aggregated/instances"
        headers = {"Authorization": f"Bearer {credentials.token}"}
        resources = []
        next_page_token = None
        while True:
            params = {"maxResults": 250}
            if next_page_token:
                params["pageToken"] = next_page_token
            payload = _http_json_request(f"{base_url}?{urllib_parse.urlencode(params)}", headers=headers)
            for zone_name, zone_payload in (payload.get("items") or {}).items():
                for instance in zone_payload.get("instances", []) or []:
                    zone = instance.get("zone", zone_name).split("/")[-1]
                    machine_type = (instance.get("machineType") or "").split("/")[-1]
                    network_interfaces = instance.get("networkInterfaces") or []
                    private_ip = network_interfaces[0].get("networkIP") if network_interfaces else None
                    public_ip = None
                    if network_interfaces:
                        access_configs = network_interfaces[0].get("accessConfigs") or []
                        if access_configs:
                            public_ip = access_configs[0].get("natIP")
                    resources.append(
                        _build_dashboard_resource(
                            provider_key,
                            "gcp",
                            str(instance.get("id") or instance.get("name")),
                            instance.get("name"),
                            instance.get("status", "unknown"),
                            "{} | {} | {}".format(machine_type or "unknown-type", zone, project),
                            private_ip=private_ip,
                            public_ip=public_ip,
                            metadata=[
                                f"Project: {project}",
                                f"Zone: {zone}",
                            ],
                            deletable=True,
                            delete_payload={"name": instance.get("name"), "zone": zone},
                        )
                    )
            next_page_token = payload.get("nextPageToken")
            if not next_page_token:
                break
    except urllib_error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        return _build_provider_status(provider_key, "gcp", "error", "GCP connectivity failed.", [message or str(exc)])
    except Exception as exc:
        return _build_provider_status(provider_key, "gcp", "error", "GCP connectivity failed.", [str(exc)])

    service_account_email = getattr(credentials, "service_account_email", "unknown-service-account")
    return _build_provider_status(
        provider_key,
        "gcp",
        "connected",
        f"Connected as {service_account_email}. {len(resources)} Compute Engine VM(s) visible in project {profile.get('gcp_project')}.",
        [
            f"Project: {profile.get('gcp_project')}",
            f"Credentials: {credentials_path}",
        ],
        sorted(resources, key=lambda item: item["name"].lower()),
    )


def inspect_azure_dashboard(saved_credentials):
    provider_key = "msazure_vm"
    profile = saved_credentials.get(provider_key, {})
    if not profile:
        return _build_provider_status(
            provider_key,
            "azure",
            "unknown",
            "No saved Azure profile yet.",
            ["Save Azure tenant, subscription, and client credentials in the wizard to enable live checks."],
        )

    missing, masked = _list_missing_or_masked(
        profile,
        ["azure_subscription_id", "azure_tenant_id", "azure_client_id", "azure_client_secret"],
    )
    if missing or masked:
        detail = []
        if missing:
            detail.append(f"Missing fields: {', '.join(missing)}.")
        if masked:
            detail.append(f"Masked-only fields: {', '.join(masked)}. Re-enter them in the wizard on this host.")
        return _build_provider_status(provider_key, "azure", "degraded", "Azure profile is incomplete for live checks.", detail)

    try:
        access_token = _azure_token_request(
            profile.get("azure_tenant_id"),
            profile.get("azure_client_id"),
            profile.get("azure_client_secret"),
        )
        headers = {"Authorization": f"Bearer {access_token}"}
        subscription_id = profile.get("azure_subscription_id")
        subscription = _http_json_request(
            f"https://management.azure.com/subscriptions/{subscription_id}?api-version=2020-01-01",
            headers=headers,
        )
        virtual_machines = _http_json_request(
            "https://management.azure.com/subscriptions/{}/providers/Microsoft.Compute/virtualMachines"
            "?api-version=2024-03-01&$expand=instanceView".format(subscription_id),
            headers=headers,
        )
    except urllib_error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        return _build_provider_status(provider_key, "azure", "error", "Azure connectivity failed.", [message or str(exc)])
    except Exception as exc:
        return _build_provider_status(provider_key, "azure", "error", "Azure connectivity failed.", [str(exc)])

    resources = []
    for vm in virtual_machines.get("value", []):
        resource_group = _extract_azure_resource_group(vm.get("id"))
        instance_view = ((vm.get("properties") or {}).get("instanceView") or {})
        state = _parse_azure_power_state(instance_view.get("statuses"))
        resources.append(
            _build_dashboard_resource(
                provider_key,
                "azure",
                vm.get("id"),
                vm.get("name"),
                state,
                "{} | {} | {}".format(
                    ((vm.get("properties") or {}).get("hardwareProfile") or {}).get("vmSize", "unknown-size"),
                    vm.get("location", "unknown-location"),
                    resource_group or "unknown-rg",
                ),
                metadata=[
                    f"Subscription: {subscription_id}",
                    f"Resource group: {resource_group or 'unknown'}",
                ],
                deletable=True,
                delete_payload={"resource_id": vm.get("id")},
            )
        )

    return _build_provider_status(
        provider_key,
        "azure",
        "connected",
        "Connected to subscription {}. {} Azure VM(s) visible.".format(
            subscription.get("displayName", subscription_id),
            len(resources),
        ),
        [
            f"Subscription: {subscription.get('displayName', subscription_id)}",
            f"Default region: {profile.get('azure_region') or 'n/a'}",
        ],
        sorted(resources, key=lambda item: item["name"].lower()),
    )



def inspect_azure_dashboard_scoped(saved_credentials):
    provider_key = "msazure_vm"
    profile = saved_credentials.get(provider_key, {})
    if not profile:
        return _build_provider_status(
            provider_key,
            "azure",
            "unknown",
            "No saved Azure profile yet.",
            ["Save Azure tenant, subscription, client credentials, and resource group in the wizard to enable live checks."],
        )

    missing, masked = _list_missing_or_masked(
        profile,
        [
            "azure_subscription_id",
            "azure_tenant_id",
            "azure_client_id",
            "azure_client_secret",
            "azure_resource_group_name",
        ],
    )
    if missing or masked:
        detail = []
        if missing:
            detail.append(f"Missing fields: {', '.join(missing)}.")
        if masked:
            detail.append(f"Masked-only fields: {', '.join(masked)}. Re-enter them in the wizard on this host.")
        return _build_provider_status(provider_key, "azure", "degraded", "Azure profile is incomplete for live checks.", detail)

    try:
        access_token = _azure_token_request(
            profile.get("azure_tenant_id"),
            profile.get("azure_client_id"),
            profile.get("azure_client_secret"),
        )
        headers = {"Authorization": f"Bearer {access_token}"}
        subscription_id = profile.get("azure_subscription_id")
        resource_group = profile.get("azure_resource_group_name")
        virtual_machines = _http_json_request(
            "https://management.azure.com/subscriptions/{}/resourceGroups/{}/providers/Microsoft.Compute/virtualMachines"
            "?api-version=2024-03-01&$expand=instanceView".format(
                urllib_parse.quote(subscription_id, safe=""),
                urllib_parse.quote(resource_group, safe=""),
            ),
            headers=headers,
        )
    except urllib_error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        return _build_provider_status(provider_key, "azure", "error", "Azure connectivity failed.", [message or str(exc)])
    except Exception as exc:
        return _build_provider_status(provider_key, "azure", "error", "Azure connectivity failed.", [str(exc)])

    resources = []
    for vm in virtual_machines.get("value", []):
        instance_view = ((vm.get("properties") or {}).get("instanceView") or {})
        state = _parse_azure_power_state(instance_view.get("statuses"))
        resources.append(
            _build_dashboard_resource(
                provider_key,
                "azure",
                vm.get("id"),
                vm.get("name"),
                state,
                "{} | {} | {}".format(
                    ((vm.get("properties") or {}).get("hardwareProfile") or {}).get("vmSize", "unknown-size"),
                    vm.get("location", "unknown-location"),
                    resource_group,
                ),
                metadata=[
                    f"Subscription: {subscription_id}",
                    f"Resource group: {resource_group}",
                ],
                deletable=True,
                delete_payload={"resource_id": vm.get("id")},
            )
        )

    return _build_provider_status(
        provider_key,
        "azure",
        "connected",
        "Connected to subscription {} resource group {}. {} Azure VM(s) visible.".format(
            subscription_id,
            resource_group,
            len(resources),
        ),
        [
            f"Subscription: {subscription_id}",
            f"Resource group: {resource_group}",
            f"Default region: {profile.get('azure_region') or 'n/a'}",
        ],
        sorted(resources, key=lambda item: item["name"].lower()),
    )


def inspect_azure_dashboard_scoped_v2(saved_credentials):
    provider_key = "msazure_vm"
    profile = saved_credentials.get(provider_key, {})
    if not profile:
        return _build_provider_status(
            provider_key,
            "azure",
            "unknown",
            "No saved Azure profile yet.",
            ["Save Azure tenant, subscription, client credentials, and resource group in the wizard to enable live checks."],
        )

    missing, masked = _list_missing_or_masked(
        profile,
        [
            "azure_subscription_id",
            "azure_tenant_id",
            "azure_client_id",
            "azure_client_secret",
            "azure_resource_group_name",
        ],
    )
    if missing or masked:
        detail = []
        if missing:
            detail.append(f"Missing fields: {', '.join(missing)}.")
        if masked:
            detail.append(f"Masked-only fields: {', '.join(masked)}. Re-enter them in the wizard on this host.")
        return _build_provider_status(provider_key, "azure", "degraded", "Azure profile is incomplete for live checks.", detail)

    try:
        access_token = _azure_token_request(
            profile.get("azure_tenant_id"),
            profile.get("azure_client_id"),
            profile.get("azure_client_secret"),
        )
        headers = {"Authorization": f"Bearer {access_token}"}
        subscription_id = profile.get("azure_subscription_id")
        resource_group = profile.get("azure_resource_group_name")
        virtual_machines = _http_json_request(
            "https://management.azure.com/subscriptions/{}/resourceGroups/{}/providers/Microsoft.Compute/virtualMachines?api-version=2024-03-01".format(
                urllib_parse.quote(subscription_id, safe=""),
                urllib_parse.quote(resource_group, safe=""),
            ),
            headers=headers,
        )
    except urllib_error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        return _build_provider_status(provider_key, "azure", "error", "Azure connectivity failed.", [message or str(exc)])
    except Exception as exc:
        return _build_provider_status(provider_key, "azure", "error", "Azure connectivity failed.", [str(exc)])

    resources = []
    for vm in virtual_machines.get("value", []):
        vm_name = vm.get("name")
        try:
            instance_view = _http_json_request(
                "https://management.azure.com/subscriptions/{}/resourceGroups/{}/providers/Microsoft.Compute/virtualMachines/{}/instanceView?api-version=2024-03-01".format(
                    urllib_parse.quote(subscription_id, safe=""),
                    urllib_parse.quote(resource_group, safe=""),
                    urllib_parse.quote(vm_name, safe=""),
                ),
                headers=headers,
            )
            state = _parse_azure_power_state(instance_view.get("statuses"))
        except Exception:
            state = "unknown"

        resources.append(
            _build_dashboard_resource(
                provider_key,
                "azure",
                vm.get("id"),
                vm_name,
                state,
                "{} | {} | {}".format(
                    ((vm.get("properties") or {}).get("hardwareProfile") or {}).get("vmSize", "unknown-size"),
                    vm.get("location", "unknown-location"),
                    resource_group,
                ),
                metadata=[
                    f"Subscription: {subscription_id}",
                    f"Resource group: {resource_group}",
                ],
                deletable=True,
                delete_payload={"resource_id": vm.get("id")},
            )
        )

    return _build_provider_status(
        provider_key,
        "azure",
        "connected",
        "Connected to subscription {} resource group {}. {} Azure VM(s) visible.".format(
            subscription_id,
            resource_group,
            len(resources),
        ),
        [
            f"Subscription: {subscription_id}",
            f"Resource group: {resource_group}",
            f"Default region: {profile.get('azure_region') or 'n/a'}",
        ],
        sorted(resources, key=lambda item: item["name"].lower()),
    )


def build_provider_dashboard(force_refresh=False):
    now = time.time()
    cached = PROVIDER_DASHBOARD_CACHE.get("value")
    if (
        not force_refresh
        and cached is not None
        and now - PROVIDER_DASHBOARD_CACHE.get("timestamp", 0) < PROVIDER_DASHBOARD_CACHE_TTL_SECONDS
    ):
        return cached

    saved_credentials = discover_saved_credentials()
    providers = [
        inspect_aws_dashboard(saved_credentials),
        inspect_gcp_dashboard(saved_credentials),
        inspect_azure_dashboard_scoped_v2(saved_credentials),
        cloudstack_support.inspect_dashboard(_build_provider_status, _build_dashboard_resource, saved_credentials),
    ]
    all_resources = []
    for provider in providers:
        all_resources.extend(provider.get("resources", []))

    payload = {
        "providers": providers,
        "resources": sorted(all_resources, key=lambda item: (item["provider"], item["name"].lower())),
        "summary": {
            "connected": sum(1 for provider in providers if provider["status"] == "connected"),
            "attention": sum(1 for provider in providers if provider["status"] != "connected"),
            "resources": len(all_resources),
        },
        "generated_at": int(now),
        "cache_ttl_seconds": PROVIDER_DASHBOARD_CACHE_TTL_SECONDS,
    }
    PROVIDER_DASHBOARD_CACHE["timestamp"] = now
    PROVIDER_DASHBOARD_CACHE["value"] = payload
    return payload


def delete_dashboard_aws_resource(payload, saved_credentials):
    profile = saved_credentials.get("aws_ec2_vs", {})
    missing, masked = _list_missing_or_masked(profile, ["aws_access_key", "aws_secret_access_key", "aws_region"])
    if missing or masked:
        raise RuntimeError("AWS delete requires a complete saved profile with unmasked credentials.")

    instance_id = payload.get("instance_id")
    if not instance_id:
        raise RuntimeError("Missing AWS instance ID.")

    import boto3

    session = boto3.session.Session(
        aws_access_key_id=profile.get("aws_access_key"),
        aws_secret_access_key=profile.get("aws_secret_access_key"),
        aws_session_token=profile.get("aws_session_token") or None,
        region_name=profile.get("aws_region"),
    )
    ec2_client = session.client("ec2", region_name=profile.get("aws_region"))
    ec2_client.terminate_instances(InstanceIds=[instance_id])
    return {"message": f"Termination requested for AWS instance {instance_id}."}


def delete_dashboard_gcp_resource(payload, saved_credentials):
    profile = saved_credentials.get("gcp_ce_vm", {})
    missing, masked = _list_missing_or_masked(profile, ["gcp_credentials_json", "gcp_project"])
    if missing or masked:
        raise RuntimeError("GCP delete requires a complete saved profile with unmasked credentials.")

    instance_name = payload.get("name")
    zone = payload.get("zone")
    if not instance_name or not zone:
        raise RuntimeError("Missing GCP instance name or zone.")

    credentials = _gcp_access_token(Path(profile.get("gcp_credentials_json")).expanduser())
    project = profile.get("gcp_project")
    _http_json_request(
        (
            "https://compute.googleapis.com/compute/v1/projects/{}/zones/{}/instances/{}"
        ).format(project, zone, urllib_parse.quote(instance_name)),
        headers={"Authorization": f"Bearer {credentials.token}"},
        method="DELETE",
    )
    return {"message": f"Delete requested for GCP instance {instance_name} in {zone}."}


def delete_dashboard_azure_resource(payload, saved_credentials):
    profile = saved_credentials.get("msazure_vm", {})
    missing, masked = _list_missing_or_masked(
        profile,
        ["azure_subscription_id", "azure_tenant_id", "azure_client_id", "azure_client_secret"],
    )
    if missing or masked:
        raise RuntimeError("Azure delete requires a complete saved profile with unmasked credentials.")

    resource_id = payload.get("resource_id")
    if not resource_id:
        raise RuntimeError("Missing Azure VM resource ID.")

    access_token = _azure_token_request(
        profile.get("azure_tenant_id"),
        profile.get("azure_client_id"),
        profile.get("azure_client_secret"),
    )
    _http_json_request(
        f"https://management.azure.com{resource_id}?api-version=2024-03-01",
        headers={"Authorization": f"Bearer {access_token}"},
        method="DELETE",
    )
    return {"message": f"Delete requested for Azure VM {resource_id}."}


def delete_dashboard_resource(provider, payload):
    delete_handlers = {
        "aws_ec2_vs": delete_dashboard_aws_resource,
        "gcp_ce_vm": delete_dashboard_gcp_resource,
        "msazure_vm": delete_dashboard_azure_resource,
        "cloudstack_vm": cloudstack_support.delete_dashboard_resource,
    }
    handler = delete_handlers.get(provider)
    if handler is None:
        raise RuntimeError(f"Delete is not supported for provider '{provider}'.")
    result = handler(payload, discover_saved_credentials())
    reset_provider_dashboard_cache()
    return result


def build_target_environment(data):
    payload = {}
    for key, value in data.items():
        if isinstance(value, str):
            payload[key] = value.strip()
        else:
            payload[key] = value

    _normalize_path_fields(payload)

    raw_yaml_override = payload.get("yaml_override", "")
    if raw_yaml_override:
        try:
            merged_override = parse_yaml_override(raw_yaml_override)
        except ValueError as exc:
            payload["_yaml_error"] = str(exc)
        else:
            payload["_yaml_override_values"] = merged_override
            payload.update(merged_override)

    scenario = payload.get("scenario", "").strip()
    iac_type = payload.get("iac_type", "").strip()
    platform = payload.get("platform", "").strip()
    host_plan = payload.get("host_plan", "").strip()
    if iac_type == "existing_hosts":
        platform = "existing_hosts"
        host_plan = "existing_hosts"

    payload.update({
        "scenario": scenario,
        "iac_type": iac_type,
        "platform": platform,
        "host_plan": host_plan,
        "sid": payload.get("sid", "").strip(),
        "hana_sid": payload.get("hana_sid", "").strip() or payload.get("sid", "").strip(),
        "product": payload.get("product", "").strip(),
        "media_dir": payload.get("media_dir", "").strip(),
        "sap_id_user": payload.get("sap_id_user", "").strip(),
        "sap_id_user_password": payload.get("sap_id_user_password", "").strip(),
        "master_password": payload.get("master_password", "").strip(),
        "system_password": payload.get("system_password", "").strip(),
        "ddic_password": payload.get("ddic_password", "").strip(),
        "schema_password": payload.get("schema_password", "").strip(),
        "sidadm_password": payload.get("sidadm_password", "").strip(),
        "inventory_file": payload.get("inventory_file", "").strip(),
        "use_single_master_password": payload.get("use_single_master_password", "") in ["on", "true", "True", "1", True],
        "ha_primary_hostname": payload.get("ha_primary_hostname", "").strip(),
        "ha_secondary_hostname": payload.get("ha_secondary_hostname", "").strip(),
        "ha_primary_ip": payload.get("ha_primary_ip", "").strip(),
        "ha_secondary_ip": payload.get("ha_secondary_ip", "").strip(),
        "ha_vip_hana_primary": payload.get("ha_vip_hana_primary", "").strip(),
        "aws_instance_type_override": payload.get("aws_instance_type_override", "").strip(),
    })

    if platform == "aws_ec2_vs":
        cli_profile = _read_aws_cli_profile()
        for key, cli_key in {
            "aws_access_key": "aws_access_key_id",
            "aws_secret_access_key": "aws_secret_access_key",
            "aws_session_token": "aws_session_token",
            "aws_region": "aws_region",
        }.items():
            if not payload.get(key) and cli_profile.get(cli_key):
                payload[key] = cli_profile.get(cli_key)

    saved_profile = discover_saved_credentials().get(platform, {}) if platform in ALLOWED_PLATFORMS else {}
    for key in PROVIDER_PROFILE_FIELDS.get(platform, []):
        if not payload.get(key) and saved_profile.get(key):
            payload[key] = saved_profile.get(key)

    if payload.get("use_single_master_password") and not payload.get("sap_id_user_password"):
        payload["sap_id_user_password"] = payload.get("master_password", "")

    return payload


def validate_config(data):
    errors = []
    config = build_target_environment(data)

    if config.get("_yaml_error"):
        errors.append(config["_yaml_error"])

    if config.get("platform") == "aws_ec2_vs" and not config.get("aws_session_token") and config.get("aws_access_key") and config.get("aws_secret_access_key"):
        # Temporary AWS credentials often need a session token; accept it if provided, but do not require it for long-lived keys.
        pass

    if not config["scenario"]:
        errors.append("Choose a scenario before continuing.")
    elif not (SCENARIO_DIR / config["scenario"] / "ansible_playbook.yml").exists():
        errors.append("The selected scenario directory does not exist.")

    if not config["iac_type"]:
        errors.append("Select the provisioning approach.")
    elif config["iac_type"] not in ALLOWED_IAC_TYPES:
        errors.append("Unsupported provisioning approach selected.")

    if config["iac_type"] != "existing_hosts":
        if not config["platform"]:
            errors.append("Select the cloud or hypervisor platform.")
        elif config["platform"] not in ALLOWED_PLATFORMS:
            errors.append("Unsupported platform selected.")

    if not config["sid"]:
        errors.append("SAP system SID is required.")
    elif len(config["sid"]) != 3:
        errors.append("SAP system SID must be exactly 3 characters, for example S01 or H01.")

    if not config["product"]:
        errors.append("Select the SAP product variant or keep the default in the generated extra vars file.")

    if not config["media_dir"]:
        errors.append("Provide the installation media directory.")
    elif not Path(config["media_dir"]).expanduser().exists():
        errors.append("The installation media directory does not exist on disk.")

    for config_key, label in [
        ("host_ssh_private_key_file", "host SSH private key"),
        ("bastion_private_key_file", "bastion SSH private key"),
    ]:
        ssh_key_path = config.get(config_key)
        if not ssh_key_path:
            continue
        resolved_ssh_key_path = Path(ssh_key_path).expanduser()
        if not resolved_ssh_key_path.exists():
            errors.append(
                f"The {label} file does not exist on disk. "
                "If you are running with Docker Compose, verify SAP_SSH_DIR points to the host SSH directory and recreate the container."
            )
        elif not resolved_ssh_key_path.is_file():
            errors.append(f"The {label} path must point to a file.")

    if not config["sap_id_user"]:
        errors.append("SAP user ID is required before the playbook can run.")

    use_single = config.get("use_single_master_password") is True
    if use_single:
        if not config["master_password"]:
            errors.append("A master password is required when the single-password option is selected.")
    else:
        if not config["sap_id_user_password"]:
            errors.append("SAP user password is required when separate passwords are enabled.")
        for key in ["master_password", "system_password", "ddic_password", "schema_password", "sidadm_password"]:
            if not config.get(key):
                errors.append(f"{key.replace('_', ' ').title()} is required before the playbook can run.")

    is_ha_deployment = (
        "ha" in (config.get("scenario") or "").lower()
        or config.get("ha_primary_hostname")
        or config.get("ha_secondary_hostname")
        or config.get("ha_vip_hana_primary")
    )
    if is_ha_deployment:
        for key in [
            ("ha_primary_hostname", "Primary host name"),
            ("ha_secondary_hostname", "Secondary host name"),
            ("ha_vip_hana_primary", "HA primary VIP"),
        ]:
            if not config.get(key[0]):
                errors.append(f"{key[1]} is required for a two-node HA deployment.")
        if not config.get("ha_primary_ip") or not config.get("ha_secondary_ip"):
            errors.append("Both HA host IP addresses are required for a two-node HA deployment.")

    platform = config.get("platform")
    if platform == "aws_ec2_vs":
        if config.get("aws_access_key", "").startswith("ASIA") and not config.get("aws_session_token"):
            errors.append("AWS session token is required when using temporary AWS credentials.")
        for key in [
            "aws_access_key",
            "aws_secret_access_key",
            "aws_region",
            "aws_vpc_availability_zone",
            "aws_vpc_subnet_id",
            "aws_key_pair_name_ssh_host_public_key",
            "aws_vpc_sg_names",
            "dns_root_domain",
        ]:
            if not config.get(key):
                errors.append(f"AWS {key.replace('aws_', '').replace('_', ' ')} is required to provision on AWS.")
    elif platform == "gcp_ce_vm":
        for key in [
            "gcp_credentials_json",
            "gcp_project",
            "gcp_region_zone",
            "gcp_vpc_name",
            "gcp_vpc_subnet_name",
            "dns_root_domain",
        ]:
            if not config.get(key):
                errors.append(f"GCP {key.replace('gcp_', '').replace('_', ' ')} is required to provision on GCP.")
    elif platform == "msazure_vm":
        for key in [
            "azure_subscription_id",
            "azure_tenant_id",
            "azure_client_id",
            "azure_client_secret",
            "azure_resource_group_name",
            "azure_region",
            "azure_vnet_name",
            "azure_vnet_subnet_name",
            "dns_root_domain",
        ]:
            if not config.get(key):
                errors.append(f"Azure {key.replace('azure_', '').replace('_', ' ')} is required to provision on Azure.")
    elif platform == "cloudstack_vm":
        if config["iac_type"] != "ansible":
            errors.append("Apache CloudStack provisioning is only available with 'Provision hosts with Ansible'.")
        if not cloudstack_support.resolve_credentials(config):
            errors.append("Apache CloudStack API URL, key and secret are required (provider profile, CLOUDSTACK_* environment or cloudstack.ini).")
        for key, label in cloudstack_support.REQUIRED_DEPLOYMENT_FIELDS.items():
            if not config.get(key):
                errors.append(f"{label} is required to provision on Apache CloudStack.")
        fencing_method = config.get("cloudstack_ha_fencing_method") or "sbd_devices"
        if fencing_method not in cloudstack_support.FENCING_METHODS:
            errors.append("Unsupported CloudStack HA fencing method.")
        elif is_ha_deployment and fencing_method == "sbd_devices" and not cloudstack_support.parse_sbd_devices(config.get("cloudstack_ha_sbd_devices")):
            errors.append("SBD device path(s) visible in both HA VMs are required for fencing method 'sbd_devices'.")
        elif is_ha_deployment and fencing_method == "sbd_linstor_kvm" and not config.get("cloudstack_kvm_host_ssh_user"):
            errors.append("KVM host SSH user is required for fencing method 'sbd_linstor_kvm'.")

    if config["iac_type"] != "existing_hosts" and config.get("scenario") and config.get("platform") in ALLOWED_PLATFORMS:
        available_host_plans = get_scenario_host_plans(config["scenario"], config["platform"])
        if not available_host_plans:
            errors.append("No host specification plans were found for the selected scenario and platform.")
        elif not config["host_plan"]:
            config["host_plan"] = available_host_plans[0]
        elif config["host_plan"] not in available_host_plans:
            errors.append(
                f"Host specification plan '{config['host_plan']}' is not available for the selected scenario and platform. "
                f"Available plans: {', '.join(available_host_plans)}."
            )

    if config["inventory_file"]:
        inventory_candidate = Path(config["inventory_file"]).expanduser()
        if not inventory_candidate.exists():
            errors.append("The custom inventory file does not exist.")

    return config, errors


def _build_preflight_check(name, ok, message):
    return {"name": name, "ok": ok, "message": message}


def _normalize_route53_zone_name(zone_name):
    text = (zone_name or "").strip()
    if not text:
        return ""
    return text if text.endswith(".") else f"{text}."


def run_aws_preflight_check(config):
    if config.get("platform") != "aws_ec2_vs":
        return []

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, PartialCredentialsError
    except ImportError:
        return [
            _build_preflight_check(
                "AWS SDK availability",
                False,
                "boto3/botocore are not installed in the application environment.",
            )
        ]

    session = boto3.session.Session(
        aws_access_key_id=config.get("aws_access_key"),
        aws_secret_access_key=config.get("aws_secret_access_key"),
        aws_session_token=config.get("aws_session_token") or None,
        region_name=config.get("aws_region"),
    )

    results = []
    sts_ok = False
    try:
        sts_client = session.client("sts")
        identity = sts_client.get_caller_identity()
        account_id = identity.get("Account", "unknown-account")
        arn = identity.get("Arn", "unknown-principal")
        results.append(
            _build_preflight_check(
                "AWS STS caller identity",
                True,
                f"Authenticated as {arn} in account {account_id}.",
            )
        )
        sts_ok = True
    except PartialCredentialsError as exc:
        results.append(_build_preflight_check("AWS STS caller identity", False, f"Incomplete credentials provided ({exc})."))
    except NoCredentialsError:
        results.append(_build_preflight_check("AWS STS caller identity", False, "No credentials were supplied to the AWS SDK."))
    except ClientError as exc:
        error = exc.response.get("Error", {})
        code = error.get("Code", "Unknown")
        message = error.get("Message", str(exc))
        results.append(_build_preflight_check("AWS STS caller identity", False, f"{code}: {message}"))
    except BotoCoreError as exc:
        results.append(_build_preflight_check("AWS STS caller identity", False, str(exc)))

    if not sts_ok:
        results.append(
            _build_preflight_check(
                "AWS EC2 image lookup",
                False,
                "Skipped because AWS STS caller identity failed.",
            )
        )
        return results
    try:
        ec2_client = session.client("ec2", region_name=config.get("aws_region"))
        response = ec2_client.describe_images(Owners=["self"])
        image_count = len(response.get("Images", []))
        results.append(
            _build_preflight_check(
                "AWS EC2 image lookup",
                True,
                f"EC2 API call succeeded in region {config.get('aws_region')}; visible self-owned images: {image_count}.",
            )
        )
    except ClientError as exc:
        error = exc.response.get("Error", {})
        code = error.get("Code", "Unknown")
        message = error.get("Message", str(exc))
        results.append(_build_preflight_check("AWS EC2 image lookup", False, f"{code}: {message}"))
    except BotoCoreError as exc:
        results.append(_build_preflight_check("AWS EC2 image lookup", False, str(exc)))

    try:
        generated_document = build_generated_document(config)
        host_spec_var = get_host_specification_variable_name("aws_ec2_vs")
        host_specs = generated_document.get(host_spec_var, {})
        selected_plan = generated_document.get("sap_vm_provision_host_specification_plan")
        selected_hosts = host_specs.get(selected_plan, {}) if isinstance(host_specs, dict) else {}
        requested_instance_types = sorted(
            {
                host_values.get("virtual_machine_profile")
                for host_values in selected_hosts.values()
                if isinstance(host_values, dict) and host_values.get("virtual_machine_profile")
            }
        )
        image_key = generated_document.get("sap_vm_provision_aws_ec2_vs_host_os_image")
        image_dictionary = generated_document.get("sap_vm_provision_aws_ec2_vs_host_os_image_dictionary", {})
        image_pattern = image_dictionary.get(image_key) if isinstance(image_dictionary, dict) else None
        if not image_pattern:
            results.append(
                _build_preflight_check(
                    "AWS EC2 launch compatibility",
                    False,
                    f"Unable to resolve an AWS Marketplace image pattern for OS image key '{image_key}'.",
                )
            )
            return results

        image_response = ec2_client.describe_images(
            Owners=["aws-marketplace"],
            Filters=[{"Name": "name", "Values": [image_pattern]}],
        )
        images = image_response.get("Images", [])
        if not images:
            results.append(
                _build_preflight_check(
                    "AWS EC2 launch compatibility",
                    False,
                    f"No AWS Marketplace image matched pattern '{image_pattern}' for OS image key '{image_key}'.",
                )
            )
            return results

        selected_image = sorted(images, key=lambda item: item.get("CreationDate", ""))[-1]
        subnet_response = ec2_client.describe_subnets(SubnetIds=[config.get("aws_vpc_subnet_id")])
        subnet = (subnet_response.get("Subnets") or [None])[0]
        if not subnet:
            results.append(_build_preflight_check("AWS EC2 launch compatibility", False, "The configured subnet could not be resolved."))
            return results

        security_group_names = [name.strip() for name in (config.get("aws_vpc_sg_names") or "").split(",") if name.strip()]
        security_group_response = ec2_client.describe_security_groups(
            Filters=[
                {"Name": "vpc-id", "Values": [subnet.get("VpcId")]},
                {"Name": "group-name", "Values": security_group_names},
            ]
        )
        security_group_ids = [group.get("GroupId") for group in security_group_response.get("SecurityGroups", []) if group.get("GroupId")]
        if len(security_group_ids) != len(security_group_names):
            results.append(
                _build_preflight_check(
                    "AWS EC2 launch compatibility",
                    False,
                    "One or more configured security group names could not be resolved in the selected VPC.",
                )
            )
            return results

        if not requested_instance_types:
            results.append(
                _build_preflight_check(
                    "AWS EC2 launch compatibility",
                    False,
                    f"No AWS instance types were resolved from host plan '{selected_plan}'.",
                )
            )
            return results

        for instance_type in requested_instance_types:
            try:
                ec2_client.run_instances(
                    ImageId=selected_image["ImageId"],
                    InstanceType=instance_type,
                    MinCount=1,
                    MaxCount=1,
                    SubnetId=config.get("aws_vpc_subnet_id"),
                    SecurityGroupIds=security_group_ids,
                    KeyName=config.get("aws_key_pair_name_ssh_host_public_key"),
                    DryRun=True,
                )
            except ClientError as exc:
                error = exc.response.get("Error", {})
                code = error.get("Code", "Unknown")
                message = error.get("Message", str(exc))
                if code == "DryRunOperation":
                    continue
                results.append(
                    _build_preflight_check(
                        "AWS EC2 launch compatibility",
                        False,
                        (
                            f"Image key '{image_key}' resolved to {selected_image['ImageId']}, but EC2 dry-run launch failed for "
                            f"instance type '{instance_type}': {code}: {message}"
                        ),
                    )
                )
                return results

        results.append(
            _build_preflight_check(
                "AWS EC2 launch compatibility",
                True,
                (
                    f"Dry-run launch succeeded for image key '{image_key}' ({selected_image['ImageId']}) with instance type"
                    f"{'s' if len(requested_instance_types) > 1 else ''} {', '.join(requested_instance_types)}."
                ),
            )
        )

        zone_name = _normalize_route53_zone_name(config.get("dns_root_domain"))
        route53_client = session.client("route53")
        hosted_zone_response = route53_client.list_hosted_zones_by_name(DNSName=zone_name, MaxItems="10")
        matching_private_zones = []
        for hosted_zone in hosted_zone_response.get("HostedZones", []):
            if hosted_zone.get("Name") != zone_name:
                continue
            if not (hosted_zone.get("Config") or {}).get("PrivateZone"):
                continue
            hosted_zone_id = (hosted_zone.get("Id") or "").split("/")[-1]
            if not hosted_zone_id:
                continue
            zone_details = route53_client.get_hosted_zone(Id=hosted_zone_id)
            vpcs = zone_details.get("VPCs", [])
            if any(vpc.get("VPCId") == subnet.get("VpcId") for vpc in vpcs):
                matching_private_zones.append(hosted_zone_id)

        if not matching_private_zones:
            results.append(
                _build_preflight_check(
                    "AWS Route53 private zone",
                    False,
                    (
                        f"No private Route53 hosted zone named '{zone_name}' is associated with VPC {subnet.get('VpcId')}. "
                        "Create or associate that hosted zone, or use a DNS root domain that already exists in Route53."
                    ),
                )
            )
            return results

        results.append(
            _build_preflight_check(
                "AWS Route53 private zone",
                True,
                f"Found private Route53 hosted zone '{zone_name}' associated with VPC {subnet.get('VpcId')}.",
            )
        )
    except ClientError as exc:
        error = exc.response.get("Error", {})
        code = error.get("Code", "Unknown")
        message = error.get("Message", str(exc))
        results.append(_build_preflight_check("AWS EC2 launch compatibility", False, f"{code}: {message}"))
    except BotoCoreError as exc:
        results.append(_build_preflight_check("AWS EC2 launch compatibility", False, str(exc)))

    return results


def run_cloudstack_preflight_check(config):
    # Use the generated variables (scenario file + YAML override + wizard HA host names), as passed to the playbook
    selected_hosts = {}
    if config.get("scenario") and config.get("host_plan"):
        try:
            document = build_generated_document(config)
        except Exception:  # pragma: no cover - fall back to the scenario file
            document = {get_host_specification_variable_name("cloudstack_vm"): get_scenario_host_specifications(config["scenario"], "cloudstack_vm")}
        host_specs = document.get(get_host_specification_variable_name("cloudstack_vm")) or {}
        selected_hosts = host_specs.get(config["host_plan"], {}) if isinstance(host_specs, dict) else {}
    return cloudstack_support.run_preflight_check({**config, "_cloudstack_selected_hosts": selected_hosts}, _build_preflight_check)


def run_platform_preflight_check(config):
    preflight_handlers = {
        "aws_ec2_vs": run_aws_preflight_check,
        "cloudstack_vm": run_cloudstack_preflight_check,
    }
    handler = preflight_handlers.get(config.get("platform"))
    if handler is None:
        return []
    return handler(config)


def build_generated_document(config):
    scenario_name = config["scenario"]
    scenario_path = SCENARIO_DIR / scenario_name
    template_path = scenario_path / "ansible_extravars.yml"
    platform_template = scenario_path / f"ansible_extravars_{config['platform']}.yml"
    if not platform_template.exists():
        platform_template = None

    # Precedence (lowest to highest): scenario extravars < platform extravars < wizard values and YAML override
    output = {}
    if template_path.exists():
        output = _load_yaml_document(template_path)
    if platform_template is not None:
        output = {**output, **_load_yaml_document(platform_template)}

    for key, value in sanitize_overrides(config).items():
        if value is not None:
            output[key] = value
    output = apply_platform_configuration_overrides(output, config)
    return apply_unique_load_balancer_names(output, config)


# Load balancer resources (health check, backend service/pool, instance groups, VIP address, forwarding rule) are named
# from these variables on GCP, MS Azure and IBM Cloud. The role defaults (lb-sap-ha-*) are identical for every deployment
# and collide in shared projects/subscriptions (e.g. GCP health checks are global), so each deployment gets its own names.
LOAD_BALANCER_NAME_VARS = {
    "sap_vm_provision_ha_load_balancer_name_hana": ("hana_primary", "hana"),
    "sap_vm_provision_ha_load_balancer_name_anydb": ("anydb_primary", "anydb"),
    "sap_vm_provision_ha_load_balancer_name_nwas": ("nwas_ascs", "nwas"),
}
# GCP resource names are limited to 63 characters; the longest suffix appended by the role is '-secondary-instance-group'
LOAD_BALANCER_NAME_MAX_LENGTH = 38


def _unique_load_balancer_name(sid, hostname, kind):
    clean = lambda value: re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower()).strip("-")
    sid_part = clean(sid) or "sap"
    suffix = f"-{kind}"
    host_budget = LOAD_BALANCER_NAME_MAX_LENGTH - len("lb-") - len(sid_part) - 1 - len(suffix)
    host_part = clean(hostname)[:max(host_budget, 0)].strip("-") or "host"
    return f"lb-{sid_part}-{host_part}{suffix}"


def apply_unique_load_balancer_names(document, config):
    platform = config.get("platform")
    host_specs = document.get(get_host_specification_variable_name(platform)) if platform else None
    selected = host_specs.get(config.get("host_plan"), {}) if isinstance(host_specs, dict) else {}
    if not isinstance(selected, dict):
        selected = {}
    user_values = config.get("_yaml_override_values") or {}
    sid = config.get("hana_sid") or config.get("sid")
    for variable, (host_type, kind) in LOAD_BALANCER_NAME_VARS.items():
        if variable in user_values:
            continue  # explicit value from the YAML override wins
        primary = next((name for name, spec in selected.items()
                        if isinstance(spec, dict) and spec.get("sap_host_type") == host_type), None)
        if primary:
            document[variable] = _unique_load_balancer_name(sid, primary, kind)
    return document


def update_template_with_overrides(template_path, overrides):
    with open(template_path, "r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}

    for key, value in overrides.items():
        if value is not None:
            document[key] = value

    return document


def sanitize_overrides(config):
    path = Path(config["media_dir"]).expanduser().resolve()
    ha_vip_hana_primary = config.get("ha_vip_hana_primary")
    remote_media_directory = str(path)

    if config.get("platform") == "aws_ec2_vs" and ha_vip_hana_primary and "/" not in ha_vip_hana_primary:
        ha_vip_hana_primary = f"{ha_vip_hana_primary}/32"

    if config.get("scenario") == "sap_hana_ha" and config.get("platform") == "aws_ec2_vs":
        remote_media_directory = "/software"

    master_password = config.get("master_password") or config.get("system_password")
    system_password = config.get("system_password") or master_password
    ddic_password = config.get("ddic_password") or master_password
    schema_password = config.get("schema_password") or master_password
    sidadm_password = config.get("sidadm_password") or master_password
    sap_id_user_password = config.get("sap_id_user_password") or master_password

    overrides = {
        "sap_vm_provision_iac_type": config["iac_type"],
        "sap_vm_provision_iac_platform": config["platform"],
        "sap_vm_provision_host_specification_plan": config["host_plan"],
        "sap_vm_provision_ssh_host_ssh_args": DEFAULT_SSH_HOST_ARGS,
        "sap_vm_provision_ssh_bastion_ssh_args": DEFAULT_SSH_BASTION_ARGS,
        "sap_system_sid": config["sid"],
        "sap_system_hana_db_sid": config["hana_sid"],
        "sap_system_hana_db_instance_nr": "90",
        "sap_software_product": config["product"],
        "sap_id_user": config["sap_id_user"],
        "sap_id_user_password": sap_id_user_password,
        "sap_install_media_detect_source_directory": remote_media_directory,
        "sap_hana_install_master_password": master_password,
        "sap_swpm_master_password": system_password,
        "sap_swpm_ddic_000_password": ddic_password,
        "sap_swpm_db_schema_abap_password": schema_password,
        "sap_swpm_db_sidadm_password": sidadm_password,
        "sap_swpm_db_system_password": system_password,
        "sap_swpm_db_systemdb_password": system_password,
        "sap_hana_install_use_master_password": "y",
        "sap_hana_install_log_mode": (
            "normal" if "ha" in (config.get("scenario") or "").split("_") else "overwrite"
        ),
    }

    platform = config.get("platform")
    if platform == "aws_ec2_vs":
        image_value = config.get("aws_host_os_image") or config.get("os_image")
        overrides.update({
            "sap_vm_provision_aws_access_key": config.get("aws_access_key"),
            "sap_vm_provision_aws_secret_access_key": config.get("aws_secret_access_key"),
            "sap_vm_provision_aws_session_token": config.get("aws_session_token"),
            "sap_vm_provision_aws_region": config.get("aws_region"),
            "sap_vm_provision_aws_vpc_availability_zone": config.get("aws_vpc_availability_zone"),
            "sap_vm_provision_aws_vpc_subnet_id": config.get("aws_vpc_subnet_id"),
            "sap_vm_provision_aws_ec2_vs_host_os_image": image_value,
            "sap_vm_provision_aws_key_pair_name_ssh_host_public_key": config.get("aws_key_pair_name_ssh_host_public_key"),
            "sap_vm_provision_aws_vpc_sg_names": config.get("aws_vpc_sg_names"),
            "sap_vm_provision_dns_root_domain": config.get("dns_root_domain"),
        })
    elif platform == "gcp_ce_vm":
        image_value = config.get("gcp_host_os_image") or config.get("os_image")
        overrides.update({
            "sap_vm_provision_gcp_credentials_json": config.get("gcp_credentials_json"),
            "sap_vm_provision_gcp_project": config.get("gcp_project"),
            "sap_vm_provision_gcp_region": config.get("gcp_region"),
            "sap_vm_provision_gcp_region_zone": config.get("gcp_region_zone"),
            "sap_vm_provision_gcp_vpc_name": config.get("gcp_vpc_name"),
            "sap_vm_provision_gcp_vpc_subnet_name": config.get("gcp_vpc_subnet_name"),
            "sap_vm_provision_gcp_ce_vm_host_os_image": image_value,
            "sap_vm_provision_dns_root_domain": config.get("dns_root_domain"),
        })
    elif platform == "msazure_vm":
        image_value = config.get("azure_host_os_image") or config.get("os_image")
        overrides.update({
            "sap_vm_provision_msazure_subscription_id": config.get("azure_subscription_id"),
            "sap_vm_provision_msazure_tenant_id": config.get("azure_tenant_id"),
            "sap_vm_provision_msazure_app_client_id": config.get("azure_client_id"),
            "sap_vm_provision_msazure_app_client_secret": config.get("azure_client_secret"),
            "sap_vm_provision_msazure_resource_group_name": config.get("azure_resource_group_name"),
            "sap_vm_provision_msazure_location_region": config.get("azure_region"),
            "sap_vm_provision_msazure_vnet_name": config.get("azure_vnet_name"),
            "sap_vm_provision_msazure_vnet_subnet_name": config.get("azure_vnet_subnet_name"),
            "sap_vm_provision_msazure_vm_host_os_image": image_value,
            "sap_vm_provision_msazure_key_pair_name_ssh_host_public_key": config.get("azure_key_pair_name_ssh_host_public_key"),
            "sap_vm_provision_dns_root_domain": config.get("dns_root_domain"),
        })
    elif platform == "cloudstack_vm":
        credentials = cloudstack_support.resolve_credentials(config)
        template_name = config.get("cloudstack_template_name")
        overrides.update({
            "sap_vm_provision_cloudstack_api_url": credentials.get("cloudstack_api_url"),
            "sap_vm_provision_cloudstack_api_key": credentials.get("cloudstack_api_key"),
            "sap_vm_provision_cloudstack_api_secret": credentials.get("cloudstack_api_secret"),
            "sap_vm_provision_cloudstack_zone": config.get("cloudstack_zone"),
            "sap_vm_provision_cloudstack_network_name": config.get("cloudstack_network_name"),
            "sap_vm_provision_cloudstack_vm_host_os_image": cloudstack_support.OS_IMAGE_KEY if template_name else None,
            "sap_vm_provision_cloudstack_vm_host_os_image_dictionary": (
                {cloudstack_support.OS_IMAGE_KEY: template_name} if template_name else None
            ),
            "sap_vm_provision_cloudstack_vm_hypervisor": config.get("cloudstack_hypervisor"),
            "sap_vm_provision_cloudstack_ha_fencing_method": config.get("cloudstack_ha_fencing_method"),
            "sap_vm_provision_cloudstack_ha_sbd_devices": cloudstack_support.parse_sbd_devices(config.get("cloudstack_ha_sbd_devices")) or None,
            "sap_vm_provision_cloudstack_kvm_host_ssh_user": config.get("cloudstack_kvm_host_ssh_user"),
            "sap_vm_provision_cloudstack_kvm_host_ssh_private_key_file_path": config.get("cloudstack_kvm_host_ssh_private_key_file"),
            # CloudStack Guest Networks often have no Internet access: download SAP media in the console, then copy
            "sap_software_download_execution_node_directory": str(STATE_DIR / "sap_media"),
            "sap_vm_provision_dns_root_domain": config.get("dns_root_domain"),
        })

    if config.get("ha_primary_hostname"):
        overrides["sap_vm_provision_ha_primary_hostname"] = config.get("ha_primary_hostname")
    if config.get("ha_secondary_hostname"):
        overrides["sap_vm_provision_ha_secondary_hostname"] = config.get("ha_secondary_hostname")
    if config.get("ha_primary_ip"):
        overrides["sap_vm_provision_ha_primary_ip"] = config.get("ha_primary_ip")
    if config.get("ha_secondary_ip"):
        overrides["sap_vm_provision_ha_secondary_ip"] = config.get("ha_secondary_ip")
    if ha_vip_hana_primary:
        overrides["sap_vm_provision_ha_vip_hana_primary"] = ha_vip_hana_primary

    if config.get("bastion_public_ip"):
        overrides["sap_vm_provision_bastion_public_ip"] = config.get("bastion_public_ip")
    if config.get("bastion_user"):
        overrides["sap_vm_provision_bastion_user"] = config.get("bastion_user")
    if config.get("bastion_ssh_port"):
        overrides["sap_vm_provision_bastion_ssh_port"] = config.get("bastion_ssh_port")
    if config.get("host_ssh_private_key_file"):
        overrides["sap_vm_provision_ssh_host_private_key_file_path"] = config.get("host_ssh_private_key_file")
    if config.get("bastion_private_key_file"):
        overrides["sap_vm_provision_ssh_bastion_private_key_file_path"] = config.get("bastion_private_key_file")

    yaml_override_values = config.get("_yaml_override_values")
    if isinstance(yaml_override_values, dict):
        overrides.update(yaml_override_values)

    return {k: v for k, v in overrides.items() if v not in (None, "")}


def apply_aws_instance_type_override(document, config):
    override_instance_type = config.get("aws_instance_type_override")
    if not override_instance_type:
        return document

    host_spec_var = get_host_specification_variable_name("aws_ec2_vs")
    host_specs = document.get(host_spec_var)
    selected_plan = config.get("host_plan")
    if not isinstance(host_specs, dict) or not selected_plan or selected_plan not in host_specs:
        return document

    selected_hosts = host_specs.get(selected_plan)
    if not isinstance(selected_hosts, dict):
        return document

    for host_config in selected_hosts.values():
        if isinstance(host_config, dict):
            host_config["virtual_machine_profile"] = override_instance_type
    return document


def apply_cloudstack_ha_host_override(document, config):
    """Apply the HA host names and static IPs from the wizard to the selected CloudStack host plan."""
    host_spec_var = get_host_specification_variable_name("cloudstack_vm")
    host_specs = document.get(host_spec_var)
    selected_plan = config.get("host_plan")
    if not isinstance(host_specs, dict) or not isinstance(host_specs.get(selected_plan), dict):
        return document

    selected_hosts = host_specs[selected_plan]
    renamed = {}
    for hostname, host_config in selected_hosts.items():
        new_name, new_ip = hostname, None
        if isinstance(host_config, dict):
            host_type = host_config.get("sap_host_type")
            if host_type == "hana_primary":
                new_name = config.get("ha_primary_hostname") or hostname
                new_ip = config.get("ha_primary_ip")
            elif host_type == "hana_secondary":
                new_name = config.get("ha_secondary_hostname") or hostname
                new_ip = config.get("ha_secondary_ip")
            if new_ip:
                host_config = {**host_config, "cloudstack_vm_ip_address": new_ip}
        renamed[new_name] = host_config
    host_specs[selected_plan] = renamed
    return document


def apply_platform_configuration_overrides(document, config):
    platform = config.get("platform")
    override_handlers = {
        "aws_ec2_vs": apply_aws_instance_type_override,
        "cloudstack_vm": apply_cloudstack_ha_host_override,
    }
    handler = override_handlers.get(platform)
    if handler is None:
        return document
    return handler(document, config)


def load_generated_vars(path):
    candidate = Path(path)
    if not candidate.exists():
        return {}
    with open(candidate, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def write_control_inventory(py_interp):
    inventory_path = GENERATED_DIR / "localhost_inventory.ini"
    inventory_path.write_text(
        "[local]\n"
        f"localhost ansible_connection=local ansible_python_interpreter={py_interp}\n",
        encoding="utf-8",
    )
    return inventory_path


def load_job_state(job_id):
    state = JOB_STATE.get(job_id)
    if state:
        return state

    status_path = RUNS_DIR / f"{job_id}.json"
    if status_path.exists():
        try:
            with open(status_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                command = loaded.get("command", "")
                match = re.search(r"--extra-vars\s+@(\S+)", command)
                if match and not loaded.get("generated_file"):
                    loaded["generated_file"] = match.group(1)
                if loaded.get("generated_file") and not loaded.get("platform"):
                    try:
                        generated_vars = load_generated_vars(loaded["generated_file"])
                    except Exception:
                        generated_vars = {}
                    loaded["platform"] = generated_vars.get("sap_vm_provision_iac_platform")
                return loaded
        except (OSError, json.JSONDecodeError):
            return None

    log_path = RUNS_DIR / f"{job_id}.log"
    if log_path.exists():
        return {"status": "unknown", "log_file": str(log_path), "summary": "Log file exists."}
    return None


def _job_last_updated_timestamp(job_id, state):
    candidates = []
    for path_value in [state.get("status_file"), state.get("log_file")]:
        if path_value:
            path = Path(path_value)
            if path.exists():
                candidates.append(path.stat().st_mtime)
    fallback_status = RUNS_DIR / f"{job_id}.json"
    fallback_log = RUNS_DIR / f"{job_id}.log"
    for path in [fallback_status, fallback_log]:
        if path.exists():
            candidates.append(path.stat().st_mtime)
    return max(candidates) if candidates else 0


def list_recent_jobs(limit=8, include_resources=True):
    job_ids = set(JOB_STATE.keys())
    job_ids.update(path.stem for path in RUNS_DIR.glob("*.json"))
    job_ids.update(path.stem for path in RUNS_DIR.glob("*.log"))

    recent_jobs = []
    for job_id in job_ids:
        state = load_job_state(job_id)
        if not state:
            continue

        recent_jobs.append({
            "job_id": job_id,
            "status": state.get("status", "unknown"),
            "summary": state.get("summary", "Execution log is available."),
            "platform": get_job_platform(state),
            "scenario": state.get("scenario"),
            "updated_at": _job_last_updated_timestamp(job_id, state),
            "instances": [],
            "resource_message": "",
            "_state": state,
        })

    recent_jobs.sort(key=lambda item: item["updated_at"], reverse=True)
    selected_jobs = recent_jobs if limit is None else recent_jobs[:limit]

    if include_resources:
        for job in selected_jobs:
            try:
                resources = list_job_resources(job["_state"])
            except Exception as exc:
                resources = {"instances": [], "message": f"Unable to load job resources: {exc}"}
            job["instances"] = resources.get("instances", [])
            job["resource_message"] = resources.get("message", "")

    for job in selected_jobs:
        job.pop("_state", None)

    return selected_jobs


def extract_job_hostnames(job_vars):
    host_dict = job_vars.get("sap_vm_provision_aws_ec2_vs_host_specifications_dictionary", {})
    host_plan = job_vars.get("sap_vm_provision_host_specification_plan")
    if host_plan and isinstance(host_dict, dict):
        selected = host_dict.get(host_plan, {})
        if isinstance(selected, dict):
            return sorted(selected.keys())
    return []


def get_job_platform(job_state):
    if job_state.get("platform"):
        return job_state["platform"]
    generated_vars_path = job_state.get("generated_file")
    if not generated_vars_path:
        return None
    job_vars = load_generated_vars(generated_vars_path)
    return job_vars.get("sap_vm_provision_iac_platform")


def build_aws_session_from_vars(job_vars):
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("boto3 is not installed in the application environment.") from exc

    return boto3.session.Session(
        aws_access_key_id=job_vars.get("sap_vm_provision_aws_access_key"),
        aws_secret_access_key=job_vars.get("sap_vm_provision_aws_secret_access_key"),
        aws_session_token=job_vars.get("sap_vm_provision_aws_session_token") or None,
        region_name=job_vars.get("sap_vm_provision_aws_region"),
    )


def list_job_aws_resources(job_state):
    generated_vars_path = job_state.get("generated_file")
    if not generated_vars_path:
        return {"platform": None, "instances": [], "volumes": [], "message": "No generated vars file recorded for this job."}

    job_vars = load_generated_vars(generated_vars_path)
    if job_vars.get("sap_vm_provision_iac_platform") != "aws_ec2_vs":
        return {"platform": job_vars.get("sap_vm_provision_iac_platform"), "instances": [], "volumes": [], "message": "Resource discovery is currently available only for AWS jobs."}

    hostnames = extract_job_hostnames(job_vars)
    if not hostnames:
        return {"platform": "aws_ec2_vs", "instances": [], "volumes": [], "message": "No AWS hostnames were found in the generated job variables."}

    session = build_aws_session_from_vars(job_vars)
    ec2_client = session.client("ec2", region_name=job_vars.get("sap_vm_provision_aws_region"))

    response = ec2_client.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": hostnames},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]},
        ]
    )

    instances = []
    volumes_by_id = {}
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            name = next((tag.get("Value") for tag in instance.get("Tags", []) if tag.get("Key") == "Name"), instance.get("InstanceId"))
            mapped_volumes = []
            for mapping in instance.get("BlockDeviceMappings", []):
                ebs = mapping.get("Ebs") or {}
                volume_id = ebs.get("VolumeId")
                if volume_id:
                    mapped_volumes.append({
                        "volume_id": volume_id,
                        "device_name": mapping.get("DeviceName"),
                        "delete_on_termination": ebs.get("DeleteOnTermination", False),
                    })
                    volumes_by_id.setdefault(volume_id, {
                        "volume_id": volume_id,
                        "device_name": mapping.get("DeviceName"),
                        "delete_on_termination": ebs.get("DeleteOnTermination", False),
                        "attached_instance_id": instance.get("InstanceId"),
                        "attached_instance_name": name,
                    })

            instances.append({
                "name": name,
                "instance_id": instance.get("InstanceId"),
                "state": (instance.get("State") or {}).get("Name"),
                "private_ip": instance.get("PrivateIpAddress"),
                "public_ip": instance.get("PublicIpAddress"),
                "subnet_id": instance.get("SubnetId"),
                "security_groups": [group.get("GroupName") for group in instance.get("SecurityGroups", [])],
                "volumes": mapped_volumes,
            })

    if volumes_by_id:
        volume_response = ec2_client.describe_volumes(VolumeIds=list(volumes_by_id.keys()))
        for volume in volume_response.get("Volumes", []):
            volume_id = volume.get("VolumeId")
            if volume_id in volumes_by_id:
                volumes_by_id[volume_id].update({
                    "size_gib": volume.get("Size"),
                    "state": volume.get("State"),
                    "volume_type": volume.get("VolumeType"),
                })

    return {
        "platform": "aws_ec2_vs",
        "platform_label": PLATFORM_LABELS.get("aws_ec2_vs", "AWS EC2"),
        "instances": sorted(instances, key=lambda item: item["name"]),
        "volumes": sorted(volumes_by_id.values(), key=lambda item: item["volume_id"]),
        "message": f"Found {len(instances)} instance(s) for this job.",
    }


def terminate_job_aws_resources(job_state):
    generated_vars_path = job_state.get("generated_file")
    if not generated_vars_path:
        return {"terminated_instances": [], "deleted_volumes": [], "message": "No generated vars file recorded for this job."}

    job_vars = load_generated_vars(generated_vars_path)
    if job_vars.get("sap_vm_provision_iac_platform") != "aws_ec2_vs":
        return {"terminated_instances": [], "deleted_volumes": [], "message": "Termination is currently available only for AWS jobs."}

    resources = list_job_aws_resources(job_state)
    instance_ids = [item["instance_id"] for item in resources.get("instances", []) if item.get("instance_id")]
    volume_ids = [item["volume_id"] for item in resources.get("volumes", []) if item.get("volume_id") and not item.get("delete_on_termination")]

    if not instance_ids:
        return {"terminated_instances": [], "deleted_volumes": [], "message": "No active AWS instances were found for this job."}

    session = build_aws_session_from_vars(job_vars)
    ec2_client = session.client("ec2", region_name=job_vars.get("sap_vm_provision_aws_region"))
    ec2_client.terminate_instances(InstanceIds=instance_ids)
    waiter = ec2_client.get_waiter("instance_terminated")
    waiter.wait(InstanceIds=instance_ids)

    deleted_volumes = []
    for volume_id in volume_ids:
        try:
            ec2_client.delete_volume(VolumeId=volume_id)
            deleted_volumes.append(volume_id)
        except Exception:
            continue

    return {
        "terminated_instances": instance_ids,
        "deleted_volumes": deleted_volumes,
        "message": f"Termination requested for {len(instance_ids)} instance(s). Deleted {len(deleted_volumes)} non-auto-delete volume(s).",
    }


def _azure_clients(job_vars):
    """Compute and network management clients for the subscription of an Azure job."""
    try:
        from azure.identity import ClientSecretCredential
        from azure.mgmt.compute import ComputeManagementClient
        from azure.mgmt.network import NetworkManagementClient
    except ImportError as exc:
        raise RuntimeError("The Azure SDK is not installed in the application environment.") from exc
    subscription = job_vars.get("sap_vm_provision_msazure_subscription_id")
    credential = ClientSecretCredential(
        tenant_id=job_vars.get("sap_vm_provision_msazure_tenant_id"),
        client_id=job_vars.get("sap_vm_provision_msazure_app_client_id"),
        client_secret=job_vars.get("sap_vm_provision_msazure_app_client_secret"),
    )
    return ComputeManagementClient(credential, subscription), NetworkManagementClient(credential, subscription)


def _load_azure_job_vars(job_state):
    generated_vars_path = job_state.get("generated_file")
    job_vars = load_generated_vars(generated_vars_path) if generated_vars_path else {}
    return job_vars if job_vars.get("sap_vm_provision_iac_platform") == "msazure_vm" else None


def list_job_azure_resources(job_state):
    job_vars = _load_azure_job_vars(job_state)
    if job_vars is None:
        return {"platform": None, "instances": [], "volumes": [], "message": "No Azure generated vars file recorded for this job."}

    hostnames = [host["name"] for host in connection_info._planned_hosts(job_vars)]
    resource_group = job_vars.get("sap_vm_provision_msazure_resource_group_name")
    compute, network = _azure_clients(job_vars)
    instances, volumes = [], []
    for name in hostnames:
        try:
            vm = compute.virtual_machines.get(resource_group, name, expand="instanceView")
        except Exception:
            continue
        private_ip = None
        for nic_ref in vm.network_profile.network_interfaces or []:
            nic = network.network_interfaces.get(resource_group, nic_ref.id.split("/")[-1])
            ips = [cfg.private_ip_address for cfg in nic.ip_configurations or [] if cfg.private_ip_address]
            private_ip = private_ip or (ips[0] if ips else None)
        statuses = [status.code for status in (vm.instance_view.statuses if vm.instance_view else []) or []]
        state = next((code.split("/", 1)[1] for code in statuses if code.startswith("PowerState/")), "unknown")
        instances.append({"name": name, "instance_id": vm.id, "state": state, "private_ip": private_ip,
                          "size": vm.hardware_profile.vm_size if vm.hardware_profile else None})
        for disk in vm.storage_profile.data_disks or []:
            volumes.append({"volume_id": disk.managed_disk.id if disk.managed_disk else disk.name, "name": disk.name,
                            "device_name": f"LUN {disk.lun}", "size_gib": disk.disk_size_gb,
                            "attached_instance_name": name})
    return {
        "platform": "msazure_vm",
        "platform_label": PLATFORM_LABELS.get("msazure_vm", "Azure VM"),
        "instances": instances,
        "volumes": volumes,
        "message": f"Found {len(instances)} instance(s) for this job.",
    }


def _azure_resource_name(resource_id):
    return (resource_id or "").rstrip("/").split("/")[-1]


def _azure_is_not_found(exc):
    return exc.__class__.__name__ == "ResourceNotFoundError" or getattr(exc, "status_code", None) == 404


def terminate_job_azure_resources(job_state):
    """Delete the VMs of an Azure job (only hosts of the job's host plan) and the resources created for them.

    Deleted: VMs, their NICs, OS and data disks; SBD shared disks (tag sap_vm_provision=sbd) whose hosts are all job
    hosts; HA load balancers created by the role once their backend pools are empty; HA availability sets once empty.
    Kept: everything else (bastion, VNet/NSG, private DNS zone, SSH keys, other disks and load balancers).
    """
    job_vars = _load_azure_job_vars(job_state)
    if job_vars is None:
        return {"terminated_instances": [], "deleted_volumes": [], "message": "No Azure generated vars file recorded for this job."}

    hostnames = [host["name"] for host in connection_info._planned_hosts(job_vars)]
    if not hostnames:
        return {"terminated_instances": [], "deleted_volumes": [], "message": "No Azure hostnames were found in the generated job variables."}
    resource_group = job_vars.get("sap_vm_provision_msazure_resource_group_name")
    compute, network = _azure_clients(job_vars)
    errors, terminated, deleted_volumes, deleted_other = [], [], [], []

    # 1. Delete the job's VMs, remember their NICs and non-shared disks
    nic_names, disk_names = [], []
    for name in hostnames:
        try:
            vm = compute.virtual_machines.get(resource_group, name)
        except Exception as exc:
            if not _azure_is_not_found(exc):
                errors.append(f"VM {name}: {exc}")
            continue
        nic_names += [_azure_resource_name(ref.id) for ref in (vm.network_profile.network_interfaces or [])]
        os_disk = vm.storage_profile.os_disk
        if os_disk is not None:
            disk_names.append(_azure_resource_name(os_disk.managed_disk.id) if os_disk.managed_disk else os_disk.name)
        for data_disk in vm.storage_profile.data_disks or []:
            disk_name = _azure_resource_name(data_disk.managed_disk.id) if data_disk.managed_disk else data_disk.name
            try:
                if (compute.disks.get(resource_group, disk_name).max_shares or 1) > 1:
                    continue  # shared disks (e.g. SBD) are handled below
            except Exception:
                pass
            disk_names.append(disk_name)
        try:
            compute.virtual_machines.begin_delete(resource_group, name).result()
            terminated.append(name)
        except Exception as exc:
            errors.append(f"VM {name}: {exc}")

    # 2. NICs and disks of the deleted VMs (skip anything still attached elsewhere)
    for nic_name in nic_names:
        try:
            network.network_interfaces.begin_delete(resource_group, nic_name).result()
            deleted_other.append(f"nic:{nic_name}")
        except Exception as exc:
            if not _azure_is_not_found(exc):
                errors.append(f"NIC {nic_name}: {exc}")

    def _delete_disk(disk_name):
        try:
            disk = compute.disks.get(resource_group, disk_name)
        except Exception as exc:
            if not _azure_is_not_found(exc):
                errors.append(f"Disk {disk_name}: {exc}")
            return
        if disk.managed_by or (disk.managed_by_extended or []):
            errors.append(f"Disk {disk_name} is still attached and was kept")
            return
        try:
            compute.disks.begin_delete(resource_group, disk_name).result()
            deleted_volumes.append(disk_name)
        except Exception as exc:
            errors.append(f"Disk {disk_name}: {exc}")

    for disk_name in disk_names:
        _delete_disk(disk_name)

    # Shared HA resources (SBD disk, load balancer, availability set) are only cleaned up together with job VMs,
    # so an empty load balancer or availability set of a deployment in progress is never removed
    if not terminated:
        message = "No Azure VMs of this job were found; shared resources were not changed."
        if errors:
            message += " Issues: " + "; ".join(errors)
        return {"terminated_instances": [], "deleted_volumes": deleted_volumes, "message": message}

    # 3. SBD shared disks (fencing method sbd_shared_disk) whose hosts are all hosts of this job
    try:
        for disk in compute.disks.list_by_resource_group(resource_group):
            tags = disk.tags or {}
            owners = [host for host in tags.get("sap_vm_provision_hosts", "").split("_") if host]
            if tags.get("sap_vm_provision") == "sbd" and owners and set(owners) <= set(hostnames):
                _delete_disk(disk.name)
    except Exception as exc:
        errors.append(f"SBD shared disks: {exc}")

    # 4. HA load balancers created by the role, only when no backend address remains
    lb_names = {job_vars.get(key) or default for key, default in (
        ("sap_vm_provision_ha_load_balancer_name_hana", "lb-sap-ha-hana"),
        ("sap_vm_provision_ha_load_balancer_name_anydb", "lb-sap-ha-anydb"),
        ("sap_vm_provision_ha_load_balancer_name_nwas", "lb-sap-ha-nwas"))}
    for lb_name in sorted(lb_names):
        try:
            lb = network.load_balancers.get(resource_group, lb_name)
        except Exception as exc:
            if not _azure_is_not_found(exc):
                errors.append(f"Load balancer {lb_name}: {exc}")
            continue
        pools_in_use = [pool.name for pool in (lb.backend_address_pools or [])
                        if (pool.backend_ip_configurations or []) or (pool.load_balancer_backend_addresses or [])]
        if pools_in_use:
            errors.append(f"Load balancer {lb_name} still has backends ({', '.join(pools_in_use)}) and was kept")
            continue
        try:
            network.load_balancers.begin_delete(resource_group, lb_name).result()
            deleted_other.append(f"lb:{lb_name}")
        except Exception as exc:
            errors.append(f"Load balancer {lb_name}: {exc}")

    # 5. HA availability sets of the role, only when empty
    avset_prefix = job_vars.get("sap_vm_provision_msazure_placement_resource_name") or "sap-availability-set-spread"
    for suffix in ("hana", "anydb", "nwas"):
        avset_name = f"{avset_prefix}-{suffix}"
        try:
            avset = compute.availability_sets.get(resource_group, avset_name)
        except Exception as exc:
            if not _azure_is_not_found(exc):
                errors.append(f"Availability set {avset_name}: {exc}")
            continue
        if avset.virtual_machines:
            continue
        try:
            compute.availability_sets.delete(resource_group, avset_name)
            deleted_other.append(f"avset:{avset_name}")
        except Exception as exc:
            errors.append(f"Availability set {avset_name}: {exc}")

    if not terminated and not deleted_volumes and not deleted_other and not errors:
        message = "No Azure resources of this job were found."
    else:
        message = (f"Deleted {len(terminated)} VM(s), {len(deleted_volumes)} disk(s)"
                   f"{' and ' + ', '.join(deleted_other) if deleted_other else ''}.")
        if errors:
            message += " Issues: " + "; ".join(errors)
    return {"terminated_instances": terminated, "deleted_volumes": deleted_volumes, "message": message}


def _gcp_job(job_state):
    generated_vars_path = job_state.get("generated_file")
    job_vars = load_generated_vars(generated_vars_path) if generated_vars_path else {}
    if job_vars.get("sap_vm_provision_iac_platform") != "gcp_ce_vm":
        return None
    credentials_path = to_container_path(job_vars.get("sap_vm_provision_gcp_credentials_json", ""))
    session = gcp_support.build_session(Path(credentials_path).expanduser())
    return gcp_support.GcpJob(job_vars, session, connection_info._planned_hosts(job_vars))


def list_job_gcp_resources(job_state):
    job = _gcp_job(job_state)
    if job is None:
        return {"platform": None, "instances": [], "volumes": [], "message": "No Google Cloud generated vars file recorded for this job."}
    return gcp_support.list_job_resources(job, PLATFORM_LABELS.get("gcp_ce_vm", "GCP Compute Engine"))


def terminate_job_gcp_resources(job_state):
    job = _gcp_job(job_state)
    if job is None:
        return {"terminated_instances": [], "deleted_volumes": [], "message": "No Google Cloud generated vars file recorded for this job."}
    return gcp_support.terminate_job_resources(job)


def build_job_connection_info(job_state):
    generated_vars_path = job_state.get("generated_file")
    job_vars = load_generated_vars(generated_vars_path) if generated_vars_path else {}
    try:
        instances = list_job_resources(job_state).get("instances", [])
    except Exception:
        instances = []
    return connection_info.build_connection_info(job_vars, instances)


def _load_cloudstack_job_vars(job_state):
    generated_vars_path = job_state.get("generated_file")
    job_vars = load_generated_vars(generated_vars_path) if generated_vars_path else {}
    if job_vars.get("sap_vm_provision_iac_platform") != "cloudstack_vm":
        return None
    return job_vars


def list_job_cloudstack_resources(job_state):
    job_vars = _load_cloudstack_job_vars(job_state)
    if job_vars is None:
        return {"platform": None, "instances": [], "volumes": [], "message": "No CloudStack generated vars file recorded for this job."}
    return cloudstack_support.list_job_resources(job_vars, PLATFORM_LABELS["cloudstack_vm"])


def terminate_job_cloudstack_resources(job_state):
    job_vars = _load_cloudstack_job_vars(job_state)
    if job_vars is None:
        return {"terminated_instances": [], "deleted_volumes": [], "message": "No CloudStack generated vars file recorded for this job."}
    return cloudstack_support.terminate_job_resources(job_vars, PLATFORM_LABELS["cloudstack_vm"])


def list_job_resources(job_state):
    platform = get_job_platform(job_state)
    resource_handlers = {
        "aws_ec2_vs": list_job_aws_resources,
        "msazure_vm": list_job_azure_resources,
        "gcp_ce_vm": list_job_gcp_resources,
        "cloudstack_vm": list_job_cloudstack_resources,
    }
    handler = resource_handlers.get(platform)
    if handler is None:
        platform_label = PLATFORM_LABELS.get(platform, platform or "selected platform")
        return {
            "platform": platform,
            "platform_label": platform_label,
            "instances": [],
            "volumes": [],
            "message": f"Resource discovery is not implemented yet for {platform_label}.",
        }
    return handler(job_state)


def terminate_job_resources_for_platform(job_state):
    platform = get_job_platform(job_state)
    terminate_handlers = {
        "aws_ec2_vs": terminate_job_aws_resources,
        "msazure_vm": terminate_job_azure_resources,
        "gcp_ce_vm": terminate_job_gcp_resources,
        "cloudstack_vm": terminate_job_cloudstack_resources,
    }
    handler = terminate_handlers.get(platform)
    if handler is None:
        platform_label = PLATFORM_LABELS.get(platform, platform or "selected platform")
        return {
            "terminated_instances": [],
            "deleted_volumes": [],
            "message": f"Termination is not implemented yet for {platform_label}.",
        }
    return handler(job_state)


def start_job(config, preflight_results=None):
    scenario_name = config["scenario"]
    scenario_path = SCENARIO_DIR / scenario_name
    playbook_path = scenario_path / "ansible_playbook.yml"

    generated_file = GENERATED_DIR / f"{scenario_name}_webui_generated.yml"
    output = build_generated_document(config)

    filtered = {}
    for key, value in output.items():
        if isinstance(value, str) and value == "ENTER_STRING_VALUE_HERE":
            continue
        if value is None:
            continue
        filtered[key] = value

    with open(generated_file, "w", encoding="utf-8") as handle:
        yaml.safe_dump(filtered, handle, sort_keys=False, default_flow_style=False)

    job_id = uuid.uuid4().hex
    log_file = RUNS_DIR / f"{job_id}.log"
    status_file = RUNS_DIR / f"{job_id}.json"

    cmd = ["ansible-playbook", str(playbook_path), "--extra-vars", f"@{generated_file}"]
    py_interp = str(Path(BASE_DIR) / ".venv" / "bin" / "python")
    if Path(py_interp).exists():
        cmd.extend(["-i", str(write_control_inventory(py_interp))])
    if config["inventory_file"]:
        cmd.extend(["-i", config["inventory_file"]])

    JOB_STATE[job_id] = {
        "status": "running",
        "exit_code": None,
        "log_file": str(log_file),
        "status_file": str(status_file),
        "command": " ".join(cmd),
        "preflight_results": preflight_results or [],
        "generated_file": str(generated_file),
        "platform": config.get("platform"),
        "scenario": scenario_name,
    }

    def runner():
        with open(log_file, "w", encoding="utf-8") as handle:
            process = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            try:
                for line in iter(process.stdout.readline, ""):
                    handle.write(line)
                    handle.flush()
                    JOB_STATE[job_id]["last_line"] = line.strip()
                process.stdout.close()
                return_code = process.wait()
            except Exception as exc:  # pragma: no cover - defensive guard
                return_code = 1
                JOB_STATE[job_id]["last_line"] = str(exc)

        JOB_STATE[job_id]["status"] = "success" if return_code == 0 else "failed"
        JOB_STATE[job_id]["exit_code"] = return_code
        JOB_STATE[job_id]["summary"] = (
            "Execution completed successfully." if return_code == 0 else f"Execution failed with exit code {return_code}."
        )
        with open(status_file, "w", encoding="utf-8") as handle:
            json.dump(JOB_STATE[job_id], handle, indent=2, sort_keys=True)

    threading.Thread(target=runner, daemon=True).start()
    return job_id


@app.route("/")
def index():
    scenarios = discover_scenarios()
    checks = check_dependencies()
    recent_jobs = list_recent_jobs(limit=3, include_resources=True)
    total_jobs = len(list_recent_jobs(limit=None, include_resources=False))
    host_plan_catalog = get_host_plan_catalog()
    instance_profile_catalog = get_instance_profile_catalog()
    return render_template(
        "index.html",
        scenarios=scenarios,
        checks=checks,
        errors=[],
        preflight_results=[],
        recent_jobs=recent_jobs,
        total_jobs=total_jobs,
        host_plan_catalog=host_plan_catalog,
        instance_profile_catalog=instance_profile_catalog,
        form={},
    )


@app.route("/api/dashboard/providers")
def provider_dashboard():
    refresh = request.args.get("refresh", "").lower() in {"1", "true", "yes"}
    return jsonify(build_provider_dashboard(force_refresh=refresh))


@app.route("/api/dashboard/providers/<provider>/delete", methods=["POST"])
def delete_provider_resource(provider):
    payload = request.get_json(silent=True) or {}
    try:
        result = delete_dashboard_resource(provider, payload)
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({"status": "ok", **result})


@app.route("/run", methods=["POST"])
def run_playbook():
    config, errors = validate_config(request.form)
    preflight_results = []
    collection_patch_results = ensure_ansible_collection_patches()
    errors.extend([item["message"] for item in collection_patch_results if not item["ok"]])
    if not errors:
        preflight_results = run_platform_preflight_check(config)
        errors.extend([check["message"] for check in preflight_results if not check["ok"]])
    if errors:
        scenarios = discover_scenarios()
        checks = check_dependencies()
        recent_jobs = list_recent_jobs(limit=3, include_resources=True)
        total_jobs = len(list_recent_jobs(limit=None, include_resources=False))
        host_plan_catalog = get_host_plan_catalog()
        instance_profile_catalog = get_instance_profile_catalog()
        return render_template(
            "index.html",
            scenarios=scenarios,
            checks=checks,
            errors=errors,
            preflight_results=preflight_results,
            recent_jobs=recent_jobs,
            total_jobs=total_jobs,
            host_plan_catalog=host_plan_catalog,
            instance_profile_catalog=instance_profile_catalog,
            form=request.form.to_dict(),
        ), 400

    save_credentials_for_platform(config)
    job_id = start_job(config, preflight_results=preflight_results)
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/job/<job_id>")
def job_detail(job_id):
    state = load_job_state(job_id)
    resources = {}
    if state:
        try:
            resources = list_job_resources(state)
        except Exception as exc:
            platform = get_job_platform(state)
            resources = {
                "platform": platform,
                "platform_label": PLATFORM_LABELS.get(platform, platform or "Cloud"),
                "instances": [],
                "volumes": [],
                "message": f"Unable to load job resources: {exc}",
            }
    return render_template("job.html", job_id=job_id, state=state or {"status": "pending", "summary": "Job not found."}, resources=resources)


@app.route("/jobs")
def jobs_index():
    jobs = list_recent_jobs(limit=None, include_resources=True)
    return render_template("jobs.html", jobs=jobs, total_jobs=len(jobs))


@app.route("/api/credentials/<platform>")
def get_saved_credentials_route(platform):
    if platform not in CONFIGURABLE_PROVIDERS:
        abort(400, description="Unsupported platform.")
    return jsonify(discover_saved_credentials().get(platform, {}))


@app.route("/api/credentials/<platform>", methods=["POST"])
def save_saved_credentials_route(platform):
    if platform not in CONFIGURABLE_PROVIDERS:
        abort(400, description="Unsupported platform.")
    payload = request.get_json(silent=True) or {}
    try:
        saved = save_provider_profile(platform, payload)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({"status": "ok", "platform": platform, "saved_fields": sorted(saved.keys())})


@app.route("/api/credentials/<platform>/clear", methods=["POST"])
def clear_saved_credentials_route(platform):
    if platform not in CONFIGURABLE_PROVIDERS:
        abort(400, description="Unsupported platform.")
    forget_saved_credentials(platform)
    return jsonify({"status": "cleared", "platform": platform})


@app.route("/api/job/<job_id>/status")
def job_status(job_id):
    state = load_job_state(job_id)
    if not state:
        return jsonify({"status": "missing", "summary": "Job not found."}), 404

    log_path = Path(state["log_file"])
    logs = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    return jsonify({
        "status": state.get("status", "pending"),
        "summary": state.get("summary", "Execution is running."),
        "exit_code": state.get("exit_code"),
        "logs": logs,
        "command": state.get("command", ""),
        "preflight_results": state.get("preflight_results", []),
    })


@app.route("/api/job/<job_id>/resources")
def job_resources(job_id):
    state = load_job_state(job_id)
    if not state:
        return jsonify({"status": "missing", "message": "Job not found."}), 404
    try:
        resources = list_job_resources(state)
    except Exception as exc:
        return jsonify({"status": "error", "message": f"Unable to load job resources: {exc}"}), 500
    return jsonify({"status": "ok", **resources})


@app.route("/api/job/<job_id>/connection")
def job_connection(job_id):
    state = load_job_state(job_id)
    if not state:
        return jsonify({"status": "missing", "message": "Job not found."}), 404
    return jsonify({"status": "ok", **build_job_connection_info(state)})


@app.route("/api/job/<job_id>/ssh_config")
def job_ssh_config(job_id):
    state = load_job_state(job_id)
    if not state:
        abort(404)
    info = build_job_connection_info(state)
    return app.response_class(
        info["ssh_config"], mimetype="text/plain",
        headers={"Content-Disposition": f'attachment; filename="ssh_config_{job_id[:8]}.conf"'},
    )


@app.route("/api/job/<job_id>/terminate", methods=["POST"])
def terminate_job_resources(job_id):
    state = load_job_state(job_id)
    if not state:
        return jsonify({"status": "missing", "message": "Job not found."}), 404
    try:
        result = terminate_job_resources_for_platform(state)
    except Exception as exc:
        return jsonify({"status": "error", "message": f"Unable to terminate resources: {exc}"}), 500
    reset_provider_dashboard_cache()
    return jsonify({"status": "ok", **result})


if __name__ == "__main__":
    app.run(host=os.environ.get("SAP_WIZARD_BIND", "0.0.0.0"), port=int(os.environ.get("SAP_WIZARD_PORT", "5000")), debug=False)
