#--------------------------------------------------------------------------------------------------------
# ECR - ON-CALL AGENT
#--------------------------------------------------------------------------------------------------------
#
#   - the agent's image, pushed by CI with the commit SHA as its tag
#
#   - IMMUTABLE: a SHA tag must always name the image built from that commit
#
#   - same encryption, scanning and untagged expiry as the application repository
#



resource "aws_ecr_repository" "oncall" {
  name                 = "${var.project_name}-oncall"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = {
    Name        = "${var.project_name}-oncall"
    Environment = var.environment
  }
}

resource "aws_ecr_lifecycle_policy" "oncall" {
  repository = aws_ecr_repository.oncall.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 1 day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the 20 most recent images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 20
        }
        action = { type = "expire" }
      },
    ]
  })
}
