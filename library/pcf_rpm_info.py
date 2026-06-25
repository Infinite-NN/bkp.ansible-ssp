#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ==============================================================
# Ansible Custom Module: pcf_rpm_info
# Purpose : Query RPM metadata from a local .rpm file without
#           installing it — replaces ad-hoc rpm -qp shell calls
# Returns : name, version, release, arch, epoch, summary,
#           buildtime, size, sigmd5, provides, requires
# Author  : Avaya Pipeline Automation (nnandanwar@avaya.com)
# ==============================================================

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r'''
---
module: pcf_rpm_info
short_description: Query metadata from a local RPM file
description:
  - Reads RPM header metadata from a local .rpm file.
  - Returns structured package information without installing the RPM.
  - Safe for use inside chroot environments when rpm binary is available.
  - Validates RPM file format before querying.
version_added: "1.0"
options:
  path:
    description:
      - Absolute path to the .rpm file to inspect.
    required: true
    type: str
  fields:
    description:
      - List of RPM header fields to return.
      - Defaults to all available fields.
    required: false
    type: list
    elements: str
    default:
      - name
      - version
      - release
      - arch
      - epoch
      - summary
      - buildtime
      - size
      - license
  validate_signature:
    description:
      - Whether to attempt GPG signature check.
      - Requires RPM keyring to be configured.
    required: false
    type: bool
    default: false
  strict_mode:
    description:
      - Fail if any required field cannot be extracted.
      - If false, continue with available fields.
    required: false
    type: bool
    default: true
author:
  - Avaya Pipeline Automation (nnandanwar@avaya.com)
examples:
  - description: Get full RPM info
    code: |
      - name: Query PCF RPM metadata
        pcf_rpm_info:
          path: /root/releases/10.2.1.5/pcf-module-10.2.1.5-24800.x86_64.rpm
        register: rpm_meta

      - debug:
          var: rpm_meta.rpm_info

  - description: Get specific fields only
    code: |
      - name: Get RPM version only
        pcf_rpm_info:
          path: /tmp/pcf-module-10.2.1.5-24800.x86_64.rpm
          fields:
            - name
            - version
            - release
        register: rpm_meta

  - description: Validate RPM and signature
    code: |
      - name: Validate PCF RPM
        pcf_rpm_info:
          path: /tmp/pcf-module.rpm
          validate_signature: true
          strict_mode: true
        register: rpm_validation
        failed_when: not rpm_validation.is_valid_rpm
'''

RETURN = r'''
rpm_info:
  description: Dictionary of RPM metadata fields
  returned: success
  type: dict
  sample:
    name: pcf-module
    version: "10.2.1.5"
    release: "24800"
    arch: x86_64
    epoch: "0"
    summary: "Avaya PCF Kernel Module"
    buildtime: "1718700000"
    size: "4567890"
    license: "Avaya Proprietary"

path:
  description: Resolved absolute path to the RPM file
  returned: always
  type: str

file_size_bytes:
  description: Size of the RPM file on disk in bytes
  returned: success
  type: int

is_valid_rpm:
  description: Whether the file is a valid RPM archive
  returned: always
  type: bool

nvr:
  description: "Name-Version-Release string (convenience)"
  returned: success
  type: str
  sample: "pcf-module-10.2.1.5-24800"

nvra:
  description: "Name-Version-Release-Arch string (convenience)"
  returned: success
  type: str
  sample: "pcf-module-10.2.1.5-24800.x86_64"

signature_check:
  description: Result of GPG signature validation if requested
  returned: when validate_signature=true
  type: str
  sample: "OK"

fields_extracted:
  description: Count of fields successfully extracted
  returned: success
  type: int

fields_missing:
  description: List of fields that could not be extracted
  returned: when strict_mode=false
  type: list
'''

import os
import subprocess
import time
import re

from ansible.module_utils.basic import AnsibleModule


# ── RPM queryformat field map ──────────────────────────────────
FIELD_MAP = {
    'name':         '%{NAME}',
    'version':      '%{VERSION}',
    'release':      '%{RELEASE}',
    'arch':         '%{ARCH}',
    'epoch':        '%{EPOCH}',
    'summary':      '%{SUMMARY}',
    'buildtime':    '%{BUILDTIME}',
    'size':         '%{SIZE}',
    'license':      '%{LICENSE}',
    'vendor':       '%{VENDOR}',
    'packager':     '%{PACKAGER}',
    'url':          '%{URL}',
    'group':        '%{GROUP}',
    'os':           '%{OS}',
    'sigmd5':       '%{SIGMD5}',
    'description':  '%{DESCRIPTION}',
}

DEFAULT_FIELDS = [
    'name', 'version', 'release', 'arch',
    'epoch', 'summary', 'buildtime', 'size', 'license'
]


def rpm_binary_exists():
    """Check if rpm binary is available in PATH."""
    return subprocess.run(
        ['which', 'rpm'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    ).returncode == 0


def validate_rpm_file(path):
    """
    Check magic bytes to confirm file is an RPM.
    RPM magic: 0xED 0xAB 0xEE 0xDB (at byte 0)
    """
    try:
        with open(path, 'rb') as f:
            magic = f.read(4)
        return magic == b'\xed\xab\xee\xdb'
    except (IOError, OSError):
        return False


def run_rpm_query(module, rpm_path, fields):
    """
    Run rpm -qp with a composed queryformat string.
    Returns dict of field→value or raises on error.
    """
    if not rpm_binary_exists():
        module.fail_json(msg="rpm binary not found in PATH")

    # Build queryformat: field=value per field, separated by newlines
    fmt_parts = []
    valid_fields = []

    for field in fields:
        if field not in FIELD_MAP:
            module.warn(f"Unknown RPM field '{field}' — skipping")
            continue
        fmt_parts.append(f"{field}={FIELD_MAP[field]}")
        valid_fields.append(field)

    if not fmt_parts:
        module.fail_json(
            msg=f"No valid RPM fields provided from: {fields}"
        )

    queryformat = r'\n'.join(fmt_parts) + r'\n'

    cmd = [
        'rpm', '-qp',
        '--queryformat', queryformat,
        '--nosignature',
        rpm_path
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            universal_newlines=True
        )
    except subprocess.TimeoutExpired:
        module.fail_json(msg=f"rpm query timed out (30s) for: {rpm_path}")
    except Exception as e:
        module.fail_json(msg=f"rpm query error: {str(e)}")

    if result.returncode != 0:
        return None, result.stderr.strip(), []

    # Parse output: field=value lines
    info = {}
    extracted_fields = []
    missing_fields = []

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if '=' in line:
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip()

            # Normalise special values
            if value.lower() in ('(none)', '(null)'):
                value = '0' if key == 'epoch' else ''

            info[key] = value
            extracted_fields.append(key)

    # Track missing fields
    for field in valid_fields:
        if field not in info:
            missing_fields.append(field)

    return info, None, missing_fields


def check_signature(module, rpm_path):
    """
    Run rpm --checksig on the file.
    Returns (status_string, is_valid_bool)
    """
    cmd = ['rpm', '--checksig', rpm_path]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            universal_newlines=True
        )

        # Parse output
        output = result.stdout.strip() or result.stderr.strip()

        if result.returncode == 0:
            return "OK", True
        else:
            return output or "SIGNATURE VALIDATION FAILED", False

    except subprocess.TimeoutExpired:
        return "Signature check timed out", False
    except Exception as e:
        return f"Signature check error: {str(e)}", False


def main():
    module = AnsibleModule(
        argument_spec=dict(
            path=dict(
                type='str',
                required=True
            ),
            fields=dict(
                type='list',
                elements='str',
                default=DEFAULT_FIELDS
            ),
            validate_signature=dict(
                type='bool',
                default=False
            ),
            strict_mode=dict(
                type='bool',
                default=True
            ),
        ),
        supports_check_mode=True,
    )

    rpm_path = os.path.realpath(module.params['path'])
    fields = module.params['fields']
    validate_sig = module.params['validate_signature']
    strict_mode = module.params['strict_mode']

    result = dict(
        changed=False,
        path=rpm_path,
        is_valid_rpm=False,
        rpm_info={},
        fields_extracted=0,
        fields_missing=[],
    )

    # ── File existence check ───────────────────────────────────
    if not os.path.isfile(rpm_path):
        module.fail_json(
            msg=f"RPM file not found: {rpm_path}",
            **result
        )

    result['file_size_bytes'] = os.path.getsize(rpm_path)

    # ── Magic byte validation ──────────────────────────────────
    result['is_valid_rpm'] = validate_rpm_file(rpm_path)

    if not result['is_valid_rpm']:
        module.fail_json(
            msg=(
                f"File does not appear to be a valid RPM "
                f"(magic bytes mismatch): {rpm_path}"
            ),
            **result
        )

    # ── Check mode: skip rpm binary call ──────────────────────
    if module.check_mode:
        result['rpm_info'] = {field: "(check mode)" for field in fields}
        module.exit_json(**result)

    # ── Query RPM metadata ─────────────────────────────────────
    info, error, missing = run_rpm_query(module, rpm_path, fields)

    if error:
        if strict_mode:
            module.fail_json(
                msg=f"rpm query failed: {error}",
                **result
            )
        else:
            module.warn(f"rpm query error (continuing): {error}")

    if info:
        result['rpm_info'] = info
        result['fields_extracted'] = len(info)
        result['fields_missing'] = missing

    # ── Strict mode check ──────────────────────────────────────
    if strict_mode and missing:
        module.fail_json(
            msg=f"strict_mode=true but {len(missing)} field(s) could not be extracted: {missing}",
            **result
        )

    # ── Optional signature check ───────────────────────────────
    if validate_sig:
        sig_result, sig_valid = check_signature(module, rpm_path)
        result['signature_check'] = sig_result
        result['signature_valid'] = sig_valid

        if strict_mode and not sig_valid:
            module.fail_json(
                msg=f"GPG signature validation failed: {sig_result}",
                **result
            )

    # ── Derived convenience fields ─────────────────────────────
    if 'name' in result['rpm_info'] and 'version' in result['rpm_info'] and 'release' in result['rpm_info']:
        name = result['rpm_info']['name']
        version = result['rpm_info']['version']
        release = result['rpm_info']['release']
        result['nvr'] = f"{name}-{version}-{release}"

    if 'arch' in result['rpm_info'] and 'nvr' in result:
        arch = result['rpm_info']['arch']
        result['nvra'] = f"{result['nvr']}.{arch}"

    module.exit_json(**result)


if __name__ == '__main__':
    main()