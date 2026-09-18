#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOYMENT_TF_VARS="$REPO_ROOT/terraform/deployment.tfvars"
DEFAULT_TF_VARS="$REPO_ROOT/terraform/terraform.tfvars"
if [[ -f "$DEPLOYMENT_TF_VARS" ]]; then
    TF_VARS="$DEPLOYMENT_TF_VARS"
else
    TF_VARS="$DEFAULT_TF_VARS"
fi
CHECK_ONLY=false

usage() {
    cat <<'EOF'
Usage:
  bash automation/update-infrastructure.sh [--check]
  bash automation/update-infrastructure.sh --var-file <path> [--check]

Options:
  --var-file <path>  File tfvars to update.
                     Default: terraform/deployment.tfvars when it exists;
                     otherwise terraform/terraform.tfvars.
  --check            Check whether my_ip_cidr needs updating, without writing.
  -h, --help         Show this help.

This script only updates my_ip_cidr in the selected tfvars file. It does not
run Terraform, overwrite the Ansible inventory, or contact any EC2 instance.
EOF
}

die() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

while (( $# > 0 )); do
    case "$1" in
        --var-file)
            (( $# >= 2 )) || die "--var-file requires a path."
            TF_VARS="$2"
            shift 2
            ;;
        --check)
            CHECK_ONLY=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "Unknown argument: $1. Use --help for usage."
            ;;
    esac
done

[[ -f "$TF_VARS" ]] || die "tfvars file not found: $TF_VARS"
[[ ! -L "$TF_VARS" ]] || die "Refusing to update a symbolic link: $TF_VARS"

for required_command in awk chmod curl mktemp mv rm tr; do
    command -v "$required_command" >/dev/null 2>&1 ||
        die "Required command not found: $required_command"
done

assignment_count="$(
    awk '
        /^[[:space:]]*my_ip_cidr[[:space:]]*=/ { count++ }
        END { print count + 0 }
    ' "$TF_VARS"
)"

[[ "$assignment_count" -eq 1 ]] ||
    die "Expected exactly one my_ip_cidr assignment in $TF_VARS; found $assignment_count."

current_cidr="$(
    awk -F '"' '
        /^[[:space:]]*my_ip_cidr[[:space:]]*=/ {
            if (NF >= 3) {
                print $2
            }
            exit
        }
    ' "$TF_VARS"
)"

[[ -n "$current_cidr" ]] ||
    die "my_ip_cidr must be a quoted string in $TF_VARS."

if ! public_ip="$(
    curl --ipv4 --fail --silent --show-error --connect-timeout 5 --max-time 15 https://api.ipify.org |
        tr -d '[:space:]'
)"; then
    die "Could not determine the public IPv4 address from api.ipify.org."
fi

[[ "$public_ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] ||
    die "The IP service did not return a valid IPv4 address."

IFS='.' read -r -a octets <<< "$public_ip"
for octet in "${octets[@]}"; do
    (( 10#$octet <= 255 )) || die "The IP service returned an invalid IPv4 address."
done

new_cidr="$public_ip/32"

if [[ "$current_cidr" == "$new_cidr" ]]; then
    printf 'my_ip_cidr is already current in %s. No file was changed.\n' "$TF_VARS"
    exit 0
fi

if [[ "$CHECK_ONLY" == true ]]; then
    printf 'my_ip_cidr needs updating in %s. No file was changed.\n' "$TF_VARS"
    exit 0
fi

temporary_file=""
cleanup() {
    if [[ -n "$temporary_file" && -e "$temporary_file" ]]; then
        rm -f -- "$temporary_file"
    fi
}
trap cleanup EXIT INT TERM

temporary_file="$(mktemp "${TF_VARS}.tmp.XXXXXX")"

awk -v cidr="$new_cidr" '
    /^[[:space:]]*my_ip_cidr[[:space:]]*=/ {
        sub(/"[^"]*"/, "\"" cidr "\"")
    }
    { print }
' "$TF_VARS" > "$temporary_file"

chmod --reference="$TF_VARS" "$temporary_file"
mv -- "$temporary_file" "$TF_VARS"
temporary_file=""

printf 'Updated my_ip_cidr in %s.\n' "$TF_VARS"
printf 'AWS infrastructure was not changed. Create and review a Terraform plan before applying.\n'
