#--------------------------------------------------------------------------------------------------------
# EKS CLUSTER
#--------------------------------------------------------------------------------------------------------

resource "aws_eks_cluster" "main" {
  name     = var.cluster_name
  role_arn = aws_iam_role.eks_cluster.arn
  version  = "1.33"

  vpc_config {
    subnet_ids              = var.private_subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = true
  }

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }

  depends_on = [aws_iam_role_policy_attachment.eks_cluster]
}

#--------------------------------------------------------------------------------------------------------
# MANAGED NODE GROUP
#--------------------------------------------------------------------------------------------------------

resource "aws_eks_node_group" "main" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "${var.project_name}-nodes"
  node_role_arn   = aws_iam_role.eks_node.arn
  subnet_ids      = var.private_subnet_ids

  instance_types = ["t3.small"]

  launch_template {
    id      = aws_launch_template.eks_nodes.id
    version = aws_launch_template.eks_nodes.latest_version
  }

  scaling_config {
    desired_size = 2
    min_size     = 1
    max_size     = 3
  }

  depends_on = [
    aws_iam_role_policy_attachment.eks_worker_node,
    aws_iam_role_policy_attachment.eks_cni,
    aws_iam_role_policy_attachment.eks_ecr_readonly,
  ]
}

#--------------------------------------------------------------------------------------------------------
# LAUNCH TEMPLATE - explicit kubelet maxPods for prefix delegation
#--------------------------------------------------------------------------------------------------------

resource "aws_launch_template" "eks_nodes" {
  name_prefix = "${var.project_name}-eks-nodes-"

  # Preserves the EKS-default 20Gi root volume; explicit here because launch template owns disk config once attached.
  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      volume_size           = 20
      volume_type           = "gp3"
      delete_on_termination = true
      encrypted             = true
    }
  }

  # nodeadm NodeConfig: declares maxPods=110 explicitly so the kubelet's pod ceiling doesn't depend on
  # auto-detection of prefix delegation (unreliable across AL2023 AMI versions). MIME-multipart format
  # required - EKS merges this with its own auto-generated bootstrap section.
  user_data = base64encode(<<-EOT
    MIME-Version: 1.0
    Content-Type: multipart/mixed; boundary="BOUNDARY"

    --BOUNDARY
    Content-Type: application/node.eks.aws

    apiVersion: node.eks.aws/v1alpha1
    kind: NodeConfig
    spec:
      kubelet:
        config:
          maxPods: 110

    --BOUNDARY--
    EOT
  )
}

#--------------------------------------------------------------------------------------------------------
# ACCESS ENTRIES - CONSOLE/CLI ADMIN
#--------------------------------------------------------------------------------------------------------
#

resource "aws_eks_access_entry" "console_admin" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = var.admin_iam_arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "console_admin" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = var.admin_iam_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }
}