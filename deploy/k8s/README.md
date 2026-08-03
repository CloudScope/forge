# Kubernetes manifests

Apply after building/pushing `forge-sdlc:latest`:

```bash
kubectl apply -f deployment.yaml
kubectl rollout status deploy/forge-studio
kubectl port-forward svc/forge-studio 8787:80
```

Create optional secret:

```bash
kubectl create secret generic forge-secrets \
  --from-literal=FORGE_LLM_API_KEY="$FORGE_LLM_API_KEY"
```
