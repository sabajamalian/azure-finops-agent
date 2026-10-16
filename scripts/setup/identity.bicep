// User-assigned managed identity that the FinOps agent's GitHub Actions jobs sign in as (OIDC, no secrets).
// Deploy at resource group scope:
//   az deployment group create -g <rg> -f identity.bicep -p repository=<owner>/<repo>
// Role assignments are separate (roles.bicep), one deployment per subscription.

targetScope = 'resourceGroup'

@description('GitHub repository in owner/name form. Only workflow jobs in this repo can use the identity.')
param repository string

@description('GitHub environment the jobs run in. The federated credential trusts only this environment.')
param environmentName string = 'finops'

@description('Managed identity name.')
param identityName string = 'id-finops-agent'

param location string = resourceGroup().location

@description('GitHub OIDC issuer. Change only for GitHub Enterprise Server or a customized issuer.')
param issuer string = 'https://token.actions.githubusercontent.com'

param tags object = {
  purpose: 'finops-agent'
  'managed-by': 'azure-finops-agent'
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

// Subject format for jobs that reference an environment: repo:<owner>/<repo>:environment:<name>.
// Restrict the environment to the default branch in GitHub so branches can't borrow this identity.
resource federated 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: identity
  name: 'github-${environmentName}'
  properties: {
    issuer: issuer
    subject: 'repo:${repository}:environment:${environmentName}'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

output clientId string = identity.properties.clientId
output principalId string = identity.properties.principalId
output tenantId string = identity.properties.tenantId
output identityId string = identity.id
