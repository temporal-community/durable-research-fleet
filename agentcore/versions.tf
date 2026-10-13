terraform {
  required_version = ">= 1.9"

  required_providers {
    # aws_bedrockagentcore_agent_runtime and _endpoint need a 6.x provider.
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project   = "research-fleet"
      component = "agentcore-serverless-worker"
      managed   = "terraform"
    }
  }
}
