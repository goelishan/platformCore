# ECR — private container registry for the app image.
#
# ECR is a thin control plane on top of S3: image *manifests* (small JSON
# docs listing layer digests) live in ECR itself, but image *layer blobs*
# are stored in S3 behind the scenes. That is why a functional private-
# subnet pull path requires three endpoints: ecr.api (control), ecr.dkr
# (data), and s3 (layer storage). Drop the S3 gateway endpoint and pulls
# hang on the first layer.
#
# Design choices worth defending:
#
#   image_tag_mutability = MUTABLE
#     Easier for dev iteration (push :v2, push :v2 again after a tweak).
#     Production usually flips this to IMMUTABLE so :latest and :v2 cannot
#     be silently overwritten — supply-chain hygiene.
#
#   scan_on_push = true
#     Free basic CVE scan against the AWS-curated vuln DB on every push.
#     Catches known-bad base images before they ship. Negligible latency.
#
#   encryption_type = AES256
#     AWS-managed keys. Switch to KMS (customer-managed) if compliance
#     requires a key you own and can rotate on your schedule.
#
#   lifecycle_policy: "expire untagged after 1 day"
#     ECR charges per GB-month. Every buildx push creates a small untagged
#     manifest list + orphans the previous tag's old manifest. Without this
#     policy the untagged entries pile up forever. The policy language is
#     camelCase (ECR native schema), NOT snake_case — easy gotcha.

resource "aws_ecr_repository" "app" {
  name                 = "${var.project_name}-app"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = {
    Name        = "${var.project_name}-app"
    Environment = var.environment
  }
}


resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 5 tagged images, expire untagged after 1 day"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 1
      }
      action = { type = "expire" }
    }]
  })
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}