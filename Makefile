.PHONY: up down down-all rebuild status logs curl

# Bring the full stack up.
up:
	cd terraform && terraform apply -auto-approve



# Tear down billable resources between learning sessions.
#
# What gets destroyed (billable):
#   - ALB + listener + target group + attachment (~$0.025/hr for the ALB)
#   - EC2 instance (~$0.01/hr for t3.micro)
#   - 6 interface VPC endpoints (~$0.01/hr each per AZ - biggest ongoing
#     cost in this stack if left running overnight)
#   - RDS db.t3.micro (~$0.017/hr; destroy takes 5-10 min, slowest step)
#
# What stays (free or effectively free):
#   - VPC, subnets, IGW, route tables, security groups
#   - IAM role + policies, instance profile
#   - ECR repository + images (~fractions of a cent/month for our image)
#   - CloudWatch log group + retained streams
#   - RDS subnet group + parameter group (no charge when no instance)
#   - S3 gateway endpoint (gateway endpoints are free)
#
# All targets use module-prefixed addresses post-Day-11. -target bypasses
# the DAG, so we enumerate explicitly in dependency-correct teardown
# order: edge -> compute -> data -> network endpoints.
down:
	cd terraform && terraform destroy -auto-approve \
	  -target=module.edge.aws_lb_listener.http \
	  -target=module.edge.aws_lb_target_group_attachment.app \
	  -target=module.edge.aws_lb_target_group.app \
	  -target=module.edge.aws_lb.app \
	  -target=module.compute.aws_instance.app \
	  -target=module.data.aws_db_instance.main \
	  -target=module.network.aws_vpc_endpoint.ssm \
	  -target=module.network.aws_vpc_endpoint.ssmmessages \
	  -target=module.network.aws_vpc_endpoint.ec2messages \
	  -target=module.network.aws_vpc_endpoint.ecr_api \
	  -target=module.network.aws_vpc_endpoint.ecr_dkr \
	  -target=module.network.aws_vpc_endpoint.logs \
	  -target=module.network.aws_vpc_endpoint.secretsmanager



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



# Smoke-test the ALB. Hits root, /health, /version, /ready. Post-Day-10,
# /ready expects 200 (RDS deployed and reachable from the EC2 SG).
curl:
	@ALB=$$(cd terraform && terraform output -raw alb_dns_name); \
	echo "Hitting http://$$ALB ..."; \
	curl -sS -w "\nHTTP %{http_code}\n" http://$$ALB/; \
	echo "---"; \
	curl -sS -w "\nHTTP %{http_code}\n" http://$$ALB/health; \
	echo "---"; \
	curl -sS -w "\nHTTP %{http_code}\n" http://$$ALB/version; \
	echo "---"; \
	echo "Readiness (DB-backed; expected 200 post-Day-10):"; \
	curl -sS -w "\nHTTP %{http_code}\n" http://$$ALB/ready
