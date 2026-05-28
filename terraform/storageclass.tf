resource "kubernetes_storage_class_v1" "gp3_retain" {
  metadata {
    name = "gp3-retain"
  }

  storage_provisioner    = "ebs.csi.aws.com"
  reclaim_policy         = "Retain"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true

  parameters = {
    type      = "gp3"
    encrypted = "true"
  }

  # Explicit dependency so Terraform destroys this resource BEFORE tearing down
  # the EKS module. Without this, the implicit provider dependency is not enough
  # — Terraform may parallelize destruction and lose kubernetes API auth mid-run.
  depends_on = [module.eks]
}


resource "kubernetes_storage_class_v1" "gp3_delete" {
  metadata {
    name = "gp3-delete"
  }

  storage_provisioner = "ebs.csi.aws.com"
  reclaim_policy = "Delete"
  volume_binding_mode = "WaitForFirstConsumer"
  allow_volume_expansion = true

  parameters = {
    type = "gp3"
    encrypted = "true"
  }

  depends_on = [ module.eks ]
}