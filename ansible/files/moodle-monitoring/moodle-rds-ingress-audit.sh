#!/usr/bin/env bash
# Read-only audit of the Moodle RDS security group for SEC-01.
# Runs as a systemd oneshot on monitor-ai-01 with an instance role that only
# allows ec2:DescribeSecurityGroupRules. It never modifies AWS resources.
#
# The RDS security group and its reviewed source groups come from instance
# tags written by Terraform (AiopsRdsSecurityGroupId and the colon-separated
# AiopsRdsReviewedSourceGroups), read through IMDSv2.
set -euo pipefail

output_dir="${MOODLE_RDS_AUDIT_OUTPUT_DIR:-/var/lib/moodle-synthetic}"
[[ "$output_dir" == /* && -d "$output_dir" && ! -L "$output_dir" ]] || {
  echo "RDS audit output directory must be an existing, non-symlink absolute path." >&2
  exit 64
}

imds=http://169.254.169.254/latest
audit_success=0
unreviewed=0
public=0

imds_get() {
  curl --fail --silent --max-time 3 -H "X-aws-ec2-metadata-token: $token" "$imds/$1"
}

if token="$(curl --fail --silent --max-time 3 -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' "$imds/api/token")" &&
  region="$(imds_get meta-data/placement/region)" &&
  rds_sg="$(imds_get meta-data/tags/instance/AiopsRdsSecurityGroupId)" &&
  reviewed="$(imds_get meta-data/tags/instance/AiopsRdsReviewedSourceGroups)" &&
  [[ "$rds_sg" =~ ^sg-[0-9a-f]+$ && "$reviewed" =~ ^sg-[0-9a-f]+(:sg-[0-9a-f]+)*$ ]] &&
  rules="$(timeout 20 aws ec2 describe-security-group-rules \
    --region "$region" \
    --filters "Name=group-id,Values=$rds_sg" \
    --query 'SecurityGroupRules[?IsEgress==`false`].[ReferencedGroupInfo.GroupId, CidrIpv4, CidrIpv6, PrefixListId]' \
    --output text 2>/dev/null)"; then
  audit_success=1
  # Text output prints "None" for absent fields, one rule per line.
  while read -r referenced cidr4 cidr6 _; do
    [[ -n "${referenced:-}" ]] || continue
    if [[ "$referenced" != None && ":$reviewed:" == *":$referenced:"* ]]; then
      continue
    fi
    # Any CIDR, prefix list, or non-reviewed group is an unreviewed source.
    unreviewed=$((unreviewed + 1))
    if [[ "$cidr4" == 0.0.0.0/0 || "$cidr6" == ::/0 ]]; then
      public=$((public + 1))
    fi
  done <<<"$rules"
fi

temporary_file="$(mktemp "$output_dir/.moodle_rds_ingress.XXXXXX")"
cleanup() { [[ -e "$temporary_file" ]] && rm -f -- "$temporary_file"; }
trap cleanup EXIT
{
  printf '%s\n' \
    '# HELP moodle_rds_sg_audit_success Whether the read-only RDS security-group audit succeeded.' \
    '# TYPE moodle_rds_sg_audit_success gauge' \
    "moodle_rds_sg_audit_success $audit_success" \
    '# HELP moodle_rds_sg_audit_timestamp_seconds Unix timestamp of this RDS security-group audit.' \
    '# TYPE moodle_rds_sg_audit_timestamp_seconds gauge' \
    "moodle_rds_sg_audit_timestamp_seconds $(date -u +%s)"
  # Counts are omitted when the audit fails, so a failed audit is never
  # mistaken for a clean security group.
  if (( audit_success == 1 )); then
    printf '%s\n' \
      '# HELP moodle_rds_sg_unreviewed_ingress_rules RDS ingress rules whose source is not a reviewed security group.' \
      '# TYPE moodle_rds_sg_unreviewed_ingress_rules gauge' \
      "moodle_rds_sg_unreviewed_ingress_rules $unreviewed" \
      '# HELP moodle_rds_sg_public_ingress_rules RDS ingress rules open to 0.0.0.0/0 or ::/0.' \
      '# TYPE moodle_rds_sg_public_ingress_rules gauge' \
      "moodle_rds_sg_public_ingress_rules $public" \
      '# HELP moodle_fault_sec01_unauthorized_ingress Whether the RDS security group has any unreviewed ingress source.' \
      '# TYPE moodle_fault_sec01_unauthorized_ingress gauge' \
      "moodle_fault_sec01_unauthorized_ingress $(( unreviewed > 0 ? 1 : 0 ))"
  fi
} > "$temporary_file"
chmod 0644 "$temporary_file"
mv -- "$temporary_file" "$output_dir/moodle_rds_ingress.prom"
trap - EXIT
