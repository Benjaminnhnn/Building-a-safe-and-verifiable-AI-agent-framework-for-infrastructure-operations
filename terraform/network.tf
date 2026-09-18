locals {
  common_tags = {
    Project         = var.project_name
    Environment     = var.environment
    ManagedBy       = "Terraform"
    Owner           = "Infrastructure"
    ExperimentScope = "thesis"
  }

  # Keep existing project/environment resource names stable during migration.
  moodle_prefix = "${var.moodle_project_name}-${var.moodle_environment}"
  moodle_tags = merge(local.common_tags, {
    Project     = "${var.moodle_project_name}-aiops"
    Environment = var.moodle_environment
  })
}

resource "aws_vpc" "main" {
  cidr_block           = "10.10.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-${var.environment}-vpc"
  })
}

resource "aws_subnet" "public_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.10.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-${var.environment}-public-a"
  })
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-${var.environment}-igw"
  })
}

resource "aws_route_table" "public_rt" {
  vpc_id = aws_vpc.main.id

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-${var.environment}-public-rt"
  })
}

resource "aws_route" "default_internet_route" {
  route_table_id         = aws_route_table.public_rt.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.igw.id
}

resource "aws_route_table_association" "public_assoc" {
  subnet_id      = aws_subnet.public_a.id
  route_table_id = aws_route_table.public_rt.id
}

# Additive network expansion. Preserve public_a and all existing compute.
resource "aws_subnet" "public_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.10.2.0/24"
  availability_zone       = data.aws_availability_zones.available.names[1]
  map_public_ip_on_launch = true
  tags                    = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-public-b", Tier = "public" })
}

resource "aws_subnet" "app_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.10.11.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false
  tags                    = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-app-a", Tier = "app" })
}

resource "aws_subnet" "app_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.10.12.0/24"
  availability_zone       = data.aws_availability_zones.available.names[1]
  map_public_ip_on_launch = false
  tags                    = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-app-b", Tier = "app" })
}

resource "aws_subnet" "data_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.10.21.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false
  tags                    = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-data-a", Tier = "data" })
}

resource "aws_subnet" "data_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.10.22.0/24"
  availability_zone       = data.aws_availability_zones.available.names[1]
  map_public_ip_on_launch = false
  tags                    = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-data-b", Tier = "data" })
}

resource "aws_subnet" "management" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.10.31.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false
  tags                    = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-management", Tier = "management" })
}

# Explicit public IP assignment for the future management EC2; no auto-assignment.
resource "aws_route_table_association" "public_b" {
  subnet_id      = aws_subnet.public_b.id
  route_table_id = aws_route_table.public_rt.id
}
resource "aws_route_table_association" "management" {
  subnet_id      = aws_subnet.management.id
  route_table_id = aws_route_table.public_rt.id
}

resource "aws_eip" "nat_a" {
  domain = "vpc"
  tags   = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-nat-a-eip" })
}
resource "aws_nat_gateway" "a" {
  allocation_id     = aws_eip.nat_a.id
  subnet_id         = aws_subnet.public_a.id
  connectivity_type = "public"
  depends_on        = [aws_internet_gateway.igw]
  tags              = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-nat-a" })
}
resource "aws_route_table" "app_a" {
  vpc_id = aws_vpc.main.id
  tags   = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-app-a-rt" })
}
resource "aws_route" "app_a_internet" {
  route_table_id         = aws_route_table.app_a.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.a.id
}
resource "aws_route_table_association" "app_a" {
  subnet_id      = aws_subnet.app_a.id
  route_table_id = aws_route_table.app_a.id
}
# Data subnets intentionally have only the VPC local route.
resource "aws_route_table" "data_a" {
  vpc_id = aws_vpc.main.id
  route  = []
  tags   = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-data-a-rt" })
}
resource "aws_route_table_association" "data_a" {
  subnet_id      = aws_subnet.data_a.id
  route_table_id = aws_route_table.data_a.id
}

resource "aws_eip" "nat_b" {
  domain = "vpc"
  tags   = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-nat-b-eip" })
}
resource "aws_nat_gateway" "b" {
  allocation_id     = aws_eip.nat_b.id
  subnet_id         = aws_subnet.public_b.id
  connectivity_type = "public"
  depends_on        = [aws_internet_gateway.igw]
  tags              = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-nat-b" })
}
resource "aws_route_table" "app_b" {
  vpc_id = aws_vpc.main.id
  tags   = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-app-b-rt" })
}
resource "aws_route" "app_b_internet" {
  route_table_id         = aws_route_table.app_b.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.b.id
}
resource "aws_route_table_association" "app_b" {
  subnet_id      = aws_subnet.app_b.id
  route_table_id = aws_route_table.app_b.id
}
# Data subnets intentionally have only the VPC local route.
resource "aws_route_table" "data_b" {
  vpc_id = aws_vpc.main.id
  route  = []
  tags   = merge(local.moodle_tags, { Name = "${local.moodle_prefix}-data-b-rt" })
}
resource "aws_route_table_association" "data_b" {
  subnet_id      = aws_subnet.data_b.id
  route_table_id = aws_route_table.data_b.id
}
