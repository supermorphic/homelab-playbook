# This file defines Helm chart metadata for render_helm_chart.sh.
# Not applied to Kubernetes.
name: argo-cd
repo: https://argoproj.github.io/argo-helm
version: 9.1.7
namespace: argocd
valuesFile: values.yaml
outputFile: ../templates/argocd.yaml
