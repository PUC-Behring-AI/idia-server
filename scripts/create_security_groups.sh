#!/usr/bin/env bash
# =============================================================================
# IDIA Server — AWS Security Group Creator
# =============================================================================
#
# Creates or updates the security groups required by the IDIA Server Ray
# cluster on AWS. Idempotent — safe to re-run.
#
# Usage:
#   ./scripts/create_security_groups.sh
#
# Prerequisites:
#   - AWS credentials configured (aws configure or instance profile)
#   - AWS CLI v2 installed
#
# Security Groups created:
#   - idia-server-sg (head node + workers):
#       Ingress: 4000 (LiteLLM) from ALLOWED_IP_RANGE or 0.0.0.0/0
#                SSH from ALLOWED_SSH_RANGE or 0.0.0.0/0
#                All intra-SG traffic (workers ↔ head)
#       Egress:  all traffic (default)
#
# Best practice: replace ALLOWED_IP_RANGE and ALLOWED_SSH_RANGE with
# the institute's public IP range for production deployments.
#
# See docs/ARCHITECTURE.md §7.3 for the security model.
# =============================================================================

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────
# Override via env vars for production deployments:
#   ALLOWED_IP_RANGE="10.0.0.0/16"
#   ALLOWED_SSH_RANGE="your-institute-ip/32"
ALLOWED_IP_RANGE="${ALLOWED_IP_RANGE:-0.0.0.0/0}"
ALLOWED_SSH_RANGE="${ALLOWED_SSH_RANGE:-0.0.0.0/0}"
SG_NAME="${SG_NAME:-idia-server-sg}"
VPC_ID="${VPC_ID:-}"

# ── Helpers ─────────────────────────────────────────────────────────────

info()  { echo "[INFO]  $*"; }
warn()  { echo "[WARN]  $*" >&2; }
error() { echo "[ERROR] $*" >&2; exit 1; }

# ── Pre-flight ──────────────────────────────────────────────────────────

if ! command -v aws &> /dev/null; then
    error "AWS CLI not found. Install with: pip install awscli"
fi

if ! aws sts get-caller-identity &> /dev/null; then
    error "AWS credentials not configured. Run: aws configure"
fi

# ── Create or find security group ──────────────────────────────────────

VPC_FLAG=""
if [ -n "$VPC_ID" ]; then
    VPC_FLAG="--vpc-id $VPC_ID"
fi

info "Creating security group '${SG_NAME}'..."

SG_ID=$(aws ec2 describe-security-groups \
    --group-names "$SG_NAME" \
    --query "SecurityGroups[0].GroupId" \
    --output text 2>/dev/null || true)

if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
    SG_ID=$(aws ec2 create-security-group \
        $VPC_FLAG \
        --group-name "$SG_NAME" \
        --description "IDIA Server - Ray cluster (LiteLLM proxy + Ray dashboard)" \
        --query "GroupId" \
        --output text)
    info "Created security group: $SG_ID"
else
    info "Security group already exists: $SG_ID"
fi

# ── Authorize ingress rules (idempotent) ────────────────────────────────

authorize_ingress() {
    local port="$1"
    local protocol="$2"
    local cidr="$3"
    local description="$4"

    # AWS CLI v2 no longer supports --description with --cidr.
    # Use --ip-permissions format (compatible with v1 and v2).
    if aws ec2 authorize-security-group-ingress \
        --group-id "$SG_ID" \
        --ip-permissions "IpProtocol=$protocol,FromPort=$port,ToPort=$port,IpRanges=[{CidrIp=$cidr,Description=$description}]" \
        2>/dev/null; then
        info "  Ingress: $protocol/$port from $cidr ($description)"
    else
        warn "  Ingress $protocol/$port from $cidr already exists (skipped)"
    fi
}

authorize_ingress 4000 tcp "$ALLOWED_IP_RANGE" "LiteLLM proxy - OpenAI-compatible API"
authorize_ingress 22   tcp "$ALLOWED_SSH_RANGE" "SSH access to Ray head"

# Intra-SG: all traffic between Ray nodes (v2-compatible format)
if aws ec2 authorize-security-group-ingress \
    --group-id "$SG_ID" \
    --ip-permissions "IpProtocol=-1,UserIdGroupPairs=[{GroupId=$SG_ID,Description=Intra-SG: Ray cluster communication}]" \
    2>/dev/null; then
    info "  Intra-SG: all traffic within $SG_NAME"
else
    warn "  Intra-SG rule already exists (skipped)"
fi

# ── Output ──────────────────────────────────────────────────────────────

echo ""
echo "=========================================="
echo " Security group ready: $SG_ID ($SG_NAME)"
echo "=========================================="
echo ""
echo "Use with cluster.yaml:"
echo "  security_group_name: $SG_NAME"
echo ""
echo "To verify:"
echo "  aws ec2 describe-security-groups --group-ids $SG_ID"
echo ""
