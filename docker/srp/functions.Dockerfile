# syntax=docker/dockerfile:1.7
# Umbrella-owned build for the .NET isolated Azure Functions apps in repos/srp.
# Build context: repos/srp. Pick the app with --build-arg PROJECT=<folder/csproj name>, e.g.
#   ScriptureRenderingPipeline        (webhook front door, HTTP)
#   ScriptureRenderingPipelineWorker  (Service Bus triggered renderer)
#
# Replaces the upstream ScriptureRenderingPipeline/Dockerfile, which builds an old Core Tools
# (4.0.4785) from source and can't host net10 isolated apps.
#
# Optional build secret `extra_ca` (PEM bundle) is trusted during `dotnet restore`, for networks
# with a TLS-intercepting proxy. It is never written into an image layer.

ARG DOTNET_VERSION=10.0

FROM mcr.microsoft.com/dotnet/sdk:${DOTNET_VERSION} AS build
ARG PROJECT
WORKDIR /src
COPY . .
RUN --mount=type=secret,id=extra_ca,required=false \
    --mount=type=cache,target=/root/.nuget/packages,sharing=locked \
    if [ -s /run/secrets/extra_ca ]; then \
      cat /etc/ssl/certs/ca-certificates.crt /run/secrets/extra_ca > /tmp/ca.pem; \
      export SSL_CERT_FILE=/tmp/ca.pem; \
    fi; \
    dotnet publish "${PROJECT}/${PROJECT}.csproj" -c Release -o /home/site/wwwroot

FROM mcr.microsoft.com/azure-functions/dotnet-isolated:4-dotnet-isolated${DOTNET_VERSION} AS runtime
ENV AzureWebJobsScriptRoot=/home/site/wwwroot \
    AzureFunctionsJobHost__Logging__Console__IsEnabled=true \
    FUNCTIONS_WORKER_RUNTIME=dotnet-isolated
COPY --from=build /home/site/wwwroot /home/site/wwwroot
