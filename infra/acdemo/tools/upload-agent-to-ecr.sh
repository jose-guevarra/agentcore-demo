#!/bin/bash

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo ""

# Get the script's directory and navigate to project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "SCRT_ROOT: ${SCRIPT_DIR}"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../../" && pwd)"

# Change to project root
echo "PROJECT_ROOT: ${PROJECT_ROOT}"
cd "${PROJECT_ROOT}"

# Check if we're in the right directory
if [ ! -f "src/chat_agent/Dockerfile" ]; then
    echo -e "${RED}❌ Could not find agent/Dockerfile in project root${NC}"
    exit 1
fi

AGENT_DIRECTORY="${PROJECT_ROOT}/src/chat_agent"

echo -e "${YELLOW}Starting deployment...${NC}"
echo ""

# Check if aws credentials are set properly
echo -e "${BLUE}Checking AWS credentials...${NC}"
AWS_IDENTITY_OUTPUT=$(aws sts get-caller-identity 2>&1)
AWS_IDENTITY_EXIT_CODE=$?

if [ $AWS_IDENTITY_EXIT_CODE -ne 0 ]; then
    echo -e "${RED}❌ AWS credentials not available${NC}"
    
    # Check if it's an SSO-related error
    if echo "$AWS_IDENTITY_OUTPUT" | grep -qi "sso"; then
        echo -e "${YELLOW}It looks like you're using AWS SSO.${NC}"
        echo -e "${YELLOW}Please run: ${BLUE}aws sso login${NC}"
        echo -e "${YELLOW}Then run this script again.${NC}"
    else
        echo -e "${YELLOW}Please authenticate with AWS:${NC}"
        echo -e "${YELLOW}  - For SSO: ${BLUE}aws sso login${NC}"
        echo -e "${YELLOW}  - Or configure credentials with: ${BLUE}aws configure${NC}"
    fi
    exit 1
fi

# Get AWS account ID
echo -e "${BLUE}Getting AWS account information...${NC}"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION_DEFAULT=$(aws configure get region || echo "us-east-1")

echo -e "${GREEN}✅ AWS Account ID: ${AWS_ACCOUNT_ID}${NC}"
echo ""

# Step 1: Load deployment configuration from scripts/.env.agent
SCRIPTS_ENV_FILE="${PROJECT_ROOT}/infra/acdemo/tools/.env.agent"
if [ ! -f "$SCRIPTS_ENV_FILE" ]; then
    echo -e "${RED}❌ Configuration file not found: ${SCRIPTS_ENV_FILE}${NC}"
    echo -e "${YELLOW}Please create scripts/.env.agent with deployment configuration${NC}"
    echo -e "${YELLOW}You can use scripts/env.agent.template as a starting point${NC}"
    exit 1
fi

echo -e "${YELLOW}Loading deployment configuration from tools/.env.agent...${NC}"
source "$SCRIPTS_ENV_FILE"

# Set defaults if not provided
AWS_REGION=${AWS_REGION:-$AWS_REGION_DEFAULT}
IMAGE_TAG=${IMAGE_TAG:-"latest"}
DOCKER_PLATFORM=${DOCKER_PLATFORM:-"linux/arm64"}

# Verify required variables are set
if [ -z "$ECR_REPOSITORY_NAME" ]; then
    echo -e "${RED}❌ Missing required configuration in tools/.env.agent${NC}"
    echo -e "${YELLOW}Required variables: ECR_REPOSITORY_NAME${NC}"
    echo -e "${YELLOW}Optional variables: AWS_REGION (default: ${AWS_REGION_DEFAULT}), IMAGE_TAG (default: latest), DOCKER_PLATFORM (default: linux/arm64)${NC}"
    exit 1
fi

# Construct ECR repository URL
ECR_REPOSITORY_URL="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY_NAME}"
FULL_IMAGE_TAG="${ECR_REPOSITORY_URL}:${IMAGE_TAG}"

echo ""
echo -e "${GREEN}✅ Deployment configuration loaded${NC}"
echo -e "  ECR Repository:       ${GREEN}${ECR_REPOSITORY_NAME}${NC}"
echo -e "  AWS Region:           ${GREEN}${AWS_REGION}${NC}"
echo -e "  Image Tag:            ${GREEN}${IMAGE_TAG}${NC}"
echo -e "  Docker Platform:      ${GREEN}${DOCKER_PLATFORM}${NC}"
echo -e "  Full Image Tag:       ${GREEN}${FULL_IMAGE_TAG}${NC}"
echo ""

# Step 2: Authenticate Docker with ECR
echo -e "${BLUE}Step 3: Authenticating Docker with ECR...${NC}"
aws ecr get-login-password --region "${AWS_REGION}" | \
    docker login --username AWS --password-stdin "${ECR_REPOSITORY_URL}"
echo -e "${GREEN}✅ Docker authenticated with ECR${NC}"
echo ""

# Step 4: Build and push Docker image
echo -e "${BLUE}Step 4: Building and pushing Docker image...${NC}"
echo -e "${YELLOW}Building for platform: ${DOCKER_PLATFORM}${NC}"

# Navigate to agent directory
cd "${AGENT_DIRECTORY}"

# Build and push the image
docker buildx build \
    --platform "${DOCKER_PLATFORM}" \
    -t "${FULL_IMAGE_TAG}" \
    --push \
    .

echo -e "${GREEN}✅ Docker image built and pushed successfully${NC}"
echo ""

# Step 5: Verify the image was pushed
echo -e "${BLUE}Step 5: Verifying image in ECR...${NC}"
IMAGE_INFO=$(aws ecr describe-images \
    --repository-name "${ECR_REPOSITORY_NAME}" \
    --image-ids imageTag="${IMAGE_TAG}" \
    --region "${AWS_REGION}" \
    --query 'imageDetails[0]' \
    --output json 2>/dev/null || echo "{}")

if [ "$IMAGE_INFO" != "{}" ] && [ -n "$IMAGE_INFO" ]; then
    IMAGE_DIGEST=$(echo "$IMAGE_INFO" | grep -o '"imageDigest":"[^"]*"' | head -1 | cut -d'"' -f4)
    IMAGE_SIZE=$(echo "$IMAGE_INFO" | grep -o '"imageSizeInBytes":[0-9]*' | cut -d':' -f2)
    IMAGE_PUSHED=$(echo "$IMAGE_INFO" | grep -o '"imagePushedAt":"[^"]*"' | cut -d'"' -f4)
    
    echo -e "${GREEN}✅ Image verified in ECR${NC}"
    echo -e "  Image Digest:        ${GREEN}${IMAGE_DIGEST}${NC}"
    echo -e "  Image Size:          ${GREEN}$((IMAGE_SIZE / 1024 / 1024)) MB${NC}"
    echo -e "  Pushed At:           ${GREEN}${IMAGE_PUSHED}${NC}"
else
    echo -e "${YELLOW}⚠️  Could not verify image details, but push may have succeeded${NC}"
fi
echo ""

# Navigate back to project root
cd "${PROJECT_ROOT}"

# Final summary
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}🎉 Deployment Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${BLUE}Deployment Summary:${NC}"
echo -e "  ECR Repository:       ${GREEN}${ECR_REPOSITORY_NAME}${NC}"
echo -e "  Repository URL:       ${GREEN}${ECR_REPOSITORY_URL}${NC}"
echo -e "  Image Tag:            ${GREEN}${FULL_IMAGE_TAG}${NC}"
echo -e "  AWS Region:           ${GREEN}${AWS_REGION}${NC}"
echo -e "  Platform:             ${GREEN}${DOCKER_PLATFORM}${NC}"
echo ""
echo -e "${YELLOW}You can pull this image with:${NC}"
echo -e "  docker pull ${FULL_IMAGE_TAG}"
echo ""
