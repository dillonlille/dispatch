#!/bin/sh
set -eu

PRODUCT_VERSION='0.0.7'
MANIFEST_URL='https://dispatch.dillonlille.com/releases/0.0.7/installation-release-manifest.json'
MANIFEST_SHA256='69e3c869dc496f81b64e0746131983bf24bf902b0ca7bdcd3a2f05b2855aa302'
INSTALLER_URL='https://dispatch.dillonlille.com/releases/0.0.7/dispatch_installer-0.1.5-py3-none-any.whl'
INSTALLER_SIZE='65043'
INSTALLER_SHA256='2db9873d46ff94965a55099a39a9b97d33a0544113ecf03c7df60c7b38522b7c'
DISPATCH_HOME="${DISPATCH_HOME:-$HOME/.dispatch}"

fail() {
    printf '%s\n' "Dispatch installation failed: $*" >&2
    exit 1
}

command -v curl >/dev/null 2>&1 || fail 'curl is required'
command -v python3 >/dev/null 2>&1 || fail 'Python 3.11 through 3.13 is required'
python3 -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)' \
    || fail 'Python 3.11 through 3.13 is required'

umask 077
mkdir -p "$DISPATCH_HOME/staging"
chmod 700 "$DISPATCH_HOME" "$DISPATCH_HOME/staging"
work="$(mktemp -d "$DISPATCH_HOME/staging/bootstrap.XXXXXX")"
installer_stage="$DISPATCH_HOME/staging/installer-venv.$$"
installer_environment="$DISPATCH_HOME/installer-venv"
installer_backup=''
install_complete=0
cleanup() {
    if [ -n "$installer_backup" ] && [ -e "$installer_backup" ]; then
        if [ "$install_complete" -eq 1 ]; then
            rm -rf "$installer_backup"
        else
            rm -rf "$installer_environment"
            mv "$installer_backup" "$installer_environment"
        fi
    fi
    rm -rf "$work" "$installer_stage"
}
trap cleanup EXIT HUP INT TERM

manifest="$work/installation-release-manifest.json"
installer_wheel="$work/${INSTALLER_URL##*/}"
curl -fsSL --proto '=https' --tlsv1.2 --max-redirs 0 "$MANIFEST_URL" -o "$manifest"
curl -fsSL --proto '=https' --tlsv1.2 --max-redirs 0 "$INSTALLER_URL" -o "$installer_wheel"

MANIFEST="$manifest" INSTALLER="$installer_wheel" \
MANIFEST_SHA256="$MANIFEST_SHA256" INSTALLER_SHA256="$INSTALLER_SHA256" INSTALLER_SIZE="$INSTALLER_SIZE" \
python3 - <<'PY' || fail 'bootstrap artifact verification failed'
import hashlib
import os
from pathlib import Path
for path_name, digest_name, size_name in (
    ('MANIFEST', 'MANIFEST_SHA256', None),
    ('INSTALLER', 'INSTALLER_SHA256', 'INSTALLER_SIZE'),
):
    path = Path(os.environ[path_name])
    data = path.read_bytes()
    if size_name is not None and len(data) != int(os.environ[size_name]):
        raise SystemExit(1)
    if hashlib.sha256(data).hexdigest() != os.environ[digest_name]:
        raise SystemExit(1)
PY

python3 -m venv "$installer_stage"
"$installer_stage/bin/python" -m pip install --disable-pip-version-check --no-deps "$installer_wheel" >/dev/null
plan="$work/plan.json"
if ! "$installer_stage/bin/python" -m dispatch_installer plan --manifest "$manifest" --manifest-sha256 "$MANIFEST_SHA256" >"$plan"; then
    fail 'product release is not ready'
fi
installer_version="$(PLAN="$plan" python3 -c 'import json,os; print(json.load(open(os.environ["PLAN"]))["data"]["manifest"]["installer_version"])')"
core_url="$(PLAN="$plan" python3 -c 'import json,os; print(json.load(open(os.environ["PLAN"]))["data"]["manifest"]["core_artifact"]["url"])')"
core_size="$(PLAN="$plan" python3 -c 'import json,os; print(json.load(open(os.environ["PLAN"]))["data"]["manifest"]["core_artifact"]["size"])')"
core_sha256="$(PLAN="$plan" python3 -c 'import json,os; print(json.load(open(os.environ["PLAN"]))["data"]["manifest"]["core_artifact"]["sha256"])')"
core_wheel="$work/${core_url##*/}"
curl -fsSL --proto '=https' --tlsv1.2 --max-redirs 0 "$core_url" -o "$core_wheel"
CORE="$core_wheel" CORE_SIZE="$core_size" CORE_SHA256="$core_sha256" python3 - <<'PY' \
    || fail 'Core artifact verification failed'
import hashlib
import os
from pathlib import Path
content = Path(os.environ['CORE']).read_bytes()
if len(content) != int(os.environ['CORE_SIZE']) or hashlib.sha256(content).hexdigest() != os.environ['CORE_SHA256']:
    raise SystemExit(1)
PY

if [ -e "$installer_environment" ] || [ -L "$installer_environment" ]; then
    INSTALLER_ENVIRONMENT="$installer_environment" python3 - <<'PY' || fail 'existing installer environment is unsafe'
import os
import stat
from pathlib import Path
path = Path(os.environ['INSTALLER_ENVIRONMENT'])
details = path.lstat()
if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or details.st_mode & 0o022:
    raise SystemExit(1)
PY
    if "$installer_environment/bin/python" -c \
        'import dispatch_installer,sys; raise SystemExit(0 if dispatch_installer.__version__ == sys.argv[1] else 1)' \
        "$installer_version" 2>/dev/null; then
        rm -rf "$installer_stage"
    else
        installer_backup="$DISPATCH_HOME/installer-venv.previous.$$"
        [ ! -e "$installer_backup" ] || fail 'temporary installer backup already exists'
        mv "$installer_environment" "$installer_backup"
        mv "$installer_stage" "$installer_environment"
    fi
else
    mv "$installer_stage" "$installer_environment"
fi
"$installer_environment/bin/python" -m dispatch_installer install \
    --manifest "$manifest" \
    --manifest-sha256 "$MANIFEST_SHA256" \
    --core-wheel "$core_wheel" \
    --yes
"$DISPATCH_HOME/bin/dispatch" health >/dev/null || fail 'Core health verification failed'
"$installer_environment/bin/python" -m dispatch_installer verify >/dev/null || fail 'installation verification failed'
install_complete=1

printf '\nDispatch %s is installed.\n' "$PRODUCT_VERSION"
choice=2
if (: </dev/tty) 2>/dev/null; then
    exec 3<>/dev/tty
    printf '1. Start Setup\n2. Skip for Now\nSelect [1-2]: ' >&3
    IFS= read -r choice <&3 || choice=2
    case "$choice" in
        1) "$DISPATCH_HOME/bin/dispatch" setup <&3 ;;
        2) printf '%s\n' "Setup skipped. Run $DISPATCH_HOME/bin/dispatch setup when ready." ;;
        *) fail 'invalid setup choice' ;;
    esac
    exec 3>&-
else
    printf '%s\n' "No controlling terminal; setup skipped. Run $DISPATCH_HOME/bin/dispatch setup when ready."
fi
