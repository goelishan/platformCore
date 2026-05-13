#--------------------------------------------------------------------------------------------------------
# OUTPUTS - EKS MODULE
#--------------------------------------------------------------------------------------------------------

output "cluster_name" {
  description = "EKS cluster name — used by aws eks update-kubeconfig and helm provider"
  value       = aws_eks_cluster.main.name
}

output "cluster_endpoint" {
  description = "API server endpoint — used by kubernetes/helm Terraform providers"
  value       = aws_eks_cluster.main.endpoint
}

output "cluster_ca_certificate" {
  description = "Base64-encoded cluster CA — needed by kubernetes provider for TLS verification"
  value       = aws_eks_cluster.main.certificate_authority[0].data
}

output "oidc_provider_arn" {
  description = "OIDC provider ARN — referenced in IRSA trust policies"
  value       = aws_iam_openid_connect_provider.eks.arn
}

output "node_role_arn" {
  description = "Node IAM role ARN — may be needed for additional policy attachments"
  value       = aws_iam_role.eks_node.arn
}

output "cluster_security_group_id" {
  description = "CLuster SG attached to ENI and all managed nodes"
  value = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
}

output "alb_controller_role_arn" {
  description = "IRSA role arn for Helm"
  value = aws_iam_role.alb_controller.arn
}