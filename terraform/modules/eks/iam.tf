#--------------------------------------------------------------------------------------------------------
# IAM - EKS CLUSTER ROLE
#--------------------------------------------------------------------------------------------------------


data "aws_iam_policy_document" "eks_cluster_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
        type = "Service"
        identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eks_cluster" {
  name = "${var.project_name}-eks-cluster-role"
  assume_role_policy = data.aws_iam_policy_document.eks_cluster_trust.json
}

resource "aws_iam_role_policy_attachment" "eks_cluster" {
  role = aws_iam_role.eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

#--------------------------------------------------------------------------------------------------------
# IAM - EKS NODE ROLE
#--------------------------------------------------------------------------------------------------------

data "aws_iam_policy_document" "eks_node_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
        type="Service"
        identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eks_node" {
    name = "${var.project_name}-eks-node-role"
    assume_role_policy = data.aws_iam_policy_document.eks_node_trust.json
}

resource "aws_iam_role_policy_attachment" "eks_worker_node" {
    role = aws_iam_role.eks_node.name
    policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "eks_cni" {
    role = aws_iam_role.eks_node.name
    policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "eks_ecr_readonly" {
    role = aws_iam_role.eks_node.name
    policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}