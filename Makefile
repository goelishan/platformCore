.PHONY: up down down-all rebuild status logs curl

# Bring the full stack up.
up:
	cd terraform && terraform apply -auto-approve

# Tear down billable resources between learning sessions.
#
# What gets destroyed (billable):
#   - ALB + listener + target group + attachment (~$0.025/hr for the ALB)
#   - EC2 instance (~$0.01/hr for t3.micro)
#   - 6 interface VPC endpoints (~$0.01/hr each, per AZ — the biggest
#     ongoing cost in this stack if left running overnight)
#
# What stays (free or effectively free):
#   - VPC, subnets, IGW, route tables, security groups
#   - IAM role + policies
#   - ECR repository + images (ECR charges per GB-month; our image is ~60MB,
#     cost is fractions of a cent per month. Keeping images across sessions
#     saves the rebuild/push cycle every morning.)
#   - S3 gateway endpoint (gateway endpoints are free)
#
# Order matters: listener -> attachment -> target group -> LB -> EC2 ->
# interface endpoints. Terraform handles most of this via the dependency
# graph, but explicit -target calls keep the destroy scoped and predictable.
down:
	cd terraform && terraform destroy -auto-approve \
	  -target=aws_lb_listener.http \
	  -target=aws_lb_target_group_attachment.app \
	  -target=aws_lb_target_group.app \
	  -target=aws_lb.app \
	  -target=aws_instance.app \
	  -target=aws_vpc_endpoint.ssm \
	  -target=aws_vpc_endpoint.ssmmessages \
	  -target=aws_vpc_endpoint.ec2messages \
	  -target=aws_vpc_endpoint.ecr_api \
	  -target=aws_vpc_endpoint.ecr_dkr \
	  -target=aws_vpc_endpoint.logs \
	  -target=aws_db_instance.main

# Full destroy: everything, including ECR repo + images and the free VPC
# resources. Use at the end of a phase or when switching projects. Will
# fail on the ECR repo if images are present; add force_delete=true to
# the aws_ecr_repository resource if you want to bypass that.
down-all:
	cd terraform && terraform destroy -auto-approve

# End-of-day teardown + morning rebuild shortcut.
rebuild: down up

# What's currently provisioned?
status:
	cd terraform && terraform state list

# Tail container logs from CloudWatch (live).
logs:
	aws logs tail /platformcore/app --region us-east-1 --follow

# Smoke-test the ALB. Hits root (/), health (/health), and version (/version).
# /ready is expected to return 503 in Phase 9 because RDS does not exist yet.
curl:
	@ALB=$$(cd terraform && terraform output -raw alb_dns_name); \
	echo "Hitting http://$$ALB ..."; \
	curl -sS -w "\nHTTP %{http_code}\n" http://$$ALB/; \
	echo "---"; \
	curl -sS -w "\nHTTP %{http_code}\n" http://$$ALB/health; \
	echo "---"; \
	curl -sS -w "\nHTTP %{http_code}\n" http://$$ALB/version; \
	echo "---"; \
	echo "Readiness (expected 503 in Phase 9, no DB yet):"; \
	curl -sS -w "\nHTTP %{http_code}\n" http://$$ALB/ready
