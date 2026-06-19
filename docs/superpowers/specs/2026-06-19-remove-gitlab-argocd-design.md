# Remove GitLab and Argo CD

## Goal

Remove the unused GitLab and Argo CD components from the repository and from
the supported MicroK8s deployment workflow. The repository must no longer
install, uninstall, configure, document, or otherwise reference either
component outside the historical design and implementation records.

## Scope

- Delete the vendored `charts/qsint-gitlab` and `charts/qsint-argocd` trees.
- Remove both releases, namespaces, CRDs, RBAC objects, repositories,
  dependency updates, credential instructions, and related messages from
  `scripts/deploy-microk8s.sh`.
- Remove their local hostnames from `scripts/update-local-hosts.sh`.
- Remove controller-specific annotations and comments from remaining
  manifests when they exist only to support Argo CD.
- Remove generic `.gitlab-ci.yml` ignore entries so deployable code and
  user-facing documentation contain no residual references.
- Rewrite README architecture, workflow, component, access, directory-tree,
  command, and reference material to describe only the remaining direct Helm
  bootstrap workflow.

## Behavior After Removal

`scripts/deploy-microk8s.sh` continues to tear down and install the remaining
platform components in dependency order. It does not attempt compatibility
cleanup for GitLab or Argo CD resources left by older deployments. Operators
who still have those releases installed must remove them independently.

The repository documents direct Helm deployment through the bootstrap script;
it does not claim an in-cluster GitOps or source-control service.

## Validation

- A case-insensitive repository search, excluding `.git` and the historical planning records, returns no matches for the removed component names.
- All shell scripts pass `bash -n`.
- Remaining first-party Helm charts pass `helm lint` using their checked-in
  dependencies.
- The working-tree diff contains only the intended component removal and the
  documentation changes needed to keep the repository accurate.

## Non-Goals

- Migrating Argo CD applications to another GitOps controller.
- Replacing GitLab with another self-hosted source-control service.
- Preserving automated cleanup for installations created by older revisions.
