# Cloud Capital

## Local development

```text
make install
make dev
```

- Frontend: `http://localhost:3000`
- API: `http://localhost:8001`
- Economics bounded context: `http://localhost:8001/econ`

The local frontend calls the API directly. The deployed Amplify frontend instead
uses `app/api/backend/[...path]/route.ts`, a server-side proxy that adds the ALB
header secret; the browser never receives that secret.

## Deployment shape

- Amplify Hosting (`WEB_COMPUTE`) hosts the Next.js application.
- A single desired-count Fargate task runs the FastAPI modular monolith, including
  the `/econ` economics bounded context.
- An HTTPS ALB forwards only requests whose `x-demo-auth` header equals the
  generated secret. Its default action is `403`.
- The parquet demo data is baked into the ECS image at `/app/data`. Move it to S3
  or EFS before treating this as a production data-delivery strategy.
  `data/` is intentionally Git-ignored (the dataset is ~357 MB); place the two
  provided parquet files there before a local Docker/CDK deployment.

The CDK stack takes these deploy-time CloudFormation parameters:

- `FrontendRepository`
- `FrontendBranch` (defaults to `main`)
- `GithubTokenSecretArn`
- `OpenRouterSecretArn`
- `ApiDomainName`
- `ApiCertificateArn`

`ApiDomainName` must resolve to the provisioned ALB and the ACM certificate must
cover it. This is required because the header secret must travel over HTTPS.
