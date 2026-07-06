#!/usr/bin/env bash
# =============================================================================
# IDIA Server — IAM Node Role Creator
# =============================================================================
#
# Cria a IAM role + instance profile usados pelos nós do cluster Ray na AWS.
# Idempotente — seguro re-executar.
#
# Por que esta role existe:
#   O Ray cluster autoscaler roda NO HEAD NODE e chama RunInstances/
#   TerminateInstances para subir/derrubar os GPU workers. Portanto o head
#   precisa de uma instance profile com permissões de EC2. Os workers herdam
#   a mesma profile (usada para acesso opcional a S3 do cache de modelos).
#
#   O cluster.yaml referencia esta profile via:
#       node_config.IamInstanceProfile.Name: ray-cluster-node-profile
#
#   Sem pré-criar isto, o Ray tentaria criar a role ray-autoscaler-v1 por
#   conta própria (exige iam:CreateRole no usuário que roda `ray up`).
#
# Prerequisites:
#   - AWS CLI v2 + credenciais com permissão de IAM (CreateRole, etc.)
#
# Usage:
#   ./scripts/create_iam_node_role.sh
# =============================================================================

set -euo pipefail

ROLE_NAME="${ROLE_NAME:-ray-cluster-node-role}"
PROFILE_NAME="${PROFILE_NAME:-ray-cluster-node-profile}"
POLICY_NAME="ray-cluster-node-policy"

info()  { echo "[INFO]  $*"; }
error() { echo "[ERROR] $*" >&2; exit 1; }

command -v aws >/dev/null 2>&1 || error "AWS CLI não encontrado."
aws sts get-caller-identity >/dev/null 2>&1 || error "Credenciais AWS não configuradas. Rode: aws configure"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# ── Trust policy: EC2 pode assumir a role ────────────────────────────────────
TRUST_DOC="$(mktemp)"
cat > "$TRUST_DOC" <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "ec2.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

# ── Permissions: EC2 (autoscaler) + PassRole (passar a profile aos workers) ──
PERM_DOC="$(mktemp)"
cat > "$PERM_DOC" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AutoscalerEC2",
      "Effect": "Allow",
      "Action": [
        "ec2:RunInstances",
        "ec2:TerminateInstances",
        "ec2:StartInstances",
        "ec2:StopInstances",
        "ec2:Describe*",
        "ec2:CreateTags",
        "ec2:DeleteTags"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PassarProfileAosWorkers",
      "Effect": "Allow",
      "Action": [
        "iam:PassRole",
        "iam:GetInstanceProfile"
      ],
      "Resource": [
        "arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}",
        "arn:aws:iam::${ACCOUNT_ID}:instance-profile/${PROFILE_NAME}"
      ]
    }
  ]
}
EOF
# NOTA: se habilitar o cache de modelos em S3 (./idia cache), adicione um
# statement com s3:GetObject/ListBucket no bucket idia-models-cache-${ACCOUNT_ID}.

# ── Criar role ───────────────────────────────────────────────────────────────
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    info "Role '$ROLE_NAME' já existe."
else
    aws iam create-role --role-name "$ROLE_NAME" \
        --assume-role-policy-document "file://$TRUST_DOC" \
        --description "IDIA Server - Ray cluster node role (autoscaler EC2)" >/dev/null
    info "Role criada: $ROLE_NAME"
fi

# ── Anexar/atualizar a policy inline ─────────────────────────────────────────
aws iam put-role-policy --role-name "$ROLE_NAME" \
    --policy-name "$POLICY_NAME" \
    --policy-document "file://$PERM_DOC"
info "Policy '$POLICY_NAME' aplicada."

# ── Criar instance profile e associar a role ─────────────────────────────────
if aws iam get-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null 2>&1; then
    info "Instance profile '$PROFILE_NAME' já existe."
else
    aws iam create-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null
    info "Instance profile criado: $PROFILE_NAME"
fi

# add-role-to-instance-profile falha se a role já estiver associada — ignore.
if aws iam add-role-to-instance-profile \
        --instance-profile-name "$PROFILE_NAME" \
        --role-name "$ROLE_NAME" 2>/dev/null; then
    info "Role '$ROLE_NAME' associada ao profile '$PROFILE_NAME'."
else
    info "Role já associada ao profile (ok)."
fi

rm -f "$TRUST_DOC" "$PERM_DOC"

echo ""
echo "=========================================="
echo " IAM pronto: ${PROFILE_NAME}"
echo "=========================================="
echo "Referenciado no cluster.yaml:"
echo "  IamInstanceProfile:"
echo "    Name: ${PROFILE_NAME}"
echo ""
echo "A propagação do IAM leva ~10s. Se o 'ray up' reclamar de instance"
echo "profile inexistente logo após criar, aguarde e tente de novo."
