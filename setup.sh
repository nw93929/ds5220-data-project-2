#!/bin/bash
set -euo pipefail

REGION="us-east-1"
BUCKET="xsj5rs-project2"
KEY_NAME="ds5220-key"
KEY_PATH="$HOME/.ssh/${KEY_NAME}.pem"
INSTANCE_TYPE="t3.micro"
ROLE_NAME="ds5220-ec2-role"
PROFILE_NAME="ds5220-ec2-profile"
POLICY_NAME="ds5220-ec2-policy"
SG_NAME="ds5220-sg"
TABLE_NAME="iss-tracking"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# S3
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
fi

aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration "BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false"

aws s3api put-bucket-website --bucket "$BUCKET" \
  --website-configuration '{"IndexDocument":{"Suffix":"index.html"},"ErrorDocument":{"Key":"error.html"}}'

aws s3api put-bucket-policy --bucket "$BUCKET" --policy "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Effect\": \"Allow\",
    \"Principal\": \"*\",
    \"Action\": \"s3:GetObject\",
    \"Resource\": \"arn:aws:s3:::${BUCKET}/*\"
  }]
}"

# IAM
if ! aws iam get-role --role-name "$ROLE_NAME" > /dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]
  }' > /dev/null
fi

aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name "$POLICY_NAME" --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Effect\": \"Allow\", \"Action\": [\"s3:PutObject\", \"s3:GetObject\"], \"Resource\": \"arn:aws:s3:::${BUCKET}/*\"},
    {\"Effect\": \"Allow\", \"Action\": [\"dynamodb:PutItem\", \"dynamodb:GetItem\", \"dynamodb:Query\"], \"Resource\": \"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/*\"}
  ]
}"

if ! aws iam get-instance-profile --instance-profile-name "$PROFILE_NAME" > /dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$PROFILE_NAME" > /dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$PROFILE_NAME" --role-name "$ROLE_NAME"
  sleep 15
fi

# Security group
VPC_ID=$(aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" \
  --query "Vpcs[0].VpcId" --output text --region "$REGION")

SG_ID=$(aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=${SG_NAME}" "Name=vpc-id,Values=${VPC_ID}" \
  --query "SecurityGroups[0].GroupId" --output text --region "$REGION")

if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
  SG_ID=$(aws ec2 create-security-group --group-name "$SG_NAME" \
    --description "DS5220 Project 2" --vpc-id "$VPC_ID" \
    --region "$REGION" --query GroupId --output text)
  for PORT in 22 80 8000 8080; do
    aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
      --protocol tcp --port "$PORT" --cidr 0.0.0.0/0 --region "$REGION" > /dev/null
  done
fi

# Key pair
if ! aws ec2 describe-key-pairs --key-names "$KEY_NAME" --region "$REGION" > /dev/null 2>&1; then
  mkdir -p "$HOME/.ssh"
  aws ec2 create-key-pair --key-name "$KEY_NAME" \
    --query "KeyMaterial" --output text --region "$REGION" > "$KEY_PATH"
  chmod 400 "$KEY_PATH"
fi

# AMI
AMI_ID=$(aws ec2 describe-images --owners 099720109477 \
  --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*" "Name=state,Values=available" \
  --query "sort_by(Images, &CreationDate)[-1].ImageId" --output text --region "$REGION")

# EC2
INSTANCE_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=ds5220-project2" "Name=instance-state-name,Values=running,pending,stopped" \
  --query "Reservations[0].Instances[0].InstanceId" --output text --region "$REGION")

if [ "$INSTANCE_ID" = "None" ] || [ -z "$INSTANCE_ID" ]; then
  cat > /tmp/ds5220-userdata.sh <<'EOF'
#!/bin/bash
set -e
apt-get update -y
curl -sfL https://get.k3s.io | sh -s - --write-kubeconfig-mode 644
mkdir -p /home/ubuntu/.kube
cp /etc/rancher/k3s/k3s.yaml /home/ubuntu/.kube/config
chown -R ubuntu:ubuntu /home/ubuntu/.kube
echo 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' >> /home/ubuntu/.bashrc
EOF

  INSTANCE_ID=$(aws ec2 run-instances \
    --image-id "$AMI_ID" --instance-type "$INSTANCE_TYPE" \
    --key-name "$KEY_NAME" --security-group-ids "$SG_ID" \
    --iam-instance-profile "Name=${PROFILE_NAME}" \
    --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":30,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
    --user-data "file:///tmp/ds5220-userdata.sh" \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=ds5220-project2}]' \
    --region "$REGION" --query "Instances[0].InstanceId" --output text)

  rm -f /tmp/ds5220-userdata.sh
  aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$REGION"
fi

# Elastic IP
ELASTIC_IP=$(aws ec2 describe-addresses \
  --filters "Name=instance-id,Values=${INSTANCE_ID}" \
  --query "Addresses[0].PublicIp" --output text --region "$REGION")

if [ "$ELASTIC_IP" = "None" ] || [ -z "$ELASTIC_IP" ]; then
  ALLOC_ID=$(aws ec2 allocate-address --domain vpc --region "$REGION" \
    --query AllocationId --output text)
  ELASTIC_IP=$(aws ec2 describe-addresses --allocation-ids "$ALLOC_ID" \
    --query "Addresses[0].PublicIp" --output text --region "$REGION")
  aws ec2 associate-address --instance-id "$INSTANCE_ID" \
    --allocation-id "$ALLOC_ID" --region "$REGION" > /dev/null
fi

# DynamoDB
TABLE_STATUS=$(aws dynamodb describe-table --table-name "$TABLE_NAME" \
  --query "Table.TableStatus" --output text --region "$REGION" 2>/dev/null || echo "NOT_FOUND")

if [ "$TABLE_STATUS" = "NOT_FOUND" ]; then
  aws dynamodb create-table --table-name "$TABLE_NAME" \
    --attribute-definitions AttributeName=satellite_id,AttributeType=S AttributeName=timestamp,AttributeType=S \
    --key-schema AttributeName=satellite_id,KeyType=HASH AttributeName=timestamp,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST --region "$REGION" > /dev/null
  aws dynamodb wait table-exists --table-name "$TABLE_NAME" --region "$REGION"
fi

echo "S3:  http://${BUCKET}.s3-website-${REGION}.amazonaws.com"
echo "IP:  $ELASTIC_IP"
echo "SSH: ssh -i $KEY_PATH ubuntu@$ELASTIC_IP"
