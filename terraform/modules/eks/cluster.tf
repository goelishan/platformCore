#--------------------------------------------------------------------------------------------------------
# EKS CLUSTER
#--------------------------------------------------------------------------------------------------------

resource "aws_eks_cluster" "main" {
  name = var.cluster_name
  role_arn = aws_iam_role.eks_cluster.arn
  version = "1.32"

  vpc_config {
    subnet_ids = var.private_subnet_ids
    endpoint_private_access = true
    endpoint_public_access = true
  }

  access_config {
    authentication_mode = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }

  depends_on = [ aws_iam_role_policy_attachment.eks_cluster ]
}

#--------------------------------------------------------------------------------------------------------
# MANAGED NODE GROUP
#--------------------------------------------------------------------------------------------------------

resource "aws_eks_node_group" "main" {
  cluster_name = aws_eks_cluster.main.name
  node_group_name = "${var.project_name}-nodes"
  node_role_arn = aws_iam_role.eks_node.arn
  subnet_ids = var.private_subnet_ids

  instance_types = ["t3.small"]

  scaling_config {
    desired_size = 1
    min_size = 1
    max_size = 3
  }

  depends_on = [ 
    aws_iam_role_policy_attachment.eks_worker_node,
    aws_iam_role_policy_attachment.eks_cni,
    aws_iam_role_policy_attachment.eks_ecr_readonly,
   ]
}

#--------------------------------------------------------------------------------------------------------
# ACCESS ENTRIES - CONSOLE/CLI ADMIN
#--------------------------------------------------------------------------------------------------------
#

resource "aws_eks_access_entry" "console_admin" {
  cluster_name = aws_eks_cluster.main.name
  principal_arn = var.admin_iam_arn
  type = "STANDARD"
}

resource "aws_eks_access_policy_association" "console_admin" {
  cluster_name = aws_eks_cluster.main.name
  principal_arn = var.admin_iam_arn
  policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }
}